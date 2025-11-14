"""
Simulation orchestrator with dynamic LRU cache management.

Manages simulator cache (expands for ensemble mode), prediction caching,
GEFS data refresh, and periodic cache trimming. Provides simulate() function
for trajectory calculations using Runge-Kutta integration.
"""
import numpy as np 
import math
import gc
import elev
import hashlib
import os
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import tempfile
from windfile import WindFile
from habsim import Simulator, Balloon
from gefs import open_gefs, load_gefs

# Try to import psutil for memory monitoring (optional)
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

EARTH_RADIUS = float(6.371e6)
DATA_STEP = 6

# Dynamic multi-simulator LRU cache: automatically expands based on workload
_simulator_cache = {}
_simulator_access_times = {}
MAX_SIMULATOR_CACHE_NORMAL = 10
MAX_SIMULATOR_CACHE_ENSEMBLE = 30
_current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
_cache_lock = threading.Lock()
elevation_cache = None
_elevation_lock = threading.Lock()
# Shared ElevationFile instance for ensemble workloads (all simulators use same elevation data)
_shared_elevation_file = None
_shared_elevation_file_lock = threading.Lock()

# Shared currgefs storage across workers (file-based on persistent volume)
_CURRGEFS_FILE = None
if Path("/app/data").exists():  # Railway persistent volume
    _CURRGEFS_FILE = Path("/app/data/currgefs.txt")
else:
    _CURRGEFS_FILE = Path(tempfile.gettempdir()) / "habsim-currgefs.txt"
_last_refresh_check = 0.0
_cache_trim_thread_started = False
_IDLE_RESET_TIMEOUT = 120.0
_IDLE_CLEAN_COOLDOWN = 120.0
_last_activity_timestamp = time.time()
_last_idle_cleanup = 0.0
_idle_cleanup_lock = threading.Lock()

# Simulator reference counting for safe cleanup
_simulator_ref_counts = {}  # model_id -> count
_simulator_ref_lock = threading.Lock()

_prediction_cache = {}
_cache_access_times = {}
MAX_CACHE_SIZE = 200
CACHE_TTL = 3600

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
    """Cache prediction result with TTL and size limit. Lock-free for performance."""
    # Evict oldest if cache is full
    if len(_prediction_cache) >= MAX_CACHE_SIZE:
        try:
            oldest_key = min(_cache_access_times, key=_cache_access_times.get)
            del _prediction_cache[oldest_key]
            del _cache_access_times[oldest_key]
        except (ValueError, KeyError, RuntimeError):
            # Race condition during concurrent access - safe to ignore
            # Another thread may have deleted the key already
            pass
    
    # Insert new entry (dict operations are atomic in CPython)
    _prediction_cache[cache_key] = result
    _cache_access_times[cache_key] = time.time()

def _read_currgefs():
    """Read currgefs from shared file (thread-safe, process-safe)."""
    try:
        if _CURRGEFS_FILE.exists():
            with open(_CURRGEFS_FILE, 'r') as f:
                content = f.read().strip()
                if content:
                    return content
    except Exception:
        pass
    return "Unavailable"

def _write_currgefs(value):
    """Write currgefs to shared file using atomic write (thread-safe, process-safe)."""
    try:
        _CURRGEFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file, then rename (atomic on most filesystems)
        temp_file = _CURRGEFS_FILE.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())  # Ensure written to disk
        # Atomic rename (replaces existing file atomically)
        temp_file.replace(_CURRGEFS_FILE)
    except Exception:
        pass

def refresh():
    """Refresh GEFS timestamp from S3 and update shared file."""
    try:
        f = open_gefs('whichgefs')
        new_gefs = f.readline().strip()
        f.close()
        
        old_gefs = _read_currgefs()
        if new_gefs and new_gefs != old_gefs:
            _write_currgefs(new_gefs)
            reset()
            _prediction_cache.clear()
            _cache_access_times.clear()
            
            if old_gefs and old_gefs != "Unavailable":
                _cleanup_old_model_files(old_gefs)
            
            return True
    except Exception as e:
        print(f"ERROR: refresh() failed: {e}", flush=True)
    return False

def get_currgefs():
    """Get current GEFS timestamp (reads from shared file)."""
    return _read_currgefs()

