# HABSIM
High Altitude Balloon Simulator

## Overview

HABSIM is a web-based trajectory prediction system for high-altitude balloons. It uses GEFS (Global Ensemble Forecast System) weather data to simulate balloon flights through three phases: ascent, coast/float, and descent. The system supports:

- **Single Model Simulations**: Fast predictions using one weather model
- **Ensemble Runs**: 21 weather models run in parallel for uncertainty analysis
- **Monte Carlo Analysis**: 420 parameter perturbations for probability density mapping
- **Multi Mode**: Sequential simulations at different launch times

The system is optimized for high-concurrency ensemble workloads with intelligent caching, memory management, and multi-worker support.

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
│  - Simulator cache management                   │
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

## File Structure and Responsibilities

### Backend Core Files

#### `app.py` - Flask WSGI Application
**Purpose**: REST API server, request routing, ensemble coordination, progress tracking

**Key Functions**:
- `singlezpb()`: Three-phase simulation (ascent, coast, descent)
- `spaceshot()`: Ensemble + Monte Carlo coordinator
- `activate_ensemble_mode()`: Expands cache, starts prefetch
- `wait_for_prefetch()`: Waits for models to load
- `perturb_*()`: Monte Carlo parameter perturbation helpers
- `extract_landing_position()`: Extracts final (lat, lon) from trajectory
- `update_progress()`: Atomically updates progress tracking (in-memory + file-based)
- `generate_request_id()`: MD5 hash for request tracking

**Endpoints**:
- `/sim/singlezpb`: Single model simulation
- `/sim/spaceshot`: Ensemble + Monte Carlo simulation
- `/sim/progress`: Polling progress endpoint
- `/sim/progress-stream`: Server-Sent Events (SSE) progress stream
- `/sim/elev`: Elevation lookup
- `/sim/wind`, `/sim/windensemble`: Wind data lookup
- `/sim/models`, `/sim/status`, `/sim/cache-status`: Status endpoints
- `/login`, `/logout`: Authentication

**Progress Tracking**:
- In-memory dictionary (`_progress_tracking`) for fast access
- File-based cache (`/app/data/progress/*.json`) for multi-worker sharing
- 30-second cleanup delay after completion
- Batched updates to reduce lock contention

#### `simulate.py` - Simulation Orchestrator
**Purpose**: Simulator cache management, trajectory calculation coordination, memory management

**Key Functions**:
- `simulate()`: Main simulation function (checks cache, gets simulator, runs physics)
- `_get_simulator()`: Gets or creates simulator for a model (with caching)
- `set_ensemble_mode()`: Activates ensemble mode (expands cache, starts prefetch)
- `_trim_cache_to_normal()`: Trims cache back to normal size
- `_periodic_cache_trim()`: Background thread for cache management
- `_idle_memory_cleanup()`: Deep cleanup when worker is idle (>120s)
- `refresh()`: Checks for GEFS model updates (every 5 minutes)
- `record_activity()`: Updates last activity timestamp for idle detection

**Cache Management**:
- `_simulator_cache`: LRU cache of Simulator objects (10 normal, 30 ensemble)
- `_prediction_cache`: LRU cache of trajectory results (200 entries, 1hr TTL)
- `_in_use_models`: Set of models currently in use (prevents cleanup races)
- `_cleanup_queue`: Delayed cleanup queue (2-second delay after eviction)
- `_shared_elevation_file`: Shared ElevationFile instance for ensemble mode

**Thread Safety**:
- Per-file locks prevent zipfile contention
- In-use tracking prevents cleanup of active simulators
- Delayed cleanup prevents race conditions

#### `windfile.py` - GEFS Wind Data Access
**Purpose**: Loads and interpolates 4D wind data (lat, lon, alt, time)

**Key Functions**:
- `WindFile.__init__()`: Loads NPZ file, extracts to memory-mapped .npy
- `WindFile.get()`: Gets wind vector at arbitrary coordinates
- `WindFile.get_indices()`: Converts physical coordinates to array indices
- `WindFile.interpolate()`: 4D linear interpolation
- `_load_memmap_data()`: Extracts NPZ to memory-mapped .npy (one-time cost)

