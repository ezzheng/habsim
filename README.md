# HABSIM
High Altitude Balloon Simulator

## Overview
This is an offshoot of the prediction server developed for the Stanford Space Initiative's Balloons team. It restores core functionality and introduces a simple UI that suits the current needs of the balloons team.

## How It Works

1. **User Interface**: Web UI (`www/`) allows users to set launch parameters and visualize predictions
2. **API Server**: Flask application (`app.py`, deployed on Railway) receives requests and coordinates simulations
3. **Wind Data**: GEFS (Global Ensemble Forecast System) weather files from Supabase are cached locally (`gefs.py`)
4. **Simulation**: Physics engine (`simulate.py`) calculates balloon trajectory using wind data
5. **Results**: JSON trajectory data is returned to browser and rendered on Google Maps

## Files

### Core Application
- **`app.py`** - Flask WSGI application serving REST API + static assets
  - Routes: `/sim/singlezpb`, `/sim/spaceshot`, `/sim/elev`, `/sim/models`, `/sim/progress`, `/sim/cache-status`
  - Startup helpers: `_start_cache_trim_thread()` (ensures background trim thread is running) and `_prewarm_cache()` (builds simulator for model 0, memory-maps `worldelev.npy`)
  - `ThreadPoolExecutor(max_workers=32)` drives ensemble + Monte Carlo runs (441 simulations)
  - Dynamic cache expansion: ensemble requests lift simulator cap to 25; trims back to 5 after the run finishes
  - Idle watchdog: workers that stay idle for 3 minutes trigger a deep cleanup (drops simulators, keeps `worldelev.npy` mapped, runs multi-pass GC + `malloc_trim`)
  - `Cache-Control` headers + `flask-compress` for smaller responses; `/sim/cache-status` surfaces cache/memory state for debugging

### Simulation Engine
- **`simulate.py`** - Simulation orchestrator
  - Multi-simulator LRU cache: 5 simulators in normal mode, 25 during ensemble (per worker)
  - Ensemble window auto-expires after 60s (capped at 5 minutes). When idle >3 minutes, a deep cleanup clears simulators, resets cache limits, and trims RSS via `malloc_trim(0)`
  - Uses memory-mapped wind files by default; ensemble mode temporarily preloads arrays into RAM for speed
  - `_periodic_cache_trim()` runs every ~20s, enforces limits, and calls the idle cleaner when appropriate
  - Prediction cache: 200 entries, 1hr TTL; refreshed whenever GEFS cycle changes
  - Coordinates `WindFile`, `ElevationFile`, and `Simulator`, returning `[timestamp, lat, lon, alt]` paths

- **`windfile.py`** - GEFS data parser with 4D interpolation
  - Loads NumPy-compressed `.npz` files (wind vectors at pressure levels)
  - 4D linear interpolation (4-dimensional): (latitude, longitude, altitude, time) → (u, v) wind components
  - **Normal mode**: Uses `mmap_mode='r'` (memory-mapped read-only mode) for memory-efficient access (~150MB per simulator)
  - **Ensemble mode**: Pre-loads full arrays into RAM (~460MB per simulator) for faster CPU-bound simulation (eliminates disk I/O)

- **`habsim/classes.py`** - Core physics classes
  - `Balloon`: State container (lat, lon, alt, time, ascent_rate, burst_alt)
  - `Simulator`: Numerical integrator using Runge-Kutta 2nd order (RK2 / Midpoint method) with wind advection
  - `ElevationFile`: Wrapper for `worldelev.npy` array with lat/lon → elevation lookup (uses memory-mapping `mmap_mode='r'` to avoid loading 430MB into RAM)
  - `Trajectory`: Time-series container for path points

### Data Pipeline
- **`gefs.py`** - GEFS file downloader with LRU cache and Supabase integration
  - Downloads from Supabase Storage via REST API
  - LRU eviction policy: max 25 weather files (~7.7GB), `worldelev.npy` (451MB) always kept
  - Files cached on disk with access-time tracking
  - Automatic cleanup before new downloads when limit exceeded
  - Robust download with per-file locking, longer timeouts (20 min for large files), progress logging, stall detection
  - Pre-downloads `worldelev.npy` at startup to avoid on-demand download failures

- **`elev.py`** - Elevation data loader
  - Loads preprocessed `worldelev.npy` (global 0.008° resolution array)
  - Fast bilinear interpolation (2D interpolation method) for lat/lon → meters above sea level
  - Results rounded to nearest hundredth (2 decimal places)

### Automation & Scripts (`scripts/`)
- **`scripts/auto_downloader.py`** - Automated GEFS downloader daemon
  - Downloads 21 GEFS models every 6 hours and uploads to Supabase
  - Configurable via `downloader.DOWNLOAD_CONTROL` and `downloader.NUM_PERTURBED_MEMBERS` (currently 21 models: 0-20)
  - Runs via GitHub Actions (scheduled every 6 hours) or as a background daemon
  - Test mode: `python3 scripts/auto_downloader.py --test`
  - Cleans up old model files from Supabase automatically

- **`downloader.py`** - GEFS data pipeline script
  - Fetches GRIB2 files (Gridded Binary format) from NOAA NOMADS, converts to `.npz` format
  - Configuration: `DOWNLOAD_CONTROL=True` (download control model), `NUM_PERTURBED_MEMBERS=20` (21 models total: 0-20)
  - Used by auto-downloader and can be run manually: `python3 downloader.py YYYYMMDDHH`

- **`scripts/save_elevation.py`** - One-time elevation preprocessing utility
  - Converts GMTED2010 GeoTIFF (Geographic Tagged Image File Format) → NumPy array format

