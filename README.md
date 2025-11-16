# HABSIM

High Altitude Balloon Trajectory Simulator. A production Flask service for predicting high-altitude balloon trajectories using NOAA GEFS ensemble weather data and GMTED2010 elevation data.

## Project Overview

HABSIM simulates three-phase balloon trajectories (ascent, coast, descent) using wind fields from the Global Ensemble Forecast System (GEFS). The system supports single-model predictions and 21-member ensemble analysis with Monte Carlo parameter perturbations for uncertainty quantification.

The service addresses the need for accurate trajectory prediction in mission planning, where forecast uncertainty must be quantified across multiple weather model realizations. Simulations integrate wind vectors using Runge-Kutta methods and account for ground elevation to determine landing positions.

## Architecture

The system consists of a Flask application served by Gunicorn with multiple worker processes. The application exposes REST endpoints for simulation requests and a Server-Sent Events (SSE) stream for real-time progress updates during long-running ensemble computations.

### Component Interaction

**Request Flow:**
1. Client submits simulation request via REST API (`/sim/singlezpb` or `/sim/spaceshot`)
2. Flask validates parameters and checks for duplicate in-progress requests
3. For ensemble requests, the system performs progressive prefetch: downloads first 12 GEFS model files, then continues remaining downloads in background
4. Simulation orchestrator loads or creates simulator instances (wind data + elevation data)
5. Physics engine executes trajectory calculation using Runge-Kutta integration
6. Results are cached and returned to client
7. SSE stream provides progress updates during ensemble execution

**Data Storage:**
- Cloudflare R2: Authoritative storage for GEFS model files (21 files per cycle, ~308MB each) and elevation data (451MB)
- Persistent volume (`/app/data`): Disk cache for GEFS files with LRU eviction (max 30 files)
- In-memory caches: Simulator cache (adaptive sizing: 10 normal, 30 ensemble), prediction cache (200 entries, 1hr TTL)

**Concurrency Model:**
- Gunicorn: 4 workers × 8 threads = 32 concurrent request capacity
- File-based locking (fcntl) coordinates inter-process operations (GEFS cycle refresh, download coordination)
- Thread-safe in-memory caches with reference counting to prevent eviction of active simulators
- Maximum 3 concurrent ensemble requests enforced via file-based counter

### GEFS Cycle Management

The system enforces strict cycle consistency to prevent mixing forecasts from different GEFS cycles. The refresh mechanism:

1. Reads `whichgefs` from R2 to detect new cycle timestamps
2. Verifies all 21 model files exist and are readable before switching cycles
3. Sets cache invalidation flag, waits 3 seconds for consistency, then updates shared state file
4. Evicts idle simulators and clears prediction caches
5. Asynchronously deletes old cycle files from disk cache

Cycle stability is enforced through multiple validation checkpoints: three consecutive stable readings required before proceeding, atomic reference count acquisition with cycle validation, and per-simulator cycle checks during construction.

## File and Directory Structure

```
habsim/
├── app.py                 # Flask application, REST endpoints, SSE progress stream
├── simulate.py            # Simulation orchestrator, cache management, GEFS cycle refresh
├── gefs.py                # R2/S3 access, disk cache, download coordination
├── windfile.py            # Wind data access, 4D interpolation, filter cache
├── elev.py                # Elevation data access, bilinear interpolation
├── downloader.py          # GRIB2 to NumPy conversion utilities
├── gunicorn_config.py     # Production server configuration
├── requirements.txt       # Python dependencies
├── habsim/
│   └── classes.py         # Physics engine: Balloon, Simulator, Location, ElevationFile
├── scripts/
│   └── auto_downloader.py # Automated GEFS downloader for GitHub Actions
└── www/                   # Static frontend assets
    ├── index.html         # Main web interface
    ├── paths.js           # Trajectory visualization, SSE client
    └── util.js            # Frontend utilities
```

**Key Modules:**

- `app.py`: Handles HTTP requests, parameter validation, ensemble coordination, idempotent request deduplication, progressive prefetch orchestration
- `simulate.py`: Manages simulator cache with adaptive sizing, prediction caching, GEFS cycle refresh logic, reference counting for safe eviction
- `gefs.py`: Provides S3-compatible storage access (Cloudflare R2 or AWS S3), implements disk-based LRU cache, coordinates concurrent downloads via file locking
- `windfile.py`: Extracts NPZ files, performs 4D wind interpolation (lat/lon/alt/time), maintains filter cache for interpolation weights
- `habsim/classes.py`: Implements Runge-Kutta integration, balloon state management, wind vector interpolation

## Core Functionality

### Simulation Execution