def reset():
    """Clear simulator cache when GEFS model changes.
    Respects in-use models to prevent breaking active simulations."""
    global _simulator_cache, _simulator_access_times, elevation_cache, _current_max_cache
    
    with _cache_lock:
        # Clear access times for models not in use
        models_to_clear = []
        for model_id in _simulator_cache.keys():
            if not _is_simulator_in_use(model_id):
                models_to_clear.append(model_id)
        
        for model_id in models_to_clear:
            simulator = _simulator_cache.get(model_id)
            if simulator:
                _cleanup_simulator_safely(simulator)
            if model_id in _simulator_cache:
                del _simulator_cache[model_id]
            if model_id in _simulator_access_times:
                del _simulator_access_times[model_id]
            with _simulator_ref_lock:
                _simulator_ref_counts.pop(model_id, None)
        
        # Reset cache size to normal
        _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
    
    # Clear elevation cache (will be reloaded on next use)
    elevation_cache = None
    
    # Light garbage collection
    gc.collect()


def record_activity():
    """Record that the worker handled a request (used for idle cleanup)."""
    global _last_activity_timestamp
    old_timestamp = _last_activity_timestamp
    _last_activity_timestamp = time.time()


def _idle_memory_cleanup(idle_duration):
    """Deep cleanup when the worker has been idle for a while.
    Returns True if cleanup ran, False if skipped (lock held or models in use).
    
    Very conservative: only runs if truly idle (no models in use, idle > 15 minutes).
    """
    global _current_max_cache, elevation_cache
    worker_pid = os.getpid()
    if not _idle_cleanup_lock.acquire(blocking=False):
        return False
    try:
        with _cache_lock:
            cache_size = len(_simulator_cache)
            # Check if any models are in use via ref counts
            models_in_use = any(_is_simulator_in_use(mid) for mid in _simulator_cache.keys())
        
        # Skip cleanup if models are in use or idle time is too short
        if models_in_use or idle_duration < 900:  # Require 15 minutes of true idle
            return False
        
        rss_before = _get_rss_memory_mb()
        if rss_before is not None:
            pass
        
        with _cache_lock:
            simulators = list(_simulator_cache.values())
            _simulator_cache.clear()
            _simulator_access_times.clear()
            _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
        
        # Clear ref counts
        with _simulator_ref_lock:
            _simulator_ref_counts.clear()
        
        # Clean up simulator resources outside the cache lock
        evicted = 0
        for simulator in simulators:
            if simulator is None:
                continue
            try:
                _cleanup_simulator_safely(simulator)
                evicted += 1
            except Exception:
                pass
        simulators.clear()
        
        # IMPORTANT: Use global keyword for module-level variables
        # Clear elevation cache to free more memory (thread-safe)
        with _elevation_lock:
            elevation_cache = None  # Note: This is a module global, assigned here
        
        # Clear prediction cache
        _prediction_cache.clear()
        _cache_access_times.clear()
        
        # Clear currgefs file to force re-check on next request
        _write_currgefs("")

        # Multiple aggressive GC passes to ensure numpy arrays are freed
        for _ in range(10):  # Increased from 5 to 10
            gc.collect()
            gc.collect(generation=2)
        
        # Force memory release back to OS when available
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
        except Exception:
            pass

        rss_after = _get_rss_memory_mb()
        if rss_after is not None and rss_before is not None:
            rss_delta = rss_before - rss_after
            print(f"INFO: [WORKER {worker_pid}] Idle cleanup: {evicted} simulators evicted, {rss_delta:.1f} MB freed, cache_size={cache_size}→0", flush=True)
        return True
    finally:
        _idle_cleanup_lock.release()

