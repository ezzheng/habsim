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
- **Models**: Pre-loads all configured models via `_get_simulator()`
- **Elevation**: Pre-loads elevation data via `elev.getElevation()`
- **Timing**: 2-second delay after app initialization to allow full startup
- **Memory Impact**: 
  - All 3 simulators loaded into cache (~150-300MB per worker)
  - Elevation data memory-mapped (no additional RAM)
- **Benefits**: Eliminates cold start delay (40-70s → 5s for first request)

### Pre-warming Process
1. Waits 2 seconds for app initialization
2. Fetches model configuration from `downloader.py` (respects `DOWNLOAD_CONTROL` and `NUM_PERTURBED_MEMBERS`)
3. Loads each model sequentially into simulator cache
4. Loads elevation data singleton
5. Logs completion status

## Caching Layers

HABSIM uses a multi-layer caching strategy to optimize performance while managing memory constraints.

### 1. Simulator Cache (`simulate.py`) - **RAM Cache**
**Location**: In-memory (RAM)  
**Storage**: `_simulator_cache` dictionary `{model_id: Simulator}`  
**Capacity**: Dynamic, 1-3 simulators based on memory pressure  
**Eviction**: LRU (Least Recently Used) with memory-aware limits  
**Thread Safety**: `_SIMULATOR_CACHE_LOCK` protects all operations

**Memory-Aware Limits**:
- Memory < 70%: 3 simulators (full cache)
- Memory 70-85%: 2 simulators (moderate reduction)
- Memory > 85%: 1 simulator (aggressive reduction)

**Memory Usage**:
- ~50-100MB per simulator (includes WindFile metadata + OS page cache)
- 3 simulators: ~150-300MB per worker
- 2 workers: ~300-600MB total

### 2. GEFS File Cache (`gefs.py`) - **Disk Cache**
**Location**: Disk (`/tmp/habsim-gefs/` or `HABSIM_CACHE_DIR`)  
**Storage**: `.npz` files (compressed NumPy arrays) and `worldelev.npy` (elevation data)  
**Capacity**: 3 `.npz` files + `worldelev.npy` (always kept)
**Eviction**: LRU based on file access time (`st_atime`); never evicts `worldelev.npy` (required elevation data)
**Thread Safety**: `_CACHE_LOCK` protects all operations

**Memory Usage**:  
- 307.83MB per `.npz` file
- 3 `.npz` files: ~924 MB on disk
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
1. **Simulator Cache** (RAM) - Priority #1: Memory-aware, adjusts 1-3 simulators
2. **File Cache** (Disk) - Priority #2: Static at 3 `.npz` files + `worldelev.npy` (doesn't directly affect RAM)
3. **Prediction Cache** (RAM) - Priority #3: Fixed at 30 entries (~6MB)
4. **Math Cache** (RAM) - Minimal overhead (<1MB)

### Current Memory Usage Breakdown (2 Workers)
- **Simulator Cache**: 300-600MB (3 simulators × 2 workers, or less if memory-constrained)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
  - `.npz` files: OS page cache (variable, not directly controlled)
  - `worldelev.npy`: Memory-mapped, OS-managed page cache (minimal overhead)
- **Prediction Cache**: ~12MB (30 entries × 2 workers)
- **Math Cache**: <2MB (2 workers)
- **Flask/Python**: ~100-200MB (2 workers)
- **OS Overhead**: ~200MB
- **Total**: ~612-1014MB typical, up to ~1.5GB under load

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
- `_MAX_CACHED_FILES = 3` in `gefs.py`

**Simulator Cache**:
- `_MAX_SIMULATOR_CACHE = 3` in `simulate.py`
- Dynamic limit based on memory pressure (1-3)

**Prediction Cache**:
- `MAX_CACHE_SIZE = 30` in `simulate.py`
- `CACHE_TTL = 3600` (1 hour) in `simulate.py`
