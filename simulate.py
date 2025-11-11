import numpy as np 
import math
import gc
import elev
import hashlib
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from windfile import WindFile
from habsim import Simulator, Balloon
from gefs import open_gefs, load_gefs

# Try to import psutil for memory monitoring (optional)
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# Optimized version with memory-efficient caching and performance improvements
# 
# Memory Management Improvements:
# - Added RSS memory monitoring to track actual memory release
# - Improved reference breaking to ensure simulators are fully unreferenced
# - Added delayed cleanup queue to safely release simulators only after they're definitely not in use
# - Enhanced logging to show memory before/after cleanup operations
# - Ensured deterministic cleanup timing when ensemble mode expires

EARTH_RADIUS = float(6.371e6)
DATA_STEP = 6 # hrs

### Cache of datacubes and files. ###
### Dynamic multi-simulator LRU cache: small for single model, expands for ensemble ###
_simulator_cache = {}  # {model_id: simulator}
_simulator_access_times = {}  # {model_id: access_time}
# Increased cache sizes for 32GB RAM system
MAX_SIMULATOR_CACHE_NORMAL = 10  # Normal cache size for single model runs (~1.5GB, increased from 5)
MAX_SIMULATOR_CACHE_ENSEMBLE = 30  # Expanded cache for ensemble runs (~4.5GB, increased from 25)
_current_max_cache = MAX_SIMULATOR_CACHE_NORMAL  # Current cache limit (dynamic)
_ensemble_mode_until = 0  # Timestamp when ensemble mode expires
_ensemble_mode_started = 0  # Timestamp when ensemble mode first started (for max duration tracking)
_cache_lock = threading.Lock()  # Thread-safe access
elevation_cache = None
_elevation_lock = threading.Lock()  # Protects elevation_cache access/cleanup

currgefs = "Unavailable"
_last_refresh_check = 0.0
_cache_trim_thread_started = False
_in_use_models = set()
_in_use_lock = threading.Lock()
_IDLE_RESET_TIMEOUT = 120.0  # seconds of no requests before forcing deep cleanup (reduced from 180)
_IDLE_CLEAN_COOLDOWN = 120.0  # minimum delay between idle cleanups (reduced from 180)
_last_activity_timestamp = time.time()
_last_idle_cleanup = 0.0
_idle_cleanup_lock = threading.Lock()

# Delayed cleanup queue: simulators scheduled for cleanup after a safety delay
# This ensures simulators are only cleaned up after they're definitely not in use
_cleanup_queue = {}  # {model_id: (simulator, cleanup_timestamp)}
_cleanup_queue_lock = threading.Lock()
_CLEANUP_DELAY = 2.0  # Wait 2 seconds after eviction before actually cleaning up (reduced for faster memory release)

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
    """Cache prediction result with TTL and size limit.
    Lock-free for performance - minor race conditions acceptable for cache sizing."""
    # PERFORMANCE: Removed lock that was causing severe serialization bottleneck
    # During ensemble (441 concurrent calls), the lock caused all threads to queue
    # Trade-off: Cache might briefly exceed MAX_CACHE_SIZE by a few entries (acceptable)
    # vs. 4+ second serialization delay (unacceptable)
    
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
    """Clear simulator cache when GEFS model changes.
    Respects in-use models to prevent breaking active simulations."""
    global _simulator_cache, _simulator_access_times, elevation_cache, _ensemble_mode_until, _ensemble_mode_started
    
    # Check if any models are currently in use
    models_in_use = set()
    with _in_use_lock:
        models_in_use = _in_use_models.copy()
    
    with _cache_lock:
        # Clear access times for models not in use
        models_to_clear = set(_simulator_cache.keys()) - models_in_use
        
        for model_id in models_to_clear:
            if model_id in _simulator_cache:
                del _simulator_cache[model_id]
            if model_id in _simulator_access_times:
                del _simulator_access_times[model_id]
        
        # Reset ensemble mode
        _ensemble_mode_until = 0
        _ensemble_mode_started = 0
        
        if models_in_use:
            logging.info(f"GEFS cycle changed: cleared cache except {len(models_in_use)} in-use model(s)")
        else:
            logging.info(f"GEFS cycle changed: cleared all cached simulators")
    
    # Clear elevation cache (will be reloaded on next use)
    elevation_cache = None
    
    # Light garbage collection
    gc.collect()


