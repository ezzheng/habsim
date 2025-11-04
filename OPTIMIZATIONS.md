# HABSIM Optimizations

Technical reference for optimizations implemented for Render (2GB RAM, 1 CPU).

## Deploy to Render

**Start Command:**
```bash
gunicorn --config gunicorn_config.py app:app
```

## Cache Pre-warming (`app.py`)

### Startup Pre-warming
- **Function**: `_prewarm_cache()` runs in background thread on startup; eliminates cold start delay (40-70s → 5s for first request)
- **Timing**: 2-second delay after app initialization to allow full startup

### What Gets Pre-warmed

**Models vs Simulators:**
- **Model**: A weather forecast dataset (e.g., Model 0 = control run, Model 1-2 = perturbed ensemble members)
  - Each model has its own weather data file (e.g., `2025110306_00.npz`, `2025110306_01.npz`, `2025110306_02.npz`); stored on disk
- **Simulator**: A simulation engine object that runs trajectory calculations
  - Each simulator contains: one `WindFile` (loaded from one model's `.npz` file) + elevation data; stored in RAM

**Pre-warming Process:**
1. Waits 2 seconds for app initialization
2. Fetches model configuration from `downloader.py` (respects `DOWNLOAD_CONTROL` and `NUM_PERTURBED_MEMBERS`)
   - Current config: Models 0, 1, 2 (3 models total)
3. Pre-warms model 0 (most common, fastest path for single model requests)
   - Downloads weather data file from Supabase if not cached: `{timestamp}_00.npz` (~307.83 MB)
   - Creates `WindFile` object
   - Creates `Simulator` object (combines WindFile + elevation data)
   - Stores simulator in cache: `_cached_simulator = Simulator(...)`
4. Loads elevation data singleton via `elev.getElevation(0, 0)`
   - Loads with memory-mapping (`mmap_mode='r'`); shared across all simulators and workers

**Memory Impact:**
- **Disk**: ~1.35 GB (3 weather files × 307.83 MB + 1 elevation file × 430.11 MB)
- **RAM**: ~50-100MB per worker (1 simulator × 50-100MB)

**Result**: Model 0 is ready in memory, allowing fast single model requests. Models 1-2 load on-demand (~5-10s) when needed for ensemble runs.

## Caching Layers

HABSIM uses a multi-layer caching strategy to optimize performance while managing memory constraints.

### 1. Simulator Cache (`simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: `_cached_simulator` (single simulator) + `_cached_model` (model ID)  
**Capacity**: 1 simulator (simple fast path for single model requests)  
**Eviction**: Replaced when a different model is requested  
**Thread Safety**: No locks needed (simple variable assignment)

**How it works**: 
- Fast path: If `_cached_model == requested_model`, return `_cached_simulator` immediately (~1μs)
- Slow path: If different model requested, load new simulator and replace cache
- For ensemble runs, each model loads on-demand (simulator cache helps if same model requested multiple times)

**Memory Usage**:
- ~50-100MB per simulator (includes WindFile metadata + OS page cache)
- 1 simulator: ~50-100MB per worker
- 2 workers: ~100-200MB total

### 2. GEFS File Cache (`gefs.py`) - **Disk Cache**
**Location**: Disk (`/opt/render/project/src/data/gefs` on Render, or `/tmp/habsim-gefs/` as fallback)  
**Storage**: `.npz` files (compressed NumPy arrays) and `worldelev.npy` (elevation data)  
**Capacity**: 3 `.npz` files + `worldelev.npy` (always kept)
**Eviction**: LRU based on file access time (`st_atime`); never evicts `worldelev.npy` (required elevation data)
**Thread Safety**: `_CACHE_LOCK` protects all operations

**Memory Usage**:  
- 307.83MB per `.npz` file
- 3 `.npz` files: ~924 MB on disk
- 430.11MB for `worldelev.npy`; loaded via memory-mapping (minimal RAM)
- Files on disk don't directly consume RAM, but OS page cache may load accessed portions into memory

**Elevation Data Cache** (`elev.py` and `habsim/classes.py`):
- **`elev.py`**: Memory-mapped singleton (`_ELEV_DATA`) used by `/sim/elev` endpoint, loaded once with `mmap_mode='r'` and shared across all workers/threads
- **`ElevationFile`** (`habsim/classes.py`): Memory-mapped read-only access for simulators


### 3. Prediction Result Cache (`simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: `_prediction_cache` dictionary `{cache_key: trajectory_data}`  
**Capacity**: 30 predictions (max)  
**Eviction**: LRU when cache is full; 1 hour TTL (Time To Live); cleared when GEFS model changes (`refresh()`)

**Memory Usage**:
- ~200KB per prediction
- 30 predictions: ~6MB

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
1. **Simulator Cache** (RAM) - Simple 1-simulator cache for fast single model requests
2. **File Cache** (Disk) - Static at 3 `.npz` files + `worldelev.npy` (doesn't directly affect RAM)
3. **Prediction Cache** (RAM) - Fixed at 30 entries (~6MB)
4. **Math Cache** (RAM) - Minimal overhead (<1MB)

### Current Memory Usage Breakdown (2 Workers)
- **Simulator Cache**: 100-200MB (1 simulator × 2 workers)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
  - `.npz` files: OS page cache (variable, not directly controlled)
  - `worldelev.npy`: Memory-mapped, OS-managed page cache (minimal overhead)
- **Prediction Cache**: ~12MB (30 entries × 2 workers)
- **Math Cache**: <2MB (2 workers)
- **Flask/Python**: ~100-200MB (2 workers)
- **OS Overhead**: ~200MB
- **Total**: ~514-814MB typical, up to ~1.2GB under load

## Other Optimizations

### Numerical Integration (`habsim/classes.py`)
- Runge-Kutta 2nd order (RK2 / Midpoint method) for trajectory integration
- Better accuracy than Euler method with minimal performance cost
- Original Euler implementation preserved in comments for reference

### Memory Leak Fixes
- **ElevationFile memory-mapping**: `ElevationFile` in `habsim/classes.py` now uses `mmap_mode='r'` instead of loading full 430MB array into RAM
  - Previously: Each simulator held 430MB in RAM (2 workers = 860MB just for elevation)
  - After fix: Memory-mapped access, OS manages page cache (minimal direct RAM usage)
  - Note: `elev.py` was already memory-mapped, but `ElevationFile` was creating a duplicate full load
- **Explicit simulator cleanup**: Old simulator is explicitly deleted and garbage collected before creating new one
- **Aggressive worker recycling**: `max_requests = 300` (reduced from 800) to prevent gradual memory buildup

### Gunicorn Config (`gunicorn_config.py`)
- 2 workers, 2 threads each = 4 concurrent requests
- `preload_app = True` shares memory between workers
- `max_requests = 300` recycles workers aggressively to prevent memory leaks (reduced from 800)

## UI Optimizations
- Elevation fetching debounced (150ms) to prevent rapid-fire requests on map clicks
- Server status polling (5s intervals) for live updates
- Model configuration fetched once on page load, cached in `window.availableModels`

## Key Settings

**Gunicorn**:
- `workers = 2` in `gunicorn_config.py`
- `threads = 2` in `gunicorn_config.py`

**File Cache**:
- `_MAX_CACHED_FILES = 3` in `gefs.py` (allows 3 weather files for ensemble runs)
- Cache directory: `/opt/render/project/src/data/gefs` on Render (persistent, avoids `/tmp` 2GB limit)

**Simulator Cache**:
- Simple 1-simulator cache in `simulate.py` (not a dict-based LRU)
- Explicit cleanup with garbage collection when replacing simulator

**Prediction Cache**:
- `MAX_CACHE_SIZE = 30` in `simulate.py`
- `CACHE_TTL = 3600` (1 hour) in `simulate.py`
