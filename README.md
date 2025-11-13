# HABSIM
High Altitude Balloon Simulator

## Overview

HABSIM is a web-based trajectory prediction system for high-altitude balloons. It uses GEFS (Global Ensemble Forecast System) weather data to simulate balloon flights through three phases: ascent, coast/float, and descent. The system supports:

- **Single Model Simulations**: Fast predictions using one weather model
- **Ensemble Runs**: 21 weather models run in parallel for uncertainty analysis
- **Monte Carlo Analysis**: 420 parameter perturbations for probability density mapping
- **Multi Mode**: Sequential simulations at different launch times

The system is optimized for high-concurrency ensemble workloads with intelligent caching, adaptive memory management, and multi-worker support.

---

## Architecture

### System Components

```
┌─────────────┐
│   Client    │  (www/index.html, paths.js, util.js, style.js)
└──────┬──────┘
       │ HTTP/SSE
       ▼
┌─────────────────────────────────────────────────┐
│           Flask Application (app.py)            │
│  - Request routing & argument parsing           │
│  - Ensemble coordination                        │
│  - Progress tracking (SSE)                      │
│  - Authentication                               │
└──────┬──────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────┐
│      Simulation Orchestrator (simulate.py)     │
│  - Adaptive simulator cache management          │
│  - Prediction caching                           │
│  - Cache trimming & memory management          │
└──────┬──────────────────────────────────────────┘
       │
       ├──► GEFS Manager (gefs.py)
       │    - S3 file downloads & caching
       │    - LRU cache management
       │
       ├──► Wind Data (windfile.py)
       │    - 4D wind interpolation
       │    - Memory-mapped or preloaded access
       │
       ├──► Elevation Data (elev.py)
       │    - Ground elevation lookup
       │
       └──► Physics Engine (habsim/classes.py)
            - Runge-Kutta integration
            - Balloon state tracking
```

### Deployment Architecture

- **Server**: Railway (4 Gunicorn workers × 8 threads = 32 concurrent capacity)
- **Memory**: 32GB RAM, 32 vCPU
- **Storage**: Persistent volume at `/app/data` for file cache
- **Data Source**: AWS S3 (GEFS weather files)

---

## Key Components

### Backend Core Files

#### `app.py` - Flask WSGI Application
**Purpose**: REST API server, request routing, ensemble coordination, progress tracking

**Key Functions**:
- `singlezpb()`: Three-phase simulation (ascent, coast, descent)
- `spaceshot()`: Ensemble + Monte Carlo coordinator
- `_ensure_ensemble_optimizations()`: Logs ensemble run status (adaptive behavior handles optimization)
- `wait_for_prefetch()`: Prefetches first few models to warm cache
- `perturb_*()`: Monte Carlo parameter perturbation helpers
- `extract_landing_position()`: Extracts final (lat, lon) from trajectory
- `update_progress()`: Atomically updates progress tracking with status

**Endpoints**:
- `/sim/singlezpb`: Single model simulation
- `/sim/spaceshot`: Ensemble + Monte Carlo simulation
- `/sim/progress-stream`: Server-Sent Events (SSE) progress stream
- `/sim/elev`: Elevation lookup
- `/sim/models`, `/sim/status`, `/sim/cache-status`: Status endpoints
- `/login`, `/logout`: Authentication

**Progress Tracking**:
- In-memory dictionary (`_progress_tracking`) for fast access
- File-based cache (`/app/data/progress/*.json`) for multi-worker sharing
- Status field: `'downloading'` (prefetching models) or `'simulating'` (running simulations)
- 30-second cleanup delay after completion
- Batched updates to reduce lock contention

#### `simulate.py` - Simulation Orchestrator
**Purpose**: Simulator cache management, trajectory calculation coordination, adaptive memory management

**Key Functions**:
- `simulate()`: Main simulation function (checks cache, gets simulator, runs physics)
- `_get_simulator()`: Gets or creates simulator for a model (with adaptive caching)
- `_should_preload_arrays()`: Auto-detects if arrays should be preloaded (15+ models = ensemble workload)
- `_get_target_cache_size()`: Auto-sizes cache based on workload (10 normal, 30 ensemble)
- `_trim_cache_to_normal()`: Trims cache to target size based on workload
- `_periodic_cache_trim()`: Background thread for cache management
- `_idle_memory_cleanup()`: Deep cleanup when worker is idle (>120s)
- `refresh()`: Checks for GEFS model updates (every 5 minutes)
- `record_activity()`: Updates last activity timestamp for idle detection

**Adaptive Cache Management**:
- `_simulator_cache`: LRU cache of Simulator objects
- Cache size automatically expands from 10 → 30 when 15+ ensemble models are cached
- Preloading automatically enabled for ensemble workloads (15+ models)
- Cache automatically trims when workload decreases
- `_prediction_cache`: LRU cache of trajectory results (200 entries, 1hr TTL)
- `_simulator_ref_counts`: Reference counting prevents cleanup of active simulators
- `_shared_elevation_file`: Shared ElevationFile instance for ensemble workloads