def record_activity():
    """Record that the worker handled a request (used for idle cleanup)."""
    global _last_activity_timestamp
    old_timestamp = _last_activity_timestamp
    _last_activity_timestamp = time.time()
    # Log only if there was significant idle time (helps debug)
    idle_before = _last_activity_timestamp - old_timestamp
    if idle_before > 60:
        logging.info(f"Activity recorded: reset idle timer (was idle for {idle_before:.1f}s)")


def _idle_memory_cleanup(idle_duration):
    """Deep cleanup when the worker has been idle for a while.
    Returns True if cleanup ran, False if skipped (lock held or models in use)."""
    global _current_max_cache, _ensemble_mode_until, _ensemble_mode_started, elevation_cache, currgefs, _cleanup_queue
    if not _idle_cleanup_lock.acquire(blocking=False):
        logging.debug(f"Idle cleanup skipped: lock already held by another thread")
        return False
    try:
        # Safety check: ensure no simulators are currently in use
        with _in_use_lock:
            models_in_use = _in_use_models.copy()
        if models_in_use:
            logging.warning(f"Idle cleanup skipped: {len(models_in_use)} models still in use: {models_in_use}")
            return False
        
        rss_before = _get_rss_memory_mb()
        if rss_before is not None:
            logging.info(f"Idle memory cleanup triggered after {idle_duration:.1f}s without requests (RSS: {rss_before:.1f} MB)")
        else:
            logging.info(f"Idle memory cleanup triggered after {idle_duration:.1f}s without requests")
        
        # Process any pending cleanups first
        _process_cleanup_queue()
        
        with _cache_lock:
            simulators = list(_simulator_cache.values())
            _simulator_cache.clear()
            _simulator_access_times.clear()
            _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
            _ensemble_mode_until = 0
            _ensemble_mode_started = 0
        
        # Clear cleanup queue and clean up any pending simulators
        with _cleanup_queue_lock:
            for model_id, (simulator, _) in _cleanup_queue.items():
                _cleanup_simulator_safely(simulator)
                del simulator
            _cleanup_queue.clear()
        
        # Clean up simulator resources outside the cache lock
        evicted = 0
        for simulator in simulators:
            if simulator is None:
                continue
            try:
                _cleanup_simulator_safely(simulator)
                evicted += 1
            except Exception as cleanup_error:
                logging.debug(f"Idle cleanup: simulator cleanup error: {cleanup_error}", exc_info=True)
        simulators.clear()
        
        # IMPORTANT: Use global keyword for module-level variables
        # Clear elevation cache to free more memory (thread-safe)
        with _elevation_lock:
            elevation_cache = None  # Note: This is a module global, assigned here
        
        # Clear prediction cache
        _prediction_cache.clear()
        _cache_access_times.clear()
        
        # Reset currgefs to force re-check on next request (module global)
        currgefs = ""  # Note: This is a module global, assigned here

        # Multiple aggressive GC passes to ensure numpy arrays are freed
        for _ in range(10):  # Increased from 5 to 10
            gc.collect()
            gc.collect(generation=2)
        
        # Force memory release back to OS when available
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
            logging.info("Idle cleanup: executed malloc_trim(0) to return free memory to OS")
        except Exception as trim_error:
            logging.debug(f"Idle cleanup: malloc_trim not available: {trim_error}")

        rss_after = _get_rss_memory_mb()
        if rss_after is not None and rss_before is not None:
            rss_delta = rss_before - rss_after
            logging.info(f"Idle cleanup complete: released {evicted} simulator(s); RSS: {rss_after:.1f} MB (released {rss_delta:.1f} MB)")
        else:
            logging.info(f"Idle cleanup complete: released {evicted} simulator(s); cache and elevation reset to baseline")
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
            logging.warning(f"Cache directory not found, cannot cleanup old model files")
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
            logging.info(f"Cleaning up {len(old_files)} old model files ({total_size:.2f}GB) from timestamp: {old_timestamp}")
            
            deleted_count = 0
            for old_file in old_files:
                try:
                    if old_file.exists():
                        old_file.unlink()
                        deleted_count += 1
                        logging.debug(f"Removed old model file: {old_file.name}")
                except Exception as e:
                    logging.warning(f"Failed to remove old model file {old_file.name}: {e}")
            
            logging.info(f"Successfully deleted {deleted_count}/{len(old_files)} old model files")
        else:
            logging.info(f"No old model files found for timestamp: {old_timestamp}")
            
        # After deleting old files, also trigger the LRU cleanup to ensure we're under limits
        # This helps when the cache has accumulated files over time
        try:
            from gefs import _cleanup_old_cache_files
            _cleanup_old_cache_files()
        except:
            pass
            
    except Exception as e:
        logging.error(f"Failed to cleanup old model files: {e}", exc_info=True)

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

