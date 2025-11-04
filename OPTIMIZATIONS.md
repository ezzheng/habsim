# HABSIM Optimizations

Technical reference for optimizations implemented for Railway (32GB RAM, 32 vCPU).

## Deploy to Railway

**Start Command:**
```bash
gunicorn --config gunicorn_config.py app:app
```

## Cache Pre-warming (`app.py`)

### Startup Pre-warming
One background thread runs on startup to optimize performance:

1. **`_prewarm_cache()`**: Pre-loads simulator for model 0 (fast single requests)
   - Files download on-demand when needed (cost-optimized to reduce Supabase egress)
   - Files are cached on disk after first download, so subsequent runs are fast

### What Gets Pre-warmed

**Models vs Simulators:**
- **Model**: A weather forecast dataset (e.g., Model 0 = control run, Model 1-20 = perturbed ensemble members)
  - Each model has its own weather data file (e.g., `2025110306_00.npz`, `2025110306_01.npz`, etc.); stored on disk
- **Simulator**: A simulation engine object that runs trajectory calculations
  - Each simulator contains: one `WindFile` (loaded from one model's `.npz` file) + elevation data; stored in RAM

**Pre-warming Process:**
1. Waits 2 seconds for app initialization
2. Fetches model configuration from `downloader.py` (respects `DOWNLOAD_CONTROL` and `NUM_PERTURBED_MEMBERS`)
   - Current config: 21 models (Model 0 + Models 1-20)
3. Pre-warms **only model 0** (cost-optimized for single requests)
   - Downloads weather data file from Supabase if not cached: `{timestamp}_00.npz` (~308 MB)
   - Creates `WindFile` object
   - Creates `Simulator` object (combines WindFile + elevation data)
   - Stores simulator in cache: `_simulator_cache[0] = Simulator(...)`
4. Loads elevation data singleton via `elev.getElevation(0, 0)`
   - Loads with memory-mapping (`mmap_mode='r'`); shared across all simulators and workers

**File Downloading (On-Demand):**
- Files are NOT pre-downloaded on startup (reduces Supabase egress costs)
- Files download on-demand when ensemble runs are requested
- Files are cached on disk after first download (~7.7GB total, no RAM cost)
- Subsequent ensemble runs use cached files (fast, no additional egress)
- **Model Change Cleanup**: When GEFS models update every 6 hours, old cached files are automatically deleted to prevent accumulation (e.g., when `2025110318` replaces `2025110312`, all `2025110312_*.npz` files are removed from cache)
- Only re-downloads if files are evicted from cache or model timestamp changes

**Memory Impact:**
- **Disk**: ~7.7 GB (21 weather files × 308 MB + 1 elevation file × 430 MB)
- **RAM**: ~150MB (1 simulator × ~150MB) - cost-optimized, only model 0

**Result**: 
- **Single requests**: Model 0 ready in memory, fast (~5-10 seconds)
- **First ensemble request**: Files download on-demand, simulators built in parallel (~30-60 seconds)
- **Subsequent ensemble requests**: Files cached on disk, simulators already in RAM cache (~5-10 seconds)

## Caching Layers

HABSIM uses a multi-layer caching strategy optimized for Railway (max 32GB RAM, 32 CPUs).

### 1. Simulator Cache (`simulate.py`) - **Dynamic RAM Cache**

**Location**: In-memory (RAM)  
**Storage**: `_simulator_cache` dictionary `{model_id: simulator}`  
**Capacity**: Dynamic - 5 simulators normal mode (~750MB), 25 simulators ensemble mode (~3.75GB)  
**Eviction**: LRU (Least Recently Used) when cache is full  
**Thread Safety**: `_cache_lock` protects all operations

**Dynamic Behavior:**
- **Normal Mode**: Cache limit = 5 simulators (~750MB)
  - Used for single model requests (default)
  - Only model 0 pre-warmed
- **Ensemble Mode**: Cache expands to 25 simulators (~3.75GB)
  - Triggered when `/sim/spaceshot` is called
  - Duration: 10 minutes (auto-extends with each ensemble run)
  - Allows all 21 models to be cached during ensemble runs
- **Auto-trimming**: After 10 minutes of no ensemble runs, cache trims to 5 most recently used models
  - Frees ~3GB RAM automatically
  - Keeps most recently used models for fast subsequent requests

**How it works**: 
- Fast path: If `model_id in _simulator_cache`, return immediately (~1μs)
- Slow path: If cache miss, load simulator from file cache and add to simulator cache
- For ensemble runs: All 21 simulators can be cached simultaneously (if in ensemble mode)

**Memory Usage**:
- ~150MB per simulator (includes WindFile metadata + OS page cache)
- Normal mode: 5 simulators = ~750MB
- Ensemble mode: 25 simulators = ~3.75GB
- Auto-trims back to ~750MB after ensemble mode expires

### 2. GEFS File Cache (`gefs.py`) - **Disk Cache**

**Location**: Disk (`/app/data/gefs` on Railway, `/opt/render/project/src/data/gefs` on Render, or `/tmp/habsim-gefs/` as fallback)  
**Storage**: `.npz` files (compressed NumPy arrays) and `worldelev.npy` (elevation data)  
**Capacity**: 25 `.npz` files (~7.7GB) + `worldelev.npy` (always kept)  
**Eviction**: LRU based on file access time (`st_atime`); never evicts `worldelev.npy` (required elevation data)  
**Thread Safety**: `_CACHE_LOCK` protects all operations  
**Model Change Cleanup**: Automatically deletes old model files when `whichgefs` changes (prevents accumulation of stale files from previous 6-hour model updates)

**Memory Usage**:  
- 308MB per `.npz` file
- 25 `.npz` files: ~7.7 GB on disk
- 430MB for `worldelev.npy`; loaded via memory-mapping (minimal RAM)
- **Files on disk don't directly consume RAM**, but OS page cache may load accessed portions into memory

**Elevation Data Cache** (`elev.py` and `habsim/classes.py`):
- **`elev.py`**: Memory-mapped singleton (`_ELEV_DATA`) used by `/sim/elev` endpoint, loaded once with `mmap_mode='r'` and shared across all workers/threads
- **`ElevationFile`** (`habsim/classes.py`): Memory-mapped read-only access for simulators

### 3. Prediction Result Cache (`simulate.py`) - **RAM Cache**

**Location**: In-memory (RAM)  
**Storage**: `_prediction_cache` dictionary `{cache_key: trajectory_data}`  
**Capacity**: 200 predictions (max)  
**Eviction**: LRU when cache is full; 1 hour TTL (Time To Live); cleared when GEFS model changes (`refresh()`)  
**Key**: Based on all simulation parameters (timestamp, lat, lon, rate, step, duration, alt, model, coefficient)

**Memory Usage**:
- ~200-300KB per prediction
- 200 predictions: ~40-60MB

### 4. Math Function Cache (`windfile.py`, `simulate.py`) - **RAM Cache**

**Location**: In-memory (RAM)  
**Storage**: Python `@lru_cache` decorators  
**Functions**: 
- `_alt_to_hpa_cached()`: Altitude to pressure conversion
- `_cos_lat_cached()`: Cosine of latitude (cached for repeated latitudes)
**Capacity**: 10,000 entries each  
**Memory**: <1MB total

## Memory Management

### Cache Priority
1. **Simulator Cache** (RAM) - Dynamic: 5 normal, 25 ensemble (cost-optimized)
2. **File Cache** (Disk) - 25 files (~7.7GB on disk, no direct RAM cost)
3. **Prediction Cache** (RAM) - 200 entries (~40-60MB)
4. **Math Cache** (RAM) - Minimal overhead (<1MB)

### Memory Usage Breakdown

**Normal Mode (Single Model Runs):**
- **Simulator Cache**: ~750MB (5 simulators)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
- **Prediction Cache**: ~40-60MB
- **Math Cache**: <2MB
- **Flask/Python**: ~200-300MB (4 workers)
- **OS Overhead**: ~200-300MB
- **Total**: ~1.2-1.4GB

**Ensemble Mode (Active Ensemble Runs):**
- **Simulator Cache**: ~3.75GB (25 simulators)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
- **Prediction Cache**: ~40-60MB
- **Math Cache**: <2MB
- **Flask/Python**: ~200-300MB (4 workers)
- **OS Overhead**: ~200-300MB
- **Total**: ~4.2-4.4GB

**After Ensemble Mode Expires (10 minutes):**
- Auto-trims back to normal mode: ~1.2-1.4GB
- Frees ~3GB RAM automatically

## Performance Optimizations

### Gunicorn Configuration (`gunicorn_config.py`)
- **Workers**: 4 workers (4 processes for better parallelism)
- **Threads**: 8 threads per worker = 32 total concurrent capacity (matches 32 CPUs)
- **Worker Class**: `gthread` (thread-based workers for I/O-bound tasks)
- **Preload**: `preload_app = True` shares memory between workers
- **Recycling**: `max_requests = 1000` (higher limit as more memory headroom)
- **Timeout**: 300 seconds (5 minutes) for long-running ensemble simulations

### Ensemble Execution (`app.py`)
- **ThreadPoolExecutor**: 16 workers for parallel ensemble simulation execution
- **Dynamic Cache Expansion**: Automatically expands simulator cache to 25 when ensemble is called
- **Auto-extension**: Each ensemble run extends ensemble mode by 10 minutes
- **Auto-trimming**: Cache trims back to 5 simulators 10 minutes after last ensemble run

### Model Change Management (`simulate.py`)
- **Automatic Detection**: `refresh()` checks `whichgefs` every 5 minutes for model updates
- **Cache Invalidation**: When model changes, clears simulator cache (RAM) and prediction cache
- **Old File Cleanup**: Automatically deletes old model files from disk cache when model timestamp changes (prevents accumulation of stale files from previous 6-hour GEFS updates)
- **Prevents**: Stale data issues, disk space bloat, and unnecessary re-downloads due to cache pressure

### Numerical Integration (`habsim/classes.py`)
- Runge-Kutta 2nd order (RK2 / Midpoint method) for trajectory integration
- Better accuracy than Euler method with minimal performance cost
- Original Euler implementation preserved in comments for reference

### Memory Leak Fixes
- **ElevationFile memory-mapping**: `ElevationFile` in `habsim/classes.py` uses `mmap_mode='r'` instead of loading full 430MB array into RAM
  - Previously: Each simulator held 430MB in RAM (4 workers = 1.7GB just for elevation)
  - After fix: Memory-mapped access, OS manages page cache (minimal direct RAM usage)
  - Note: `elev.py` was already memory-mapped, but `ElevationFile` was creating a duplicate full load
- **Explicit simulator cleanup**: Old simulators are explicitly deleted and garbage collected when cache evicts them
- **Worker recycling**: `max_requests = 1000` (reduced from original 800 for more aggressive recycling)

## UI Optimizations
- Elevation fetching debounced (150ms) to prevent rapid-fire requests on map clicks
- Server status polling (5s intervals) for live updates
- Model configuration fetched once on page load, cached in `window.availableModels`
- Ensemble runs use `/sim/spaceshot` endpoint for parallel execution

## Key Settings

**Gunicorn**:
- `workers = 4` in `gunicorn_config.py` (4 workers for parallelism)
- `threads = 8` in `gunicorn_config.py` (8 threads per worker = 32 concurrent capacity)

**File Cache**:
- `_MAX_CACHED_FILES = 25` in `gefs.py` (allows 25 weather files for 21-model ensemble + buffer)
- Cache directory: `/app/data/gefs` on Railway (persistent volume)

**Simulator Cache**:
- `MAX_SIMULATOR_CACHE_NORMAL = 5` in `simulate.py` (normal mode: 5 simulators)
- `MAX_SIMULATOR_CACHE_ENSEMBLE = 25` in `simulate.py` (ensemble mode: 25 simulators)
- Dynamic expansion/contraction based on ensemble mode

**Prediction Cache**:
- `MAX_CACHE_SIZE = 200` in `simulate.py` (increased from 30)
- `CACHE_TTL = 3600` (1 hour) in `simulate.py`

**Ensemble Mode**:
- Auto-enabled when `/sim/spaceshot` is called
- Duration: 10 minutes (auto-extends with each ensemble run)
- Auto-trims cache to 5 simulators after expiration

## Performance Profile

### Single Model Runs (Default)
- **Speed**: ~5-10 seconds
- **RAM**: ~1.2-1.4GB (normal mode)
- **CPU**: Minimal (single request processing)
- **Why Fast**: Model 0 pre-warmed in RAM, files on disk

### First Ensemble Run (21 Models)
- **Speed**: ~30-60 seconds (if files on disk) or ~2-5 minutes (if files need download)
- **RAM**: Expands to ~4.2-4.4GB (ensemble mode)
- **CPU**: Moderate (16 parallel threads active)
- **Process**: 
  - Check if files exist on disk → download from Supabase if missing
  - Read files from disk → create simulators in parallel → cache in RAM
  - Files cached on disk for subsequent runs (no additional egress)

### Subsequent Ensemble Runs (Within 10 Minutes)
- **Speed**: ~5-10 seconds
- **RAM**: ~4.2-4.4GB (maintained from first run)
- **CPU**: Moderate (16 parallel threads active)
- **Why Fast**: All 21 simulators already in RAM cache

### After 10 Minutes (Auto-trim)
- **RAM**: Trims to ~1.2-1.4GB (back to normal mode)
- **CPU**: Minimal (idle)
- **Cost**: Minimal RAM usage when not doing ensemble runs