**Access Modes**:
- **Normal mode**: Memory-mapped (`mmap_mode='r'`) - ~150MB per simulator
- **Ensemble mode**: Preloaded arrays - ~460MB per simulator (faster, CPU-bound)

**Thread Safety**:
- Per-file locks prevent concurrent zipfile reads
- Shared filter cache for ensemble mode

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
- Retry logic with exponential backoff
- Semaphore limits concurrent downloads
- LRU eviction when cache exceeds limits

#### `habsim/classes.py` - Core Physics Classes
**Purpose**: Balloon state, physics engine, trajectory container

**Key Classes**:
- `Balloon`: State container (location, altitude, time, wind_vector, ground_elev)
- `Simulator`: Physics engine using Runge-Kutta 2nd order integration
  - `Simulator.step()`: Single time step (RK2 integration)
  - `Simulator.simulate()`: Full trajectory simulation
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
- `_get_elev_data()`: Thread-safe singleton loader (memory-mapped)

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
- `clearAllVisualizations()`: Clears all map overlays

**Features**:
- Progress tracking via SSE (`/sim/progress-stream`)
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
- Custom map controls (map type, search, fullscreen)

#### `www/style.js` - Mode Switching
**Purpose**: UI mode management and defaults

**Key Functions**:
- `setMode()`: Standard/ZPB/Float balloon type switching
- Server status polling (`/sim/status`)
- Waypoint toggle functionality
- Default value initialization

### Configuration Files

#### `gunicorn_config.py` - Gunicorn Configuration
- 4 workers × 8 threads = 32 concurrent capacity
- 15-minute timeout for ensemble simulations
- Access log filtering (suppresses `/sim/status`)
- Post-fork initialization (starts cache trim thread per worker)

#### `requirements.txt` - Python Dependencies
- Flask, Flask-CORS, Flask-Compress
- NumPy, boto3
- Gunicorn

---

## Request Flow and Call Patterns

### 1. `/sim/singlezpb` - Single Model Simulation

**Client Request** (`www/paths.js`):
```javascript
// User clicks "Simulate" button
fetch('/sim/singlezpb?timestamp=...&lat=...&lon=...&alt=...&equil=...&eqtime=...&asc=...&desc=...&model=0')
```

**Server Processing** (`app.py`):

1. **Route Handler** (`singlezpbh()`):
   - Parses request arguments using `get_arg()` helper
   - Converts timestamp to UTC datetime
   - Calls `singlezpb()` function

2. **Simulation Function** (`singlezpb()` in `app.py`):
   - **Phase 1 - Ascent**: Calls `simulate.simulate()` with positive ascent rate
     - Duration: `(equil - alt) / asc / 3600` hours
     - `elevation=False` (no ground checks during ascent)
   - **Phase 2 - Coast**: Calls `simulate.simulate()` with zero vertical rate
     - Balloon floats at burst altitude for `eqtime` hours
   - **Phase 3 - Descent**: Calls `simulate.simulate()` with negative descent rate
     - Duration: `alt / desc / 3600` hours
     - `elevation=True` (stops when balloon hits ground)

3. **Core Simulation** (`simulate.simulate()` in `simulate.py`):
   - Checks prediction cache first (LRU cache, 200 entries, 1hr TTL)
   - Marks model as in-use to prevent cleanup races
   - Gets simulator via `_get_simulator(model)`:
     - Checks if simulator exists in cache (`_simulator_cache`)
     - If cache miss: loads GEFS file via `gefs.load_gefs()`, creates `WindFile`, creates `Simulator`
     - Validates simulator wind_file is not None (race condition protection)
   - Creates `Balloon` object with initial state
   - Calls `simulator.simulate()` to compute trajectory
   - Converts trajectory to path array: `[timestamp, lat, lon, alt, u_wind, v_wind, 0, 0]`
   - Caches successful result
   - Returns path array

4. **Physics Engine** (`Simulator.simulate()` in `habsim/classes.py`):
   - Uses Runge-Kutta 2nd order (RK2) integration
   - For each time step:
     - Calls `Simulator.step()`:
       - Gets wind vector via `wind_file.get(lat, lon, alt, time)`:
         - `WindFile.get()` calls `get_indices()` to convert coordinates to array indices
         - Calls `interpolate()` for 4D linear interpolation (lat, lon, altitude, time)
         - Returns (u, v) wind components
       - Computes horizontal movement from wind + air velocity
       - Updates balloon position using RK2 integration
       - Checks ground elevation if descending (`elevation=True`)
       - Stops if balloon altitude ≤ ground elevation
     - Appends new record to trajectory
   - Returns `Trajectory` object with all time steps