def set_ensemble_mode(duration_seconds=60):
    """Enable ensemble mode (larger cache) for specified duration (default 60 seconds / 1 minute)
    
    Note: Maximum ensemble mode duration is capped at 5 minutes to prevent memory bloat
    from consecutive ensemble calls. After 5 minutes, cache will trim even if ensemble mode
    is still being extended.
    
    Also triggers background prefetching of all 21 models for faster ensemble runs.
    """
    global _current_max_cache, _ensemble_mode_until, _ensemble_mode_started
    now = time.time()
    MAX_ENSEMBLE_DURATION = 300  # 5 minutes maximum
    is_new_ensemble = False
    with _cache_lock:
        _current_max_cache = MAX_SIMULATOR_CACHE_ENSEMBLE
        # Track when ensemble mode first started
        if _ensemble_mode_until <= now:
            # Starting new ensemble mode
            is_new_ensemble = True
            _ensemble_mode_started = now
            _ensemble_mode_until = now + duration_seconds
        else:
            # Already in ensemble mode, check if we've exceeded max duration
            if now - _ensemble_mode_started >= MAX_ENSEMBLE_DURATION:
                # Exceeded max duration - don't extend, let it expire
                logging.info(f"Ensemble mode exceeded max duration ({MAX_ENSEMBLE_DURATION}s), not extending further")
            else:
                # Extend ensemble mode but respect max duration
                new_expiry = now + duration_seconds
                max_allowed_expiry = _ensemble_mode_started + MAX_ENSEMBLE_DURATION
                _ensemble_mode_until = min(new_expiry, max_allowed_expiry)
    
    # Prefetch all models in background when starting new ensemble mode
    if is_new_ensemble:
        _prefetch_ensemble_models()

def _is_ensemble_mode():
    """Check if currently in ensemble mode"""
    global _ensemble_mode_until
    now = time.time()
    return _ensemble_mode_until > 0 and now < _ensemble_mode_until

def _prefetch_ensemble_models():
    """Prefetch all 21 ensemble models in background thread for faster ensemble runs.
    This pre-warms the cache so simulations don't wait for downloads."""
    def prefetch_worker():
        try:
            # Prefetch models 0-20 in background
            for model in range(21):
                try:
                    # Try to get simulator (will download if needed, but won't block main thread)
                    # Use a short timeout to avoid blocking if model is unavailable
                    _get_simulator(model)
                    logging.debug(f"Prefetched model {model} for ensemble mode")
                except Exception as e:
                    # Non-critical: some models might not be available yet
                    logging.debug(f"Could not prefetch model {model}: {e}")
        except Exception as e:
            logging.warning(f"Ensemble prefetch error: {e}")
    
    # Start background thread (daemon so it doesn't block shutdown)
    thread = threading.Thread(target=prefetch_worker, daemon=True, name="EnsemblePrefetch")
    thread.start()
    logging.info("Started background prefetching of ensemble models")

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
    except Exception as cleanup_error:
        logging.debug(f"Simulator cleanup error (non-critical): {cleanup_error}")

def _process_cleanup_queue():
    """Process delayed cleanup queue - actually clean up simulators that were evicted."""
    global _cleanup_queue
    now = time.time()
    to_cleanup = []
    
    with _cleanup_queue_lock:
        # Find simulators ready for cleanup
        for model_id, (simulator, cleanup_time) in list(_cleanup_queue.items()):
            if now >= cleanup_time:
                to_cleanup.append((model_id, simulator))
                del _cleanup_queue[model_id]
    
    # Clean up outside the lock
    for model_id, simulator in to_cleanup:
        # Double-check model is not in use
        with _in_use_lock:
            if model_id in _in_use_models:
                # Still in use - reschedule cleanup
                with _cleanup_queue_lock:
                    _cleanup_queue[model_id] = (simulator, now + _CLEANUP_DELAY)
                continue
        
        # CRITICAL: Also check if simulator is still in cache (shouldn't be, but safety check)
        # If it's back in cache, don't clean it up (it was re-added after eviction)
        with _cache_lock:
            if model_id in _simulator_cache:
                # Simulator is back in cache - don't clean it up, remove from queue
                logging.warning(f"Simulator {model_id} is back in cache, skipping cleanup (was re-added after eviction)")
                continue
        
        # Clean up the simulator (it's already removed from cache and not in use)
        _cleanup_simulator_safely(simulator)
        
        # Explicitly break reference
        del simulator

