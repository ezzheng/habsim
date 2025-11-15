# HABSIM – High Altitude Balloon Trajectory Simulator

**Production-ready Flask service for predicting high-altitude balloon trajectories using NOAA GEFS data.**

[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/flask-3.1-green.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Overview
HABSIM powers flight planning for the Stanford Student Space Initiative. A single Flask app (served with Gunicorn) exposes REST endpoints plus an SSE stream for progress updates. Simulations couple a three-phase balloon model with wind fields derived from GEFS ensemble members and GMTED2010 elevation data.

- Single-model predictions finish in seconds once the wind file is cached.
- 21-member ensemble runs add Monte Carlo perturbations (420 extra trajectories by default) and stream progress back to the browser.
- Adaptive caching layers (prediction, simulator, GEFS files) keep RAM and disk usage bounded while enabling warm starts.
- AWS S3 stores authoritative GEFS and elevation assets; local disk caches keep downloads off the hot path.

## Quick Start
### Requirements
- Python 3.13+
- `pip install -r requirements.txt`
- AWS credentials with read/write access to the GEFS bucket (defaults to `habsim-storage` in `us-west-1`)

### Run locally
```bash
git clone <your-fork>
cd habsim
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-1
export S3_BUCKET_NAME=habsim-storage
export SECRET_KEY=$(python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
python app.py  # http://localhost:8000
```

### Smoke tests
```bash
curl http://localhost:8000/health
curl http://localhost:8000/sim/which
curl "http://localhost:8000/sim/singlezpb?timestamp=$(date +%s)&lat=37.3553&lon=-121.8763&alt=24&equil=30000&eqtime=0&asc=4&desc=8&model=0"
```

## Current GEFS Cycle Logic
The simulator refuses to mix forecasts from different GEFS cycles. This section documents the currently implemented logic shared between `simulate.py`, `gefs.py`, and `app.py`.

### Artifacts & signals
- `S3://<bucket>/whichgefs` – tiny text file containing the active cycle (e.g., `2025111312`). `gefs.open_gefs('whichgefs')` always performs a `head_object` first to compare ETags before trusting its 15s in-memory cache.
- `/app/data/currgefs.txt` – shared file (with `fcntl` locks + atomic renames) holding the last known cycle. Workers read it via `_read_currgefs()` and cache the value for 1 second to reduce I/O.
- `_cache_invalidation_cycle` – process-local flag that forces simulator cache entries to prove they were built with the current cycle.
- `/app/data/gefs/*.npz` – disk cache of the 21 model files plus `worldelev.npy`. LRU eviction (max 30 files) runs before every download, excluding the elevation file and any file that is actively downloading.

### Refresh trigger & ownership
`simulate.refresh()` is called whenever:
1. `wait_for_prefetch()` starts an ensemble and `currgefs` is empty/out-of-date.
2. `/sim/refresh` is hit manually (useful after uploading a new cycle).
3. Prefetch detects a cache invalidation while loading simulators.

The function acquires an inter-process lock (`/app/data/currgefs.refresh.lock`) so only one worker performs a refresh sequence at a time.

### Refresh flow
1. Read `whichgefs` and compare to the previous `currgefs`. No change → synchronize `_cache_invalidation_cycle` and exit.
2. If a new timestamp appears, `_check_cycle_files_available()` confirms that all 21 `*.npz` exist and are readable in S3 (with exponential backoff and optional disk cache checks). If files are still uploading, the call returns `(False, pending_cycle)` so the caller can wait.
3. Once verified, `_cache_invalidation_cycle` is set to the new timestamp, the code sleeps for 3 seconds so late-arriving writes settle, then `_write_currgefs()` updates the shared file via atomic rename.
4. `reset(new_cycle)` evicts any idle simulators, clears prediction caches, resets forced-preload hints, and trims disk caches for the old timestamp. In-flight simulators keep running but will fail validation on their next use and rebuild against the new cycle.
5. `_cleanup_old_model_files()` runs asynchronously to delete stale `.npz` files from previous cycles; active downloads are skipped so partially written files are never removed mid-transfer.

### Stability & fallback guards
- `_wait_for_cycle_stable()` (called during prefetch) requires three consecutive matching reads of `_cache_invalidation_cycle` and `currgefs` (~1.5s) before it trusts the cycle.
- `_wait_for_pending_cycle()` polls S3 for up to 120s when `refresh()` reports "pending" so that ensembles never start in the middle of an upload. It double-checks `currgefs` on every loop to piggyback on another worker’s refresh.
- `_acquire_ref_counts_atomic()` locks all requested model IDs, re-validates the cycle both before and after grabbing references, and restarts if anything drifted.
- `_prefetch_model()` runs on every simulator build, re-checking `_cache_invalidation_cycle` both before and after `_get_simulator()` to catch race conditions that occur during long downloads.
- `gefs.open_gefs('whichgefs')` caches the body but always validates the S3 ETag to detect flips instantly without hammering the bucket.

### Failure handling & observability
- Missing files after verification → `refresh()` logs a warning and keeps the old cycle instead of flipping to incomplete data.
- Cycle stabilization timeouts emit warnings but keep the previous stable cycle so requests don’t fail unless no usable cycle exists.
- If both the current and pending cycles are unavailable, prefetch raises a fatal error and callers return `503` to clients. Logs clearly state which cycle was missing to speed up S3 triage.
- Download errors in `gefs.py` clean up temp files, release locks, and remove the file from `_downloading_files` so later retries are not blocked.

## Running the API locally
1. Configure environment variables as shown in the quick start. Optional knobs: `HABSIM_PASSWORD` (frontend gate), `PORT`, and `HABSIM_CACHE_DIR`.
2. Create `data/gefs` if you plan to seed local files; otherwise the app will download from S3 on demand.
3. Start the app with `python app.py` for single-process development or `gunicorn --config gunicorn_config.py app:app` to mimic production (4 workers × 8 threads, 15-minute timeout).
4. Visit `http://localhost:8000`, authenticate if `HABSIM_PASSWORD` is set, pick a launch site on the map, and run "Single" or "Ensemble". SSE updates stream from `/sim/progress-stream` until completion.

## Deployment Notes
- Production deploys run on Railway with a 32GB RAM plan and a persistent volume mounted at `/app/data` so GEFS caches survive restarts.
- Allow at least 10GB on the volume (21 models ≈ 6.5GB + elevation file + temp files).
- Keep `AWS_ACCESS_KEY_ID/SECRET` and `SECRET_KEY` in the platform’s secrets manager; never commit them.
- Gunicorn workers emit health metrics via `/sim/status`; polling every 5s is safe thanks to suppressed access logs.

## Auto-downloader & data prep
`scripts/auto_downloader.py` can run once (`--mode single --cycle 2025111312`) or as a daemon (`--mode daemon`). It fetches GRIB2 products from NOAA NOMADS, converts them to NumPy, packs them into `YYYYMMDDHH_XX.npz`, and uploads them to S3. Production pipelines typically run it as a separate Railway service or cron job every 6 hours so the main app only has to download from S3.

## Project layout
- `app.py` – Flask app + SSE progress stream + ensemble orchestration.
- `simulate.py` – simulator cache, GEFS cycle refresh logic, Monte Carlo orchestration.
- `gefs.py` – S3 access, disk cache, download integrity checks.
- `windfile.py`, `elev.py`, `habsim/classes.py` – physics engine and data access.
- `www/` – static frontend (vanilla JS + Google Maps).

## Contributing
1. Fork, create a feature branch, and run the quick start commands.
2. Add or update tests (where available), run `python app.py` locally, and verify `/health` + `/sim/singlezpb`.
3. Open a PR describing the change, especially if it touches GEFS cycle logic or cache behavior.

## License
Released under the MIT License. See `LICENSE` for the full text.