5. **Response**:
   ```json
   [
     [rise_path],    // Ascent phase: [[timestamp, lat, lon, alt, u_wind, v_wind, 0, 0], ...]
     [coast_path],   // Coast phase: [[timestamp, lat, lon, alt, u_wind, v_wind, 0, 0], ...]
     [fall_path]     // Descent phase: [[timestamp, lat, lon, alt, u_wind, v_wind, 0, 0], ...]
   ]
   ```

**Client Rendering** (`www/paths.js`):
- Receives trajectory data
- Draws `google.maps.Polyline` on map
- Adds waypoint markers with altitude/time info
- Places end pin marker at landing location

---

### 2. `/sim/spaceshot` - Ensemble + Monte Carlo Simulation

**Client Request** (`www/paths.js`):
```javascript
// User enables "Ensemble" toggle and clicks "Simulate"
// Client generates request_id using MD5 hash
const requestKey = `${timestamp}_${lat}_${lon}_${alt}_${equil}_${eqtime}_${asc}_${desc}_1.0`;
const requestId = CryptoJS.MD5(requestKey).toString().substring(0, 16);

// Start SSE connection for progress updates
const eventSource = new EventSource(`/sim/progress-stream?request_id=${requestId}`);

// Make spaceshot request
fetch('/sim/spaceshot?timestamp=...&lat=...&lon=...&alt=...&equil=...&eqtime=...&asc=...&desc=...&num_perturbations=20')
```

**Server Processing** (`app.py`):

1. **Route Handler** (`spaceshot()`):
   - Parses arguments using `get_arg()` helper
   - Generates request ID using `generate_request_id()` (MD5 hash matching client)
   - Activates ensemble mode via `activate_ensemble_mode()`:
     - Calls `simulate.set_ensemble_mode()` which expands cache limit from 10 → 30 simulators
     - Starts background prefetch thread to pre-load all 21 models
   - Gets model IDs via `get_model_ids()` (typically 0-20)
   - Initializes progress tracking dictionary (in-memory + file-based)
   - Generates Monte Carlo perturbations:
     - Creates 20 parameter variations using perturbation helpers:
       - `perturb_lat()`: ±0.001° ≈ ±111m
       - `perturb_lon()`: ±0.001° ≈ ±111m
       - `perturb_alt()`: ±50m
       - `perturb_equil()`: ±200m
       - `perturb_eqtime()`: ±10%
       - `perturb_rate()`: ±0.1 m/s
       - `perturb_coefficient()`: 0.9-1.0 (weighted: 90% chance of 0.95-1.0, 10% chance of 0.9-0.95)
   - Waits for prefetch via `wait_for_prefetch()` (waits for first 5 models to load)

2. **Parallel Execution**:
   - Creates `ThreadPoolExecutor` with `max_workers=min(32, os.cpu_count())`
   - Submits 21 ensemble simulations (one per model):
     - Each calls `run_ensemble_simulation(model)`:
       - Calls `singlezpb()` with base parameters
       - Extracts landing position via `extract_landing_position()`
       - Returns full trajectory path
   - Submits 420 Monte Carlo simulations (20 perturbations × 21 models):
     - Each calls `run_montecarlo_simulation(pert, model)`:
       - Calls `singlezpb()` with perturbed parameters
       - Extracts only landing position (lat, lon)
       - Returns landing dict with `perturbation_id`, `model_id`, `weight=1.0`
   - Total: 441 simulations running in parallel

3. **Progress Tracking**:
   - Batches progress updates (every 10 completions) to reduce lock contention
   - Updates `_progress_tracking[request_id]` with:
     - `completed`: total simulations done
     - `ensemble_completed`: ensemble paths done
     - `montecarlo_completed`: Monte Carlo simulations done
   - Writes to file-based cache for multi-worker access
   - Client receives updates via SSE (`/sim/progress-stream`)

