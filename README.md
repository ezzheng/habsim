# HABSIM – High Altitude Balloon Trajectory Simulator

**Production-grade web service for predicting high-altitude balloon trajectories using GEFS weather data**

[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/flask-3.1-green.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Developed by the Stanford Student Space Initiative for mission planning and uncertainty quantification. Supports single-model predictions, 21-model ensemble analysis, and Monte Carlo parameter perturbations for probability density mapping.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Features](#features)
4. [System Architecture](#system-architecture)
5. [Installation & Setup](#installation--setup)
6. [Configuration](#configuration)
7. [Running the Application](#running-the-application)
8. [Deployment](#deployment)
9. [Frontend Usage Guide](#frontend-usage-guide)
10. [API Reference](#api-reference)
11. [Backend Architecture](#backend-architecture)
12. [Caching & Optimization](#caching--optimization)
13. [Concurrency & Locking](#concurrency--locking)
14. [Error Handling](#error-handling)
15. [Logging & Monitoring](#logging--monitoring)
16. [Performance Recommendations](#performance-recommendations)
17. [Troubleshooting](#troubleshooting)
18. [FAQ](#faq)
19. [Contributing](#contributing)
20. [License](#license)

---

## Overview

HABSIM is a high-performance web service that simulates high-altitude balloon trajectories using real-world weather data from NOAA's GEFS (Global Ensemble Forecast System). It provides:

- **Single Model Simulations**: Fast trajectory predictions using one weather model (~5-10 seconds)
- **Ensemble Analysis**: Parallel execution of 21 weather models to quantify forecast uncertainty (~5-15 minutes)
- **Monte Carlo Perturbations**: 420 parameter variations for probabilistic landing zone mapping
- **Real-time Progress Tracking**: Server-Sent Events (SSE) for live updates during long-running simulations
- **Intelligent Caching**: Multi-layer caching system with adaptive sizing and LRU eviction
- **Production-Hardened**: Comprehensive error handling, resource leak prevention, and concurrency safety

### Use Cases

- **Mission Planning**: Pre-launch trajectory prediction for balloon missions
- **Uncertainty Quantification**: Ensemble spread analysis for risk assessment
- **Landing Zone Probability**: Monte Carlo analysis for recovery team coordination
- **Educational Research**: Weather model behavior and atmospheric dynamics

---

## Quick Start

### Example: Single Model Simulation

```bash
curl "http://localhost:8000/sim/singlezpb?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0"
```

**Response**: Array of trajectory paths `[[rise_path], [coast_path], [fall_path]]`

### Example: Ensemble + Monte Carlo

```bash
curl "http://localhost:8000/sim/spaceshot?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&num_perturbations=20"
```

**Response**:
```json
{
  "paths": [...],           // 21 ensemble trajectory paths
  "heatmap_data": [...],    // 441 landing positions (21 ensemble + 420 Monte Carlo)
  "request_id": "a1b2c3d4"  // For progress tracking via SSE
}
```

### Example: Track Progress in Real-Time

```bash
curl "http://localhost:8000/sim/progress-stream?request_id=a1b2c3d4"
```

**SSE Stream**:
```
data: {"completed": 100, "total": 441, "percentage": 23, "status": "simulating"}

data: {"completed": 200, "total": 441, "percentage": 45, "status": "simulating"}
```

---

## Features

### Simulation Capabilities

- ✅ **Three-Phase Physics Model**: Ascent (buoyancy-driven) → Coast (equilibrium float) → Descent (parachute)
- ✅ **4D Wind Interpolation**: Bilinear interpolation in latitude, longitude, altitude, and time
- ✅ **Ground Elevation Awareness**: Uses GMTED2010 global elevation data for accurate landing detection
- ✅ **Runge-Kutta Integration**: RK2 (second-order) numerical integration for trajectory calculation
- ✅ **Multi-Model Ensemble**: Runs all 21 GEFS ensemble members in parallel
- ✅ **Monte Carlo Perturbations**: Systematic parameter variations (launch coords, altitude, rates, timing)

### Performance & Reliability

- ✅ **Adaptive Caching**: Auto-expands from 10 → 30 simulators for ensemble workloads
- ✅ **Progressive Prefetch**: Downloads first 12 models, continues rest in background (fast startup)
- ✅ **Idempotent Requests**: Duplicate ensemble requests reuse in-progress results (no duplicate work)
- ✅ **GEFS Cycle Protection**: Dual validation prevents mixing data from different forecast cycles
- ✅ **Automatic Refresh**: Checks for new GEFS cycles every 5 minutes, auto-downloads latest data
- ✅ **Resource Leak Prevention**: File descriptor cleanup, lock management, memory trimming
- ✅ **Race Condition Safety**: File-based locking with fcntl for inter-process coordination

### User Experience

- ✅ **Real-time Progress**: SSE streams provide live updates during ensemble/Monte Carlo runs
- ✅ **Interactive Map Interface**: Google Maps integration with trajectory visualization
- ✅ **Multi-Mode Support**: Sequential simulations at different launch times
- ✅ **Heatmap Visualization**: Probability density map for landing zones
- ✅ **Downloadable Results**: CSV export of landing positions

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Client Browser                           │
│  (www/index.html, www/paths.js, Google Maps API)               │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP/SSE
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│                   Flask Application (app.py)                     │
│  - REST API routing & parameter validation                      │
│  - Ensemble coordination & idempotent deduplication             │
│  - Progressive prefetch (12 models wait, 9 background)          │
│  - Real-time progress tracking (SSE)                            │
│  - GEFS cycle stabilization & validation                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│            Simulation Orchestrator (simulate.py)                 │
│  - Adaptive cache management (10 → 30 simulators)               │
│  - Prediction caching (200 entries, 1hr TTL)                    │
│  - Reference counting for safe eviction                         │
│  - GEFS cycle validation & auto-refresh                         │
│  - Memory management & periodic cleanup                         │
└─────┬───────────────┬────────────────┬──────────────────┬───────┘
      │               │                │                  │
      ↓               ↓                ↓                  ↓
┌──────────┐   ┌─────────────┐   ┌──────────┐   ┌──────────────┐
│ GEFS     │   │ Wind Data   │   │Elevation │   │Physics Engine│
│(gefs.py) │   │(windfile.py)│   │(elev.py) │   │(classes.py)  │
│          │   │             │   │          │   │              │
│S3 access │   │4D wind      │   │Bilinear  │   │Runge-Kutta   │
│LRU cache │   │interpolation│   │interp    │   │integration   │
│Integrity │   │Filter cache │   │451MB file│   │Balloon state │
└──────────┘   └─────────────┘   └──────────┘   └──────────────┘
      │
      ↓
┌─────────────────────────────────────────────────────────────────┐
│              AWS S3 (habsim-storage bucket)                      │
│  - GEFS weather files (~308MB each, 21 ensemble members)        │
│  - whichgefs: Current GEFS cycle timestamp                      │
│  - Auto-downloader updates S3 every 6 hours                     │
└─────────────────────────────────────────────────────────────────┘
```

### Deployment Architecture

**Platform**: Railway (cloud PaaS)
- **Workers**: 4 Gunicorn workers × 8 threads = 32 concurrent requests
- **Resources**: 32GB RAM, 32 vCPUs
- **Storage**: Persistent volume at `/app/data` (disk cache for GEFS files)
- **Timeout**: 15 minutes (ensemble simulations can take 5-15 minutes)

**Data Flow**:
1. Client submits simulation request via REST API
2. Flask validates parameters, checks for duplicate requests
3. Progressive prefetch downloads GEFS files from S3 (first 12 models)
4. Simulation orchestrator loads cached simulators or creates new ones
5. Physics engine runs trajectory calculation (RK2 integration)
6. Results cached and returned to client
7. SSE stream provides real-time progress updates

---

## Installation & Setup

### Prerequisites

- **Python**: 3.13+ (uses modern type hints and performance optimizations)
- **AWS Account**: For S3 access to GEFS weather data
- **Operating System**: Linux (production) or macOS (development)
  - Windows requires WSL2 for proper fcntl file locking support

### Local Development Setup

```bash
# 1. Clone repository
git clone <repository-url>
cd habsim

# 2. Create virtual environment
python3.13 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variables
export AWS_ACCESS_KEY_ID="your-key-id"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_REGION="us-west-1"
export S3_BUCKET_NAME="habsim-storage"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export HABSIM_PASSWORD="your-secure-password"  # Optional

# 5. Create data directories
mkdir -p data/gefs data/progress

# 6. Run development server
python app.py
# Server runs on http://localhost:8000

# Alternative: Run with Gunicorn (production-like)
gunicorn --config gunicorn_config.py app:app
```

### Verify Installation

```bash
# Check server health
curl http://localhost:8000/health
# Expected: {"status": "ok"}

# Check GEFS cycle
curl http://localhost:8000/sim/which
# Expected: {"gefs": "2025111312"} (current GEFS timestamp)

# Check available models
curl http://localhost:8000/sim/models
# Expected: {"models": [0, 1, 2, ..., 20]}
```

---

## Configuration

### Environment Variables

#### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS access key for S3 | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key for S3 | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` |
| `SECRET_KEY` | Flask session secret (32+ bytes) | `<random-hex-string>` |

#### Optional

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | S3 region | `us-west-1` |
| `S3_BUCKET_NAME` | S3 bucket name | `habsim-storage` |
| `HABSIM_PASSWORD` | Password for web interface | `None` (no auth) |
| `PORT` | Server port | `8000` |
| `FLASK_ENV` | Environment mode | `production` |

#### Production Warnings

- **SECRET_KEY**: If not set, sessions invalidate on restart. Set this in production for multi-instance deployments.
- **HABSIM_PASSWORD**: Recommended for public deployments to prevent abuse.
- **AWS Credentials**: Use IAM roles or environment variables, never commit to code.

### S3 Bucket Structure

```
habsim-storage/
├── whichgefs              # Current GEFS cycle (e.g., "2025111312")
├── 2025111312_00.npz      # Ensemble member 0 (~308MB)
├── 2025111312_01.npz      # Ensemble member 1
├── ...
├── 2025111312_20.npz      # Ensemble member 20
└── worldelev.npy          # Global elevation data (451MB, never changes)
```

### Cache Configuration

Located in `/app/data/` (persistent volume on Railway):

```
/app/data/
├── gefs/                  # GEFS file cache (max 30 files, ~9.2GB)
│   ├── 2025111312_00.npz
│   ├── 2025111312_01.npz
│   └── ...
├── progress/              # Progress tracking (shared across workers)
│   ├── a1b2c3d4.json
│   └── ...
├── currgefs.txt           # Current GEFS cycle (shared state)
├── ensemble_counter.txt   # Ensemble mode detection
└── refresh.lock           # Refresh coordination lock
```

**Cache Limits** (configured in `simulate.py` and `gefs.py`):
- Simulator cache: 10 (normal) → 30 (ensemble)
- Prediction cache: 200 entries, 1hr TTL
- GEFS file cache: 30 files (~9.2GB)
- Filter cache: 1000 (normal) → 2000 (ensemble) entries

---

## Running the Application

### Development Mode

```bash
# Single-process Flask server (development only)
python app.py
# Runs on http://localhost:8000
# Auto-reloads on code changes
```

### Production Mode

```bash
# Multi-worker Gunicorn server
gunicorn --config gunicorn_config.py app:app

# Configuration (gunicorn_config.py):
# - Workers: 4
# - Threads per worker: 8
# - Total concurrency: 32
# - Timeout: 900 seconds (15 minutes)
# - Preload: True (faster startup)
```

### Accessing the Web Interface

1. Open browser to `http://localhost:8000`
2. If password is set, enter password to login
3. Use interactive map to select launch location
4. Configure simulation parameters in sidebar
5. Click "Run Single" or "Run Ensemble" to start simulation

---

## Deployment

### Railway Deployment

**Setup**:
1. Create new Railway project
2. Connect GitHub repository
3. Add persistent volume:
   - Mount path: `/app/data`
   - Size: 20GB minimum (stores GEFS cache)
4. Set environment variables (see [Configuration](#configuration))
5. Deploy

**Start Command**: `gunicorn --config gunicorn_config.py app:app`

**Resources**:
- **RAM**: 32GB recommended (ensemble mode uses ~13.8GB per worker)
- **CPU**: 32 vCPUs recommended (parallel simulations benefit from CPU cores)
- **Disk**: 20GB+ for GEFS cache (30 files × ~308MB + 451MB elevation)

### Health Monitoring

Railway automatically monitors:
- `/health` endpoint (returns `{"status": "ok"}`)
- Process crashes (auto-restart)
- Memory usage

### Graceful Shutdown

Gunicorn handles SIGTERM gracefully:
1. Stops accepting new requests
2. Waits for in-progress requests to complete (up to 15 min timeout)
3. Cleans up resources
4. Exits

### Auto-Downloader (Optional)

HABSIM includes an automated GEFS downloader that fetches new cycles from NOAA NOMADS and uploads to S3.

**Location**: `scripts/auto_downloader.py`

**Modes**:
- **Single-cycle**: Downloads one GEFS cycle and exits
- **Daemon**: Continuous monitoring, downloads new cycles every 6 hours

**Usage**:
```bash
# Single cycle
python scripts/auto_downloader.py --mode single --cycle 2025111312

# Daemon mode (recommended for production)
python scripts/auto_downloader.py --mode daemon
```

**Deployment**: Run as separate service (Railway/cron job)

---

## Frontend Usage Guide

### Interface Overview

The web interface provides an interactive Google Maps-based trajectory planner.

**Components**:
- **Map**: Click to set launch location
- **Parameters Sidebar**: Configure simulation settings
- **Mode Selector**: Choose simulation mode (Single/Ensemble/Multi)
- **Results**: Trajectory paths and landing zone heatmap

### Simulation Parameters

| Parameter | Description | Default | Range | Units |
|-----------|-------------|---------|-------|-------|
| **Launch Latitude** | Launch site latitude | 37.3553° | -90° to 90° | Decimal degrees |
| **Launch Longitude** | Launch site longitude | -121.8763° | -180° to 180° | Decimal degrees |
| **Launch Altitude** | Ground elevation at launch | 24 | 0 to 50,000 | Meters |
| **Burst Altitude** | Target altitude before descent | 30,000 | ≥ Launch Alt | Meters |
| **Equilibrium Time** | Float duration at burst altitude | 0 | 0 to 48 | Hours |
| **Ascent Rate** | Vertical climb speed | 4 | 0.1 to 20 | m/s |
| **Descent Rate** | Parachute descent speed | 8 | 0.1 to 20 | m/s |
| **Launch Time** | Simulation start time | Now | Any | Unix timestamp |

### Simulation Modes

#### Single Model Mode
- **Speed**: 5-10 seconds
- **Output**: Single trajectory path (3 phases: ascent → coast → descent)
- **Use Case**: Quick predictions, testing parameters

**How to Run**:
1. Set parameters in sidebar
2. Click "Run Single"
3. View trajectory on map immediately

#### Ensemble Mode
- **Speed**: 5-15 minutes (first run), faster if cached
- **Output**: 21 trajectory paths + 420 Monte Carlo landing positions
- **Use Case**: Uncertainty quantification, mission planning

**How to Run**:
1. Set parameters in sidebar
2. Select number of Monte Carlo perturbations (default: 20)
3. Click "Run Ensemble"
4. Watch progress bar (prefetch → simulation)
5. View trajectory spread and landing zone heatmap

**Perturbations**: System applies small variations to parameters:
- Launch coordinates: ±0.001° (±111m)
- Launch altitude: ±1m
- Burst altitude: ±10m
- Equilibrium time: ±0.1 hours
- Ascent/descent rates: ±0.1 m/s

#### Multi Mode
- **Speed**: Sequential single simulations
- **Output**: Multiple trajectories at different launch times
- **Use Case**: Launch window analysis

**How to Run**:
1. Set base parameters
2. Enter start time and interval
3. Click "Run Multi"
4. View all trajectories overlaid on map

### Interpreting Results

**Trajectory Paths**:
- **Blue line**: Ascent phase (buoyancy-driven climb)
- **Orange line**: Coast phase (equilibrium float at burst altitude)
- **Red line**: Descent phase (parachute descent)

**Landing Zone Heatmap**:
- **Hot colors (red/orange)**: High probability landing zones
- **Cool colors (blue/green)**: Low probability landing zones
- **Spread**: Wider spread = higher forecast uncertainty

**Landing Positions**:
- Click "Download CSV" to export all landing positions
- Format: `latitude, longitude, model_id`

### Tips for Best Results

1. **Launch Time**: Use recent times (within 6 hours) for best forecast accuracy
2. **Burst Altitude**: Typical range 25,000-35,000m for weather balloons
3. **Ascent Rate**: 4-6 m/s typical for latex balloons
4. **Descent Rate**: 5-10 m/s typical with parachute
5. **Ensemble Mode**: Wait for progress bar to reach 100% (can take 5-15 min)
6. **Cached Results**: Identical requests return instantly if recently computed

---

## API Reference

### Simulation Endpoints

#### `GET /sim/singlezpb`

Single model trajectory simulation with three phases (ascent, coast, descent).

**Parameters**:
- `timestamp` (float, required): Unix timestamp for launch time
- `lat` (float, required): Launch latitude in decimal degrees [-90, 90]
- `lon` (float, required): Launch longitude in decimal degrees [-180, 180]
- `alt` (float, required): Launch altitude in meters [0, 50000]
- `equil` (float, required): Burst altitude in meters [≥ alt, < 50000]
- `eqtime` (float, required): Equilibrium float time in hours [0, 48]
- `asc` (float, required): Ascent rate in m/s [0.1, 20]
- `desc` (float, required): Descent rate in m/s [0.1, 20]
- `model` (int, required): GEFS ensemble member [0-20]
- `coeff` (float, optional): Floating coefficient [0.5, 1.5], default: 1.0

**Response** (JSON):
```json
[
  [[lat1, lon1, alt1, time1], ...],  // Ascent phase
  [[lat2, lon2, alt2, time2], ...],  // Coast phase
  [[lat3, lon3, alt3, time3], ...]   // Descent phase
]
```

**Example**:
```bash
curl "http://localhost:8000/sim/singlezpb?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0"
```

**Error Responses**:
- `400 Bad Request`: Invalid parameter values
- `404 Not Found`: GEFS model file not available
- `500 Internal Server Error`: Simulation failure
- `503 Service Unavailable`: GEFS cycle unavailable

---

#### `GET /sim/spaceshot`

Ensemble simulation (21 models) + Monte Carlo perturbations with real-time progress tracking.

**Parameters**: Same as `/sim/singlezpb`, plus:
- `num_perturbations` (int, optional): Monte Carlo perturbations per model [1-100], default: 20

**Response** (JSON):
```json
{
  "paths": [
    [[lat, lon, alt, time], ...],  // Model 0 trajectory
    [[lat, lon, alt, time], ...],  // Model 1 trajectory
    ...
  ],
  "heatmap_data": [
    [lat, lon, "model_0"],           // Model 0 landing
    [lat, lon, "model_1"],           // Model 1 landing
    ...
    [lat, lon, "mc_0_0"],            // Monte Carlo landing
    ...
  ],
  "request_id": "a1b2c3d4e5f6"      // For progress tracking
}
```

**Execution Flow**:
1. **Deduplication**: Checks if identical request is in-progress (up to 15 min)
   - If yes: Waits for existing request and returns same result
   - If no: Proceeds with new simulation
2. **Prefetch Phase**: Downloads GEFS files from S3
   - Downloads first 12 models (wait for completion)
   - Continues downloading models 13-21 in background
   - Status: `"loading"`
3. **Simulation Phase**: Runs trajectories in parallel
   - 21 ensemble models (ThreadPoolExecutor, 10 min timeout)
   - 420 Monte Carlo perturbations (21 models × 20 perturbations each)
   - Status: `"simulating"`
4. **Progress Updates**: Real-time SSE stream (batched every 10 completions)
5. **Result Caching**: Results cached for 1 hour (subsequent identical requests instant)

**Example**:
```bash
curl "http://localhost:8000/sim/spaceshot?timestamp=1763077920&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&num_perturbations=20"
```

---

#### `GET /sim/progress-stream`

Server-Sent Events (SSE) stream for real-time progress tracking.

**Parameters**:
- `request_id` (string, required): Request ID from `/sim/spaceshot` response

**Response** (SSE stream):
```
data: {"completed": 50, "total": 441, "percentage": 11, "status": "loading"}

data: {"completed": 100, "total": 441, "percentage": 23, "status": "simulating"}

data: {"completed": 441, "total": 441, "percentage": 100, "status": "complete"}
```

**Fields**:
- `completed`: Number of completed tasks
- `total`: Total number of tasks (21 ensemble + 420 Monte Carlo = 441)
- `percentage`: Completion percentage (0-100)
- `status`: Current phase
  - `"loading"`: Prefetching GEFS files
- `"simulating"`: Running simulations
  - `"complete"`: All done

**Connection Handling**:
- Stream stays open until completion or client disconnect
- Automatic cleanup after 30 seconds of completion
- Graceful handling of late connections (reads from file cache)

**Example** (JavaScript):
```javascript
const eventSource = new EventSource(`/sim/progress-stream?request_id=${requestId}`);
eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(`Progress: ${data.percentage}% (${data.status})`);
  if (data.percentage === 100) {
    eventSource.close();
  }
};
```

---

### Utility Endpoints

#### `GET /sim/elev`

Ground elevation lookup using GMTED2010 global data.

**Parameters**:
- `lat` (float, required): Latitude [-90, 90]
- `lon` (float, required): Longitude [-180, 180]

**Response** (JSON):
```json
{"elevation": 24.5}  // Meters above sea level
```

**Example**:
```bash
curl "http://localhost:8000/sim/elev?lat=37.3553&lon=-121.8763"
```

---

#### `GET /sim/models`

List of available GEFS ensemble members.

**Response** (JSON):
```json
{"models": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]}
```

---

#### `GET /sim/which`

Current GEFS forecast cycle timestamp.

**Response** (JSON):
```json
{"gefs": "2025111312"}  // YYYYMMDDHH format
```

**Explanation**: GEFS updates every 6 hours (00, 06, 12, 18 UTC)

---

#### `GET /sim/status`

Server health and cache status.

**Response** (JSON):
```json
{
  "status": "ok",
  "memory_mb": 8192,
  "simulator_cache_size": 10,
  "prediction_cache_size": 50,
  "active_requests": 2,
  "gefs_cycle": "2025111312"
}
```

---

#### `GET /sim/cache-status`

Detailed cache diagnostics (for debugging).

**Response** (JSON):
```json
{
  "simulator_cache": {
    "size": 10,
    "max_size": 30,
    "entries": [...]
  },
  "gefs_cache": {
    "files": 21,
    "total_size_mb": 6468
  },
  "prediction_cache": {
    "size": 50,
    "max_size": 200
  }
}
```

---

#### `GET /health`

Health check endpoint (for load balancers/monitoring).

**Response** (JSON):
```json
{"status": "ok"}
```

---

## Backend Architecture

### Core Components

#### `app.py` – Flask Application

**Responsibilities**:
- REST API routing and parameter validation
- Ensemble coordination and idempotent deduplication
- Progressive prefetch orchestration
- Real-time progress tracking (SSE)
- GEFS cycle stabilization

**Key Functions**:
- `singlezpb()`: Single model three-phase simulation
- `spaceshot()`: Ensemble + Monte Carlo coordinator
- `_acquire_inflight_request()`: Prevents duplicate ensemble runs (cross-worker coordination)
- `_complete_inflight_request()`: Publishes results to all waiting clients
- `wait_for_prefetch()`: Progressive prefetch (12 models wait, 9 background)
- `_prefetch_model()`: Prefetches single model with cycle validation
- `_generate_perturbations()`: Generates Monte Carlo parameter variations
- `update_progress()`: Atomic progress updates (in-memory + file-based)
- `_wait_for_cycle_stable()`: Ensures GEFS cycle stability before proceeding (3 consecutive stable readings)

**Progressive Prefetch Strategy**:
- Downloads first 12 models sequentially (guaranteed available)
- Submits models 13-21 for background download
- Simulations start immediately after 12 models ready
- By the time simulations need models 13-21, they're usually cached
- Early abort if 5+ models fail (indicates GEFS cycle change)

**Idempotent Request Handling**:
- Identical requests share computation via `_inflight_ensembles` dict
- First request becomes "owner" and runs computation
- Subsequent requests become "waiters" and receive same result
- Coordination via threading.Event (cross-worker via file-based progress)
- 15-minute timeout for in-progress requests

---

#### `simulate.py` – Simulation Orchestrator

**Responsibilities**:
- Simulator cache management (adaptive sizing)
- Trajectory calculation and prediction caching
- GEFS cycle management and auto-refresh
- Reference counting for safe eviction
- Memory management and periodic cleanup

**Key Functions**:
- `simulate()`: Main simulation entry (cache check → physics → result cache)
- `_get_simulator()`: Retrieves or creates simulator with adaptive caching
- `_validate_simulator_cycle()`: Ensures cached simulator matches current GEFS cycle
- `refresh()`: Checks S3 for new GEFS cycle, updates shared state
- `reset()`: Clears simulator cache when GEFS cycle changes (preserves active ones)
- `get_currgefs()`: Reads current GEFS cycle from shared file (with file locking)
- `_should_preload_arrays()`: Auto-detects ensemble workload (10+ models)
- `_get_target_cache_size()`: Auto-sizes cache (10 normal, 30 ensemble)
- `_cleanup_old_model_files()`: Deletes GEFS files from previous cycle

**Adaptive Cache Management**:
- **Normal Mode**: 10 simulators (~1.5GB memory)
- **Ensemble Mode**: 30 simulators (~13.8GB memory)
- **Auto-detection**: Expands when 10+ different models requested within 60s
- **Reference Counting**: Tracks active simulators to prevent mid-use eviction
- **LRU Eviction**: Evicts least-recently-used unused simulators
- **Shared Elevation**: Single `ElevationFile` instance shared across ensemble

**GEFS Cycle Validation**:
- Extracts GEFS timestamp from `wind_file._source_path` (e.g., `2025111312_00.npz` → `2025111312`)
- Compares with current `currgefs` on every cache access
- Rejects stale simulators (different cycle) and forces reload
- Handles edge cases (missing `_source_path`, invalid filenames)

**Auto-refresh**:
- Background thread checks S3 every 5 minutes for new GEFS cycle
- Updates shared file (`/app/data/currgefs.txt`) with file locking
- Triggers cache invalidation and old file cleanup
- Coordinates across all workers via file-based IPC

---

#### `windfile.py` – Wind Data Access

**Responsibilities**:
- 4D wind interpolation (latitude, longitude, altitude, time)
- NPZ file extraction and memory-mapped access
- Filter cache for interpolation arrays
- Thread-safe file loading

**Access Modes** (auto-detected):
- **Normal Mode**: Memory-mapped (`mmap_mode='r'`) – ~150MB per simulator
- **Ensemble Mode**: Preloaded arrays – ~460MB per simulator (3× memory, 2× faster)

**Features**:
- Extracts NPZ to `.npy` files for memory-mapped access (faster than zipfile)
- Per-file locks prevent zipfile contention during concurrent access
- Filter cache stores interpolation weight arrays (reduces allocations)
- LRU eviction for filter cache (10% at a time, preserves hot entries)
- Stores `_source_path` for GEFS cycle validation

**Interpolation**:
- Trilinear interpolation in 4D space (lat, lon, alt, time)
- Bilinear filters for each dimension
- Cached filter arrays reduce computation by 90%

---

#### `gefs.py` – GEFS File Management

**Responsibilities**:
- Downloads GEFS files from AWS S3
- Disk-based LRU cache management
- File integrity verification
- Concurrent download coordination

**Caching**:
- **Location**: `/app/data/gefs/` (persistent volume)
- **Max Size**: 30 weather files (~9.2GB) + worldelev.npy (451MB, never evicted)
- **Eviction**: LRU (least-recently-used files deleted first)
- **Integrity**: Validates NPZ files before returning

**S3 Configuration**:
- **TransferManager**: Multipart parallel downloads (16 threads per file)
- **Connection Pool**: 64 connections (high concurrency)
- **Download Semaphore**: 16 concurrent downloads (prevents connection exhaustion)
- **Retry Logic**: Exponential backoff (up to 5 retries)
- **Timeout**: Per-download handled by Gunicorn worker timeout (900s)

**Concurrency Control**:
- **File-based Locking** (fcntl): Prevents duplicate downloads across workers
  - Worker 1 acquires lock → downloads file
  - Workers 2-N wait on lock → use completed file
- **Download Tracking**: `_downloading_files` set prevents cleanup of active downloads
- **Cache Lock**: Protects cache metadata during concurrent access

**File Integrity**:
- Validates file size matches S3 metadata
- Validates NPZ structure (can be opened)
- Deletes corrupted files and re-downloads

**Cleanup**:
- Removes old GEFS cycle files when new cycle detected (30s delay)
- Tracks cleanup failures, raises error if >50% fail (prevents disk exhaustion)

---

#### `elev.py` – Elevation Data

**Responsibilities**:
- Ground elevation lookup using GMTED2010 global data
- Bilinear interpolation for smooth elevation values

**Data Source**:
- **File**: `worldelev.npy` (451MB)
- **Resolution**: ~1km (30 arc-seconds)
- **Coverage**: Global (-90° to 90° lat, -180° to 180° lon)

**Features**:
- Bilinear interpolation between grid points
- Automatic coordinate clamping to valid range
- Shared instance for ensemble workloads (memory efficiency)

---

#### `habsim/classes.py` – Physics Engine

**Responsibilities**:
- Balloon state management
- Runge-Kutta integration
- Wind vector interpolation

**Key Classes**:
- `Balloon`: State container (location, altitude, time, wind_vector, trajectory history)
- `Simulator`: RK2 integration engine with wind interpolation
- `Location`: Geographic coordinates with haversine distance calculations
- `ElevationFile`: Ground elevation data access with bilinear interpolation

**Physics Model**:
- **Ascent Phase**: Buoyancy-driven climb at constant ascent rate
- **Coast Phase**: Equilibrium float at burst altitude (wind-driven horizontal movement)
- **Descent Phase**: Parachute descent at constant descent rate
- **Wind Integration**: Runge-Kutta 2nd order (RK2) for accurate trajectory
- **Ground Detection**: Stops simulation when altitude ≤ ground elevation

---

## Caching & Optimization

### Multi-Layer Caching

HABSIM uses four distinct caching layers for optimal performance:

#### 1. Prediction Cache (In-Memory)

**Purpose**: Cache complete simulation results to avoid re-computation.

**Configuration**:
- **Max Size**: 200 entries
- **TTL**: 1 hour (3600 seconds)
- **Eviction**: Oldest entries removed when cache full
- **Key**: MD5 hash of simulation parameters

**Behavior**:
- Cache hit: Return result instantly (<1ms)
- Cache miss: Run simulation, cache result
- Cross-worker: Each worker has own cache (no sharing)

**Memory**: ~1MB (200 entries × ~5KB per result)

---

#### 2. Simulator Cache (In-Memory)

**Purpose**: Cache `Simulator` objects (wind data + elevation data) to avoid reload.

**Configuration**:
- **Normal Mode**: 10 simulators (~1.5GB memory)
- **Ensemble Mode**: 30 simulators (~13.8GB memory)
- **Auto-detection**: Expands at 10+ different models within 60s
- **Eviction**: LRU (least-recently-used unused simulators)
- **Reference Counting**: Active simulators cannot be evicted

**Behavior**:
- Cache hit: Return existing simulator instantly
- Cache miss: Load from disk (1-5s) or download from S3 (5-30s)
- Cycle validation: Rejects stale simulators from old GEFS cycles
- Auto-trim: Shrinks back to 10 when workload decreases

**Memory Breakdown** (per simulator):
- Normal mode: ~150MB (memory-mapped wind data + elevation)
- Ensemble mode: ~460MB (preloaded wind arrays + elevation)

---

#### 3. GEFS File Cache (Disk)

**Purpose**: Cache GEFS files downloaded from S3 to avoid re-download.

**Configuration**:
- **Location**: `/app/data/gefs/`
- **Max Size**: 30 files (~9.2GB) + worldelev.npy (451MB)
- **Eviction**: LRU (oldest files deleted when limit exceeded)
- **Protection**: `worldelev.npy` never evicted (global elevation data)

**Behavior**:
- Cache hit: File read from disk (<1s)
- Cache miss: Download from S3 (5-30s per file)
- Concurrent download: File-based locking prevents duplicate downloads
- Integrity check: Validates NPZ files before use

**Disk Usage**:
- Per file: ~308MB (compressed GEFS data)
- Full cache: ~9.7GB (30 files + elevation)

---

#### 4. Filter Cache (In-Memory, per WindFile)

**Purpose**: Cache interpolation weight arrays to reduce allocations.

**Configuration**:
- **Normal Mode**: 1000 entries per WindFile
- **Ensemble Mode**: 2000 entries (shared across all WindFiles)
- **Eviction**: LRU (remove oldest 10% when full)
- **Key**: Rounded fractional coordinates (lat, lon, alt, time)

**Behavior**:
- Cache hit: Reuse existing filter arrays (no allocation)
- Cache miss: Compute filter arrays (~10µs)
- LRU tracking: Recently used entries moved to end (OrderedDict)

**Memory**: ~35MB total in ensemble mode (2000 entries × 5 arrays × 16 bytes)

---

### Cache Warming Strategies

**Cold Start** (first request after deployment):
- No files cached → downloads from S3 (5-30s per file)
- No simulators cached → loads from disk (1-5s per simulator)
- Total: 30-120s for single simulation, 5-15min for ensemble

**Warm Start** (subsequent requests):
- Files cached on disk → instant load
- Simulators cached in RAM → instant access
- Total: 5-10s for single simulation, 5-10min for ensemble

**Pre-warming** (optional):
```bash
# Warm up model 0 (most common)
curl "http://localhost:8000/sim/singlezpb?timestamp=$(date +%s)&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0"
```

---

### Performance Tuning

**For Faster Single Simulations**:
- Use model 0 (usually cached)
- Use recent timestamps (within 6 hours)
- Avoid cache-busting parameters

**For Faster Ensemble Simulations**:
- Increase `_download_semaphore` in `gefs.py` (more concurrent downloads)
- Increase RAM allocation (allows larger simulator cache)
- Use persistent volume (files survive restarts)

**For Lower Memory Usage**:
- Reduce `MAX_SIMULATOR_CACHE_ENSEMBLE` in `simulate.py`
- Reduce `MAX_CACHE_SIZE` (prediction cache)
- Use memory-mapped mode (disable preloading)

---

## Concurrency & Locking

HABSIM uses a sophisticated concurrency model to coordinate multiple workers and threads safely.

### Thread Safety Guarantees

**Python GIL**: Python's Global Interpreter Lock ensures atomic dict operations but NOT compound operations.

**Threading Primitives Used**:
- `threading.Lock`: Protects shared in-memory state
- `threading.Event`: Coordinates async operations
- `threading.Semaphore`: Limits concurrent operations
- `ThreadPoolExecutor`: Parallel execution
- `fcntl.flock`: Inter-process file locking

---

### Lock Hierarchy (Avoid Deadlocks)

Locks must be acquired in this order:

1. `_cache_invalidation_lock` (simulate.py) – Highest priority
2. `_simulator_ref_lock` (simulate.py)
3. `_cache_lock` (simulate.py)
4. `_progress_lock` (app.py)
5. `_inflight_lock` (app.py)
6. `_whichgefs_lock` (gefs.py)
7. `_downloading_lock` (gefs.py)
8. `_recently_downloaded_lock` (gefs.py)

**Rule**: Never acquire a higher-priority lock while holding a lower-priority lock.

---

### File-Based IPC (Inter-Process Communication)

**Why Needed**: Gunicorn uses multiple worker processes (not threads), so in-memory state is not shared.

**Coordination Files**:

| File | Purpose | Locking |
|------|---------|---------|
| `/app/data/currgefs.txt` | Current GEFS cycle | `fcntl.LOCK_SH` (read), `fcntl.LOCK_EX` (write) |
| `/app/data/ensemble_counter.txt` | Ensemble mode detection | `fcntl.LOCK_EX` (atomic increment) |
| `/app/data/progress/<id>.json` | Progress tracking | Atomic rename (no explicit lock) |
| `/app/data/gefs/.*.lock` | Download coordination | `fcntl.LOCK_EX` (non-blocking) |
| `/app/data/refresh.lock` | Refresh coordination | `fcntl.LOCK_EX` (non-blocking) |

**Pattern**: Write to temp file → fsync → atomic rename
- Atomic rename ensures readers never see partial writes
- fsync ensures data persisted before rename
- File locking coordinates concurrent access

---

### Race Condition Prevention

**1. GEFS Cycle Read/Write Race** (FIXED in C-2):
```python
# Problem: Read can overlap with write, return partial data
# Solution: Use fcntl.LOCK_SH for reads, fcntl.LOCK_EX for writes

# Read (simulate.py:_read_currgefs)
fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # Shared lock
content = f.read()
fcntl.flock(f.fileno(), fcntl.LOCK_UN)

# Write (simulate.py:_write_currgefs)
fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock
f.write(value)
fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

**2. Progress Delete Race** (FIXED in H-2):
```python
# Problem: Delete dict first → readers see stale file data
# Solution: Delete file first → readers see neither or both

# Old (WRONG):
with lock: del dict[id]
os.unlink(file)  # Race: readers see stale file

# New (CORRECT):
os.unlink(file)  # File gone first
with lock: del dict[id]  # Then dict
```

**3. GEFS Cycle Instability** (FIXED in H-3):
```python
# Problem: Single read can be inconsistent during cycle transition
# Solution: Require 3 consecutive stable readings

stable_count = 0
while stable_count < 3:
    cycle = get_currgefs()
    if cycle == invalidation_cycle:
        stable_count += 1
    else:
        stable_count = 0
# Now guaranteed stable for 1.5 seconds
```

**4. File Descriptor Leak** (FIXED in C-1, C-4):
```python
# Problem: Exception during write leaves FD open
# Solution: Explicit close in try-finally

tmp_file = open(temp_path, 'w')
try:
    json.dump(data, tmp_file)
finally:
    tmp_file.close()  # Always close
```

---

### Semaphores & Resource Limits

**Download Semaphore** (`_download_semaphore` in gefs.py):
- **Limit**: 16 concurrent S3 downloads (increased from 8)
- **Purpose**: Prevent connection pool exhaustion
- **Effect**: Blocks until slot available

**Connection Pool** (boto3 TransferManager):
- **Max Connections**: 64
- **Per-download Threads**: 16 (multipart parallel download)
- **Total Capacity**: 64 / 16 = 4 simultaneous full-speed downloads

---

### Timeouts

| Operation | Timeout | Configurable | Consequence |
|-----------|---------|--------------|-------------|
| Ensemble simulation | 600s (10 min) | `app.py:1596` | Abort, return partial results |
| Gunicorn worker | 900s (15 min) | `gunicorn_config.py` | Kill worker, auto-restart |
| Cycle stabilization | 12s | `app.py:266` | Raise error, abort prefetch |
| Pending cycle wait | 120s | `app.py:333` | Fall back to current cycle |
| S3 download | Gunicorn timeout | N/A | Worker killed, download aborted |

---

## Error Handling

### Error Classification

**1. Client Errors (4xx)**:
- Invalid parameters → `400 Bad Request`
- Missing GEFS file → `404 Not Found`
- Authentication failure → `403 Forbidden`

**2. Server Errors (5xx)**:
- Simulation failure → `500 Internal Server Error`
- GEFS cycle unavailable → `503 Service Unavailable`
- Timeout → `504 Gateway Timeout`

---

### Error Responses

All errors return JSON with this structure:
```json
{
  "error": "Descriptive error message",
  "details": "Technical details (development only)"
}
```

**Example** (invalid parameter):
```json
{
  "error": "Parameter 'equil' must be >= 'alt'",
  "details": "equil=10000 < alt=30000"
}
```

---

### Retry Strategy

**Client-Side Retries** (recommended):
- `503 Service Unavailable`: Retry after 5 seconds (GEFS cycle transition)
- `504 Gateway Timeout`: Retry immediately (rare transient failure)
- `500 Internal Server Error`: Do not retry (likely bad parameters)

**Server-Side Retries**:
- S3 downloads: Automatic retry with exponential backoff (up to 5 attempts)
- GEFS cycle checks: Retry with 2-second delay (eventual consistency)
- Model prefetch: Automatic retry on transient failures

---

### Common Error Scenarios

**1. "GEFS cycle unavailable"**
- **Cause**: New GEFS cycle not yet uploaded to S3
- **Resolution**: Wait 5-10 minutes, retry
- **Prevention**: System auto-waits up to 2 minutes

**2. "GEFS cycle failed to stabilize"**
- **Cause**: Race condition during cycle transition
- **Resolution**: Retry immediately (cycle should be stable now)
- **Prevention**: System requires 3 consecutive stable readings

**3. "Model file not found in S3"**
- **Cause**: GEFS cycle incomplete or incorrect timestamp
- **Resolution**: Check `whichgefs` file, verify timestamp
- **Prevention**: System validates all 21 files before proceeding

**4. "Ensemble simulation timed out"**
- **Cause**: Slow S3 downloads or high load
- **Resolution**: Retry (files likely cached now)
- **Prevention**: System aborts after 10 minutes, partial results returned

---

### Logging Errors

All errors logged to stdout with context:
```
ERROR: Model file not found in S3: 2025111312_00.npz. Check S3 storage or verify timestamp.
```

Production deployments should collect logs for analysis.

---

## Logging & Monitoring

### Log Levels

HABSIM uses standard Python logging with these levels:

- **INFO**: Normal operations (startup, cache hits, downloads, GEFS cycle changes)
- **WARNING**: Recoverable issues (cleanup failures, retry attempts, cycle stabilization delays)
- **ERROR**: Fatal issues (simulation failures, file corruption, download errors)

### Log Format

```
INFO: [WORKER 1] Prefetch completed (12 models cached, 0 failed)
WARNING: Failed to delete 3 old files: [...]
ERROR: GEFS cycle failed to stabilize after 12.0s
```

**Prefix Convention**:
- `[WORKER N]`: Worker process ID (Gunicorn worker)
- `INFO:`, `WARNING:`, `ERROR:`: Log level
- Context included in message

### Key Log Messages

**Startup**:
```
INFO: Starting HABSIM Flask app
WARNING: SECRET_KEY not set - sessions will be invalidated on restart
INFO: S3 bucket: habsim-storage, region: us-west-1
```

**GEFS Cycle Management**:
```
INFO: Detected new GEFS cycle: 2025111318 (was: 2025111312)
INFO: Verified 21 GEFS files exist and are readable in S3
INFO: Cache cleared: 10 simulators evicted
INFO: Cleaned up 21 old GEFS files for timestamp 2025111312
```

**Ensemble Execution**:
```
INFO: [WORKER 2] Progressive prefetch started (request: a1b2c3d4)
INFO: [WORKER 2] Prefetch phase 1: waiting for first 12 models
INFO: [WORKER 2] Prefetch completed (12 models cached, 0 failed)
INFO: [WORKER 2] Ensemble simulation completed in 342.5s
```

**S3 Downloads**:
```
INFO: Downloaded 2025111312_00.npz (308.2 MB) in 8.3s
WARNING: Download failed (attempt 1/5): ConnectionError. Retrying in 2s...
ERROR: File not found in S3: 2025111312_00.npz
```

**Cache Management**:
```
INFO: Simulator cache expanded: 10 → 30 (ensemble mode detected)
INFO: Cleaned up 21 old GEFS files for timestamp 2025111312
WARNING: Failed to delete 3 files: [(...)]
```

**Errors**:
```
ERROR: Simulation failed: Invalid burst altitude (equil=10000 < alt=30000)
ERROR: GEFS cycle failed to stabilize after 12.0s
ERROR: Cleanup failed for 15/21 files (71%). This may lead to disk exhaustion.
```

---

### Monitoring Metrics

**Key Metrics to Monitor**:

| Metric | Endpoint | Healthy Range | Alert Threshold |
|--------|----------|---------------|-----------------|
| Memory usage | `/sim/status` | < 28GB | > 30GB |
| Open file descriptors | `lsof -p <pid>` | < 200 | > 512 |
| Disk usage | `df -h /app/data` | < 15GB | > 18GB |
| Active requests | `/sim/status` | < 10 | > 20 |
| Simulator cache size | `/sim/cache-status` | 10-30 | > 30 |
| GEFS cache files | `/sim/cache-status` | < 30 | > 30 |

**Health Check**:
```bash
# Basic health
curl http://localhost:8000/health
# Expected: {"status": "ok"}

# Detailed status
curl http://localhost:8000/sim/status
```

**Disk Space**:
```bash
# Check cache directory size
du -sh /app/data/gefs
# Expected: < 10GB

# Count cached files
ls -1 /app/data/gefs/*.npz | wc -l
# Expected: < 30
```

**File Descriptors**:
```bash
# Count open FDs per worker
lsof -p $(pgrep -f gunicorn) | wc -l
# Expected: < 200 per worker
```

---

### Log Aggregation (Production)

**Railway**: Logs automatically streamed to Railway dashboard
- View in real-time: Railway project → Logs tab
- Search by keyword, filter by severity
- Download logs for analysis

**Custom Setup**:
```bash
# Redirect to file
gunicorn --config gunicorn_config.py app:app 2>&1 | tee -a habsim.log

# Log rotation (logrotate)
/var/log/habsim.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

---

### Alerting (Recommended)

**Critical Alerts**:
1. **Disk Usage > 18GB**: GEFS cache cleanup failing
2. **Memory > 30GB**: Possible memory leak
3. **File Descriptors > 512**: FD leak (restart required)
4. **GEFS Cycle Errors**: New cycle not downloading

**Warning Alerts**:
1. **Active Requests > 20**: High load, consider scaling
2. **Simulator Cache > 30**: Unexpected cache growth
3. **S3 Download Failures**: Network issues or S3 outage

---

## Performance Recommendations

### Single Model Performance

**Target**: 5-10 seconds per simulation

**Optimization Tips**:
1. Use model 0 (usually cached)
2. Use recent timestamps (within 6 hours)
3. Ensure persistent volume for file cache
4. Pre-warm cache with common parameters

**Bottlenecks**:
- Cold start: 30-120s (S3 download)
- Warm start: 5-10s (disk load + physics)
- Hot cache: 1-5s (RAM cache + physics)

---

### Ensemble Performance

**Target**: 5-15 minutes per ensemble

**Optimization Tips**:
1. **Increase Download Concurrency**:
   ```python
   # gefs.py, line 160
   _download_semaphore = threading.Semaphore(16)  # Default: 16
   ```
2. **Increase RAM** (allows larger cache):
   - 32GB: 2 concurrent ensembles
   - 64GB: 4 concurrent ensembles
3. **Use Persistent Volume** (files survive restarts)
4. **Pre-warm Cache**:
```bash
   # Run ensemble once to cache files
   curl ".../sim/spaceshot?..."
   ```

**Bottlenecks**:
- Prefetch: 30-60s (S3 downloads)
- Physics: 5-10s per model (21 models = 105-210s)
- Monte Carlo: 4-14 minutes (420 simulations)

**Parallelism**:
- Prefetch: 16 concurrent downloads (semaphore limit)
- Simulation: 32 concurrent threads (4 workers × 8 threads)

---

### Memory Optimization

**Reduce Memory Usage**:
```python
# simulate.py, lines 37-41
MAX_SIMULATOR_CACHE_NORMAL = 5      # Default: 10
MAX_SIMULATOR_CACHE_ENSEMBLE = 15   # Default: 30

# simulate.py, lines 75-78
MAX_CACHE_SIZE = 100                # Default: 200
```

**Trade-offs**:
- Smaller cache = more cache misses = slower simulations
- Larger cache = higher memory = more concurrent ensembles

**Memory Breakdown** (ensemble mode):
- Simulator cache: ~13.8GB (30 simulators × 460MB)
- Elevation data: ~451MB (shared)
- Filter cache: ~35MB (shared)
- Prediction cache: ~1MB
- Python overhead: ~1GB
- **Total per worker**: ~15GB

---

### Disk Optimization

**Reduce Disk Usage**:
```python
# gefs.py, line 114
_MAX_CACHE_FILES = 20  # Default: 30
```

**Trade-offs**:
- Smaller cache = more S3 downloads = higher egress costs
- Larger cache = faster warmup = lower latency

**Disk Breakdown**:
- GEFS files: ~9.2GB (30 files × 308MB)
- Elevation data: ~451MB
- Progress files: ~1MB (transient)
- **Total**: ~9.7GB

---

### Network Optimization

**Reduce S3 Egress**:
1. Use persistent volume (cache survives restarts)
2. Increase cache size (fewer re-downloads)
3. Run auto-downloader (pre-populates S3)

**Reduce API Latency**:
1. Use CDN for frontend assets
2. Enable CORS caching (`max_age=3600`)
3. Use HTTP/2 for multiplexing

---

### Scaling Recommendations

**Vertical Scaling** (single instance):
- **4GB RAM**: Single simulations only
- **16GB RAM**: 1 ensemble at a time
- **32GB RAM**: 2 concurrent ensembles (recommended)
- **64GB RAM**: 4 concurrent ensembles

**Horizontal Scaling** (multiple instances):
- Shared S3 bucket (all instances read same data)
- Separate persistent volumes (no cache sharing)
- Load balancer with sticky sessions (for SSE)
- Idempotent requests handle cross-instance duplication

---

## Troubleshooting

### Common Issues

#### Issue: "GEFS cycle unavailable"

**Symptoms**:
- API returns `503 Service Unavailable`
- Logs show "GEFS cycle: Unavailable"

**Causes**:
1. New GEFS cycle not yet uploaded to S3
2. S3 connectivity issues
3. Auto-downloader not running

**Resolution**:
```bash
# Check S3 bucket
aws s3 ls s3://habsim-storage/whichgefs

# Verify file exists
aws s3 ls s3://habsim-storage/ | grep 2025111312

# Force refresh
curl http://localhost:8000/sim/refresh
```

---

#### Issue: "Ensemble simulation timed out"

**Symptoms**:
- Ensemble returns partial results
- Logs show "Ensemble simulation timed out after 600s"

**Causes**:
1. Slow S3 downloads (network issues)
2. High CPU load (too many concurrent simulations)
3. Low memory (swapping)

**Resolution**:
```bash
# Check memory usage
free -h

# Check CPU load
top

# Check disk I/O
iostat -x 1

# Increase timeout (gunicorn_config.py)
timeout = 1200  # 20 minutes
```

---

#### Issue: "Memory usage keeps growing"

**Symptoms**:
- Memory usage increases over time
- Eventually hits OOM (Out of Memory)
- Workers killed by Railway

**Causes**:
1. Cache not trimming properly
2. Too many concurrent ensembles
3. Memory leak (unlikely after C-1, H-2 fixes)

**Resolution**:
```bash
# Check cache sizes
curl http://localhost:8000/sim/cache-status

# Force cache clear (restart)
pkill -HUP gunicorn

# Reduce cache size (simulate.py)
MAX_SIMULATOR_CACHE_ENSEMBLE = 15  # Default: 30
```

---

#### Issue: "Disk full"

**Symptoms**:
- `No space left on device` errors
- Old GEFS files not deleted

**Causes**:
1. Cleanup failures (permissions, locks)
2. Cache size too large
3. Persistent volume too small

**Resolution**:
```bash
# Check disk usage
df -h /app/data

# Count cached files
ls -1 /app/data/gefs/*.npz | wc -l

# Manual cleanup (delete old cycles)
rm /app/data/gefs/2025111200_*.npz

# Check logs for cleanup failures
grep "Failed to delete" habsim.log
```

---

#### Issue: "File descriptor limit reached"

**Symptoms**:
- `Too many open files` errors
- Simulations fail randomly

**Causes**:
1. FD leaks in error paths (fixed in C-1, C-4)
2. Too many concurrent downloads
3. System limit too low

**Resolution**:
```bash
# Check open FDs
lsof -p $(pgrep -f gunicorn) | wc -l

# Check system limit
ulimit -n

# Increase limit (temporarily)
ulimit -n 4096

# Increase limit (permanently, /etc/security/limits.conf)
* soft nofile 4096
* hard nofile 8192
```

---

#### Issue: "Progress stream disconnects"

**Symptoms**:
- SSE stream closes prematurely
- Frontend shows "Connection lost"

**Causes**:
1. Load balancer timeout (Railway default: 60s)
2. Browser timeout
3. Progress file deleted too early

**Resolution**:
```bash
# Increase SSE keepalive interval (paths.js)
const keepaliveInterval = 10000;  // 10 seconds

# Increase progress file retention (app.py)
cleanup_delay = 60  # 60 seconds
```

---

### Debugging Tips

**Enable Verbose Logging**:
```python
# app.py, top of file
import logging
logging.basicConfig(level=logging.DEBUG)
```

**Check Cache State**:
```bash
curl http://localhost:8000/sim/cache-status | jq
```

**Monitor Resource Usage**:
```bash
# Memory
watch -n 1 'free -h'

# Disk
watch -n 1 'df -h /app/data'

# File descriptors
watch -n 1 'lsof -p $(pgrep -f gunicorn) | wc -l'

# CPU
htop
```

**Test Single Model** (isolate issues):
```bash
curl "http://localhost:8000/sim/singlezpb?timestamp=$(date +%s)&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0"
```

---

## FAQ

### General

**Q: What is HABSIM used for?**
A: High-altitude balloon trajectory prediction for mission planning, landing zone probability analysis, and educational research.

**Q: How accurate are the predictions?**
A: Accuracy depends on GEFS forecast quality (typically ±10-50km for 24-hour forecasts). Ensemble spread quantifies uncertainty.

**Q: How often does GEFS data update?**
A: GEFS updates every 6 hours (00, 06, 12, 18 UTC). HABSIM auto-refreshes every 5 minutes.

**Q: Can I use historical GEFS data?**
A: Yes, but you must manually upload to S3. Auto-downloader only fetches current cycles.

**Q: Is HABSIM open source?**
A: See LICENSE file for details.

---

### Performance

**Q: Why is the first ensemble so slow?**
A: Cold start requires downloading 21 files (~6.5GB) from S3. Subsequent ensembles use cached files (5-10min).

**Q: Can I speed up ensemble simulations?**
A: Yes – increase `_download_semaphore` (more concurrent downloads), increase RAM (larger cache), or use persistent volume (cache survives restarts).

**Q: Why does memory usage spike during ensemble?**
A: Ensemble mode preloads all wind arrays into RAM for speed (30 simulators × 460MB = 13.8GB). Memory releases after auto-trim.

**Q: How long do cached results last?**
A: Prediction cache: 1 hour. Simulator cache: until GEFS cycle changes or cache eviction. GEFS file cache: indefinite (LRU eviction).

---

### Deployment

**Q: What are minimum system requirements?**
A: 16GB RAM (1 ensemble), 20GB disk (GEFS cache), Linux/macOS (for fcntl), Python 3.13+.

**Q: Can I run on Windows?**
A: Yes, with WSL2 (Windows Subsystem for Linux). Native Windows lacks proper fcntl support.

**Q: Do I need a persistent volume?**
A: Recommended but not required. Without it, cache clears on restart (slower warmup, higher S3 costs).

**Q: Can I scale horizontally?**
A: Yes, but each instance has its own cache (no sharing). Use load balancer with sticky sessions for SSE.

**Q: How do I update to a new version?**
A: Pull latest code, restart Gunicorn. Cache survives restarts (persistent volume).

---

### Troubleshooting

**Q: Why am I getting "GEFS cycle unavailable"?**
A: New cycle not yet uploaded to S3. Wait 5-10 minutes or check auto-downloader logs.

**Q: Why did my ensemble timeout?**
A: Slow S3 downloads or high load. Retry (files likely cached now) or increase timeout.

**Q: Why is disk usage growing?**
A: Old GEFS files not deleted. Check cleanup logs for errors or manually delete old cycles.

**Q: Why are simulations failing randomly?**
A: Possible FD leak. Check `lsof` count and restart workers if > 512.

**Q: Why is the progress bar stuck?**
A: SSE connection dropped. Refresh page to reconnect.

---

## Contributing

### Development Workflow

1. **Fork repository**
2. **Create feature branch**: `git checkout -b feature/amazing-feature`
3. **Make changes**: Follow code style, add tests
4. **Test locally**: Run full test suite
5. **Commit changes**: `git commit -m "Add amazing feature"`
6. **Push to branch**: `git push origin feature/amazing-feature`
7. **Open Pull Request**: Describe changes, link issues

### Code Style

- **Python**: Follow PEP 8 (use `black` formatter)
- **JavaScript**: Use ES6+, 2-space indent
- **Comments**: Explain *why*, not *what*
- **Type Hints**: Use Python 3.13+ type annotations

### Testing

**Run Tests**:
```bash
# Unit tests (if available)
pytest tests/

# Integration test (single simulation)
curl "http://localhost:8000/sim/singlezpb?..."

# Load test (ensemble simulation)
curl "http://localhost:8000/sim/spaceshot?..."
```

**Test Coverage**:
- Single model simulation (all models 0-20)
- Ensemble simulation (various perturbation counts)
- GEFS cycle transition (manual trigger)
- Error handling (invalid parameters)
- Cache behavior (hit/miss scenarios)

### Safe Extension Points

**Add New Parameter**:
1. Add to `get_arg()` validation in `app.py`
2. Pass through to `simulate.simulate()`
3. Use in `Simulator` physics (if needed)
4. Document in API Reference

**Add New Endpoint**:
1. Define route in `app.py`
2. Use `get_arg()` for parameter validation
3. Return JSON response
4. Document in API Reference

**Add New Cache Layer**:
1. Define in appropriate module (`simulate.py`, `gefs.py`, etc.)
2. Use LRU eviction (OrderedDict)
3. Add cleanup logic
4. Document in Caching section

**Modify Physics**:
1. Edit `habsim/classes.py:Simulator`
2. Test with known trajectories (regression tests)
3. Document changes in docstrings

---

## License

See [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- **Stanford Student Space Initiative**: Project funding and requirements
- **NOAA NOMADS**: GEFS weather data source
- **GMTED2010**: Global elevation data
- **Flask**: Web framework
- **boto3**: AWS S3 SDK
- **NumPy**: Scientific computing

---

## Contact

For questions, issues, or contributions:
- **GitHub Issues**: [Create an issue](https://github.com/...)
- **Email**: [Contact team]

---

**Built with ❤️ by the Stanford Student Space Initiative**