**Thread Safety**:
- Per-file locks prevent zipfile contention
- Reference counting prevents cleanup of active simulators
- Atomic operations for progress updates

#### `windfile.py` - GEFS Wind Data Access
**Purpose**: Loads and interpolates 4D wind data (lat, lon, alt, time)

**Key Functions**:
- `WindFile.__init__()`: Loads NPZ file, extracts to memory-mapped .npy
- `WindFile.get()`: Gets wind vector at arbitrary coordinates
- `WindFile.interpolate()`: 4D linear interpolation

**Access Modes** (auto-detected):
- **Normal workload**: Memory-mapped (`mmap_mode='r'`) - ~150MB per simulator
- **Ensemble workload** (15+ models): Preloaded arrays - ~460MB per simulator (faster, CPU-bound)

#### `gefs.py` - GEFS File Management
**Purpose**: Downloads and caches GEFS weather model files from AWS S3

**Key Functions**:
- `load_gefs()`: Ensures file is cached, returns path
- `_ensure_cached()`: Downloads from S3 if not cached, handles locking/retries
- `open_gefs()`: Opens text files (e.g., `whichgefs` timestamp)
- `_cleanup_old_cache_files()`: LRU eviction when cache exceeds limits

**Caching**:
- Disk cache: `/app/data/gefs` (or `/tmp/habsim-gefs/` fallback)
- Max 30 weather files (~9.2GB) + `worldelev.npy` (451MB, never evicted)
- Per-file locking prevents duplicate downloads across workers
- Connection pooling (64 connections) for high concurrency
- Separate S3 client for status checks (small files, 4 connections)

**Features**:
- Atomic writes (temp file then rename)
- Retry logic with exponential backoff (up to 5 retries)
- Semaphore limits concurrent downloads (4 at a time)
- LRU eviction when cache exceeds limits
- Automatic cleanup of old GEFS cycle files

#### `habsim/classes.py` - Core Physics Classes
**Purpose**: Balloon state, physics engine, trajectory container

**Key Classes**:
- `Balloon`: State container (location, altitude, time, wind_vector, ground_elev)
- `Simulator`: Physics engine using Runge-Kutta 2nd order integration
- `Location`: Geographic coordinates (lat, lon) with haversine distance
- `Trajectory`: Time-series container for path points
- `ElevationFile`: Ground elevation data access (memory-mapped)

**Physics**:
- Runge-Kutta 2nd order (RK2) integration
- Wind interpolation at each time step
- Ground elevation checks during descent
- Horizontal movement from wind + air velocity

#### `elev.py` - Elevation Data Access
**Purpose**: Bilinear interpolation for ground elevation lookup

**Key Functions**:
- `getElevation()`: Returns elevation at (lat, lon) using bilinear interpolation

**Data Source**: `worldelev.npy` (451MB, global elevation grid)

### Frontend Files

#### `www/index.html` - Single-Page Application
- Google Maps integration with custom controls
- Parameter input forms (launch time, location, balloon parameters)
- Ensemble/Multi mode toggles
- Authentication check (redirects to login if not authenticated)
- Responsive design for mobile and desktop

#### `www/paths.js` - Map Rendering and API Client
**Purpose**: Controls fetching and rendering of trajectories

**Key Functions**:
- `simulate()`: Main simulation function (routes to singlezpb or spaceshot)
- `addMultiEndPin()`: Multi mode end marker placement
- `CustomHeatmapOverlay`: Monte Carlo heatmap visualization (kernel density estimation)
- `showWaypoints()`: Displays waypoint markers along trajectory

**Features**:
- Progress tracking via SSE (`/sim/progress-stream`)
- Shows "Downloading..." status before simulations start
- Client-side request ID generation (MD5 hash matching server)
- Ensemble path rendering (21 colored polylines)
- Monte Carlo heatmap (30/50/70/90% probability contours)
- Multi mode sequential simulation

#### `www/util.js` - Map Utilities
**Purpose**: Google Maps initialization and utilities

**Key Functions**:
- Google Maps initialization with custom map types
- Coordinate formatting and display
- Elevation API calls (`/sim/elev`)
- Search bar with Google Places Autocomplete

#### `www/style.js` - Mode Switching
**Purpose**: UI mode management and defaults

**Key Functions**:
- `setMode()`: Standard/ZPB/Float balloon type switching
- Server status polling (`/sim/status`)
- Waypoint toggle functionality

---

## Request Flow

### Single Model Simulation (`/sim/singlezpb`)

1. **Client**: Sends request with launch parameters
2. **Server**: Parses arguments, calls `singlezpb()`
3. **Three Phases**:
   - **Ascent**: From launch altitude to burst altitude
   - **Coast**: Float at burst altitude for specified duration
   - **Descent**: From burst altitude to ground (stops when hits ground)