4. **Result Collection**:
   - Ensemble paths: stored in `paths` array (21 trajectories for line plotting)
   - Landing positions: stored in `landing_positions` array (441 total: 21 ensemble + 420 Monte Carlo)
   - Ensemble landings have `perturbation_id=-1` and `weight=2.0`
   - Monte Carlo landings have `perturbation_id=0-19` and `weight=1.0`

5. **Cleanup**:
   - Decrements ensemble counter
   - Trims cache back to normal (10 simulators) via `simulate._trim_cache_to_normal()`
   - Schedules progress tracking cleanup after 30 seconds

6. **Response**:
   ```json
   {
     "paths": [[rise, coast, fall], ...],  // 21 ensemble trajectories
     "heatmap_data": [                      // 441 landing positions
       {"lat": ..., "lon": ..., "perturbation_id": -1, "model_id": 0, "weight": 2.0},
       ...
     ],
     "request_id": "..."
   }
   ```

**Client Rendering** (`www/paths.js`):
- Receives ensemble paths and heatmap data
- Draws 21 colored `google.maps.Polyline` objects (one per model)
- Creates custom heatmap overlay using `CustomHeatmapOverlay`:
  - Performs kernel density estimation on 420 Monte Carlo landing positions
  - Renders probability contours (30/50/70/90% cumulative mass)
  - Color gradient: Green (low) → Yellow → Orange → Red (high density)
- Updates button text with progress percentage via SSE

---

### 3. Multi Mode - Sequential Single Simulations

**Client Request** (`www/paths.js`):
```javascript
// User enables "Multi" toggle and clicks "Simulate"
// Frontend makes sequential calls to /sim/singlezpb with different hour offsets
for (const hourOffset of [-6, -3, 0, +3, +6]) {
  const offsetTimestamp = baseTimestamp + (hourOffset * 3600);
  const response = await fetch(`/sim/singlezpb?timestamp=${offsetTimestamp}&...&model=0`);
  // Process response and place end marker
}
```

**Client Processing** (`www/paths.js`):
1. **Multi Simulation Loop**:
   - Calculates hour offsets (e.g., -6, -3, 0, +3, +6 hours from base time)
   - For each offset:
     - Calls `/sim/singlezpb` with `timestamp = base_time + offset * 3600`
     - Uses `model=0` (control run only)
     - Waits for response
   - Each call follows the same flow as singlezpb (see above)

2. **Rendering**:
   - For each successful response:
     - Calls `addMultiEndPin()` to place end marker on map
     - Stores trajectory data but doesn't draw it yet
     - End markers are clickable - clicking draws the full trajectory
   - Draws connector line between all end positions

**Server Processing**:
- Each request is handled independently as a normal `/sim/singlezpb` call
- No special server-side coordination needed
- Cache benefits: subsequent calls with same parameters use cached results

---

## Key Design Decisions

### Caching Strategy

**Three-layer caching**:
1. **Simulator cache (RAM)**: 10 normal, 30 ensemble - fast access to physics engines
2. **File cache (disk)**: 30 weather files - eliminates S3 downloads after first use
3. **Prediction cache (RAM)**: 200 entries, 1hr TTL - avoids recomputing identical trajectories

**Benefits**:
- First ensemble run: ~5-15 minutes (files download from S3)
- Subsequent runs: ~5-15 minutes (files cached, simulators in RAM)
- Single model runs: ~5-10 seconds (model 0 pre-warmed)

### Ensemble Mode

**Dynamic cache expansion**: Normal mode (10 simulators) → Ensemble mode (30 simulators)

**Preloading**: Ensemble mode preloads wind arrays into RAM for CPU-bound performance:
- Memory-mapped: ~50-80s per simulation (I/O-bound)
- Preloaded: ~5-10s per simulation (CPU-bound)

**Auto-trimming**: Cache automatically trims back to 10 simulators 60 seconds after ensemble completes

**Background prefetching**: Pre-loads all 21 models when ensemble mode starts

**Maximum duration**: 5 minutes cap to prevent memory bloat from consecutive ensemble calls

### Thread Safety

**Per-file locks**: Prevents zipfile contention when multiple threads load same NPZ file

**In-use tracking**: `_in_use_models` set prevents cleanup of simulators currently in use