Single-model simulations (`/sim/singlezpb`):
- Validates parameters, loads simulator from cache or creates new instance
- Executes three-phase trajectory: ascent (buoyancy-driven), coast (equilibrium float), descent (parachute)
- Returns array of trajectory paths: `[[ascent_path], [coast_path], [descent_path]]`
- Typical execution time: 5-10 seconds (cache hit) or 30-120 seconds (cold start)

Ensemble simulations (`/sim/spaceshot`):
- Checks for duplicate in-progress requests (idempotent deduplication)
- Performs progressive prefetch: blocks until first 12 models ready, continues remaining in background
- Executes 21 ensemble models in parallel using ThreadPoolExecutor
- Applies Monte Carlo perturbations (default: 20 per model = 420 additional trajectories)
- Streams progress updates via SSE
- Typical execution time: 5-15 minutes (first run), faster on subsequent runs with cached files

### Caching Strategy

**Prediction Cache:** MD5 hash of simulation parameters → cached result. TTL: 1 hour. Max size: 200 entries.

**Simulator Cache:** Model ID → Simulator instance. Adaptive sizing: 10 simulators in normal mode (~1.5GB), expands to 30 in ensemble mode (~13.8GB). LRU eviction of unused simulators. Reference counting prevents eviction of active simulators.

**GEFS File Cache:** Disk-based LRU cache at `/app/data/gefs/`. Max 30 files (~9.2GB). Files validated before use (NPZ structure check). Elevation file (`worldelev.npy`) never evicted.

**Filter Cache:** Per-WindFile cache of interpolation weight arrays. Reduces allocations by ~90%. Normal mode: 1000 entries, ensemble mode: 2000 entries shared across all WindFiles.

### GEFS Cycle Refresh

The refresh mechanism (`simulate.refresh()`) ensures cycle consistency:

1. Acquires inter-process lock to prevent concurrent refreshes
2. Reads `whichgefs` from R2 and compares to current `currgefs`
3. If new cycle detected, verifies all 21 model files exist (with retry logic for S3 eventual consistency)
4. Sets `_cache_invalidation_cycle` flag, waits 3 seconds, then atomically updates shared state file
5. Calls `reset()` to evict idle simulators and clear prediction caches
6. Schedules asynchronous cleanup of old cycle files

Refresh is triggered by: ensemble prefetch operations, manual `/sim/refresh` endpoint, or simulator cache misses when cycle appears stale.

### Download Coordination

Large file downloads (GEFS model files, ~308MB each) use file-based locking to prevent duplicate downloads across Gunicorn workers:

- Worker 1 acquires lock → downloads file
- Workers 2-N wait on lock → use completed file

Download semaphore limits concurrent downloads to 16 to prevent connection pool exhaustion. TransferManager handles multipart parallel downloads (16 threads per file) for improved throughput and resilience.

## Deployment and Services Used

**Railway:** Production hosting platform. Deploys Flask application with Gunicorn (4 workers, 8 threads per worker). Persistent volume mounted at `/app/data` for disk cache persistence. 32GB RAM allocation supports ensemble workloads. Health checks via `/health` endpoint.

**Cloudflare R2:** Object storage for GEFS model files and elevation data. S3-compatible API. Zero egress fees. Bucket name: `habsim`. Endpoint configured via `S3_ENDPOINT_URL` environment variable. Auto-downloader uploads new cycles every 6 hours via GitHub Actions.

**GitHub Actions:** Scheduled workflow (`.github/workflows/gefs-downloader.yml`) runs every 6 hours to download GEFS data from NOAA NOMADS, convert GRIB2 to NumPy format, and upload to R2. Uses same R2 credentials as main application.

**Environment Variables:**
- `S3_ENDPOINT_URL`: Cloudflare R2 endpoint URL
- `S3_BUCKET_NAME`: R2 bucket name (`habsim`)
- `AWS_ACCESS_KEY_ID`: R2 access key ID
- `AWS_SECRET_ACCESS_KEY`: R2 secret access key
- `SECRET_KEY`: Flask session secret (required for production)
- `HABSIM_PASSWORD`: Optional frontend authentication password
- `PORT`: Server port (default: 8000)

## Requirements

- Python 3.13+
- Dependencies: See `requirements.txt`
- Storage credentials: Cloudflare R2 API token with Object Read & Write permissions

## Local Development

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
export S3_BUCKET_NAME=habsim
export AWS_ACCESS_KEY_ID=<r2-access-key>
export AWS_SECRET_ACCESS_KEY=<r2-secret-key>
export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')
python app.py
```

For production-like testing:
```bash
gunicorn --config gunicorn_config.py app:app
```

## License

MIT License. See `LICENSE` file for details.
