import numpy as np 
import math
import gc
import elev
import hashlib
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from windfile import WindFile
from habsim import Simulator, Balloon
from gefs import open_gefs, load_gefs

# Optimized version with memory-efficient caching and performance improvements

EARTH_RADIUS = float(6.371e6)
DATA_STEP = 6 # hrs

### Cache of datacubes and files. ###
### Keep at most one simulator resident to stay within tight memory budgets. ###
filecache = None
filecache_model = None
elevation_cache = None

currgefs = "Unavailable"
_last_refresh_check = 0.0

# Lightweight prediction cache (max 50 predictions, ~5-10MB)
_prediction_cache = {}
_cache_access_times = {}
MAX_CACHE_SIZE = 50
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
        currgefs = s
        reset()
        # Clear cache when model changes
        _prediction_cache.clear()
        _cache_access_times.clear()
        return True
    return False

def reset():
    global filecache, filecache_model, elevation_cache
    filecache = None
    filecache_model = None
    elevation_cache = None
    gc.collect()

def _get_elevation_data():
    global elevation_cache
    if elevation_cache is None:
        elevation_cache = load_gefs('worldelev.npy')
    return elevation_cache

def _get_simulator(model):
    global filecache, filecache_model, _last_refresh_check
    now = time.time()
    if currgefs == "Unavailable" or now - _last_refresh_check > 300:
        refresh()
        _last_refresh_check = now
    if filecache_model == model and filecache is not None:
        return filecache

    # Drop the previous simulator to free memory before loading the next one.
    filecache = None
    filecache_model = None
    gc.collect()  # Force garbage collection

    wind_file = WindFile(load_gefs(f'{currgefs}_{str(model).zfill(2)}.npz'))
    filecache = Simulator(wind_file, _get_elevation_data())
    filecache_model = model
    return filecache

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
            path.append((timestamp, i.location.getLat(), i.location.getLon(), 
                        i.alt, i.wind_vector[0], i.wind_vector[1], 0, 0))
            
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
    finally:
        # Aggressive cleanup between simulations to stay under 2GB
        gc.collect()

