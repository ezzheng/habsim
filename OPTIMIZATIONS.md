# HABSIM Optimizations

Technical reference for optimizations implemented for Render (2GB RAM, 1 CPU).

## Deploy to Render

**Start Command:**
```bash
gunicorn --config gunicorn_config.py app:app
```

## Code Optimizations

### 1. Cache Pre-warming (`app.py`)
- Model 0 loads on startup in background thread (`_prewarm_cache()`)
- Eliminates cold start delay on first request
- Memory: ~150MB

### 2. Parallel Execution (`app.py`)
- `ThreadPoolExecutor` with 2 workers for ensemble mode
- I/O-bound tasks benefit from threading vs multiprocessing
- `with ThreadPoolExecutor(max_workers=2)` in `/sim/spaceshot`

### 3. HTTP Response Caching (`app.py`)
- `@cache_for(600)` decorator adds `Cache-Control` headers
- Browser caches results for 10 minutes
- Applied to all simulation endpoints

### 4. Response Compression (`requirements.txt`, `app.py`)
- `flask-compress` automatically gzips responses
- Typical 10x reduction (500KB → 50KB)

### 5. Prediction Result Cache (`simulate.py`)
- `_prediction_cache` dict with LRU eviction
- 30 predictions max (~6MB), 1hr TTL
- Hash-based lookup: `_cache_key()` generates MD5 from parameters
- Check `_get_cached_prediction()` before running simulation

### 6. GEFS File Cache (`gefs.py`)
- `_MAX_CACHED_FILES = 3` keeps 3 most recent files (~450MB)
- `_cleanup_old_cache_files()` evicts by access time
- `cache_path.touch()` updates access time on use

### 7. Math Caching (`windfile.py`, `simulate.py`)
- `@lru_cache(maxsize=10000)` on `_alt_to_hpa_cached()`, `_cos_lat_cached()`
- Caches repeated coordinate transformations
- Memory: <1MB

### 8. Gunicorn Config (`gunicorn_config.py`)
- 2 workers, 2 threads each = 4 concurrent requests
- `preload_app = True` shares memory between workers
- `max_requests = 800` recycles workers to prevent leaks

**Key Settings:**
- `workers = 2` in `gunicorn_config.py`
- `threads = 2` in `gunicorn_config.py`
- `_MAX_CACHED_FILES = 3` in `gefs.py`
- `MAX_CACHE_SIZE = 30` in `simulate.py`

## UI Changes

### Ensemble Toggle (`www/index.html`, `www/paths.js`)
- Button between "Simulate" and "Waypoints"
- Default: OFF (runs model 0 only)
- Enabled: Emerald green border (`border-color: #10b981`), runs models 0, 1, 2
- State: `window.ensembleEnabled` boolean
- Logic: `paths.js` checks state, sets `modelIds = ensembleEnabled ? [0,1,2] : [0]`

## Tuning

### Increase Cache (if more RAM available)
- `MAX_CACHE_SIZE` in `simulate.py`: 30 → 50 (~10MB)
- `_MAX_CACHED_FILES` in `gefs.py`: 3 → 5 (~750MB)

### Increase Workers (if upgraded to 4GB RAM)
- `workers` in `gunicorn_config.py`: 2 → 4
- `threads` in `gunicorn_config.py`: 2 → 3