def _trim_cache_to_normal():
    """Trim cache back to normal size, keeping most recently used models.
    Uses delayed cleanup queue to ensure simulators are only cleaned up after they're definitely not in use.
    Optimized to be fast when called frequently - only processes cleanup queue if items are ready."""
    global _current_max_cache, _simulator_cache, _simulator_access_times, _ensemble_mode_until, _ensemble_mode_started, _cleanup_queue
    
    now = time.time()
    
    # Fast path: Quick check if trimming is needed (without processing cleanup queue)
    with _cache_lock:
        cache_size = len(_simulator_cache)
        MAX_ENSEMBLE_DURATION = 300  # 5 minutes maximum
        ensemble_expired = _ensemble_mode_until > 0 and now > _ensemble_mode_until
        ensemble_exceeded_max = _ensemble_mode_started > 0 and (now - _ensemble_mode_started) >= MAX_ENSEMBLE_DURATION
        needs_trim = (ensemble_expired or ensemble_exceeded_max or 
                     cache_size > _current_max_cache or cache_size > MAX_SIMULATOR_CACHE_ENSEMBLE)
    
    # Only process cleanup queue if we're actually going to trim (saves time on frequent calls)
    if needs_trim:
        # Quick check if cleanup queue has items ready (non-blocking check)
        with _cleanup_queue_lock:
            has_ready_items = any(cleanup_time <= now for _, (_, cleanup_time) in _cleanup_queue.items())
        
        # Only process cleanup queue if items are ready (avoid expensive operations when not needed)
        if has_ready_items:
            _process_cleanup_queue()
    
    # Get memory before cleanup (for logging) - only if actually trimming
    rss_before = _get_rss_memory_mb() if needs_trim else None
    
    with _cache_lock:
        # Check if ensemble mode has expired or exceeded max duration
        MAX_ENSEMBLE_DURATION = 300  # 5 minutes maximum
        ensemble_expired = _ensemble_mode_until > 0 and now > _ensemble_mode_until
        ensemble_exceeded_max = _ensemble_mode_started > 0 and (now - _ensemble_mode_started) >= MAX_ENSEMBLE_DURATION
        
        if ensemble_expired or ensemble_exceeded_max:
            _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
            if ensemble_exceeded_max:
                _ensemble_mode_until = 0  # Force expiration even if still being extended
                _ensemble_mode_started = 0
                logging.info("Ensemble mode exceeded max duration (5 min): forcing cache trim to normal (5 simulators)")
            else:
                _ensemble_mode_until = 0
                _ensemble_mode_started = 0
                logging.info("Ensemble mode expired: cache limit reset to normal (5 simulators)")
        
        # If cache is too large, trim to normal size keeping most recently used
        # Also trim if cache is significantly larger than current limit (memory leak protection)
        if len(_simulator_cache) > _current_max_cache or len(_simulator_cache) > MAX_SIMULATOR_CACHE_ENSEMBLE:
            # Sort by access time (most recent first)
            sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
            # Keep only the most recently used models
            models_to_keep = {model_id for model_id, _ in sorted_models[:_current_max_cache]}
            
            # Evict models not in the keep list
            models_to_evict = set(_simulator_cache.keys()) - models_to_keep
            # Do not evict models currently in use
            with _in_use_lock:
                models_to_evict = {m for m in models_to_evict if m not in _in_use_models}
            evicted_count = len(models_to_evict)
            
            # Remove from cache immediately, but schedule delayed cleanup
            # This ensures simulators are only cleaned up after they're definitely not in use
            simulators_to_cleanup = []
            for model_id in models_to_evict:
                simulator = _simulator_cache.get(model_id)
                if simulator:
                    simulators_to_cleanup.append((model_id, simulator))
                del _simulator_cache[model_id]
                if model_id in _simulator_access_times:
                    del _simulator_access_times[model_id]
            
            # Schedule delayed cleanup for evicted simulators
            # Keep references to simulators in cleanup queue to prevent GC before cleanup
            cleanup_time = now + _CLEANUP_DELAY
            with _cleanup_queue_lock:
                for model_id, simulator in simulators_to_cleanup:
                    _cleanup_queue[model_id] = (simulator, cleanup_time)
            
            if evicted_count > 0:
                logging.info(f"Trimmed cache: evicted {evicted_count} simulators, keeping {len(_simulator_cache)} (limit: {_current_max_cache})")
                if rss_before is not None:
                    logging.info(f"Memory before trim: {rss_before:.1f} MB RSS")
                logging.info(f"Scheduled {evicted_count} simulators for delayed cleanup (will process after {_CLEANUP_DELAY}s)")