def _cleanup_old_model_files(old_timestamp: str):
    """Clean up disk cache files from old model timestamp when model changes.
    This is critical to prevent disk bloat when GEFS cycles change every 6 hours."""
    try:
        from pathlib import Path
        import logging
        
        # Import gefs module to access its cache directory
        from gefs import _CACHE_DIR
        
        if not _CACHE_DIR or not _CACHE_DIR.exists():
            return
        
        # Remove the trailing newline if present (currgefs sometimes has \n)
        old_timestamp = old_timestamp.strip()
        
        # Get all files matching old model timestamp pattern
        # Pattern: YYYYMMDDHH_NN.npz (e.g., 2025110306_00.npz)
        # CRITICAL: Also match extracted .data.npy files to prevent orphan accumulation
        old_model_pattern = f"{old_timestamp}_*.npz"
        old_files = list(_CACHE_DIR.glob(old_model_pattern))
        
        # CRITICAL: Also delete extracted .data.npy files (322MB each)
        # Without this, every GEFS cycle leaves 6.7GB of orphaned files
        extracted_pattern = f"{old_timestamp}_*.npz.data.npy"
        extracted_files = list(_CACHE_DIR.glob(extracted_pattern))
        old_files.extend(extracted_files)
        
        # Also check for files without model number (e.g., whichgefs)
        other_patterns = [f"{old_timestamp}.npz", f"{old_timestamp}"]
        for pattern in other_patterns:
            matching = list(_CACHE_DIR.glob(pattern))
            old_files.extend(matching)
        
        if old_files:
            total_size = sum(f.stat().st_size for f in old_files if f.exists()) / (1024**3)  # GB
            
            deleted_count = 0
            for old_file in old_files:
                try:
                    if old_file.exists():
                        old_file.unlink()
                        deleted_count += 1
                except Exception:
                    pass
            
        # After deleting old files, also trigger the LRU cleanup to ensure we're under limits
        # This helps when the cache has accumulated files over time
        try:
            from gefs import _cleanup_old_cache_files
            _cleanup_old_cache_files()
        except:
            pass
            
    except Exception as e:
        print(f"ERROR: Failed to cleanup old model files: {e}", flush=True)

def _get_elevation_data():
    """Get cached elevation data with thread-safe initialization.
    Prevents race conditions during concurrent access."""
    global elevation_cache
    # Double-checked locking pattern for performance
    if elevation_cache is None:
        with _elevation_lock:
            # Check again inside lock (another thread might have initialized it)
            if elevation_cache is None:
                elevation_cache = load_gefs('worldelev.npy')
    return elevation_cache

def _should_preload_arrays():
    """Auto-detect if we should preload arrays based on cache size.
    Preloading is faster (CPU-bound) but uses more memory.
    Returns True if cache has many models (ensemble workload), False otherwise."""
    with _cache_lock:
        # If we have 10+ models cached, we're likely doing ensemble work
        # Preload arrays for better performance in this case
        ensemble_models = len([m for m in _simulator_cache.keys() if isinstance(m, int) and m < 21])
        return ensemble_models >= 10

def _get_target_cache_size():
    """Auto-size cache based on current usage patterns.
    Returns target cache size (normal or ensemble) based on how many models are cached.
    NOTE: Must be called while holding _cache_lock."""
    ensemble_models = len([m for m in _simulator_cache.keys() if isinstance(m, int) and m < 21])
    if ensemble_models >= 10:
        return MAX_SIMULATOR_CACHE_ENSEMBLE
    return MAX_SIMULATOR_CACHE_NORMAL

def _update_cache_size():
    """Update cache size limit based on current workload (adaptive sizing)."""
    global _current_max_cache
    with _cache_lock:
        _current_max_cache = _get_target_cache_size()

def _get_rss_memory_mb():
    """Get current RSS memory usage in MB (if psutil available)"""
    if _PSUTIL_AVAILABLE:
        try:
            process = psutil.Process()
            return process.memory_info().rss / (1024 * 1024)
        except:
            pass
    return None

