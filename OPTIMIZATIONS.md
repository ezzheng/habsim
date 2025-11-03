# HABSIM Optimizations

Technical reference for optimizations implemented for Render (2GB RAM, 1 CPU).

## Deploy to Render

**Start Command:**
```bash
gunicorn --config gunicorn_config.py app:app
```

## Cache Pre-warming (`app.py`)

### Startup Pre-warming
- **Function**: `_prewarm_cache()` runs in background thread on startup
- **Timing**: 2-second delay after app initialization to allow full startup
- **Benefits**: Eliminates cold start delay (40-70s → 5s for first request)

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
3. Pre-warms only first 2 models (0 and 1) to prevent memory spikes on startup
   - For each model (0, 1):
     - Downloads weather data file from Supabase if not cached: `{timestamp}_{model_id}.npz` (~307.83 MB each)
     - Creates `WindFile` object
     - Creates `Simulator` object (combines WindFile + elevation data)
     - Stores simulator in cache: `_simulator_cache[model_id] = Simulator(...)`
   - Model 2 loads on-demand when needed
4. Loads elevation data singleton via `elev.getElevation(0, 0)`
   - Loads with memory-mapping (`mmap_mode='r'`)
   - Shared across all simulators and workers
5. Logs completion status

**Memory Impact:**
- **Disk**: ~1.05 GB (2 weather files × 307.83 MB + 1 elevation file × 430.11 MB)
- **RAM**: ~100-200MB per worker (2 simulators × 50-100MB each)

**Result**: Models 0-1 are ready in memory, allowing fast single and most ensemble runs. Model 2 loads on-demand (~5-10s) when needed.

## Caching Layers

HABSIM uses a multi-layer caching strategy to optimize performance while managing memory constraints.

### 1. Simulator Cache (`simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: `_simulator_cache` dictionary `{model_id: Simulator}`  
**Capacity**: Dynamic, 1-2 simulators based on memory pressure  
**Eviction**: LRU (Least Recently Used) with memory-aware limits and proactive monitoring  
**Thread Safety**: `_SIMULATOR_CACHE_LOCK` protects all operations

**Memory-Aware Limits**:
- Memory < 65%: 2 simulators (full cache)
- Memory 65-80%: 1 simulator (aggressive reduction)
- Memory > 80%: 1 simulator (maximum reduction)

**Proactive Monitoring**: Memory is checked on every cache access when cache is at capacity (2 simulators), or periodically (every 30s) when below capacity. If memory pressure increases, simulators are evicted immediately even if already loaded.

**Memory Usage**:
- ~50-100MB per simulator (includes WindFile metadata + OS page cache)
- 2 simulators: ~100-200MB per worker
- 2 workers: ~200-400MB total

### 2. GEFS File Cache (`gefs.py`) - **Disk Cache**
**Location**: Disk (`/opt/render/project/src/data/gefs` on Render, or `/tmp/habsim-gefs/` as fallback)  
**Storage**: `.npz` files (compressed NumPy arrays) and `worldelev.npy` (elevation data)  
**Capacity**: 2 `.npz` files + `worldelev.npy` (always kept)
**Eviction**: LRU based on file access time (`st_atime`); never evicts `worldelev.npy` (required elevation data)
**Thread Safety**: `_CACHE_LOCK` protects all operations

**Why persistent directory**: Uses `/opt/render/project/src/data/gefs` on Render instead of `/tmp` to avoid 2GB temporary storage limit.

**Memory Usage**:  
- 307.83MB per `.npz` file
- 2 `.npz` files: ~615 MB on disk
- 430.11MB for `worldelev.npy`; loaded via memory-mapping (minimal RAM)
- Files on disk don't directly consume RAM, but OS page cache may load accessed portions into memory

**Elevation Data Cache** (`elev.py`):
- Part of GEFS file cache; memory-mapped access, loaded once with `mmap_mode='r'` (read-only, OS-managed page cache) and then shared across all workers/threads