**Delayed cleanup**: 2-second delay after eviction prevents race conditions

**Atomic operations**: Progress updates use locks to ensure consistency

**File-based progress cache**: Shared across workers via JSON files in `/app/data/progress/`

### Memory Management

**Idle cleanup**: Workers idle >120s trigger deep cleanup (drops simulators, keeps elevation mapped)

**Shared elevation**: All simulators in ensemble mode share same `ElevationFile` instance

**Shared filter cache**: WindFile interpolation filters shared across instances in ensemble mode

**Garbage collection**: Multi-pass GC + `malloc_trim(0)` after cache trims

**Old file cleanup**: Automatically deletes files from previous GEFS cycles (prevents disk bloat)

### Progress Tracking

**Dual storage**: In-memory (fast) + file-based (shared across workers)

**Batched updates**: Updates every 10 completions to reduce lock contention

**SSE streaming**: Real-time progress updates via Server-Sent Events

**30-second cleanup delay**: Allows late-connecting SSE clients to read final progress

---

## Performance Characteristics

### Single Model Run
- **Speed**: ~5-10 seconds
- **Memory**: ~1.5GB per worker (worst case: 4 workers × 1.5GB = 6GB)
- **Why fast**: Model 0 pre-warmed, files on disk

### Ensemble Run (First Time)
- **Speed**: ~5-15 minutes
  - 21 ensemble paths: ~5-10 seconds each (with preloading)
  - 420 Monte Carlo: ~4-14 minutes total
- **Memory**: ~13.8GB per worker (worst case: 4 workers × 13.8GB = 55GB)
- **Why slower**: Files download from S3, simulators built in parallel

### Ensemble Run (Subsequent)
- **Speed**: ~5-15 minutes (same computation, but files cached)
- **Memory**: ~13.8GB per worker (simulators cached in RAM)
- **Why faster**: Files on disk, simulators in RAM cache

### After Ensemble Completes
- **Auto-trim**: Cache trims to 10 simulators within 60-90 seconds
- **Memory freed**: ~49GB RAM (from 55GB → 6GB worst case)
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

### Code Organization
- **Backend**: Python files in root directory
- **Frontend**: JavaScript/HTML files in `www/` directory
- **Core physics**: `habsim/classes.py`
- **Deprecated code**: Moved to `deprecated/` directory

---

## Data Flow Diagrams

### Single Model Simulation Flow

```
Client Request
    ↓
app.py: singlezpbh() [route handler]
    ↓
app.py: singlezpb() [three-phase simulation]
    ↓
simulate.py: simulate() [orchestrator]
    ├─ Check prediction cache
    ├─ simulate.py: _get_simulator()
    │   ├─ Check simulator cache
    │   ├─ gefs.py: load_gefs() [download if needed]
    │   ├─ windfile.py: WindFile() [load wind data]
    │   └─ habsim/classes.py: Simulator() [create physics engine]
    ├─ habsim/classes.py: Balloon() [create state]
    └─ habsim/classes.py: Simulator.simulate()
        └─ habsim/classes.py: Simulator.step() [RK2 integration]
            ├─ windfile.py: WindFile.get() [wind interpolation]
            └─ habsim/classes.py: ElevationFile.elev() [ground check]
    ↓
Response: [rise, coast, fall] arrays
```

### Ensemble Simulation Flow

```
Client Request
    ↓
app.py: spaceshot() [route handler]
    ├─ activate_ensemble_mode() [expand cache, start prefetch]
    ├─ Generate 20 Monte Carlo perturbations
    └─ ThreadPoolExecutor (32 workers)
        ├─ 21 ensemble futures → singlezpb() → extract landing
        └─ 420 Monte Carlo futures → singlezpb(perturbed) → extract landing
    ↓
Response: {paths: [...], heatmap_data: [...], request_id: "..."}
```

### Multi Mode Flow

```
Client Request (sequential)
    ↓
For each hour offset:
    app.py: singlezpbh() [same as single model]
    ↓
    [Same flow as single model simulation]
    ↓
    Response: [rise, coast, fall]
    ↓
    paths.js: addMultiEndPin() [place marker, store trajectory]
    ↓
Draw connector line between all endpoints
```

---

## License
See LICENSE file for details.
