# HABSIM
High Altitude Balloon Simulator

## Overview
This is an offshoot of the prediction server developed for the Stanford Space Initiative's Balloons team. It restores core functionality and introduces a simple UI that suits the current needs of the balloons team.

## How It Works

1. **User Interface**: Web UI (`www/`) allows users to set launch parameters and visualize predictions
2. **API Server**: Flask application (`app.py`, deployed on Railway) receives requests and coordinates simulations
3. **Wind Data**: GEFS (Global Ensemble Forecast System) weather files from AWS S3 are cached locally (`gefs.py`)
4. **Simulation**: Physics engine (`simulate.py`) calculates balloon trajectory using wind data
5. **Results**: JSON trajectory data is returned to browser and rendered on Google Maps

## Files

### Core Application
- **`app.py`** - Flask WSGI application serving REST API + static assets
  - Routes: `/sim/singlezpb`, `/sim/spaceshot`, `/sim/elev`, `/sim/models`, `/sim/progress`, `/sim/cache-status`
  - Startup helpers: `_start_cache_trim_thread()` (ensures background trim thread is running) and `_prewarm_cache()` (builds simulator for model 0, memory-maps `worldelev.npy`)
  - `ThreadPoolExecutor(max_workers=32)` drives ensemble + Monte Carlo runs (441 simulations)
  - Dynamic cache expansion: ensemble requests lift simulator cap to 30; trims back to 10 after the run finishes
  - Idle watchdog: workers that stay idle for 2 minutes trigger a deep cleanup (drops simulators, keeps `worldelev.npy` mapped, runs multi-pass GC + `malloc_trim`)
  - `Cache-Control` headers + `flask-compress` for smaller responses; `/sim/cache-status` surfaces cache/memory state for debugging

### Simulation Engine
- **`simulate.py`** - Simulation orchestrator
  - Multi-simulator LRU cache: 10 simulators in normal mode, 30 during ensemble (per worker, optimized for 32GB RAM)
  - Ensemble window auto-expires after 60s (capped at 5 minutes). When idle >2 minutes, a deep cleanup clears simulators, resets cache limits, and trims RSS via `malloc_trim(0)`
  - **Normal mode**: Uses memory-mapped wind files for memory efficiency (~150MB per simulator)
  - **Ensemble mode**: Preloads arrays into RAM for CPU-bound performance (~460MB per simulator, 5-10s simulations vs 50-80s with memory-mapping)
  - Model prefetching: Background thread prefetches all 21 models when ensemble mode starts
  - `_periodic_cache_trim()` runs every ~20s, enforces limits, and calls the idle cleaner when appropriate
  - Delayed cleanup queue: 2-second delay after eviction to prevent race conditions
  - Prediction cache: 200 entries, 1hr TTL; refreshed whenever GEFS cycle changes
  - Defensive checks: Validates simulator wind_file is not None to prevent race condition crashes
  - Coordinates `WindFile`, `ElevationFile`, and `Simulator`, returning `[timestamp, lat, lon, alt]` paths

- **`windfile.py`** - GEFS data parser with 4D interpolation
  - Loads NumPy-compressed `.npz` files (wind vectors at pressure levels)
  - 4D linear interpolation (4-dimensional): (latitude, longitude, altitude, time) → (u, v) wind components
  - **Normal mode**: Uses `mmap_mode='r'` (memory-mapped read-only mode) for memory-efficient access (~150MB per simulator)
  - **Ensemble mode**: Pre-loads full arrays into RAM (~460MB per simulator) for faster CPU-bound simulation (eliminates disk I/O, 5-10s vs 50-80s)

- **`habsim/classes.py`** - Core physics classes
  - `Balloon`: State container (lat, lon, alt, time, ascent_rate, burst_alt)
  - `Simulator`: Numerical integrator using Runge-Kutta 2nd order (RK2 / Midpoint method) with wind advection
  - `ElevationFile`: Wrapper for `worldelev.npy` array with lat/lon → elevation lookup (uses memory-mapping `mmap_mode='r'` to avoid loading 430MB into RAM)
  - `Trajectory`: Time-series container for path points

### Data Pipeline
- **`gefs.py`** - GEFS file downloader with LRU cache and AWS S3 integration
  - **Storage Backend**: AWS S3 via boto3 SDK (`boto3.client('s3')`)
  - **Authentication**: IAM credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) with region (`AWS_REGION`)
  - **Bucket**: Configurable via `S3_BUCKET_NAME` env var (default: `habsim-storage`)
  - **Client Configuration**: Dual S3 clients - main client (64 connections, 3 retries) for large files, status client (4 connections, 2 retries) for `whichgefs` checks
  - **Operations**: `get_object()` for downloads (streaming), `upload_file()` for uploads (auto-multipart), `delete_object()` for cleanup, `list_objects_v2()` for listing
  - **Error Handling**: `ClientError` exceptions with `NoSuchKey` detection for missing files
  - **LRU Eviction**: Max 30 weather files (~9.2GB), `worldelev.npy` (451MB) always kept
  - **Caching**: Files cached on disk with access-time tracking, automatic cleanup before new downloads
  - **Download Features**: Per-file locking (fcntl), extended timeouts (30 min for large files), progress logging, stall detection (120s), NPZ validation
  - **Pre-warming**: Pre-downloads `worldelev.npy` at startup to avoid on-demand failures

