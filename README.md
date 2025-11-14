# HABSIM
**High Altitude Balloon Simulator**

A web-based trajectory prediction system for high-altitude balloons using GEFS (Global Ensemble Forecast System) weather data. Supports single model simulations, ensemble runs (21 models), and Monte Carlo analysis (420 perturbations) for uncertainty quantification. Designed for the Stanford Student Space Initiative.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Features](#features)
- [Architecture](#architecture)
- [API Reference](#api-reference)
- [Core Components](#core-components)
- [Performance & Optimization](#performance--optimization)
- [GEFS Cycle Management](#gefs-cycle-management)
- [Deployment](#deployment)
- [Development](#development)

---

## Quick Start

### Single Simulation
```bash
GET /sim/singlezpb?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0
```

### Ensemble + Monte Carlo
```bash
GET /sim/spaceshot?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&num_perturbations=20
```

**Response**: `{paths: [...], heatmap_data: [...], request_id: "..."}`

---

## Features

- **Single Model**: Fast predictions using one weather model (~5-10s)
- **Ensemble Runs**: 21 weather models in parallel for uncertainty analysis (~5-15min)
- **Monte Carlo**: 420 parameter perturbations for probability density mapping
- **Multi Mode**: Sequential simulations at different launch times
- **Real-time Progress**: Server-Sent Events (SSE) for progress tracking
- **Idempotent Requests**: Duplicate ensemble requests reuse in-progress results
- **Adaptive Caching**: Auto-expands cache for ensemble workloads (10 → 30 simulators)
- **Progressive Prefetch**: Waits for first 12 models, continues rest in background
- **GEFS Cycle Protection**: Dual validation prevents stale data during cycle changes
- **Memory Management**: Intelligent cleanup and resource management

---

## Architecture

### System Overview

```
Client (www/) 
    ↓ HTTP/SSE
Flask App (app.py)
    ├─ Request routing & validation
    ├─ Ensemble coordination
    ├─ Progressive prefetch (12 models wait, rest background)
    └─ Progress tracking (SSE)
    ↓
Simulation Orchestrator (simulate.py)
    ├─ Adaptive cache management (10 → 30 simulators)
    ├─ GEFS cycle validation
    ├─ Prediction caching (200 entries, 1hr TTL)
    └─ Memory management (reference counting, periodic trim)
    ↓
┌─────────────────────────────────────┐
│  GEFS Manager (gefs.py)             │  S3 downloads, LRU cache
│  Wind Data (windfile.py)            │  4D interpolation
│  Elevation (elev.py)                │  Ground elevation lookup
│  Physics Engine (classes.py)        │  Runge-Kutta integration
└─────────────────────────────────────┘
```

### Deployment

- **Platform**: Railway
- **Workers**: 4 Gunicorn workers × 8 threads = 32 concurrent capacity
- **Resources**: 32GB RAM, 32 vCPU
- **Storage**: Persistent volume at `/app/data` for file cache
- **Data Source**: AWS S3 (GEFS weather files, ~308MB each)

---

## API Reference

### Simulation Endpoints

#### `GET /sim/singlezpb`
Single model trajectory simulation (three phases: ascent, coast, descent).

**Parameters**:
- `timestamp` (float): Unix timestamp
- `lat`, `lon` (float): Launch coordinates
- `alt` (float): Launch altitude (meters, 0-50000)
- `equil` (float): Burst altitude (meters, >= alt, < 50000)
- `eqtime` (float): Equilibrium time (hours, 0-48)
- `asc` (float): Ascent rate (m/s, 0-20)
- `desc` (float): Descent rate (m/s, 0-20)
- `model` (int): GEFS model ID (0-20)

**Response**: `[[rise_path], [coast_path], [fall_path]]`

#### `GET /sim/spaceshot`
Ensemble + Monte Carlo simulation with progress tracking and idempotent deduplication.

**Parameters**: Same as `/sim/singlezpb`, plus:
- `num_perturbations` (int): Monte Carlo perturbations (1-100, default: 20)
- `coeff` (float): Floating coefficient (0.5-1.5, default: 1.0)

**Response**:
```json
{
  "paths": [...],           // 21 ensemble trajectories
  "heatmap_data": [...],    // 441 landing positions (21 + 420)
  "request_id": "..."       // For progress tracking
}
```

**Flow**:
1. **Deduplication**: Identical requests reuse in-progress results (up to 15min)
2. **Prefetch Phase**: Downloads first 12 models (progressive prefetch)
3. **Simulation Phase**: Runs 21 ensemble + 420 Monte Carlo simulations in parallel
4. **Progress**: Real-time updates via SSE stream with collision-safe file writes

#### `GET /sim/progress-stream?request_id=...`
Server-Sent Events stream for real-time progress updates.

**Response** (SSE):
```
data: {"completed": 100, "total": 441, "percentage": 23, "status": "simulating"}
```

**Status Values**:
- `"loading"`: Prefetch phase (downloading models)
- `"simulating"`: Running simulations

### Utility Endpoints

- `GET /sim/elev?lat=...&lon=...` - Elevation lookup
- `GET /sim/models` - Available model IDs (0-20)
- `GET /sim/status` - Server status (memory, cache, active requests)
- `GET /sim/cache-status` - Cache diagnostics (simulator cache, GEFS cache)
- `GET /sim/which` - Current GEFS timestamp

---

## Core Components

### `app.py` - Flask Application
**Purpose**: REST API server, request routing, ensemble coordination

**Key Functions**:
- `singlezpb()`: Three-phase simulation (ascent → coast → descent)
- `spaceshot()`: Ensemble + Monte Carlo coordinator with idempotent deduplication
- `_acquire_inflight_request()`: Prevents duplicate ensemble runs (shared across workers)
- `_complete_inflight_request()`: Publishes results to waiting clients
- `wait_for_prefetch()`: Progressive prefetch (waits for 12 models, continues rest in background)
- `_prefetch_model()`: Prefetches single model with GEFS cycle validation
- `_generate_perturbations()`: Monte Carlo parameter generation
- `update_progress()`: Atomic progress tracking (in-memory + file-based)
- `run_ensemble_simulation()`: Single ensemble model simulation
- `run_montecarlo_simulation()`: Single Monte Carlo perturbation

**Progressive Prefetch Strategy**:
- Waits for first 12 models to complete (fast simulation start)
- Continues prefetching remaining 9 models in background
- When simulations need models 13-21, they're likely ready (avoids 100+ second delays)
- Early abort if 5+ models fail (detects GEFS cycle change)

**Progress Tracking**:
- Status: `'loading'` (prefetch) → `'simulating'` (running)
- Batched updates (every 10 completions) to reduce lock contention
- Dual storage: in-memory (fast) + file-based (shared across workers)
- Collision-safe file writes using NamedTemporaryFile + fsync
- 30-second cleanup delay after completion

**GEFS Cycle Protection**:
- Captures `initial_gefs` at prefetch start
- Validates cycle BEFORE and AFTER loading each model
- Aborts prefetch if cycle changes mid-prefetch (prevents stale data)

### `simulate.py` - Simulation Orchestrator
**Purpose**: Simulator cache management, trajectory calculation, GEFS cycle management

**Key Functions**:
- `simulate()`: Main simulation (checks cache, runs physics)
- `_get_simulator()`: Gets/creates simulator with adaptive caching
- `_validate_simulator_cycle()`: Validates cached simulator matches current GEFS cycle
- `refresh()`: Checks S3 for new GEFS cycle, updates shared file
- `reset()`: Clears simulator cache when GEFS cycle changes (only unused simulators)
- `get_currgefs()`: Gets current GEFS timestamp from shared file
- `_should_preload_arrays()`: Auto-detects ensemble workload (10+ models)
- `_get_target_cache_size()`: Auto-sizes cache (10 normal, 30 ensemble)
- `_trim_cache_to_normal()`: Trims cache based on workload
- `_periodic_cache_trim()`: Background thread for cache management
- `_idle_memory_cleanup()`: Deep cleanup when idle (>15 minutes)

**Adaptive Cache Management**:
- **Simulator cache**: 10 normal → 30 ensemble (auto-expands at 10+ models)
- **Prediction cache**: 200 entries, 1hr TTL
- **Reference counting**: Prevents cleanup of active simulators
- **Shared elevation**: Single `ElevationFile` instance for ensemble workloads
- **LRU eviction**: Evicts least recently used unused simulators

**GEFS Cycle Validation**:
- Extracts GEFS timestamp from `wind_file._source_path` (e.g., `"2025111312_00.npz"` → `"2025111312"`)
- Compares with current `currgefs` on cache hits
- Rejects stale simulators (different cycle) and forces reload
- Handles edge cases (missing `_source_path`, invalid filenames)

**Auto-refresh**:
- Checks for new GEFS cycle every 5 minutes
- Updates shared file (`/app/data/currgefs.txt`) across workers
- Clears cache and cleans up old files when cycle changes

### `windfile.py` - Wind Data Access
**Purpose**: 4D wind interpolation (lat, lon, alt, time)

**Access Modes** (auto-detected):
- **Normal**: Memory-mapped (`mmap_mode='r'`) - ~150MB per simulator
- **Ensemble** (10+ models): Preloaded arrays - ~460MB per simulator (faster)

**Features**:
- Extracts NPZ to memory-mapped `.npy` for fast access
- Per-file locks prevent zipfile contention
- Filter cache for interpolation arrays
- Stores `_source_path` for GEFS cycle validation

### `gefs.py` - GEFS File Management
**Purpose**: Downloads and caches GEFS files from AWS S3

**Caching**:
- Disk cache: `/app/data/gefs` (or `/tmp/habsim-gefs/` fallback)
- Max 30 weather files (~9.2GB) + `worldelev.npy` (451MB, never evicted)
- LRU eviction when cache exceeds limits

**Features**:
- **S3 TransferManager**: Multipart parallel downloads (faster, more resilient)
- **Connection pooling**: 64 connections for high concurrency
- **Download semaphore**: 8 concurrent downloads (increased from 4)
- **Per-file locking**: Prevents duplicate downloads across workers
- **Retry logic**: Exponential backoff (up to 5 retries)
- **File integrity**: Validates NPZ files before returning
- **Automatic cleanup**: Removes old GEFS cycle files when new cycle detected
- **Download protection**: `_downloading_files` set prevents cleanup of active downloads

**Performance**:
- Cache hits: <1s (file already on disk)
- Cache misses: 5-30s (download from S3, ~308MB per file)
- Parallel downloads: 8 concurrent (improves ensemble prefetch speed)

### `habsim/classes.py` - Physics Engine
**Purpose**: Balloon state, Runge-Kutta integration

**Key Classes**:
- `Balloon`: State container (location, altitude, time, wind_vector)
- `Simulator`: RK2 integration engine
- `Location`: Geographic coordinates with haversine distance
- `ElevationFile`: Ground elevation data access

**Physics**:
- Runge-Kutta 2nd order (RK2) integration
- Wind interpolation at each time step
- Ground elevation checks during descent
- Horizontal movement from wind + air velocity

### `elev.py` - Elevation Data
**Purpose**: Bilinear interpolation for ground elevation

**Data Source**: `worldelev.npy` (451MB, global elevation grid)

---

## Performance & Optimization

### Single Model Run
- **Speed**: ~5-10 seconds
- **Memory**: ~1.5GB per worker
- **Why fast**: Model 0 pre-warmed, files on disk

### Ensemble Run (First Time)
- **Speed**: ~5-15 minutes
  - Prefetch (12 models): ~30-60s
  - 21 ensemble paths: ~5-10 seconds each
  - 420 Monte Carlo: ~4-14 minutes total
- **Memory**: ~13.8GB per worker
- **Why slower**: Files download from S3, simulators built in parallel

### Ensemble Run (Subsequent)
- **Speed**: ~5-15 minutes (same computation, files cached)
- **Memory**: ~13.8GB per worker (simulators cached in RAM)
- **Why faster**: Files on disk, simulators in RAM cache

### Progressive Prefetch Benefits
- **Fast startup**: Simulations start after 12 models (vs waiting for all 21)
- **Background loading**: Models 13-21 continue prefetching while simulations run
- **Reduced delays**: On-demand requests for models 13-21 are likely already ready
- **Early abort**: Detects GEFS cycle changes quickly (5+ failures)

### After Ensemble Completes
- **Auto-trim**: Cache automatically trims when workload decreases
- **Idle cleanup**: Workers idle >15 minutes trigger deep cleanup
- **Memory recovery**: Multi-pass GC + `malloc_trim(0)` after cache trims

---

## GEFS Cycle Management

### Cycle Change Detection
- **Auto-refresh**: Checks S3 every 5 minutes for new GEFS cycle
- **Shared state**: Uses `/app/data/currgefs.txt` file (shared across workers)
- **Cache invalidation**: Clears unused simulators when cycle changes

### Cycle Change Protocol

When a new GEFS cycle is detected, the system follows a strict protocol to ensure consistency:

1. **File Verification**: Verify all 21 model files exist *and are readable* in S3 (Range-read sanity check with retries)
2. **Cache Invalidation**: Set `_cache_invalidation_cycle` to signal other workers
3. **Grace Period**: Wait 3 seconds for S3 propagation across regions (optimized from 5s)
4. **Update currgefs**: Write new cycle to shared file (other workers can now see it)
5. **Cache Cleanup**: Clear unused simulators and schedule old file deletion

This order ensures other workers see `invalidation_cycle` before `currgefs`, allowing them to detect and wait for cycle transitions properly.

**Recent Improvements**:
- **Cycle Stabilization Timeout**: Increased from 6s to 12s, downgraded log level to INFO for less noise
- **Refresh Synchronization**: Even when `whichgefs` unchanged, syncs invalidation cycle to prevent timeout
- **Streamlined Logging**: Condensed multi-line messages into single info-packed lines

### Protection Mechanisms

**1. Optimized Grace Period Handling**
- `refresh()` sets `invalidation_cycle` before `currgefs` (3s grace period, reduced from 5s)
- `wait_for_prefetch()` still waits out the transition but now *always* re-verifies files post-refresh
- Prevents redundant checks while ensuring freshly announced cycles are actually downloadable

**2. Atomic Ref Count Acquisition**
- Validates cycle consistency before and after acquiring ref counts
- If cycle changes during acquisition, releases ref counts and retries
- Ensures all models use the same cycle
- Validates cycle immediately before prefetch task submission (prevents race conditions)

**3. Enhanced Pending Cycle Handling**
- If new cycle detected but files not ready, waits up to 2 minutes
- Checks `currgefs` frequently (every 0.5s) to detect concurrent worker updates
- Checks S3 files less frequently (every 2s) to reduce API calls
- Falls back gracefully if timeout (uses current cycle or cached files)

**4. Cycle Consistency Validation**
- Validates cycle before and after each model prefetch and refuses to proceed if any readable-file check fails
- Checks cache invalidation cycle before loading simulators (prevents stale data)
- Aborts prefetch if cycle changes mid-prefetch (prevents mixed cycles)
- Early abort if 5+ models fail with cycle change errors (likely cycle change)

**5. Cache Validation (Runtime)**
- Validates cached simulators on every access
- Extracts GEFS timestamp from `wind_file._source_path`
- Rejects stale simulators (different cycle) and forces reload
- Checks `invalidation_cycle` before using cached simulators

**6. File Protection**
- `_downloading_files` set prevents cleanup of active downloads
- Per-file locks prevent concurrent downloads
- Old files only deleted after new cycle is confirmed (30s delay)

**7. Redundancy Elimination**
- Skips redundant file availability checks after successful `refresh()` (already verified)
- Skips cycle stabilization wait if cycle was just updated (already stable)
- Reduces S3 API calls and improves response time

### Flow During Cycle Change

1. **Refresh detects new cycle**: Reads `whichgefs` from S3
2. **Verify files exist**: Checks all 21 model files (retries for S3 eventual consistency)
3. **Set invalidation cycle**: Signals other workers that cache is invalid
4. **Grace period**: Wait 3 seconds for S3 propagation (optimized from 5s)
5. **Update currgefs**: Write new timestamp to `/app/data/currgefs.txt`
6. **Clear cache**: `reset()` evicts unused simulators (preserves active ones)
7. **Schedule cleanup**: Delete old GEFS files after 30s delay
8. **New requests**: Wait for cycle to stabilize, then load new cycle files

---

## Deployment

### Railway Configuration

**Gunicorn** (`gunicorn_config.py`):
- 4 workers, 8 threads each (32 concurrent capacity)
- 15-minute timeout (ensemble simulations can take 5-15 minutes)
- Preload app for faster startup

**Start Command**:
```bash
gunicorn --config gunicorn_config.py app:app
```

### Environment Variables

**Required**:
- `AWS_ACCESS_KEY_ID`: S3 access key
- `AWS_SECRET_ACCESS_KEY`: S3 secret key

**Optional**:
- `AWS_REGION`: S3 region (default: `us-west-1`)
- `S3_BUCKET_NAME`: S3 bucket (default: `habsim-storage`)
- `HABSIM_PASSWORD`: Login password (optional)
- `PORT`: Server port (default: `8000`)
- `RAILWAY_ENVIRONMENT`: Auto-detected for Railway-specific init

### Persistent Volume

**Mount**: `/app/data`

**Benefits**:
- Lower S3 egress costs
- Faster warmups (files already on disk)
- Shared cache across workers

**Contents**:
- `/app/data/gefs/`: GEFS weather files (30 files max, ~9.2GB)
- `/app/data/progress/`: Progress tracking files (shared across workers)
- `/app/data/currgefs.txt`: Current GEFS timestamp (shared across workers)

---

## Development

### Local Setup

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
# Or with Gunicorn
gunicorn --config gunicorn_config.py app:app
```

### Project Structure

```
habsim/
├── app.py                 # Flask application
├── simulate.py            # Simulation orchestrator
├── gefs.py                # GEFS file management
├── windfile.py            # Wind data access
├── elev.py                # Elevation data
├── downloader.py          # GEFS data downloader
├── gunicorn_config.py     # Gunicorn configuration
├── scripts/
│   └── auto_downloader.py  # Automated GEFS downloader (single-cycle/daemon modes)
├── habsim/
│   └── classes.py         # Physics engine
├── www/                   # Frontend
│   ├── index.html         # Main application
│   ├── paths.js           # Map rendering & API client
│   ├── util.js            # Map utilities
│   └── style.js           # Mode switching
└── requirements.txt       # Python dependencies
```

### Key Design Decisions

**Idempotent Ensemble Requests**:
- Identical requests reuse in-progress results (up to 15min timeout)
- Prevents duplicate work when multiple clients/tabs request same simulation
- Cross-worker coordination via shared file-based event tracking

**Adaptive Caching**:
- Cache automatically expands from 10 → 30 simulators when 10+ ensemble models detected
- Preloading automatically enabled for ensemble workloads
- Cold-start hint forces first ensemble after a cycle change to preload every model
- No explicit "ensemble mode" - system adapts to workload automatically

**Progressive Prefetch**:
- Waits for first 12 models (fast startup)
- Continues prefetching remaining 9 models in background
- Balances speed (simulations start quickly) with completeness (all models ready)

**Memory Management**:
- Reference counting prevents cleanup of active simulators
- Shared elevation file for ensemble workloads
- Multi-pass GC + `malloc_trim(0)` after cache trims
- Automatic cleanup of old GEFS cycle files

**GEFS Cycle Protection**:
- Dual validation (before + after loading)
- Cache validation on every access
- Early abort on cycle change detection
- File protection during downloads

**Progress Tracking**:
- Dual storage: in-memory (fast) + file-based (shared across workers)
- Status: `'loading'` → `'simulating'`
- Batched updates (every 10 completions) to reduce lock contention
- 30-second cleanup delay for late-connecting SSE clients

---

## License

See LICENSE file for details.