def _cleanup_simulator_safely(simulator):
    """Safely clean up a simulator object, breaking all references to numpy arrays.
    This is called after a delay to ensure the simulator is definitely not in use."""
    if simulator is None:
        return
    try:
        # Clear WindFile data (pre-loaded arrays are the main memory consumers)
        if hasattr(simulator, 'wind_file') and simulator.wind_file:
            wind_file = simulator.wind_file
            try:
                if hasattr(wind_file, 'cleanup'):
                    wind_file.cleanup()
                else:
                    # Manual cleanup fallback - explicitly break all references
                    if hasattr(wind_file, 'data') and wind_file.data is not None:
                        if isinstance(wind_file.data, np.ndarray):
                            # Explicitly clear the array
                            wind_file.data.setflags(write=True)
                            wind_file.data = None
                    if hasattr(wind_file, 'levels') and isinstance(wind_file.levels, np.ndarray):
                        wind_file.levels.setflags(write=True)
                        wind_file.levels = None
                    # Clear any other numpy array attributes
                    for attr_name in dir(wind_file):
                        if not attr_name.startswith('_'):
                            attr = getattr(wind_file, attr_name, None)
                            if isinstance(attr, np.ndarray):
                                try:
                                    attr.setflags(write=True)
                                    setattr(wind_file, attr_name, None)
                                except:
                                    pass
            finally:
                # Break all references
                simulator.wind_file = None
                del wind_file
        # Clear any other references in simulator
        if hasattr(simulator, 'elevation_file'):
            simulator.elevation_file = None
    except Exception:
        pass

def _acquire_simulator_ref(model_id):
    """Acquire a reference to a simulator (increment ref count)."""
    with _simulator_ref_lock:
        _simulator_ref_counts[model_id] = _simulator_ref_counts.get(model_id, 0) + 1

def _release_simulator_ref(model_id):
    """Release a reference to a simulator (decrement ref count)."""
    with _simulator_ref_lock:
        count = _simulator_ref_counts.get(model_id, 0)
        if count > 1:
            _simulator_ref_counts[model_id] = count - 1
        else:
            _simulator_ref_counts.pop(model_id, None)

def _is_simulator_in_use(model_id):
    """Check if simulator has active references."""
    with _simulator_ref_lock:
        return _simulator_ref_counts.get(model_id, 0) > 0

def _trim_cache_to_normal():
    """Trim cache to target size, keeping most recently used models.
    Uses reference counting to ensure simulators are only cleaned up when not in use.
    Automatically adjusts target size based on workload (adaptive sizing)."""
    global _current_max_cache, _simulator_cache, _simulator_access_times
    
    # Update cache size based on current workload
    _update_cache_size()
    
    with _cache_lock:
        target_size = _current_max_cache
        cache_size = len(_simulator_cache)
        
        # If cache is too large, trim to target size keeping most recently used
        if cache_size > target_size:
            # Sort by access time (most recent first)
            sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
            # Keep only the most recently used models
            models_to_keep = {model_id for model_id, _ in sorted_models[:target_size]}
            
            # Evict models not in the keep list
            models_to_evict = set(_simulator_cache.keys()) - models_to_keep
            
            # CRITICAL: Do not evict models with active references
            models_to_evict = {m for m in models_to_evict if not _is_simulator_in_use(m)}
            
            evicted_count = len(models_to_evict)
            
            # Remove from cache and clean up immediately (simulators not in use are safe to clean)
            for model_id in models_to_evict:
                simulator = _simulator_cache.get(model_id)
                if simulator:
                    # Final safety check: verify no active references
                    if not _is_simulator_in_use(model_id):
                        _cleanup_simulator_safely(simulator)
                del _simulator_cache[model_id]
                if model_id in _simulator_access_times:
                    del _simulator_access_times[model_id]
                # Clear ref count if exists
                with _simulator_ref_lock:
                    _simulator_ref_counts.pop(model_id, None)
            
            if evicted_count > 0:
                gc.collect()  # Help GC reclaim memory

