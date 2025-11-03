# HABSIM
High Altitude Balloon Simulator

## Overview
This is an offshoot of the prediction server developed for the Stanford Space Initiative's Balloons team. It restores core functionality and introduces a simple UI that suits the current needs of the balloons team.

### Architecture Changes
**Old Version (Client Library):** Python package making HTTP requests to `habsim.org` API. Installed via pip, called functions like `util.predict()`.

**Current Version (Self-Contained Server):** Self-hosted web application (built with Flask framework, deployed on Render hosting platform) hosting the UI, REST API endpoints, and running simulations locally with GEFS data from Supabase.

**Benefits:** Independence from external services, non-technical users visit URL directly, full control over performance/caching.

**Downsides:**
- **Resource Costs:** Pay for compute/storage (Render instance vs. shared infrastructure)
- **Scaling Responsibility:** Multiple concurrent users require careful memory/worker configuration; no automatic horizontal scaling
- **Maintenance Burden:** Responsible for uptime, deployments, bug fixes, infrastructure monitoring
- **No Programmatic API:** Old version allowed `from habsim import util; util.predict(...)` - current requires manual HTTP requests or web UI
- **Single Point of Failure:** If Render instance fails, all users lose access (vs. centralized server with redundancy)

**Note:** The `habsim/` folder is both the Python package (`classes.py`) AND a virtual environment (`lib/`, `bin/`). Legacy client code moved to `deprecated/`. 

## How It Works

1. **User Interface**: Web UI (`www/`) allows users to set launch parameters and visualize predictions
2. **API Server**: Flask application (`app.py`, deployed on Render) receives requests and coordinates simulations
3. **Wind Data**: GEFS (Global Ensemble Forecast System) weather files from Supabase are cached locally (`gefs.py`)
4. **Simulation**: Physics engine (`simulate.py`) calculates balloon trajectory using wind data
5. **Results**: JSON (JavaScript Object Notation) trajectory data is returned to browser and rendered on Google Maps

## Files

### Core Application
- **`app.py`** - Flask (Python web framework) WSGI (Web Server Gateway Interface) application serving REST API (Representational State Transfer API) and static files
  - Routes: `/sim/singlezpb` (ZPB prediction), `/sim/spaceshot` (ensemble), `/sim/elev` (elevation)
  - Background thread pre-warms cache on startup
  - `ThreadPoolExecutor` (concurrent execution) parallelizes ensemble requests (max_workers=2)
  - HTTP caching headers (`Cache-Control`) + Flask-Compress Gzip compression

### Simulation Engine
- **`simulate.py`** - Main simulation orchestrator
  - LRU cache (Least Recently Used eviction policy) for predictions (30 entries, 1hr TTL - Time To Live)
  - Coordinates `WindFile`, `ElevationFile`, and `Simulator` classes
  - Handles ascent/coast/descent phases with configurable rates
  - Returns trajectory as `[[timestamp, lat, lon, alt], ...]` arrays

- **`windfile.py`** - GEFS data parser with 4D interpolation
  - Loads NumPy-compressed `.npz` files (wind vectors at pressure levels)
  - 4D linear interpolation (4-dimensional): (latitude, longitude, altitude, time) → (u, v) wind components
  - Uses `mmap_mode='r'` (memory-mapped read-only mode) for memory-efficient access to large datasets (~150MB per file)

- **`habsim/classes.py`** - Core physics classes
  - `Balloon`: State container (lat, lon, alt, time, ascent_rate, burst_alt)
  - `Simulator`: Numerical integrator using Euler method (numerical approximation) with wind advection
  - `ElevationFile`: Wrapper for `worldelev.npy` array with lat/lon → elevation lookup
  - `Trajectory`: Time-series container for path points