### Frontend (`www/`)
- **`index.html`** - Single-page application with embedded CSS/JS
  - CSS Grid layout for mobile (2x3 grid), Flexbox for desktop
  - Google Maps API v3 integration
  - Real-time parameter inputs with validation
  - Ensemble toggle vs. single simulation (model 0)
  - Vercel Speed Insights integration for performance monitoring

- **`paths.js`** - Map rendering and API client
  - Fetches trajectories via `fetch()` (JavaScript HTTP client) from `/sim/singlezpb` or `/sim/spaceshot`
  - Uses `/sim/spaceshot` endpoint for ensemble runs (parallel execution + Monte Carlo)
  - Falls back to sequential `/sim/singlezpb` calls for single model or FLOAT mode
  - Fetches model configuration from `/sim/models` endpoint on page load
  - Dynamically uses server-configured model IDs for ensemble runs
  - Draws `google.maps.Polyline` objects with color-coded paths (21 ensemble paths)
  - **Monte Carlo Heatmap**: Custom canvas overlay showing probability density from 420 landing positions
    - Custom kernel density estimation with configurable smoothing (`'epanechnikov'` default, shape-preserving)
    - Color gradient: Cyan (low) → Green → Yellow → Orange → Red (high density)
    - Clears automatically when paths are cleared (map click or new simulation)
  - Waypoint circles with click handlers showing altitude/time info windows
  - Debounced elevation fetching (150ms) to prevent rapid-fire requests

- **`style.js`** - Mode switching logic (Standard/ZPB/Float balloon types)
  - Server status polling (every 5 seconds) for live updates
  - Fetches model configuration on page load

- **`util.js`** - Map utilities and coordinate helpers
  - Google Maps initialization, lat/lon formatting, elevation API calls

### Deployment Configuration
- **`gunicorn_config.py`** - Production WSGI server config for Gunicorn (Python WSGI HTTP server)
  - Optimized for Railway (32GB RAM, 32 vCPU)
  - `workers=4`, `threads=8` (32 concurrent capacity via `gthread` worker class)
  - `preload_app=True` for shared code between workers (reduces memory duplication)
  - **Strategy**: Fewer workers + more threads = same CPU capacity with less RAM (threads share memory)
  - `max_requests=1000` for automatic worker recycling to prevent memory leaks
  - `timeout=900` (15 minutes) for long-running simulations (ensemble ~30-60s, Monte Carlo ~5-15min)

- **`Procfile`** - Railway deployment configuration
  - Specifies start command: `gunicorn app:app -c gunicorn_config.py`

- **`requirements.txt`** - Python package dependencies
  - `flask==3.0.2`, `flask-cors==4.0.0`, `flask-compress==1.15` (Gzip compression)
  - `numpy==1.26.4` (numerical computing), `requests==2.32.3` (HTTP library), `gunicorn==22.0.0`

- **`vercel.json`** - Vercel deployment configuration
  - Routes `/sim/*` → Python build (`app.py`), `/*` → static files (`www/`)
  - Enables serverless deployment on Vercel platform (frontend only)

### Documentation
- **`OPTIMIZATIONS.md`** - Performance tuning reference
  - Caching strategies (dynamic simulator cache, file cache, prediction cache)
  - Memory management and auto-trimming behavior
  - Performance profiles for single vs ensemble runs
  - Railway configuration details

## Data Storage

### Supabase Storage (Cloud)
- **Location**: Supabase Storage bucket (`habsim`)
- **Files**: 21 model `.npz` files per forecast cycle + `whichgefs` timestamp file
- **Purpose**: Long-term storage, source of truth for weather datasets
- **Access pattern**: First request per worker per model hits Supabase; subsequent accesses come from CDN cache or local disk
- **Cached egress**: Supabase reports CDN-served bytes as "cached egress". Large values usually mean many workers warmed the same files (or downloads resumed after stalls), not repeated origin downloads

### Railway/Render Instance (Local Disk Cache)
- **Location**: `/app/data/gefs` on Railway (ephemeral storage)
- **Files**: Up to 25 `.npz` files (~7.7GB) cached on disk
- **Purpose**: Fast local access, eliminates download delays after first download
- **Eviction**: LRU when cache exceeds 25 files (`worldelev.npy` is exempt)
- **Download Strategy**: Files download on-demand with per-file locking, extended timeouts, and stall detection
- **Model Change Cleanup**: Automatically deletes old model files when GEFS updates every 6 hours
- **Idle effect**: Idle worker cleanup does not delete disk cache; simulators are rebuilt from these on next request
- **Note**: Railway persistent volumes are currently in private beta. Without volume access, files are cached in ephemeral storage (lost on restart but reduce egress during active sessions).

## Architecture Changes From Prev. HABSIM

**Old Version (Client Library):** Python package making HTTP requests to `habsim.org` API. Installed via pip, called functions like `util.predict()`.

**Current Version (Self-Contained Server):** Self-hosted web application (built with Flask framework, deployed on Railway hosting platform) hosting the UI, REST API endpoints, and running simulations locally with GEFS data from Supabase.

**Benefits:** Independence from external services, non-technical users visit URL directly, full control over performance/caching.

**Downsides:**
- **Resource Costs:** Pay for compute/storage (Railway instance vs. shared infrastructure)
- **Scaling Responsibility:** Multiple concurrent users require careful memory/worker configuration; no automatic horizontal scaling
- **Maintenance Burden:** Responsible for uptime, deployments, bug fixes, infrastructure monitoring
- **No Programmatic API:** Old version allowed `from habsim import util; util.predict(...)` - current requires manual HTTP requests or web UI
- **Single Point of Failure:** If Railway instance fails, all users lose access (vs. centralized server with redundancy)

**Note:** The `habsim/` folder is both the Python package (`classes.py`) AND a virtual environment (`lib/`, `bin/`). Legacy client code moved to `deprecated/`.
