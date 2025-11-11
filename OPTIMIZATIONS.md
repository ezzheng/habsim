# HABSIM Optimizations

Technical reference for optimizations implemented for Railway (32GB RAM, 32 vCPU).

## Monte Carlo Simulation & Heatmap Visualization

### Overview
The ensemble endpoint (`/sim/spaceshot`) includes Monte Carlo simulation to quantify landing position uncertainty. This creates a probability density heatmap showing where the balloon is most likely to land based on parameter variations and weather model uncertainty.

### Monte Carlo Process
1. **Parameter Perturbation Generation**: Creates 20 random variations of input parameters
   - Latitude/Longitude: ±0.1° (≈ ±11km) - accounts for launch site uncertainty
   - Launch Altitude: ±50m - launch altitude measurement variation
   - Equilibrium Altitude: ±200m - burst altitude uncertainty
   - Equilibrium Time: ±10% - timing variation in reaching equilibrium
   - Ascent/Descent Rate: ±0.1 m/s - rate measurement uncertainty

2. **Simulation Execution**: Runs each perturbation through all 21 weather models
   - Total simulations: 420 (20 perturbations × 21 models)
   - Runs in parallel with ensemble paths (441 total simulations)
   - Each simulation extracts only final landing position (lat, lon)

3. **Result Collection**: Aggregates 420 landing positions for heatmap visualization
   - High-density areas (red) indicate many simulations landed there
   - Low-density areas (cyan) indicate few simulations landed there

### Heatmap Visualization
- **Implementation**: Custom canvas overlay (`CustomHeatmapOverlay` extending `google.maps.OverlayView`)
- **Data**: 420 landing positions (lat, lon) with equal weight
- **Density Estimation**: Custom kernel density estimation with configurable smoothing kernels
- **Smoothing Options**:
  - `'epanechnikov'` (default) - Epanechnikov kernel, shape-preserving, recommended for preserving actual data distribution
  - `'none'` - Raw density grid, no smoothing, maximum shape preservation
  - `'uniform'` - Uniform kernel, rectangular shape
  - `'gaussian'` - Gaussian kernel, smooth but circular (similar to Google Maps default)
- **Color Gradient**: Green (low) → Yellow → Orange → Red (high, inner)
- **Contours**: Cumulative mass thresholds at 30/50/70/90% (higher % encloses larger area)
- **Interactivity**: Heatmap is placed below interactive overlay panes; contour polygons/labels are non-clickable so waypoint hovers work through the heatmap
- **Properties**: 
  - `opacity: 0.6` - overlay opacity (allows seeing map/ensemble paths underneath)
  - `gridResolution: 100` - density grid resolution (higher = smoother but slower)
  - `smoothingBandwidth: null` - auto-calculated (5% of data range) or manually specified
- **Advantage**: Avoids Google Maps' forced circular Gaussian smoothing, preserving actual data distribution shape
- **Visualization**: Overlays on ensemble paths to show both individual trajectories and probability density

## Deploy to Railway

**Start Command:**
```bash
gunicorn --config gunicorn_config.py app:app
```

## Cache Pre-warming (`app.py`)

### Startup Pre-warming
One background thread runs on startup to optimize performance:

1. **`_prewarm_cache()`**: Builds the model 0 simulator (fast single requests) and memory-maps `worldelev.npy`
   - Weather files still download on-demand; once cached on disk they are reused by later runs

### What Gets Pre-warmed

**Models vs Simulators:**
- **Model**: A weather forecast dataset (e.g., Model 0 = control run, Model 1-20 = perturbed ensemble members)
  - Each model has its own weather data file (e.g., `2025110306_00.npz`, `2025110306_01.npz`, etc.); stored on disk