def _periodic_cache_trim():
    """Background thread that periodically trims cache when ensemble mode expires.
    This ensures idle workers trim their cache even if they don't receive requests.
    Without this, each worker process maintains its own cache, and idle workers never trim.
    """
    logging.info("Cache trim background thread started")
    consecutive_trim_failures = 0
    global _last_idle_cleanup
    while True:
        try:
            now = time.time()
            idle_duration = now - _last_activity_timestamp
            # Log idle status periodically for debugging (every 30s when idle)
            if idle_duration > 30 and int(idle_duration) % 30 < 2:  # Log roughly every 30s when idle
                with _cache_lock:
                    cache_size = len(_simulator_cache)
                last_cleanup_ago = (now - _last_idle_cleanup) if _last_idle_cleanup > 0 else 0
                should_trigger = idle_duration >= _IDLE_RESET_TIMEOUT and (last_cleanup_ago >= _IDLE_CLEAN_COOLDOWN or _last_idle_cleanup == 0)
                logging.info(f"Idle check: {idle_duration:.1f}s idle (threshold: {_IDLE_RESET_TIMEOUT}s), cache size: {cache_size}, last_cleanup: {last_cleanup_ago:.1f}s ago, should_trigger: {should_trigger}")
            # Fix: Handle case where cleanup never ran (_last_idle_cleanup = 0)
            # If cleanup never ran, allow it immediately if idle threshold reached
            if _last_idle_cleanup == 0:
                # Cleanup never ran - allow it immediately if idle threshold reached
                time_since_last_cleanup = float('inf')
            else:
                time_since_last_cleanup = now - _last_idle_cleanup
            
            # Check if idle cleanup should run
            should_run_idle_cleanup = (idle_duration >= _IDLE_RESET_TIMEOUT and 
                                      (time_since_last_cleanup >= _IDLE_CLEAN_COOLDOWN or _last_idle_cleanup == 0))
            
            # Log when cleanup should run but hasn't (for debugging)
            if should_run_idle_cleanup and _last_idle_cleanup == 0 and idle_duration >= _IDLE_RESET_TIMEOUT + 10:
                logging.warning(f"Idle cleanup should run but hasn't: idle={idle_duration:.1f}s, threshold={_IDLE_RESET_TIMEOUT}s, last_cleanup={_last_idle_cleanup}")
            
            # Emergency cleanup: if idle >600s and cleanup never ran, force it immediately
            # Also trigger if idle >180s and cleanup never ran (lower threshold for first cleanup)
            if ((idle_duration > 600 and _last_idle_cleanup == 0) or 
                (idle_duration > 180 and _last_idle_cleanup == 0 and idle_duration >= _IDLE_RESET_TIMEOUT + 60)):
                logging.error(f"EMERGENCY: Worker idle for {idle_duration:.1f}s but cleanup never ran! Forcing cleanup now.")
                try:
                    with _cache_lock:
                        cache_size = len(_simulator_cache)
                    cleanup_ran = _idle_memory_cleanup(idle_duration)
                    # Always mark cleanup as attempted, even if skipped
                    _last_idle_cleanup = time.time()
                    if cleanup_ran:
                        logging.info(f"Emergency idle cleanup completed successfully")
                    else:
                        logging.warning(f"Emergency cleanup was skipped (lock held or models in use), but marked as attempted")
                except Exception as emergency_error:
                    logging.error(f"Emergency cleanup failed: {emergency_error}", exc_info=True)
                    _last_idle_cleanup = time.time()  # Mark as attempted
                consecutive_trim_failures = 0
                time.sleep(5)
                continue
            
            if should_run_idle_cleanup:
                with _cache_lock:
                    cache_size = len(_simulator_cache)
                logging.info(f"Idle threshold reached: {idle_duration:.1f}s without user activity, cache size: {cache_size}, triggering cleanup")
                try:
                    cleanup_ran = _idle_memory_cleanup(idle_duration)
                    # Always mark cleanup as attempted, even if it was skipped (lock held or models in use)
                    # This prevents infinite retries when cleanup can't run
                    _last_idle_cleanup = time.time()
                    if cleanup_ran:
                        logging.info(f"Idle cleanup completed successfully, marked timestamp: {_last_idle_cleanup}")
                    else:
                        logging.info(f"Idle cleanup was skipped (lock held or models in use), but marked as attempted: {_last_idle_cleanup}")
                except Exception as cleanup_error:
                    logging.error(f"Idle cleanup failed: {cleanup_error}", exc_info=True)
                    # Still mark as attempted to prevent repeated failures
                    _last_idle_cleanup = time.time()
                consecutive_trim_failures = 0
                time.sleep(5)
                continue
            # Check if ensemble mode has expired - if so, trim more aggressively
            with _cache_lock:
                ensemble_expired = _ensemble_mode_until > 0 and now > _ensemble_mode_until
                ensemble_exceeded_max = _ensemble_mode_started > 0 and (now - _ensemble_mode_started) >= 300  # 5 minutes
                cache_size = len(_simulator_cache)
                current_max = _current_max_cache
            
            # Log state for debugging
            if cache_size > MAX_SIMULATOR_CACHE_NORMAL:
                logging.info(f"Cache trim check: size={cache_size}, max={current_max}, ensemble_expired={ensemble_expired}, exceeded_max={ensemble_exceeded_max}")
            
            if (ensemble_expired or ensemble_exceeded_max) and cache_size > MAX_SIMULATOR_CACHE_NORMAL:
                # Ensemble mode expired but cache still large - trim immediately and aggressively
                rss_before = _get_rss_memory_mb()
                if rss_before is not None:
                    logging.info(f"Ensemble mode expired/exceeded, trimming cache from {cache_size} to {MAX_SIMULATOR_CACHE_NORMAL} (RSS: {rss_before:.1f} MB)")
                else:
                    logging.info(f"Ensemble mode expired/exceeded, trimming cache from {cache_size} to {MAX_SIMULATOR_CACHE_NORMAL}")
                
                _trim_cache_to_normal()
                
                # Wait for delayed cleanup to process (simulators are cleaned up after _CLEANUP_DELAY)
                time.sleep(_CLEANUP_DELAY + 1)  # Wait slightly longer than cleanup delay
                
                # Process cleanup queue to actually free memory
                _process_cleanup_queue()
                
                # Force additional GC passes to ensure memory is released
                for _ in range(3):
                    gc.collect()
                    gc.collect(generation=2)
                try:
                    import ctypes
                    libc = ctypes.CDLL("libc.so.6")
                    libc.malloc_trim(0)
                    logging.info("Forced malloc_trim after cache trim")
                except:
                    pass
                
                # Check immediately after trimming to see if it worked
                with _cache_lock:
                    new_size = len(_simulator_cache)
                rss_after = _get_rss_memory_mb()
                
                if new_size > MAX_SIMULATOR_CACHE_NORMAL:
                    consecutive_trim_failures += 1
                    if rss_after is not None and rss_before is not None:
                        rss_delta = rss_before - rss_after
                        logging.warning(f"Cache trim didn't reduce size enough: {new_size} > {MAX_SIMULATOR_CACHE_NORMAL} (failure #{consecutive_trim_failures}, RSS: {rss_after:.1f} MB, released: {rss_delta:.1f} MB)")
                    else:
                        logging.warning(f"Cache trim didn't reduce size enough: {new_size} > {MAX_SIMULATOR_CACHE_NORMAL} (failure #{consecutive_trim_failures})")
                    if consecutive_trim_failures > 2:  # Reduced from 3 to 2 for faster response
                        # Force more aggressive trimming
                        logging.warning("Multiple trim failures, forcing aggressive cleanup")
                        _force_aggressive_trim()
                        consecutive_trim_failures = 0
                else:
                    consecutive_trim_failures = 0
                    if rss_after is not None and rss_before is not None:
                        rss_delta = rss_before - rss_after
                        logging.info(f"Cache trim successful: {new_size} simulators (RSS: {rss_after:.1f} MB, released: {rss_delta:.1f} MB)")
                time.sleep(3)  # Check even more frequently (reduced from 5s) when trim failing
            else:
                # Normal check interval - always call trim to handle edge cases
                # Also process cleanup queue periodically
                _process_cleanup_queue()
                _trim_cache_to_normal()
                consecutive_trim_failures = 0
                time.sleep(20)  # Check more frequently (reduced from 30s)
        except Exception as e:
            logging.error(f"Cache trim thread error: {e}", exc_info=True)
            # If idle for a very long time and cleanup hasn't run, force it
            now_error = time.time()
            idle_duration_error = now_error - _last_activity_timestamp
            if idle_duration_error > 600 and _last_idle_cleanup == 0:  # 10 minutes idle, cleanup never ran
                logging.error(f"EMERGENCY: Worker idle for {idle_duration_error:.1f}s but cleanup never ran! Forcing cleanup now.")
                try:
                    _idle_memory_cleanup(idle_duration_error)
                    _last_idle_cleanup = time.time()
                    logging.info(f"Emergency cleanup completed after thread error")
                except Exception as emergency_error:
                    logging.error(f"Emergency cleanup also failed: {emergency_error}", exc_info=True)
                    _last_idle_cleanup = time.time()  # Mark as attempted even on failure
            time.sleep(10)  # Wait shorter on error to retry faster