### Data Pipeline
- **`gefs.py`** - GEFS file downloader with LRU cache
  - Downloads from Supabase Storage via REST API
  - LRU eviction policy (Least Recently Used): max 3 files (~450MB) to respect 2GB RAM (Random Access Memory) limit
  - Files cached in `/tmp` (temporary directory) with access-time tracking
  - Automatic cleanup before new downloads when limit exceeded

- **`elev.py`** - Elevation data loader
  - Loads preprocessed `worldelev.npy` (global 0.008° resolution array)
  - Fast bilinear interpolation (2D interpolation method) for lat/lon → meters above sea level

- **`downloader.py`** - GEFS data pipeline script (offline use)
  - Fetches GRIB2 files (Gridded Binary format) from NOAA NOMADS, converts to `.npz` format
  - Not used in production (Supabase pre-populated)

- **`save_elevation.py`** - One-time elevation preprocessing utility
  - Converts GMTED2010 GeoTIFF (Geographic Tagged Image File Format) → NumPy array format

### Frontend (`www/`)
- **`index.html`** - Single-page application with embedded CSS (Cascading Style Sheets)/JS (JavaScript)
  - CSS Grid layout (CSS layout system) for mobile (2x3 grid), Flexbox (CSS flexbox layout) for desktop
  - Google Maps API v3 (Application Programming Interface version 3) integration
  - Real-time parameter inputs with validation
  - Ensemble toggle (models 0-2) vs. single simulation (model 0)

- **`paths.js`** - Map rendering and API client
  - Fetches trajectories via `fetch()` (JavaScript HTTP client) from `/sim/singlezpb` or `/sim/spaceshot`
  - Draws `google.maps.Polyline` objects (Google Maps line drawing) with color-coded paths
  - Waypoint circles with click handlers showing altitude/time info windows

- **`style.js`** - Mode switching logic (Standard/ZPB/Float balloon types)

- **`util.js`** - Map utilities and coordinate helpers
  - Google Maps initialization, lat/lon formatting, elevation API calls

### Deployment Configuration
- **`gunicorn_config.py`** - Production WSGI server config for Gunicorn (Python WSGI HTTP server)
  - Optimized for Render free tier (2GB RAM, 1 CPU - Central Processing Unit)
  - `workers=2`, `threads=2` (4 concurrent requests via `gthread` worker class - threaded worker)
  - `preload_app=True` for shared memory between workers (critical optimization)
  - `max_requests=800` for automatic worker recycling to prevent memory leaks

- **`requirements.txt`** - Python package dependencies
  - `flask==3.0.2`, `flask-cors==4.0.0`, `flask-compress==1.15` (Gzip compression)
  - `numpy==1.26.4` (numerical computing), `requests==2.32.3` (HTTP library), `gunicorn==22.0.0`

- **`vercel.json`** - Vercel deployment configuration
  - Routes `/sim/*` → Python build (`app.py`), `/*` → static files (`www/`)
  - Enables serverless deployment on Vercel platform (alternative to Render)

### Documentation
- **`OPTIMIZATIONS.md`** - Performance tuning reference
  - Caching strategies, memory budget breakdown, Gunicorn tuning
  - Troubleshooting for `/tmp` storage limits, worker crashes

### Data Storage
- **`data/gefs/`** - GEFS file cache directory
  - `whichgefs`: Current model timestamp (YYYYMMDDHH format)
  - `YYYYMMDDHH_NN.npz`: Wind data arrays (NN = 00 control + 01-20 ensemble members)
  - Files downloaded on-demand from Supabase, cached with LRU eviction

- **`data/worldelev.npy`** - Global elevation dataset
  - NumPy array format, loaded once on import
  - ~250MB file, accessed via memory mapping (direct file-to-memory mapping) in production

### Virtual Environment
- **`habsim/`** - Dual-purpose directory: Python package + virtual environment
  - Package: `classes.py`, `__init__.py` (exported via `from habsim import ...`)
  - Virtualenv: `bin/activate`, `lib/python3.13/site-packages/` (installed dependencies)
  - Activate: `source habsim/bin/activate`