- **Simulator**: A simulation engine object that runs trajectory calculations
  - Each simulator contains: one `WindFile` (loaded from one model's `.npz` file) + elevation data; stored in RAM

**Pre-warming Process:**
1. Waits 2 seconds for app initialization
2. Builds the model 0 simulator (fast single requests)
3. Touches `worldelev.npy` (451 MB) so the elevation grid is memory-mapped before first use
4. Reuses on-disk GEFS cache from persistent volume when mounted

**File Downloading:**
- Weather files download on-demand when ensemble runs are requested (cost-optimized)
- `worldelev.npy` pre-downloaded at startup (always available, never evicted)
- Files cached on disk after download (~7.7GB total weather files + 451MB elevation)
- Automatic cleanup of old model files when GEFS models update every 6 hours

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

**Location**: RAM  
**Storage**: `_simulator_cache` `{model_id: simulator}`  
**Capacity**: 5 simulators (normal) → 25 simulators (ensemble) per worker  
**Eviction**: LRU with aggressive cleanup, guarded by `_cache_lock`

**Dynamic behavior**
- **Normal mode**: 10 simulators (~1.5 GB per worker, increased from 5 for 32GB RAM system). Only model 0 is built during startup warm-up.
- **Ensemble mode**: `/sim/spaceshot` raises the cap to 30 for 60 s (auto-extends, hard cap 5 min). Ensemble mode preloads wind arrays into RAM so runs stay CPU-bound (5-10s vs 50-80s with memory-mapping).
- **Model prefetching**: Background thread prefetches all 21 models when ensemble mode starts, pre-warming cache.
- **Auto-trim**: `_periodic_cache_trim()` wakes roughly every 20 s (or 3 s when trims fail) and shrinks back to 10 simulators once the ensemble window closes.
- **Delayed cleanup**: 2-second delay after eviction (reduced from 5s) to prevent race conditions while ensuring faster memory release.
- **Idle reset**: If the worker is idle for ≥120 s (reduced from 180s), `_idle_memory_cleanup()` clears every simulator, resets limits, runs multi-pass GC, and calls `malloc_trim(0)` to release memory while keeping `worldelev.npy` mapped.
- **Defensive checks**: Validates simulator wind_file is not None at multiple points to prevent race condition crashes.

**Memory usage**
- ~150 MB per simulator when memory-mapped (normal mode)
- ~460 MB per simulator when preloaded (ensemble mode, faster CPU-bound performance)
- Worst case (30 simulators preloaded) ≈ 13.8 GB per worker; idle cleanup drops usage back near the cold-start baseline automatically.

### 2. GEFS File Cache (`gefs.py`) - **Disk Cache**

**Location**: `/app/data/gefs` on Railway persistent volume (or `/tmp/habsim-gefs/` fallback if PV not mounted)
**Storage**: 21 `.npz` wind files + `worldelev.npy`
**Capacity**: 30 `.npz` files (~9.2 GB, increased from 25 for 32GB RAM system) + `worldelev.npy` (451 MB, never evicted)
**Size Limits**: Triggers cleanup at 25GB total size, cleans down to 24GB (increased from 21GB/20GB)
**Eviction**: LRU by file access time (`worldelev.npy` exempt)
**Thread safety**: `_CACHE_LOCK` plus inter-process file locks stop duplicate downloads
**Reliability**: Extended timeouts (30 min read), stall detection, resumable downloads, corruption checks, download progress logging, temp file race condition protection
**Connection Pool**: 64 connections (increased from 32) for high concurrency (supports 2 devices × 21 models = 42 concurrent downloads)
**Persistent Volume Benefits**: Lower S3 egress, faster warmups, shared cache across workers and restarts. Mount at `/app/data` to enable.

**Memory Usage**:  
- 308MB per `.npz` file
- 30 `.npz` files: ~9.2 GB on disk
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
1. **Simulator Cache** (RAM) - Dynamic: 10 normal (~1.5GB), 30 ensemble (~13.8GB per worker when preloaded)
   - Normal mode: Memory-mapped (I/O-bound, memory-efficient, ~150MB per simulator)
   - Ensemble mode: Preloaded arrays (CPU-bound, faster, ~460MB per simulator, 5-10s vs 50-80s)
2. **File Cache** (Disk) - 30 files (~9.2GB on disk, no direct RAM cost)
3. **Prediction Cache** (RAM) - 200 entries (~40-60MB)
4. **Math Cache** (RAM) - Minimal overhead (<1MB)

### Memory Usage Breakdown

**Normal Mode (Single Model Runs):**
- **Simulator Cache**: ~1.5GB per worker × 4 workers = ~6GB total (worst case if all workers cache different models)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
- **Prediction Cache**: ~40-60MB per worker
- **Math Cache**: <2MB per worker
- **Flask/Python**: ~200-300MB (4 workers, preload_app=True shares code)
- **OS Overhead**: ~200-300MB
- **Total**: ~6.5-7GB (worst case), typically ~2-3GB (models distributed across workers)

**Ensemble Mode (Active Ensemble Runs):**
- **Simulator Cache**: ~13.8GB per worker × 4 workers = ~55GB total (worst case - all workers cache all 30 models with preloaded arrays)
  - Each simulator: ~460MB when preloaded (includes WindFile full array in RAM)
  - Preloading enables CPU-bound performance (5-10s simulations vs 50-80s with memory-mapping)
- **File Cache**: 0MB direct (on disk, OS page cache managed separately)
- **Prediction Cache**: ~40-60MB per worker
- **Math Cache**: <2MB per worker
- **Flask/Python**: ~200-300MB (4 workers, preload_app=True shares code)
- **OS Overhead**: ~200-300MB
- **Total**: ~55-56GB (worst case), typically ~15-20GB (models distributed across workers, not all preloaded simultaneously)

**After Ensemble Mode Expires (60 seconds):**
- Background thread trims cache in all workers every 20 seconds
- Simulators are evicted → memory automatically freed (2-second delayed cleanup)
- Each worker auto-trims back to normal mode: ~1.5GB per worker
- Total: ~6GB (4 workers × 1.5GB) after trimming
- Frees ~49GB RAM automatically (from 55GB → 6GB worst case)

## Performance Optimizations

### Gunicorn Configuration (`gunicorn_config.py`)
- **Workers**: 4 workers (4 processes to minimize memory duplication)
- **Threads**: 8 threads per worker = 32 total concurrent capacity (matches 32 CPUs)
- **Worker Class**: `gthread` (thread-based workers; NumPy releases GIL for CPU-bound computation)
- **Preload**: `preload_app = True` shares memory between workers
- **Strategy**: Fewer workers + more threads = same CPU capacity with less RAM (threads share memory)
- **Recycling**: `max_requests = 1000` (higher limit as more memory headroom)
- **Timeout**: 900 seconds (15 minutes) for long-running simulations (ensemble ~30-60s, Monte Carlo ~5-15min)

### Ensemble Execution (`app.py`)
- **ThreadPoolExecutor**: 32 workers for parallel ensemble simulation execution (fully utilizes all 32 CPUs)
- **Dynamic Cache Expansion**: Automatically expands simulator cache to 30 when ensemble is called
- **Preloading**: Ensemble mode preloads wind arrays into RAM for CPU-bound performance (5-10s vs 50-80s with memory-mapping)
- **Model Prefetching**: Background thread prefetches all 21 models when ensemble mode starts, pre-warming cache
- **Monte Carlo Integration**: `/sim/spaceshot` now runs both:
  - 21 ensemble paths (for line plotting)
  - 420 Monte Carlo simulations (20 perturbations × 21 models) for heatmap visualization
  - Both run in parallel using the same 32-worker pool
  - Returns `{paths: [...], heatmap_data: [...]}` for frontend visualization
- **Auto-extension**: Each ensemble run extends ensemble mode by 60 seconds (maximum 5 minutes total)
- **Auto-trimming**: Cache trims back to 5 simulators 60 seconds after last ensemble run
- **Maximum Duration Cap**: Ensemble mode is capped at 5 minutes to prevent indefinite extension from consecutive calls
- **Ensemble Mode Only**: Ensemble mode is ONLY extended by `/sim/spaceshot` endpoint (explicit ensemble + Monte Carlo calls). Single model requests (`/sim/singlezpb`) do NOT extend ensemble mode

### Model Change Management (`simulate.py`)
- **Automatic Detection**: `refresh()` checks `whichgefs` every 5 minutes for model updates
- **Cache Invalidation**: When model changes, clears simulator cache (RAM) and prediction cache
- **Old File Cleanup**: Automatically deletes old model files from disk cache when model timestamp changes (prevents accumulation of stale files from previous 6-hour GEFS updates)
- **Prevents**: Stale data issues, disk space bloat, and unnecessary re-downloads due to cache pressure

### Numerical Integration (`habsim/classes.py`)
- Runge-Kutta 2nd order (RK2 / Midpoint method) for trajectory integration
- Better accuracy than Euler method with minimal performance cost
- Original Euler implementation preserved in comments for reference

### Memory Management
- **WindFile cleanup**: Every simulator eviction calls `WindFile.cleanup()` to drop numpy arrays before references are cleared.
- **Aggressive GC**: Trim passes run multiple GC cycles (including generation 2) followed by `malloc_trim(0)` so freed pages return to the OS.
- **Idle reset**: Workers that stay idle for 180 s trigger `_idle_memory_cleanup()`—clears simulators, resets ensemble mode, runs GC, trims RSS—while leaving `worldelev.npy` mapped.
- **Ensemble cap**: Cache expansion is capped at 5 minutes of continuous ensemble mode; after that, limits snap back to normal even if calls continue.
- **Worker recycling**: `max_requests = 800` provides a final safeguard against long-lived leaks.

## UI Optimizations
- Elevation fetching debounced (150ms) to prevent rapid-fire requests
- Centralized visualization clearing (`clearAllVisualizations()`) ensures heatmap clears when paths clear
- Model configuration fetched once on page load, cached in `window.availableModels`
- Ensemble runs use `/sim/spaceshot` endpoint for parallel execution

## Key Settings

**Gunicorn**:
- `workers = 4` in `gunicorn_config.py` (4 workers to minimize memory duplication)
- `threads = 8` in `gunicorn_config.py` (8 threads per worker = 32 concurrent capacity, threads share memory)

**File Cache**:
- `_MAX_CACHED_FILES = 30` in `gefs.py` (allows 30 weather files for 21-model ensemble + buffer, increased for 32GB RAM)
- Size limits: 25GB trigger, 24GB target (increased from 21GB/20GB)
- Cache directory: `/app/data/gefs` on Railway (persistent volume if mounted)
- Connection pool: 64 connections (increased from 32) for high concurrency

**Simulator Cache**:
- `MAX_SIMULATOR_CACHE_NORMAL = 10` in `simulate.py` (normal mode: 10 simulators, ~1.5GB per worker, increased from 5)
- `MAX_SIMULATOR_CACHE_ENSEMBLE = 30` in `simulate.py` (ensemble mode: 30 simulators, ~13.8GB per worker when preloaded, increased from 25)
- Dynamic expansion/contraction based on ensemble mode
- **Normal mode**: Memory-mapped files for memory efficiency (I/O-bound, ~150MB per simulator)
- **Ensemble mode**: Preloaded arrays for CPU-bound performance (5-10s vs 50-80s, ~460MB per simulator)

**Prediction Cache**:
- `MAX_CACHE_SIZE = 200` in `simulate.py` (increased from 30)
- `CACHE_TTL = 3600` (1 hour) in `simulate.py`

**Ensemble Mode**:
- Auto-enabled when `/sim/spaceshot` is called (ONLY endpoint that extends ensemble mode)
- Duration: 60 seconds (1 minute, auto-extends with each ensemble run, but capped at 5 minutes maximum)
- Auto-trims cache to 5 simulators after expiration (within 60-90 seconds after last ensemble call)
- **Important**: Single model requests (`/sim/singlezpb`) do NOT extend ensemble mode to prevent memory bloat

## Performance Profile

### Single Model Runs (Default)
- **Speed**: ~5-10 seconds
- **RAM**: ~3.5-4GB (worst case: 4 workers × 750MB), typically ~1.5-2GB (models distributed across workers)
- **CPU**: Minimal (single request processing)
- **Why Fast**: Model 0 pre-warmed in RAM, files on disk

### First Ensemble Run (21 Models + Monte Carlo)
- **Speed**: ~5-15 minutes (if files on disk) or ~5-20 minutes (if files need download)
  - 21 ensemble paths: ~5-10 seconds (with preloading, vs 30-60s with memory-mapping)
  - 420 Monte Carlo simulations: ~4-14 minutes (20× more simulations)
  - Both run in parallel using same 32-worker pool
  - Model prefetching pre-warms cache, eliminating download wait times
- **RAM**: Expands to ~55GB peak (worst case: 4 workers × 13.8GB each with all 30 simulators preloaded)
  - All 30 simulators with pre-loaded arrays (460MB each) = ~13.8GB per worker
  - Additional overhead: Python objects, elevation data, thread overhead = ~1-2GB per worker
  - Total: 4 workers × 14-15GB = 55-60GB peak (worst case)
  - Typical usage: ~15-20GB (models distributed across workers, not all preloaded simultaneously)
  - Monte Carlo adds trajectory computation overhead (~420KB-2MB for results)
- **CPU**: High (32 ThreadPoolExecutor workers + 32 Gunicorn threads, CPU-bound with pre-loaded arrays)
- **Process**: 
  - Check if files exist on disk → download from S3 via `get_object()` if missing (streaming, 1MB chunks, 64 connection pool)
  - Background prefetching loads all 21 models when ensemble mode starts
  - Create simulators with pre-loaded arrays (ensemble mode) → cache in RAM
  - **Monte Carlo Generation**: Generate 20 parameter perturbations (random variations in launch conditions)
  - Run 21 ensemble paths + 420 Monte Carlo simulations in parallel (441 total)
  - Simulation runs CPU-bound (arrays in RAM, no disk I/O during simulation, 5-10s per simulation)
  - Files cached on disk for subsequent runs (no additional S3 egress)
- **Output**: Returns both `paths` (21 ensemble trajectories) and `heatmap_data` (420 landing positions)
  - **Monte Carlo Perturbations**: ±0.001° lat/lon (≈ ±111m), ±50m altitude, ±200m equilibrium altitude, ±0.5h equilibrium time, ±0.5 m/s ascent/descent rates, floating coefficient 0.9-1.0 (weighted)
  - **Heatmap Visualization**: 420 landing positions aggregated into probability density contours (green → red gradient)

### Subsequent Ensemble Runs (Within 60 Seconds)
- **Speed**: ~5-15 minutes (simulators cached, but still need to run all simulations)
- **RAM**: ~55GB peak (worst case), maintained from first run
- **CPU**: High (32 ThreadPoolExecutor workers + 32 Gunicorn threads, CPU-bound)
- **Why Faster**: All 30 simulators already in RAM cache across workers (pre-loaded arrays, 5-10s per simulation)

### After Ensemble Completes
- **Trim window**: `_periodic_cache_trim()` collapses the cache back to 5 simulators once the 60 s ensemble timer expires (still capped at 5 min total).
- **Idle cleanup**: If no further requests arrive for ~3 min, `_idle_memory_cleanup()` purges remaining simulators, runs multi-pass GC, and calls `malloc_trim(0)` so RSS returns close to cold-start levels.
- **Disk cache**: Wind files stay on disk, so the next ensemble rebuilds simulators from local storage instead of redownloading from S3 via `get_object()`.
- **Maximum Duration**: Even with consecutive ensemble calls, cache will force trim after 5 minutes to prevent memory bloat