def _periodic_cache_trim():
    """Background thread that periodically trims cache and handles idle cleanup.
    This ensures idle workers trim their cache even if they don't receive requests.
    Without this, each worker process maintains its own cache, and idle workers never trim.
    """
    consecutive_trim_failures = 0
    global _last_idle_cleanup
    while True:
        try:
            now = time.time()
            idle_duration = now - _last_activity_timestamp
            
            # Fix: Handle case where cleanup never ran (_last_idle_cleanup = 0)
            if _last_idle_cleanup == 0:
                time_since_last_cleanup = float('inf')
            else:
                time_since_last_cleanup = now - _last_idle_cleanup
            
            # Very conservative: only run cleanup if idle > 15 minutes and no models in use
            # This prevents cleanup during active work
            should_run_idle_cleanup = (idle_duration >= 900 and 
                                      (time_since_last_cleanup >= _IDLE_CLEAN_COOLDOWN or _last_idle_cleanup == 0))
            
            if should_run_idle_cleanup:
                try:
                    cleanup_ran = _idle_memory_cleanup(idle_duration)
                    if cleanup_ran:
                        _last_idle_cleanup = time.time()
                except Exception as cleanup_error:
                    print(f"ERROR: Idle cleanup failed: {cleanup_error}", flush=True)
                    _last_idle_cleanup = time.time()
                consecutive_trim_failures = 0
                time.sleep(5)
                continue
            
            # Regular cache trimming - check if cache is too large and trim if needed
            with _cache_lock:
                cache_size = len(_simulator_cache)
                target_size = _get_target_cache_size()  # Must be called while holding lock
            
            # If cache is larger than target, trim it
            if cache_size > target_size:
                # Check if any models are currently in use before trimming
                models_in_use = any(_is_simulator_in_use(mid) for mid in _simulator_cache.keys())
                
                if not models_in_use:
                    rss_before = _get_rss_memory_mb()
                    _trim_cache_to_normal()
                    gc.collect()
                    
                    # Force additional GC passes to ensure memory is released
                    for _ in range(3):
                        gc.collect()
                        gc.collect(generation=2)
                    try:
                        import ctypes
                        libc = ctypes.CDLL("libc.so.6")
                        libc.malloc_trim(0)
                    except:
                        pass
                    
                    # Check immediately after trimming to see if it worked
                    with _cache_lock:
                        new_size = len(_simulator_cache)
                    rss_after = _get_rss_memory_mb()
                    
                    if new_size > target_size:
                        consecutive_trim_failures += 1
                        if rss_after is not None and rss_before is not None:
                            rss_delta = rss_before - rss_after
                            print(f"WARNING: Cache trim failed #{consecutive_trim_failures}: {new_size} > {target_size}", flush=True)
                        if consecutive_trim_failures > 2:
                            print(f"WARNING: Multiple trim failures, forcing aggressive cleanup", flush=True)
                            _force_aggressive_trim()
                            consecutive_trim_failures = 0
                    else:
                        consecutive_trim_failures = 0
                    time.sleep(3)
                else:
                    time.sleep(20)
            else:
                # Normal check interval - always call trim to handle edge cases
                _trim_cache_to_normal()
                consecutive_trim_failures = 0
                time.sleep(20)
        except Exception as e:
            print(f"ERROR: Cache trim thread error: {e}", flush=True)
            # If idle for a very long time and cleanup hasn't run, force it
            now_error = time.time()
            idle_duration_error = now_error - _last_activity_timestamp
            if idle_duration_error > 600 and _last_idle_cleanup == 0:
                try:
                    _idle_memory_cleanup(idle_duration_error)
                    _last_idle_cleanup = time.time()
                except Exception as emergency_error:
                    print(f"ERROR: Emergency cleanup failed after {idle_duration_error:.1f}s idle: {emergency_error}", flush=True)
                    _last_idle_cleanup = time.time()
            time.sleep(10)

def _force_aggressive_trim():
    """Force aggressive cache trimming - removes all but 1 most recently used simulator"""
    global _simulator_cache, _simulator_access_times, _current_max_cache
    with _cache_lock:
        if len(_simulator_cache) <= 1:
            return
        
        cache_size_before = len(_simulator_cache)
        
        # Sort by access time (most recent first)
        sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
        
        # Keep only the most recently used model
        model_to_keep = sorted_models[0][0] if sorted_models else None
        
        # Evict all others (skip if in use)
        for model_id in list(_simulator_cache.keys()):
            if _is_simulator_in_use(model_id):
                continue
            if model_id != model_to_keep:
                simulator = _simulator_cache.get(model_id)
                if simulator:
                    # Clear WindFile data (pre-loaded arrays are the main memory consumers)
                    if hasattr(simulator, 'wind_file') and simulator.wind_file:
                        wind_file = simulator.wind_file
                        # Use WindFile's cleanup method if available
                        if hasattr(wind_file, 'cleanup'):
                            wind_file.cleanup()
                        else:
                            # Manual cleanup fallback
                            if hasattr(wind_file, 'data') and wind_file.data is not None:
                                if isinstance(wind_file.data, np.ndarray) and not hasattr(wind_file.data, 'filename'):
                                    del wind_file.data
                                    wind_file.data = None
                            if hasattr(wind_file, 'levels') and isinstance(wind_file.levels, np.ndarray):
                                del wind_file.levels
                                wind_file.levels = None
                        # Break reference to WindFile
                        del wind_file
                        simulator.wind_file = None
                    # Break reference to Simulator
                    del simulator
                del _simulator_cache[model_id]
                del _simulator_access_times[model_id]
        
        _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
        
        # Multiple GC passes
        for _ in range(5):
            gc.collect()
        
        # Force OS memory release
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
        except:
            pass
        

