import numpy as np 
import math
import gc
import elev
import hashlib
import time
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from windfile import WindFile
from habsim import Simulator, Balloon
from gefs import open_gefs, load_gefs

# Optimized version with memory-efficient caching and performance improvements

EARTH_RADIUS = float(6.371e6)
DATA_STEP = 6 # hrs

### Cache of datacubes and files. ###
### Dynamic multi-simulator LRU cache: small for single model, expands for ensemble ###
_simulator_cache = {}  # {model_id: simulator}
_simulator_access_times = {}  # {model_id: access_time}
MAX_SIMULATOR_CACHE_NORMAL = 5  # Normal cache size for single model runs (~750MB)
MAX_SIMULATOR_CACHE_ENSEMBLE = 25  # Expanded cache for ensemble runs (~3.75GB)
_current_max_cache = MAX_SIMULATOR_CACHE_NORMAL  # Current cache limit (dynamic)
_ensemble_mode_until = 0  # Timestamp when ensemble mode expires
_cache_lock = threading.Lock()  # Thread-safe access
elevation_cache = None

currgefs = "Unavailable"
_last_refresh_check = 0.0

# Prediction cache - increased for 32GB RAM
_prediction_cache = {}
_cache_access_times = {}
MAX_CACHE_SIZE = 200  # Increased from 30 for better hit rate
CACHE_TTL = 3600  # 1 hour

def _cache_key(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient):
    """Generate cache key from prediction parameters"""
    # Round floats to reduce cache misses from tiny differences
    key_str = f"{simtime.timestamp():.0f}_{lat:.4f}_{lon:.4f}_{rate:.2f}_{step}_{max_duration:.1f}_{alt:.1f}_{model}_{coefficient:.3f}"
    return hashlib.md5(key_str.encode()).hexdigest()

def _get_cached_prediction(cache_key):
    """Get cached prediction if available and not expired"""
    if cache_key in _prediction_cache:
        if time.time() - _cache_access_times[cache_key] < CACHE_TTL:
            return _prediction_cache[cache_key]
        else:
            # Expired, remove
            del _prediction_cache[cache_key]
            del _cache_access_times[cache_key]
    return None

def _cache_prediction(cache_key, result):
    """Cache prediction result with TTL and size limit"""
    # Evict oldest if cache is full
    if len(_prediction_cache) >= MAX_CACHE_SIZE:
        oldest_key = min(_cache_access_times, key=_cache_access_times.get)
        del _prediction_cache[oldest_key]
        del _cache_access_times[oldest_key]
    
    _prediction_cache[cache_key] = result
    _cache_access_times[cache_key] = time.time()

def refresh():
    global currgefs
    f = open_gefs('whichgefs')
    s = f.readline()
    f.close()
    if s != currgefs:
        old_currgefs = currgefs  # Save old timestamp for cleanup
        currgefs = s
        reset()
        # Clear cache when model changes
        _prediction_cache.clear()
        _cache_access_times.clear()
        
        # Clean up old model files from disk cache when model changes
        if old_currgefs and old_currgefs != "Unavailable":
            _cleanup_old_model_files(old_currgefs)
        
        return True
    return False

def reset():
    global _simulator_cache, _simulator_access_times, elevation_cache
    with _cache_lock:
        _simulator_cache.clear()
        _simulator_access_times.clear()
    elevation_cache = None
    gc.collect()

def _cleanup_old_model_files(old_timestamp: str):
    """Clean up disk cache files from old model timestamp when model changes"""
    try:
        from pathlib import Path
        import logging
        import gefs
        
        # Access cache directory through gefs module
        # Try to get it from the module, fallback to default if not accessible
        cache_dir = getattr(gefs, '_CACHE_DIR', None)
        if cache_dir is None:
            # Fallback to default path detection (same logic as gefs.py)
            if Path("/app/data").exists():
                cache_dir = Path("/app/data/gefs")
            elif Path("/opt/render/project/src").exists():
                cache_dir = Path("/opt/render/project/src/data/gefs")
            else:
                import tempfile
                cache_dir = Path(tempfile.gettempdir()) / "habsim-gefs"
        
        # Get all files matching old model timestamp pattern (e.g., 2025110306_*.npz)
        old_model_pattern = f"{old_timestamp}_*.npz"
        old_files = list(cache_dir.glob(old_model_pattern))
        
        if old_files:
            logging.info(f"Cleaning up {len(old_files)} old model files from previous timestamp: {old_timestamp}")
            for old_file in old_files:
                try:
                    old_file.unlink()
                    logging.debug(f"Removed old model file: {old_file.name}")
                except Exception as e:
                    logging.warning(f"Failed to remove old model file {old_file.name}: {e}")
        else:
            logging.debug(f"No old model files found for timestamp: {old_timestamp}")
    except Exception as e:
        logging.warning(f"Failed to cleanup old model files (non-critical): {e}")

def _get_elevation_data():
    global elevation_cache
    if elevation_cache is None:
        elevation_cache = load_gefs('worldelev.npy')
    return elevation_cache

