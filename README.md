# HABSIM
**High Altitude Balloon Simulator**

A web-based trajectory prediction system for high-altitude balloons using GEFS (Global Ensemble Forecast System) weather data. Supports single model simulations, ensemble runs (21 models), and Monte Carlo analysis (420 perturbations) for uncertainty quantification.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Features](#features)
- [Architecture](#architecture)
- [API Reference](#api-reference)
- [Core Components](#core-components)
- [Performance](#performance)
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
- **Adaptive Caching**: Auto-expands cache for ensemble workloads
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
    └─ Progress tracking
    ↓
Simulation Orchestrator (simulate.py)
    ├─ Adaptive cache management
    ├─ Prediction caching
    └─ Memory management
    ↓
┌─────────────────────────────────────┐
│  GEFS Manager (gefs.py)            │  S3 downloads, LRU cache
│  Wind Data (windfile.py)           │  4D interpolation
│  Elevation (elev.py)                │  Ground elevation lookup
│  Physics Engine (classes.py)        │  Runge-Kutta integration
└─────────────────────────────────────┘
```

### Deployment

- **Platform**: Railway
- **Workers**: 4 Gunicorn workers × 8 threads = 32 concurrent capacity
- **Resources**: 32GB RAM, 32 vCPU
- **Storage**: Persistent volume at `/app/data` for file cache
- **Data Source**: AWS S3 (GEFS weather files)

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
Ensemble + Monte Carlo simulation with progress tracking.

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

#### `GET /sim/progress-stream?request_id=...`
Server-Sent Events stream for real-time progress updates.

**Response** (SSE):
```
data: {"completed": 100, "total": 441, "percentage": 23, "status": "simulating"}
```

### Utility Endpoints

- `GET /sim/elev?lat=...&lon=...` - Elevation lookup
- `GET /sim/models` - Available model IDs
- `GET /sim/status` - Server status
- `GET /sim/cache-status` - Cache diagnostics
- `GET /sim/which` - Current GEFS timestamp

---

## Core Components

### `app.py` - Flask Application
**Purpose**: REST API server, request routing, ensemble coordination

**Key Functions**:
- `singlezpb()`: Three-phase simulation (ascent → coast → descent)
- `spaceshot()`: Ensemble + Monte Carlo coordinator
- `wait_for_prefetch()`: Prefetches first models to warm cache
- `_generate_perturbations()`: Monte Carlo parameter generation
- `update_progress()`: Atomic progress tracking (in-memory + file-based)

**Progress Tracking**:
- Status: `'downloading'` (prefetch) → `'simulating'` (running)
- Batched updates (every 10 completions) to reduce lock contention
- 30-second cleanup delay after completion

### `simulate.py` - Simulation Orchestrator
**Purpose**: Simulator cache management, trajectory calculation

**Key Functions**:
- `simulate()`: Main simulation (checks cache, runs physics)
- `_get_simulator()`: Gets/creates simulator with adaptive caching
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

### `windfile.py` - Wind Data Access
**Purpose**: 4D wind interpolation (lat, lon, alt, time)

**Access Modes** (auto-detected):
- **Normal**: Memory-mapped (`mmap_mode='r'`) - ~150MB per simulator
- **Ensemble** (10+ models): Preloaded arrays - ~460MB per simulator (faster)

**Features**:
- Extracts NPZ to memory-mapped `.npy` for fast access
- Per-file locks prevent zipfile contention
- Filter cache for interpolation arrays

### `gefs.py` - GEFS File Management
**Purpose**: Downloads and caches GEFS files from AWS S3

**Caching**:
- Disk cache: `/app/data/gefs` (or `/tmp/habsim-gefs/` fallback)
- Max 30 weather files (~9.2GB) + `worldelev.npy` (451MB, never evicted)
- LRU eviction when cache exceeds limits

**Features**:
- Per-file locking prevents duplicate downloads across workers
- Connection pooling (64 connections) for high concurrency
- Retry logic with exponential backoff (up to 5 retries)
- Semaphore limits concurrent downloads (4 at a time)
- Automatic cleanup of old GEFS cycle files

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

## Performance

### Single Model Run
- **Speed**: ~5-10 seconds
- **Memory**: ~1.5GB per worker
- **Why fast**: Model 0 pre-warmed, files on disk

### Ensemble Run (First Time)
- **Speed**: ~5-15 minutes
  - 21 ensemble paths: ~5-10 seconds each
  - 420 Monte Carlo: ~4-14 minutes total
- **Memory**: ~13.8GB per worker
- **Why slower**: Files download from S3, simulators built in parallel

### Ensemble Run (Subsequent)
- **Speed**: ~5-15 minutes (same computation, files cached)
- **Memory**: ~13.8GB per worker (simulators cached in RAM)
- **Why faster**: Files on disk, simulators in RAM cache

### After Ensemble Completes
- **Auto-trim**: Cache automatically trims when workload decreases
- **Idle cleanup**: Workers idle >15 minutes trigger deep cleanup

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

### Testing

- **Single model**: Visit `http://localhost:5000`, disable ensemble, click "Simulate"
- **Ensemble**: Enable ensemble toggle, click "Simulate"
- **Multi mode**: Enable multi toggle, click "Simulate"

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

**Adaptive Caching**:
- Cache automatically expands from 10 → 30 simulators when 10+ ensemble models detected
- Preloading automatically enabled for ensemble workloads
- No explicit "ensemble mode" - system adapts to workload automatically

**Memory Management**:
- Reference counting prevents cleanup of active simulators
- Shared elevation file for ensemble workloads
- Multi-pass GC + `malloc_trim(0)` after cache trims
- Automatic cleanup of old GEFS cycle files

**Progress Tracking**:
- Dual storage: in-memory (fast) + file-based (shared across workers)
- Status: `'downloading'` → `'simulating'`
- Batched updates (every 10 completions) to reduce lock contention
- 30-second cleanup delay for late-connecting SSE clients

---

## License

See LICENSE file for details.