def _start_cache_trim_thread():
    """Start background thread for periodic cache trimming (called once per worker)
    This thread is critical for idle cleanup - it must start even if no simulators are accessed."""
    global _cache_trim_thread_started
    if not _cache_trim_thread_started:
        _cache_trim_thread_started = True
        worker_pid = os.getpid()
        thread = threading.Thread(target=_periodic_cache_trim, daemon=True, name=f"CacheTrimThread-{worker_pid}")
        thread.start()
        # Logging handled by post_fork hook for workers, module-level call logs for master process

# Start cleanup thread immediately when module is imported (after function definition)
# This ensures idle cleanup works even if no simulators are accessed
# The thread will monitor idle time and trigger cleanup after 120 seconds of inactivity
_start_cache_trim_thread()

def _get_simulator(model):
    """Get simulator for given model, with dynamic multi-simulator LRU cache."""
    global _simulator_cache, _simulator_access_times, _last_refresh_check, _current_max_cache
    now = time.time()
    func_start = now
    
    # Start background cache trim thread if not already started
    _start_cache_trim_thread()
    
    # Refresh GEFS data if needed
    currgefs = get_currgefs()
    if not currgefs or currgefs == "Unavailable" or now - _last_refresh_check > 300:
        refresh()
        _last_refresh_check = now
        currgefs = get_currgefs()
        if not currgefs or currgefs == "Unavailable":
            raise RuntimeError(f"GEFS timestamp not available after refresh (currgefs='{currgefs}'). Cannot load model files.")
    
    # Update cache size based on current workload (adaptive sizing)
    _update_cache_size()
    
    # Trim cache if it's too large (safety check for memory leaks)
    # The background thread handles periodic trimming, but we check here for safety
    with _cache_lock:
        cache_too_large = len(_simulator_cache) > MAX_SIMULATOR_CACHE_ENSEMBLE
    if cache_too_large:
        _trim_cache_to_normal()
    
    with _cache_lock:
        # Fast path: return cached simulator if available
        if model in _simulator_cache:
            simulator = _simulator_cache[model]
            # Verify simulator is still valid (wind_file.data hasn't been cleaned up)
            if simulator:
                if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                elif getattr(simulator.wind_file, 'data', None) is None:
                    # Simulator was cleaned up but still in cache - remove it and recreate
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                else:
                    # Valid simulator - return it
                    _simulator_access_times[model] = now
                    return simulator
            else:
                # Invalid simulator - remove it
                del _simulator_cache[model]
                del _simulator_access_times[model]
        
        # Check if eviction is needed (still holding lock for atomic check)
        cache_size = len(_simulator_cache)
        needs_eviction = cache_size >= _current_max_cache
    
    # Cache miss - need to load new simulator
    # Evict oldest if cache is full (only evict if not in use)
    # Do expensive operations OUTSIDE the lock to reduce contention
    oldest_model = None
    if needs_eviction:
        # Get access times snapshot outside lock
        with _cache_lock:
            access_times_snapshot = dict(_simulator_access_times)
        
        # Sort and find candidate outside lock (no nested locks)
        sorted_models = sorted(access_times_snapshot.items(), key=lambda x: x[1])
        for candidate_model, _ in sorted_models:
            if not _is_simulator_in_use(candidate_model):
                oldest_model = candidate_model
                break
        
        # Re-acquire lock only to actually evict
        if oldest_model:
            with _cache_lock:
                # Double-check it still exists and isn't in use (race condition protection)
                if oldest_model in _simulator_cache and not _is_simulator_in_use(oldest_model):
                    simulator = _simulator_cache.get(oldest_model)
                    if simulator:
                        _cleanup_simulator_safely(simulator)
                    del _simulator_cache[oldest_model]
                    del _simulator_access_times[oldest_model]
                    with _simulator_ref_lock:
                        _simulator_ref_counts.pop(oldest_model, None)
                    gc.collect()  # Help GC reclaim memory from evicted simulator
    
    # Load new simulator (outside lock to avoid blocking other threads)
    # Auto-detect if we should preload arrays based on workload (adaptive)
    # Preloading is faster (CPU-bound) but uses more memory
    preload_arrays = _should_preload_arrays()
    
    try:
        model_file = f'{currgefs}_{str(model).zfill(2)}.npz'
        wind_file_path = load_gefs(model_file)
        wind_file = WindFile(wind_file_path, preload=preload_arrays)
        
        # Use shared ElevationFile instance when preloading (all simulators use same elevation data)
        if preload_arrays:  # Ensemble workload detected
            global _shared_elevation_file
            if _shared_elevation_file is None:
                with _shared_elevation_file_lock:
                    if _shared_elevation_file is None:
                        from habsim import ElevationFile
                        _shared_elevation_file = ElevationFile(_get_elevation_data())
            elev_file = _shared_elevation_file
        else:
            elev_file = _get_elevation_data()
        
        simulator = Simulator(wind_file, elev_file)
    except Exception as e:
        print(f"ERROR: Failed to load simulator for model {model}: {e}", flush=True)
        raise
    
    # Cache it (re-acquire lock)
    with _cache_lock:
        _simulator_cache[model] = simulator
        _simulator_access_times[model] = now
    
    total_get_sim = time.time() - func_start
    if total_get_sim > 3.0:
        pass
    
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
    func_start = time.time()
    
    # Check cache first
    cache_key = _cache_key(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient)
    cached_result = _get_cached_prediction(cache_key)
    if cached_result is not None:
        cache_time = time.time() - func_start
        return cached_result
    
    # Acquire simulator reference (prevents cleanup while in use)
    _acquire_simulator_ref(model)
    try:
        get_sim_start = time.time()
        simulator = _get_simulator(model)
        get_sim_time = time.time() - get_sim_start
        if get_sim_time > 1.0:
            pass
        
        # Validate simulator before use (race condition protection)
        if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
            print(f"ERROR: Simulator {model} wind_file is None - simulator was cleaned up during use", flush=True)
            raise RuntimeError(f"Simulator {model} is invalid: wind_file is None - simulator was cleaned up during use")
        
        balloon = Balloon(location=(lat, lon), alt=alt, time=simtime, ascent_rate=rate)
        
        traj = simulator.simulate(balloon, step, coefficient, elevation, dur=max_duration)
        
        # Pre-allocate path array with estimated size (reduces reallocations)
        estimated_points = int(max_duration / step) + 20  # Add buffer for safety
        path = [None] * estimated_points
        path_index = 0
        epoch = datetime(1970, 1, 1).replace(tzinfo=timezone.utc)
        
        for i in traj:
            if i.wind_vector is None:
                raise Exception("alt out of range")
            
            # Extend path array if we exceed pre-allocated size
            if path_index >= len(path):
                path.extend([None] * 50)  # Extend by 50 more slots
            
            timestamp = (i.time - epoch).total_seconds()
            path[path_index] = (float(timestamp), float(i.location.getLat()), float(i.location.getLon()), 
                               float(i.alt), float(i.wind_vector[0]), float(i.wind_vector[1]), 0, 0)
            path_index += 1
            
            if i.location.getLat() < -90 or i.location.getLat() > 90:
                break
        
        # Trim unused pre-allocated space
        path = path[:path_index]
        
        # Cache successful result
        _cache_prediction(cache_key, path)
        
        total_time = time.time() - func_start
        if total_time > 5.0:
            pass
        
        return path
        
    except Exception as e:
        # Don't cache errors
        total_time = time.time() - func_start
        print(f"ERROR: [PERF] simulate() FAILED: model={model}, time={total_time:.2f}s, error={e}", flush=True)
        raise e
    finally:
        # Release simulator reference
        _release_simulator_ref(model)