def _force_aggressive_trim():
    """Force aggressive cache trimming - removes all but 1 most recently used simulator"""
    global _simulator_cache, _simulator_access_times, _current_max_cache
    with _cache_lock:
        if len(_simulator_cache) <= 1:
            return
        
        logging.warning(f"Force aggressive trim: removing {len(_simulator_cache) - 1} simulators, keeping only 1")
        
        # Sort by access time (most recent first)
        sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
        
        # Keep only the most recently used model
        model_to_keep = sorted_models[0][0] if sorted_models else None
        
        # Evict all others
        for model_id in list(_simulator_cache.keys()):
            with _in_use_lock:
                if model_id in _in_use_models:
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
        
        logging.info(f"Aggressive trim complete: kept {len(_simulator_cache)} simulator(s)")

def _start_cache_trim_thread():
    """Start background thread for periodic cache trimming (called once per worker)
    This thread is critical for idle cleanup - it must start even if no simulators are accessed."""
    global _cache_trim_thread_started
    if not _cache_trim_thread_started:
        _cache_trim_thread_started = True
        thread = threading.Thread(target=_periodic_cache_trim, daemon=True, name="CacheTrimThread")
        thread.start()
        logging.info("Cache trim background thread started (idle cleanup will run after 120s of inactivity)")

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
    refresh_start = time.time()
    if currgefs == "Unavailable" or now - _last_refresh_check > 300:
        refresh()
        _last_refresh_check = now
        refresh_time = time.time() - refresh_start
        if refresh_time > 1.0:
            logging.warning(f"[PERF] refresh() slow: time={refresh_time:.2f}s")
    
    # Trim cache if ensemble mode expired (called on every simulator access)
    # Optimized to be fast - only does expensive work if actually needed
    trim_start = time.time()
    _trim_cache_to_normal()
    trim_time = time.time() - trim_start
    if trim_time > 0.5:  # Reduced threshold since it should be much faster now
        logging.warning(f"[PERF] _trim_cache_to_normal() slow: time={trim_time:.2f}s")
    
    with _cache_lock:
        # Fast path: return cached simulator if available
        if model in _simulator_cache:
            simulator = _simulator_cache[model]
            # Verify simulator is still valid (wind_file.data hasn't been cleaned up)
            if simulator:
                if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
                    logging.warning(f"Simulator {model} has no wind_file - removing and recreating")
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                elif getattr(simulator.wind_file, 'data', None) is None:
                    # Simulator was cleaned up but still in cache - remove it and recreate
                    logging.warning(f"Simulator {model} found in cache but wind_file.data is None - removing and recreating")
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                else:
                    # Valid simulator - return it
                    _simulator_access_times[model] = now
                    return simulator
            else:
                # Invalid simulator - remove it
                logging.warning(f"Simulator {model} found in cache but is invalid - removing")
                del _simulator_cache[model]
                del _simulator_access_times[model]
        
        # Cache miss - need to load new simulator
        # Evict oldest if cache is full
        if len(_simulator_cache) >= _current_max_cache:
            oldest_model = min(_simulator_access_times, key=_simulator_access_times.get)
            simulator = _simulator_cache.get(oldest_model)
            # Explicitly clear WindFile and all its data before deleting
            if simulator:
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
            del _simulator_cache[oldest_model]
            del _simulator_access_times[oldest_model]
            gc.collect()  # Help GC reclaim memory from evicted simulator
    
    # Load new simulator (outside lock to avoid blocking other threads)
    # Preload arrays in ensemble mode for CPU-bound performance (faster simulations)
    # Use memory-mapping in normal mode for memory efficiency
    preload_arrays = _is_ensemble_mode()
    
    try:
        load_start = time.time()
        wind_file_path = load_gefs(f'{currgefs}_{str(model).zfill(2)}.npz')
        load_time = time.time() - load_start
        if load_time > 2.0:
            logging.warning(f"[PERF] load_gefs() slow: model={model}, time={load_time:.2f}s")
        
        windfile_start = time.time()
        wind_file = WindFile(wind_file_path, preload=preload_arrays)
        windfile_time = time.time() - windfile_start
        if windfile_time > 2.0:
            logging.warning(f"[PERF] WindFile() slow: model={model}, time={windfile_time:.2f}s")
        
        simulator = Simulator(wind_file, _get_elevation_data())
        
        total_load = time.time() - load_start
        logging.info(f"[PERF] Loaded new simulator: model={model}, total_time={total_load:.2f}s")
    except Exception as e:
        logging.error(f"Failed to load simulator for model {model}: {e}", exc_info=True)
        raise
    
    # Cache it (re-acquire lock)
    with _cache_lock:
        _simulator_cache[model] = simulator
        _simulator_access_times[model] = now
    
    total_get_sim = time.time() - func_start
    if total_get_sim > 3.0:
        logging.warning(f"[PERF] _get_simulator() TOTAL slow: model={model}, time={total_get_sim:.2f}s")
    
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
        logging.debug(f"[PERF] simulate() cache HIT: model={model}, time={cache_time:.3f}s")
        return cached_result
    
    # Mark model as in use to prevent cleanup races (do this BEFORE getting simulator)
    with _in_use_lock:
        _in_use_models.add(model)
    try:
        get_sim_start = time.time()
        simulator = _get_simulator(model)
        get_sim_time = time.time() - get_sim_start
        if get_sim_time > 1.0:
            logging.warning(f"[PERF] _get_simulator() slow: model={model}, time={get_sim_time:.2f}s")
        
        # CRITICAL: Verify simulator is still valid after getting it (race condition protection)
        # Cleanup might have run between returning from _get_simulator() and here
        if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
            logging.error(f"Simulator {model} wind_file is None after retrieval - this indicates a race condition")
            raise RuntimeError(f"Simulator {model} is invalid: wind_file is None (likely cleaned up during retrieval)")
        
        balloon = Balloon(location=(lat, lon), alt=alt, time=simtime, ascent_rate=rate)
        
        # Additional defensive check right before use (double protection against race conditions)
        if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
            logging.error(f"Simulator {model} wind_file became None between retrieval and use - race condition")
            raise RuntimeError(f"Simulator {model} is invalid: wind_file is None (cleaned up during use)")
        
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
        
        total_time = time.time() - func_start
        if total_time > 5.0:
            logging.warning(f"[PERF] simulate() completed SLOW: model={model}, time={total_time:.2f}s, points={len(path)}")
        else:
            logging.debug(f"[PERF] simulate() completed: model={model}, time={total_time:.2f}s, points={len(path)}")
        
        return path
        
    except Exception as e:
        # Don't cache errors
        total_time = time.time() - func_start
        logging.error(f"[PERF] simulate() FAILED: model={model}, time={total_time:.2f}s, error={e}")
        raise e
    finally:
        with _in_use_lock:
            _in_use_models.discard(model)