- **`elev.py`** - Elevation data loader
  - Loads preprocessed `worldelev.npy` (global ~0.017° resolution array, ~60 arc-seconds)
  - Fast bilinear interpolation (2D interpolation method) for lat/lon → meters above sea level
  - Results rounded to nearest hundredth (2 decimal places)

### Automation & Scripts (`scripts/`)
- **`scripts/auto_downloader.py`** - Automated GEFS downloader daemon
  - Downloads 21 GEFS models every 6 hours and uploads to AWS S3
  - Configurable via `downloader.DOWNLOAD_CONTROL` and `downloader.NUM_PERTURBED_MEMBERS` (currently 21 models: 0-20)
  - Runs via GitHub Actions (scheduled every 6 hours) or as a background daemon
  - Test mode: `python3 scripts/auto_downloader.py --test`
  - Cleans up old model files from S3 automatically

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
    - Color gradient: Green (low) → Yellow → Orange → Red (high density, inner)
    - Probability contours at cumulative mass 30/50/70/90% (higher % encloses larger area)
    - Waypoints/labels remain hoverable (heatmap below interactive pane; contour polygons non-clickable)
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
  - `boto3==1.35.0` (AWS S3 SDK for cloud storage operations)

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

### AWS S3 Storage (Cloud)
- **Location**: AWS S3 bucket (`habsim-storage`, configurable via `S3_BUCKET_NAME`)
- **Region**: `us-west-1` (configurable via `AWS_REGION`)
- **Files**: 21 model `.npz` files per forecast cycle + `whichgefs` timestamp file
- **Purpose**: Long-term storage, source of truth for weather datasets
- **SDK**: boto3 (`boto3.client('s3')`) with adaptive retries and connection pooling
- **Authentication**: IAM credentials via environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- **Access Pattern**: First request per worker per model hits S3 via `get_object()`; subsequent accesses use local disk cache
- **Upload**: GitHub Actions auto-downloader uses `upload_file()` (auto-multipart for large files) every 6 hours
- **Cleanup**: Old model files deleted via `delete_object()` when new cycle uploads complete

### Railway Instance (Local Disk Cache)
- **Location**: `/app/data/gefs` on Railway (persistent volume if mounted, otherwise ephemeral storage)
- **Files**: Up to 30 `.npz` files (~9.2GB) cached on disk (increased for 32GB RAM system)
- **Purpose**: Fast local access, eliminates download delays after first download
- **Eviction**: LRU when cache exceeds 30 files or 25GB total size (`worldelev.npy` is always exempt)
- **Download Strategy**: Files download on-demand with per-file locking, extended timeouts, and stall detection
- **Model Change Cleanup**: Automatically deletes old model files when GEFS updates every 6 hours
- **Idle effect**: Idle worker cleanup does not delete disk cache; simulators are rebuilt from these on next request
- **Persistent Volume**: When mounted to `/app/data`, files persist across restarts and are shared across all workers. 
Benefits:
  - Lower S3 egress (one shared download per forecast cycle)
  - Faster warmups after deploys/restarts
  - Consistent performance across workers
  - To enable: mount Railway persistent volume at `/app/data`

## Architecture Changes From Prev. HABSIM

**Old Version (Client Library):** Python package making HTTP requests to `habsim.org` API. Installed via pip, called functions like `util.predict()`.

**Current Version (Self-Contained Server):** Self-hosted web application (built with Flask framework, deployed on Railway hosting platform) hosting the UI, REST API endpoints, and running simulations locally with GEFS data from AWS S3.

**Benefits:** Independence from external services, non-technical users visit URL directly, full control over performance/caching.

**Downsides:**
- **Resource Costs:** Pay for compute/storage (Railway instance vs. shared infrastructure)
- **Scaling Responsibility:** Multiple concurrent users require careful memory/worker configuration; no automatic horizontal scaling
- **Maintenance Burden:** Responsible for uptime, deployments, bug fixes, infrastructure monitoring
- **No Programmatic API:** Old version allowed `from habsim import util; util.predict(...)` - current requires manual HTTP requests or web UI
- **Single Point of Failure:** If Railway instance fails, all users lose access (vs. centralized server with redundancy)

**Note:** The `habsim/` folder is both the Python package (`classes.py`) AND a virtual environment (`lib/`, `bin/`). Legacy client code moved to `deprecated/`.