def set_ensemble_mode(duration_seconds=600):
    """Enable ensemble mode (larger cache) for specified duration (default 10 minutes)"""
    global _current_max_cache, _ensemble_mode_until
    now = time.time()
    with _cache_lock:
        _current_max_cache = MAX_SIMULATOR_CACHE_ENSEMBLE
        # Extend ensemble mode if it's already active, otherwise set new expiration
        if _ensemble_mode_until > now:
            # Already in ensemble mode, extend it
            _ensemble_mode_until = max(_ensemble_mode_until, now + duration_seconds)
        else:
            # Start new ensemble mode
            _ensemble_mode_until = now + duration_seconds

def _trim_cache_to_normal():
    """Trim cache back to normal size, keeping most recently used models"""
    global _current_max_cache, _simulator_cache, _simulator_access_times, _ensemble_mode_until
    now = time.time()
    
    with _cache_lock:
        # Check if ensemble mode has expired
        if _ensemble_mode_until > 0 and now > _ensemble_mode_until:
            _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
            _ensemble_mode_until = 0
        
        # If cache is too large, trim to normal size keeping most recently used
        if len(_simulator_cache) > _current_max_cache:
            # Sort by access time (most recent first)
            sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
            # Keep only the most recently used models
            models_to_keep = {model_id for model_id, _ in sorted_models[:_current_max_cache]}
            
            # Evict models not in the keep list
            models_to_evict = set(_simulator_cache.keys()) - models_to_keep
            for model_id in models_to_evict:
                del _simulator_cache[model_id]
                del _simulator_access_times[model_id]
            
            if models_to_evict:
                gc.collect()  # Help GC reclaim memory from evicted simulators

def _get_simulator(model):
    """Get simulator for given model, with dynamic multi-simulator LRU cache."""
    global _simulator_cache, _simulator_access_times, _last_refresh_check, _current_max_cache
    now = time.time()
    
    # Refresh GEFS data if needed
    if currgefs == "Unavailable" or now - _last_refresh_check > 300:
        refresh()
        _last_refresh_check = now
    
    # Trim cache if ensemble mode expired
    _trim_cache_to_normal()
    
    with _cache_lock:
        # Fast path: return cached simulator if available
        if model in _simulator_cache:
            _simulator_access_times[model] = now
            return _simulator_cache[model]
        
        # Cache miss - need to load new simulator
        # Evict oldest if cache is full
        if len(_simulator_cache) >= _current_max_cache:
            oldest_model = min(_simulator_access_times, key=_simulator_access_times.get)
            del _simulator_cache[oldest_model]
            del _simulator_access_times[oldest_model]
            gc.collect()  # Help GC reclaim memory from evicted simulator
    
    # Load new simulator (outside lock to avoid blocking other threads)
    wind_file = WindFile(load_gefs(f'{currgefs}_{str(model).zfill(2)}.npz'))
    simulator = Simulator(wind_file, _get_elevation_data())
    
    # Cache it (re-acquire lock)
    with _cache_lock:
        _simulator_cache[model] = simulator
        _simulator_access_times[model] = now
    
    return simulator

# Optimize coordinate transformations with vectorization-ready math
@lru_cache(maxsize=10000)
def _cos_lat_cached(lat_rounded):
    """Cache cosine calculations for repeated latitudes"""
    return math.cos(math.radians(lat_rounded))

def lin_to_angular_velocities(lat, lon, u, v):
    """Convert linear velocities to angular velocities (optimized)"""
    dlat = math.degrees(v / EARTH_RADIUS)
    # Round latitude to nearest 0.01 for caching
    lat_rounded = round(lat * 100) / 100
    cos_lat = _cos_lat_cached(lat_rounded)
    dlon = math.degrees(u / (EARTH_RADIUS * cos_lat)) if cos_lat > 1e-10 else 0.0
    return dlat, dlon

def simulate(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient=1, elevation=True):
    """
    Optimized simulation with caching and early termination
    """
    # Check cache first
    cache_key = _cache_key(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient)
    cached_result = _get_cached_prediction(cache_key)
    if cached_result is not None:
        return cached_result
    
    try:
        simulator = _get_simulator(model)
        balloon = Balloon(location=(lat, lon), alt=alt, time=simtime, ascent_rate=rate)
        traj = simulator.simulate(balloon, step, coefficient, elevation, dur=max_duration)
        
        # Pre-allocate list for better memory efficiency
        path = []
        epoch = datetime(1970, 1, 1).replace(tzinfo=timezone.utc)
        
        for i in traj:
            if i.wind_vector is None:
                raise Exception("alt out of range")
            
            timestamp = (i.time - epoch).total_seconds()
            # Convert numpy types to native Python types for JSON serialization
            path.append((float(timestamp), float(i.location.getLat()), float(i.location.getLon()), 
                        float(i.alt), float(i.wind_vector[0]), float(i.wind_vector[1]), 0, 0))
            
            # Early termination if balloon goes way out of bounds
            lat_check = i.location.getLat()
            lon_check = i.location.getLon()
            if lat_check < -90 or lat_check > 90:
                break
        
        # Cache successful result
        _cache_prediction(cache_key, path)
        
        return path
        
    except Exception as e:
        # Don't cache errors
        raise e