4. **Core Simulation**: 
   - Checks prediction cache
   - Gets simulator (cached or creates new)
   - Runs physics engine (RK2 integration)
   - Returns trajectory path
5. **Response**: `[rise_path, coast_path, fall_path]`

### Ensemble + Monte Carlo (`/sim/spaceshot`)

1. **Client**: Generates request ID, starts SSE connection, sends request
2. **Server**: 
   - Initializes progress tracking with `status='downloading'`
   - Prefetches first few models (updates status to `'simulating'`)
   - Generates 20 Monte Carlo perturbations
   - Runs 441 simulations in parallel (21 ensemble + 420 Monte Carlo)
   - Updates progress via SSE (batched every 10 completions)
3. **Response**: `{paths: [...], heatmap_data: [...], request_id: "..."}`
4. **Client**: Renders 21 ensemble paths + Monte Carlo heatmap

### Multi Mode

- Sequential calls to `/sim/singlezpb` with different hour offsets
- Each call handled independently
- End markers placed on map with connector line

---

## Key Design Decisions

### Adaptive Caching Strategy

**Three-layer caching**:
1. **Simulator cache (RAM)**: 10 normal, 30 ensemble (auto-expands based on workload)
2. **File cache (disk)**: 30 weather files - eliminates S3 downloads after first use
3. **Prediction cache (RAM)**: 200 entries, 1hr TTL - avoids recomputing identical trajectories

**Adaptive Behavior**:
- Cache automatically expands to 30 simulators when 15+ ensemble models are cached
- Preloading automatically enabled for ensemble workloads (15+ models)
- Cache automatically trims when workload decreases
- No explicit "ensemble mode" - system adapts to workload automatically

**Benefits**:
- First ensemble run: ~5-15 minutes (files download from S3)
- Subsequent runs: ~5-15 minutes (files cached, simulators in RAM)
- Single model runs: ~5-10 seconds (model 0 pre-warmed)

### Memory Management

**Idle cleanup**: Workers idle >120s trigger deep cleanup (drops simulators, keeps elevation mapped)

**Shared elevation**: All simulators in ensemble workloads share same `ElevationFile` instance

**Garbage collection**: Multi-pass GC + `malloc_trim(0)` after cache trims

**Old file cleanup**: Automatically deletes files from previous GEFS cycles (prevents disk bloat)

**Reference counting**: Prevents cleanup of simulators currently in use

### Progress Tracking

**Dual storage**: In-memory (fast) + file-based (shared across workers)

**Status tracking**: Shows `'downloading'` during prefetch, `'simulating'` during actual simulations

**Batched updates**: Updates every 10 completions to reduce lock contention

**SSE streaming**: Real-time progress updates via Server-Sent Events

**30-second cleanup delay**: Allows late-connecting SSE clients to read final progress

---

## Performance Characteristics

### Single Model Run
- **Speed**: ~5-10 seconds
- **Memory**: ~1.5GB per worker
- **Why fast**: Model 0 pre-warmed, files on disk

### Ensemble Run (First Time)
- **Speed**: ~5-15 minutes
  - 21 ensemble paths: ~5-10 seconds each (with preloading)
  - 420 Monte Carlo: ~4-14 minutes total
- **Memory**: ~13.8GB per worker
- **Why slower**: Files download from S3, simulators built in parallel

### Ensemble Run (Subsequent)
- **Speed**: ~5-15 minutes (same computation, but files cached)
- **Memory**: ~13.8GB per worker (simulators cached in RAM)
- **Why faster**: Files on disk, simulators in RAM cache

### After Ensemble Completes
- **Auto-trim**: Cache automatically trims when workload decreases
- **Memory freed**: System adapts to current workload
- **Idle cleanup**: Workers idle >120s trigger deep cleanup

---

## Deployment

### Railway Configuration
- **Gunicorn**: 4 workers, 8 threads each (32 concurrent capacity)
- **Memory**: 32GB RAM, 32 vCPU
- **Start command**: `gunicorn --config gunicorn_config.py app:app`
- **Persistent volume**: Mount at `/app/data` for file cache

### Environment Variables
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: S3 credentials
- `AWS_REGION`: S3 region (default: us-west-1)
- `S3_BUCKET_NAME`: S3 bucket (default: habsim-storage)
- `HABSIM_PASSWORD`: Login password (optional)
- `RAILWAY_ENVIRONMENT`: Detected automatically for Railway-specific initialization
- `PORT`: Server port (default: 8000)

### Persistent Volume Benefits
- Lower S3 egress costs
- Faster warmups (files already on disk)
- Shared cache across workers

---

## Development

### Running Locally
```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-1
export S3_BUCKET_NAME=habsim-storage
export HABSIM_PASSWORD=your_password

# Run development server
python app.py
```

### Testing
- **Single model**: Visit `http://localhost:5000`, disable ensemble, click "Simulate"
- **Ensemble**: Enable ensemble toggle, click "Simulate"
- **Multi mode**: Enable multi toggle, click "Simulate"

---

## License
See LICENSE file for details.