### 3. Prediction Result Cache (`simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: `_prediction_cache` dictionary `{cache_key: trajectory_data}`  
**Capacity**: 30 predictions (max)  
**Eviction**: LRU when cache is full; 1 hour TTL (Time To Live); cleared when GEFS model changes (`refresh()`)

**Memory Usage**:
- ~200KB per prediction
- 30 predictions: ~6MB

**Features**:
- Hash-based lookup: `_cache_key()` generates MD5 from simulation parameters
- Checks cache before running simulation
- Automatically expires entries after 1 hour
- Cleared when GEFS model changes (`refresh()`)

### 4. Math Function Cache (`windfile.py`, `simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: Python `@lru_cache` decorators  
**Functions**: 
- `_alt_to_hpa_cached()`: Altitude to pressure conversion
- `_cos_lat_cached()`: Cosine of latitude (cached for repeated latitudes)
**Capacity**: 10,000 entries each  
**Memory**: <1MB total

## Memory Management

### Cache Priority (Memory-Aware)
1. **Simulator Cache** (RAM) - Priority #1: Memory-aware, adjusts 1-2 simulators
2. **File Cache** (Disk) - Priority #2: Static at 2 `.npz` files + `worldelev.npy` (doesn't directly affect RAM)
3. **Prediction Cache** (RAM) - Priority #3: Fixed at 30 entries (~6MB)
4. **Math Cache** (RAM) - Minimal overhead (<1MB)

### Current Memory Usage Breakdown (2 Workers)
- **Simulator Cache**: 200-400MB (2 simulators × 2 workers, or less if memory-constrained)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
  - `.npz` files: OS page cache (variable, not directly controlled)
  - `worldelev.npy`: Memory-mapped, OS-managed page cache (minimal overhead)
- **Prediction Cache**: ~12MB (30 entries × 2 workers)
- **Math Cache**: <2MB (2 workers)
- **Flask/Python**: ~100-200MB (2 workers)
- **OS Overhead**: ~200MB
- **Total**: ~514-814MB typical, up to ~1.2GB under load

## Other Optimizations

### Parallel Execution (`app.py`)
- `ThreadPoolExecutor` with 2 workers for ensemble mode
- I/O-bound tasks benefit from threading vs multiprocessing
- `with ThreadPoolExecutor(max_workers=2)` in `/sim/spaceshot`

### HTTP Response Caching (`app.py`)
- `@cache_for(600)` decorator adds `Cache-Control` headers
- Browser caches results for 10 minutes
- Applied to all simulation endpoints

### Response Compression (`requirements.txt`, `app.py`)
- `flask-compress` automatically gzips responses
- Typical 10x reduction (500KB → 50KB)

### Numerical Integration (`habsim/classes.py`)
- Runge-Kutta 2nd order (RK2 / Midpoint method) for trajectory integration
- Better accuracy than Euler method with minimal performance cost
- Original Euler implementation preserved in comments for reference

### Gunicorn Config (`gunicorn_config.py`)
- 2 workers, 2 threads each = 4 concurrent requests
- `preload_app = True` shares memory between workers
- `max_requests = 800` recycles workers to prevent leaks

## UI Optimizations
- Elevation fetching debounced (150ms) to prevent rapid-fire requests on map clicks
- Server status polling (5s intervals) for live updates
- Model configuration fetched once on page load, cached in `window.availableModels`

## Key Settings

**Gunicorn**:
- `workers = 2` in `gunicorn_config.py`
- `threads = 2` in `gunicorn_config.py`

**File Cache**:
- `_MAX_CACHED_FILES = 2` in `gefs.py`
- Cache directory: `/opt/render/project/src/data/gefs` on Render (persistent, avoids `/tmp` 2GB limit)

**Simulator Cache**:
- `_MAX_SIMULATOR_CACHE = 2` in `simulate.py`
- Dynamic limit based on memory pressure (1-2)

**Prediction Cache**:
- `MAX_CACHE_SIZE = 30` in `simulate.py`
- `CACHE_TTL = 3600` (1 hour) in `simulate.py`
