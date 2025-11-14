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
from datetime import datetime, timezone
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

# Force preloading hint (ensures cold ensemble loads still preload their wind arrays)
_force_preload_lock = threading.Lock()
_force_preload_remaining = 0

# Shared currgefs storage across workers (file-based on persistent volume)
_CURRGEFS_FILE = None
if Path("/app/data").exists():  # Railway persistent volume
    _CURRGEFS_FILE = Path("/app/data/currgefs.txt")
else:
    _CURRGEFS_FILE = Path(tempfile.gettempdir()) / "habsim-currgefs.txt"
_REFRESH_LOCK_FILE = _CURRGEFS_FILE.with_suffix('.refresh.lock')
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

def force_preload_for_next_models(model_count: int):
    """Force the next N simulator loads to preload arrays (ensemble cold-start hint)."""
    if not isinstance(model_count, int) or model_count <= 0:
        return
    global _force_preload_remaining
    with _force_preload_lock:
        _force_preload_remaining += model_count

def _clear_forced_preload():
    """Reset forced preload hints (called after cache resets)."""
    global _force_preload_remaining
    with _force_preload_lock:
        _force_preload_remaining = 0

def _has_forced_preload_hint() -> bool:
    with _force_preload_lock:
        return _force_preload_remaining > 0

def _consume_forced_preload_slot() -> bool:
    """Consume a forced preload slot after a simulator is successfully built."""
    global _force_preload_remaining
    with _force_preload_lock:
        if _force_preload_remaining > 0:
            _force_preload_remaining -= 1
            return True
    return False

def refresh():
    """Refresh GEFS cycle from S3 and update shared state atomically.
    
    Steps:
    1. Read `whichgefs` and verify all 21 model files are readable.
    2. Set `_cache_invalidation_cycle` so other workers stop using cached simulators.
    3. Wait 3 seconds for S3 propagation, then write `currgefs`.
    4. Call `reset()` and schedule cleanup of the previous cycle's files.
    
    Returns:
        True if the cycle was updated.
        False if nothing changed or verification failed.
        (False, pending_cycle) when a new timestamp is announced but files are still uploading.
    """
    import fcntl
    global _last_refresh_check, _cache_invalidation_cycle
    
    # File-based lock prevents concurrent refreshes across workers
    lock_file = None
    try:
        _REFRESH_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(_REFRESH_LOCK_FILE, 'w')
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another worker is refreshing - wait briefly and check if it completed
            lock_file.close()
            for _ in range(10):  # Wait up to 1 second
                time.sleep(0.1)
                current = _read_currgefs()
                if current and current != "Unavailable":
                    # Another worker refreshed - update our refresh check time to avoid immediate re-check
                    _last_refresh_check = time.time()
                    return False  # Another worker refreshed successfully
            # Still locked after wait - try blocking lock
            lock_file = open(_REFRESH_LOCK_FILE, 'w')
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            # Double-check after acquiring lock
            current = _read_currgefs()
            if current and current != "Unavailable":
                # Another worker refreshed - update our refresh check time
                _last_refresh_check = time.time()
                return False
        
        # Read current timestamp before refresh (preserve if refresh fails)
        old_gefs = _read_currgefs()
        
        try:
            f = open_gefs('whichgefs')
            new_gefs = f.readline().strip()
            f.close()
            
            # Only update if we got a valid timestamp (non-empty, not just whitespace)
            if not new_gefs:
                return False
            
            # Only proceed if timestamp actually changed
            if new_gefs == old_gefs:
                return False
            
            # Verify all 21 model files exist before updating currgefs
            # Ensemble needs all 21 models (0-20), so we must verify all are available
            # This prevents updating to a cycle that hasn't fully uploaded yet
            # Uses retry_for_consistency to handle S3 eventual consistency
            max_retries = 5
            retry_delay = 2.0
            for attempt in range(max_retries):
                try:
                    # Use improved file check with S3 eventual consistency handling
                    files_available = _check_cycle_files_available(
                        new_gefs,
                        max_models=21,
                        check_disk_cache=False,
                        retry_for_consistency=(attempt < max_retries - 1),
                        verify_content=True,
                    )
                    
                    if files_available:
                        # All files available - proceed with update
                        break
                    else:
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            print(f"INFO: New GEFS cycle {new_gefs} detected but files not available yet, retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})...", flush=True)
                            time.sleep(wait_time)
                            continue
                        else:
                            # Final attempt - still missing files
                            # Return special status: new cycle detected but files not ready
                            print(f"WARNING: New GEFS cycle {new_gefs} detected in whichgefs but files not available after {max_retries} attempts. Using current cycle {old_gefs} until files are ready.", flush=True)
                            return (False, new_gefs)  # Return tuple: (False, pending_cycle)
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        print(f"INFO: Error checking GEFS files for cycle {new_gefs}, retrying in {wait_time:.1f}s: {e}", flush=True)
                        time.sleep(wait_time)
                    else:
                        print(f"INFO: New GEFS cycle {new_gefs} detected but file check failed after {max_retries} attempts: {e}", flush=True)
                        return False
            
            # Set cache invalidation cycle and update currgefs atomically
            # This ensures other workers see consistent state (no timing window between updates)
            # Order: invalidation_cycle first (signals cache invalidation), then currgefs (signals new cycle)
            with _cache_invalidation_lock:
                old_invalidation_cycle = _cache_invalidation_cycle
                _cache_invalidation_cycle = new_gefs
                if old_invalidation_cycle != new_gefs:
                    print(f"INFO: Cache invalidation cycle set to {new_gefs} (was {old_invalidation_cycle}). All cached simulators will be re-validated.", flush=True)
            
            # Grace period: short wait keeps currgefs hidden until files are definitely readable
            time.sleep(3.0)
            
            # Update timestamp atomically (after grace period, files should be fully available)
            # This completes the atomic update: invalidation_cycle -> wait -> currgefs
            _write_currgefs(new_gefs)
            
            # Log cycle change for debugging
            print(f"INFO: GEFS cycle updated: {old_gefs} -> {new_gefs}. Invalidating cache and clearing simulators.", flush=True)
            
            # Clear caches after successful update (pass cycle explicitly to avoid race)
            reset(new_gefs)
            _prediction_cache.clear()
            _cache_access_times.clear()
            
            # Log cache invalidation completion
            print(f"INFO: Cache invalidation complete for cycle {new_gefs}. All simulators from cycle {old_gefs} are now invalid.", flush=True)
            
            # Delay old file cleanup - only delete after confirming new cycle is available
            # This prevents "file not found" errors if new files aren't uploaded yet
            if old_gefs and old_gefs != "Unavailable":
                # Schedule cleanup in background (non-blocking)
                def delayed_cleanup():
                    time.sleep(30)  # Wait 30s for new files to be available
                    try:
                        _cleanup_old_model_files(old_gefs)
                    except Exception:
                        pass
                import threading
                threading.Thread(target=delayed_cleanup, daemon=True).start()
            
            return True
        except Exception as e:
            # Refresh failed - preserve old timestamp (don't leave "Unavailable")
            print(f"ERROR: refresh() failed: {e}", flush=True)
            return False
    except Exception as e:
        print(f"ERROR: refresh() lock failed: {e}", flush=True)
        return False
    finally:
        if lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass

def get_currgefs():
    """Get current GEFS timestamp (reads from shared file)."""
    return _read_currgefs()

def _check_cycle_files_available(gefs_cycle, max_models=21, check_disk_cache=False, retry_for_consistency=False, verify_content=False):
    """Check if all model files exist for a given GEFS cycle.
    
    Args:
        gefs_cycle: GEFS timestamp to check (e.g., "2025103012")
        max_models: Number of models to check (default: 21 for ensemble)
        check_disk_cache: If True, also verify files exist in disk cache (default: False, only S3)
        retry_for_consistency: If True, retry with exponential backoff for S3 eventual consistency (default: False)
        verify_content: If True, read first byte via Range request to ensure file is readable
    
    Returns:
        True if all files exist, False otherwise
    """
    if not gefs_cycle or gefs_cycle == "Unavailable":
        return False
    
    try:
        from gefs import _STATUS_S3_CLIENT, _BUCKET, _CACHE_DIR
        from botocore.exceptions import ClientError
        
        # Retry logic for S3 eventual consistency (FIX: Problem 2)
        max_retries = 5 if retry_for_consistency else 1
        retry_delay = 1.0
        
        for retry_attempt in range(max_retries):
            all_files_exist = True
            missing_files = []
            
            # Check all models (0 to max_models-1)
            for i in range(max_models):
                test_file = f'{gefs_cycle}_{str(i).zfill(2)}.npz'
                # Check S3 availability with retry for eventual consistency
                file_exists = False
                try:
                    _STATUS_S3_CLIENT.head_object(Bucket=_BUCKET, Key=test_file)
                    if verify_content:
                        try:
                            _STATUS_S3_CLIENT.get_object(Bucket=_BUCKET, Key=test_file, Range='bytes=0-1')
                        except ClientError as content_error:
                            content_code = content_error.response.get('Error', {}).get('Code', '')
                            if content_code in ('404', 'NoSuchKey'):
                                file_exists = False
                            elif retry_for_consistency and retry_attempt < max_retries - 1:
                                file_exists = None
                            else:
                                raise
                        except Exception:
                            if retry_for_consistency and retry_attempt < max_retries - 1:
                                file_exists = None
                            else:
                                raise
                        else:
                            file_exists = True
                    else:
                        file_exists = True
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    # 404/NoSuchKey means file doesn't exist (not a consistency issue)
                    if error_code not in ('404', 'NoSuchKey'):
                        # Other errors (403, 500, etc.) might be transient - retry if enabled
                        if retry_for_consistency and retry_attempt < max_retries - 1:
                            file_exists = None  # Mark as unknown, will retry
                        else:
                            file_exists = False
                    else:
                        file_exists = False
                except Exception:
                    # Network errors, timeouts, etc. - retry if enabled
                    if retry_for_consistency and retry_attempt < max_retries - 1:
                        file_exists = None  # Mark as unknown, will retry
                    else:
                        file_exists = False
                
                if file_exists is False:
                    all_files_exist = False
                    missing_files.append(test_file)
                elif file_exists is None:
                    # Transient error - will retry
                    all_files_exist = False
                    missing_files.append(test_file)
                
                # Optionally check disk cache
                if file_exists and check_disk_cache:
                    cache_path = _CACHE_DIR / test_file
                    if not cache_path.exists():
                        all_files_exist = False
                        missing_files.append(test_file)
            
            if all_files_exist:
                return True  # All files exist
            
            # If retry enabled and not last attempt, wait and retry
            if retry_for_consistency and retry_attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** retry_attempt)  # Exponential backoff
                print(f"INFO: S3 eventual consistency check: {len(missing_files)}/{max_models} files not found for cycle {gefs_cycle}, retrying in {wait_time:.1f}s (attempt {retry_attempt + 1}/{max_retries})...", flush=True)
                time.sleep(wait_time)
                continue
            else:
                # No retry or last attempt - return False
                return False
        
        return False  # All retries exhausted
    except Exception:
        return False  # Error checking files

# Track cycle invalidation to force re-validation of cached simulators
_cache_invalidation_cycle = None
_cache_invalidation_lock = threading.Lock()

def reset(cycle=None):
    """
    Clear simulator cache when GEFS model changes.
    
    CRITICAL: Only evicts simulators that are NOT currently in use (ref_count == 0).
    However, ALL cached simulators are marked as invalid (even with ref_count > 0)
    to force re-validation on next access. This ensures cache consistency even when
    reset() runs during active prefetch operations.
    
    FIX: Problem 4 - Accept cycle parameter to avoid race condition where cycle
    changes between reading currgefs and setting invalidation cycle.
    
    Args:
        cycle: Optional GEFS cycle to use for invalidation. If None, reads from currgefs.
    
    Lock order: _simulator_ref_lock -> _cache_lock (consistent across all functions)
    """
    global _simulator_cache, _simulator_access_times, elevation_cache, _current_max_cache, _cache_invalidation_cycle
    
    # FIX: Problem 4 - Use provided cycle or read current, but invalidation cycle
    # should already be set by refresh() before calling reset()
    if cycle is None:
        cycle = get_currgefs()
    
    # Invalidation cycle should already be set by refresh() before calling reset()
    # But verify it matches to catch any inconsistencies
    with _cache_invalidation_lock:
        if _cache_invalidation_cycle != cycle:
            print(f"WARNING: Cache invalidation cycle mismatch: expected {cycle}, got {_cache_invalidation_cycle}. Updating to {cycle}.", flush=True)
            _cache_invalidation_cycle = cycle
    
    # DEADLOCK PREVENTION: Get snapshot of ref counts BEFORE acquiring cache lock
    # If we acquired cache_lock first, then tried to acquire ref_lock, we'd deadlock
    # with simulate() which acquires ref_lock then cache_lock
    with _simulator_ref_lock:
        ref_counts_snapshot = dict(_simulator_ref_counts)
    
    with _cache_lock:
        # Identify models to clear (only those not in use)
        # Use snapshot to avoid nested lock acquisition
        models_to_clear = []
        for model_id in _simulator_cache.keys():
            if ref_counts_snapshot.get(model_id, 0) == 0:
                models_to_clear.append(model_id)
        
        # Evict unused models
        for model_id in models_to_clear:
            simulator = _simulator_cache.get(model_id)
            if simulator:
                _cleanup_simulator_safely(simulator)
            if model_id in _simulator_cache:
                del _simulator_cache[model_id]
            if model_id in _simulator_access_times:
                del _simulator_access_times[model_id]
            # Clear ref count (re-acquire lock for this operation)
            with _simulator_ref_lock:
                _simulator_ref_counts.pop(model_id, None)
        
        # Reset cache size to normal (was expanded for ensemble)
        _current_max_cache = MAX_SIMULATOR_CACHE_NORMAL
    
    # Clear elevation cache (will be reloaded on next use)
    elevation_cache = None
    
    # Light garbage collection to help free memory
    gc.collect()

    # Ensure we don't carry forced preload hints across cycles (cold start only)
    _clear_forced_preload()


def record_activity():
    """Record that the worker handled a request (used for idle cleanup)."""
    global _last_activity_timestamp
    _last_activity_timestamp = time.time()


def _idle_memory_cleanup(idle_duration):
    """
    Deep cleanup when the worker has been idle for a while.
    
    Returns True if cleanup ran, False if skipped (lock held or models in use).
    
    VERY CONSERVATIVE: Only runs if:
    1. No models are currently in use (ref_count == 0 for all)
    2. Worker has been idle for > 15 minutes
    3. Cleanup lock is available (not already running)
    
    This prevents cleanup from running during active work, which would evict
    simulators that are actively being used and break running simulations.
    """
    global _current_max_cache, elevation_cache
    worker_pid = os.getpid()
    
    # Try to acquire cleanup lock (non-blocking)
    # If already running, skip (prevents concurrent cleanup)
    if not _idle_cleanup_lock.acquire(blocking=False):
        return False
    try:
        # DEADLOCK PREVENTION: Get snapshot of ref counts BEFORE acquiring cache lock
        # This prevents nested lock acquisition (ref_lock -> cache_lock would deadlock)
        with _simulator_ref_lock:
            ref_counts_snapshot = dict(_simulator_ref_counts)
        
        with _cache_lock:
            cache_size = len(_simulator_cache)
            # Check if any models are in use via ref counts (use snapshot to avoid nested locks)
            # If any model has ref_count > 0, it's actively being used - don't clean up
            models_in_use = any(ref_counts_snapshot.get(mid, 0) > 0 for mid in _simulator_cache.keys())
        
        # Skip cleanup if models are in use or idle time is too short
        # 900 seconds = 15 minutes of true idle (no active simulations)
        if models_in_use or idle_duration < 900:
            return False
        
        rss_before = _get_rss_memory_mb()
        
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

def _count_ensemble_models():
    """Count ensemble models (0-20) in cache. Must be called while holding _cache_lock."""
    return len([m for m in _simulator_cache.keys() if isinstance(m, int) and m < 21])

def _should_preload_arrays():
    """Auto-detect if we should preload arrays.
    
    Returns:
        Tuple[bool, bool]: (should_preload, forced_hint_used)
    """
    if _has_forced_preload_hint():
        return True, True
    
    with _cache_lock:
        return _count_ensemble_models() >= 10, False

def _get_target_cache_size():
    """Auto-size cache based on current usage patterns.
    Returns target cache size (normal or ensemble) based on how many models are cached.
    NOTE: Must be called while holding _cache_lock."""
    if _count_ensemble_models() >= 10:
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
    """
    Trim cache to target size using LRU eviction, keeping most recently used models.
    
    CRITICAL: Uses reference counting to ensure simulators are only cleaned up when
    not in use. Never evicts a simulator with ref_count > 0 (actively in use).
    
    DEADLOCK PREVENTION: Acquires ref_lock BEFORE cache_lock to maintain consistent
    lock order across all functions. Uses snapshots to avoid nested lock acquisition.
    
    Adaptive sizing: Target size automatically adjusts based on workload (normal vs ensemble).
    """
    global _current_max_cache, _simulator_cache, _simulator_access_times
    
    # Update cache size based on current workload (normal or ensemble)
    _update_cache_size()
    
    with _cache_lock:
        target_size = _current_max_cache
        cache_size = len(_simulator_cache)
        
        # If cache is too large, trim to target size keeping most recently used
        if cache_size > target_size:
            # Sort by access time (most recent first) - LRU eviction
            sorted_models = sorted(_simulator_access_times.items(), key=lambda x: x[1], reverse=True)
            # Keep only the most recently used models (up to target_size)
            models_to_keep = {model_id for model_id, _ in sorted_models[:target_size]}
            
            # Evict models not in the keep list
            models_to_evict = set(_simulator_cache.keys()) - models_to_keep
        else:
            # Cache is not too large - nothing to evict
            models_to_evict = set()
    
    # DEADLOCK PREVENTION: Get snapshot of ref counts OUTSIDE cache_lock
    # Lock order must be: ref_lock -> cache_lock (consistent with simulate())
    # If we acquired cache_lock first, then ref_lock, we'd deadlock with simulate()
    with _simulator_ref_lock:
        ref_counts_snapshot = dict(_simulator_ref_counts)
    
    # CRITICAL: Filter out models with active references (ref_count > 0)
    # These simulators are actively being used - must NOT be evicted
    # Use snapshot to avoid nested lock acquisition
    models_to_evict = {m for m in models_to_evict if ref_counts_snapshot.get(m, 0) == 0}
    
    # Re-acquire cache_lock only to actually evict (if any models to evict)
    if models_to_evict:
        with _cache_lock:
            # RACE CONDITION PROTECTION: Double-check models still exist and aren't in use
            # Another thread may have evicted them or started using them since we checked
            models_to_evict = {m for m in models_to_evict if m in _simulator_cache and ref_counts_snapshot.get(m, 0) == 0}
            
            evicted_count = len(models_to_evict)
            
            # Remove from cache and clean up immediately
            # Simulators not in use (ref_count == 0) are safe to clean
            for model_id in models_to_evict:
                simulator = _simulator_cache.get(model_id)
                if simulator:
                    # Final safety check: verify no active references (use snapshot)
                    if ref_counts_snapshot.get(model_id, 0) == 0:
                        _cleanup_simulator_safely(simulator)
                del _simulator_cache[model_id]
                if model_id in _simulator_access_times:
                    del _simulator_access_times[model_id]
                # Clear ref count (re-acquire lock for this operation)
                with _simulator_ref_lock:
                    _simulator_ref_counts.pop(model_id, None)
        
        # Do GC outside lock to avoid blocking other threads
        # Holding locks during GC would serialize all threads
        if evicted_count > 0:
            gc.collect()  # Help GC reclaim memory from evicted simulators

def _periodic_cache_trim():
    """Background thread that periodically trims cache and handles idle cleanup.
    This ensures idle workers trim their cache even if they don't receive requests.
    Without this, each worker process maintains its own cache, and idle workers never trim.
    """
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
                time.sleep(5)
                continue
            
            # Regular cache trimming - check if cache is too large and trim if needed
            with _cache_lock:
                cache_size = len(_simulator_cache)
                target_size = _get_target_cache_size()  # Must be called while holding lock
                # Get snapshot of cache keys while holding lock
                cache_keys_snapshot = list(_simulator_cache.keys())
            
            # Get snapshot of ref counts BEFORE checking (prevents deadlock)
            with _simulator_ref_lock:
                ref_counts_snapshot = dict(_simulator_ref_counts)
            
            # If cache is larger than target, trim it
            if cache_size > target_size:
                # Check if any models are currently in use before trimming (use snapshot to avoid nested locks)
                models_in_use = any(ref_counts_snapshot.get(mid, 0) > 0 for mid in cache_keys_snapshot)
                
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
                        # Cache trim didn't reduce size enough - log warning
                        # This can happen if models are in use (protected from eviction)
                        print(f"WARNING: Cache trim incomplete: {new_size} > {target_size} (models may be in use)", flush=True)
                    time.sleep(3)
                else:
                    time.sleep(20)
            else:
                # Normal check interval - always call trim to handle edge cases
                _trim_cache_to_normal()
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

def _validate_simulator_cycle(simulator, currgefs):
    """Validate that cached simulator matches current GEFS cycle.
    
    Extracts GEFS timestamp from wind_file path (e.g., "2025110312_00.npz" -> "2025110312")
    and compares with current currgefs. Returns True if valid, False if stale.
    
    FIX: Problem 7 - Improved validation to handle more path formats and edge cases.
    Now tries multiple methods to extract cycle from path before rejecting.
    
    Args:
        simulator: Simulator instance to validate
        currgefs: Current GEFS timestamp to compare against
    
    Returns:
        True if simulator matches current cycle, False otherwise (rejects if cannot validate)
    """
    if not currgefs or currgefs == "Unavailable":
        # Can't validate without current cycle - reject to be safe
        return False
    
    try:
        # Try multiple methods to get the path
        wind_file_path = None
        
        # Method 1: Check _source_path attribute
        if hasattr(simulator, 'wind_file') and simulator.wind_file:
            wind_file_path = getattr(simulator.wind_file, '_source_path', None)
        
        # Method 2: Check if wind_file has a path attribute
        if not wind_file_path and hasattr(simulator, 'wind_file') and simulator.wind_file:
            wind_file_path = getattr(simulator.wind_file, 'path', None)
        
        # Method 3: Check if wind_file is a string (direct path)
        if not wind_file_path and hasattr(simulator, 'wind_file'):
            if isinstance(simulator.wind_file, str):
                wind_file_path = simulator.wind_file
        
        if not wind_file_path:
            # No source path - cannot validate, reject to force reload (safer than assuming valid)
            # This prevents using stale simulators when path information is missing
            return False
        
        # Convert to string and normalize path separators
        path_str = str(wind_file_path)
        # Handle both forward and backward slashes
        path_str = path_str.replace('\\', '/')
        
        # Extract filename from path
        filename = os.path.basename(path_str)
        
        # Try to extract timestamp from filename
        # Format 1: YYYYMMDDHH_NN.npz (standard format)
        if '_' in filename:
            cached_gefs = filename.split('_')[0]
            # Validate format (should be 10 digits: YYYYMMDDHH)
            if len(cached_gefs) == 10 and cached_gefs.isdigit():
                return cached_gefs == currgefs
        
        # Format 2: YYYYMMDDHHNN.npz (alternative format without underscore)
        # Check if filename starts with digits matching currgefs length
        if filename.startswith(currgefs):
            # Filename starts with current cycle - likely valid
            return True
        
        # Format 3: Check if path contains cycle as directory or filename component
        if currgefs in path_str:
            # Path contains current cycle - likely valid (but less certain)
            # Only accept if it's in the filename part, not just anywhere in path
            if currgefs in filename:
                return True
        
        # Can't extract or match timestamp - reject to force reload (safer than assuming valid)
        # This prevents using stale simulators when filename format is unexpected
        return False
    except Exception as e:
        # If validation fails, reject simulator (safe fallback)
        # Log the error for debugging but don't raise
        print(f"WARNING: Simulator cycle validation failed: {e}", flush=True)
        return False

def _get_simulator(model):
    """
    Get simulator for given model with dynamic multi-simulator LRU cache.
    
    Implements LRU eviction: when cache is full, evicts least recently used simulator
    that is not currently in use (ref_count == 0). Uses reference counting to prevent
    evicting simulators that are actively being used.
    
    DEADLOCK PREVENTION: Acquires ref_lock BEFORE cache_lock when checking ref counts.
    Uses snapshots to avoid nested lock acquisition.
    
    PERFORMANCE: Fast path for cache hits. Expensive operations (file I/O, simulator
    creation) done outside locks to reduce contention.
    """
    global _simulator_cache, _simulator_access_times, _last_refresh_check, _current_max_cache
    now = time.time()
    
    # Start background cache trim thread if not already started
    _start_cache_trim_thread()
    
    # Refresh GEFS data if needed (check every 5 minutes)
    # NOTE: refresh() uses file-based locking to prevent concurrent refreshes, but this
    # does NOT prevent cycle changes during simulator loading. Cycle validation is handled
    # in the retry loop below to catch changes during file download.
    currgefs = get_currgefs()
    if not currgefs or currgefs == "Unavailable" or now - _last_refresh_check > 300:
        refresh()
        _last_refresh_check = now
        currgefs = get_currgefs()
        
        if not currgefs or currgefs == "Unavailable":
            raise RuntimeError(f"GEFS timestamp not available after refresh (currgefs='{currgefs}'). Cannot load model files.")
    
    # Update cache size based on current workload (adaptive sizing)
    _update_cache_size()
    
    # Safety check: trim cache if it's too large (prevents memory leaks)
    # Background thread handles periodic trimming, but we check here for safety
    with _cache_lock:
        cache_too_large = len(_simulator_cache) > MAX_SIMULATOR_CACHE_ENSEMBLE
    
    if cache_too_large:
        _trim_cache_to_normal()
    
    with _cache_lock:
        # FAST PATH: Return cached simulator if available
        if model in _simulator_cache:
            simulator = _simulator_cache[model]
            # Verify simulator is still valid (wind_file.data hasn't been cleaned up)
            # This handles race condition where simulator was evicted/cleaned up
            if simulator:
                if not hasattr(simulator, 'wind_file') or simulator.wind_file is None:
                    # Simulator was partially cleaned up - remove from cache
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                elif getattr(simulator.wind_file, 'data', None) is None:
                    # Simulator was cleaned up but still in cache - remove it and recreate
                    del _simulator_cache[model]
                    del _simulator_access_times[model]
                else:
                    # CRITICAL: Check if cache was invalidated by reset()
                    # This ensures stale simulators are rejected even if ref_count > 0
                    cache_invalid = False
                    with _cache_invalidation_lock:
                        if _cache_invalidation_cycle is not None:
                            # Cache was invalidated - check if simulator matches invalidation cycle
                            if not _validate_simulator_cycle(simulator, _cache_invalidation_cycle):
                                cache_invalid = True
                    
                    # CRITICAL: Validate cached simulator matches current GEFS cycle
                    # Prevents using stale simulators after cycle change during prefetch
                    if not cache_invalid and _validate_simulator_cycle(simulator, currgefs):
                        # Valid simulator matching current cycle - update access time and return
                        _simulator_access_times[model] = now
                        return simulator
                    else:
                        # Cached simulator is from different GEFS cycle or was invalidated - remove it
                        if cache_invalid:
                            print(f"INFO: Cached simulator {model} invalidated by cycle change, forcing reload", flush=True)
                        del _simulator_cache[model]
                        del _simulator_access_times[model]
            else:
                # Invalid simulator - remove it
                del _simulator_cache[model]
                del _simulator_access_times[model]
        
        # Check if eviction is needed (still holding lock for atomic check)
        cache_size = len(_simulator_cache)
        needs_eviction = cache_size >= _current_max_cache
    
    # CACHE MISS: Need to load new simulator
    # If cache is full, evict oldest unused simulator first
    # Do expensive operations OUTSIDE the lock to reduce contention
    oldest_model = None
    if needs_eviction:
        # Get access times snapshot (need to release lock to avoid holding it during sorting)
        with _cache_lock:
            access_times_snapshot = dict(_simulator_access_times)
        
        # DEADLOCK PREVENTION: Get snapshot of ref counts BEFORE checking
        # Lock order must be: ref_lock -> cache_lock (consistent with simulate())
        with _simulator_ref_lock:
            ref_counts_snapshot = dict(_simulator_ref_counts)
        
        # Sort and find eviction candidate OUTSIDE locks (no nested locks)
        # Find oldest simulator that is not in use (ref_count == 0)
        sorted_models = sorted(access_times_snapshot.items(), key=lambda x: x[1])  # Oldest first
        for candidate_model, _ in sorted_models:
            if ref_counts_snapshot.get(candidate_model, 0) == 0:
                oldest_model = candidate_model
                break
        
        # Re-acquire lock only to actually evict
        if oldest_model:
            with _cache_lock:
                # RACE CONDITION PROTECTION: Double-check it still exists and isn't in use
                # Another thread may have evicted it or started using it since we checked
                if oldest_model in _simulator_cache and ref_counts_snapshot.get(oldest_model, 0) == 0:
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
    preload_arrays, forced_preload_hint = _should_preload_arrays()
    
    # Retry logic for FileNotFoundError and cycle changes
    # Files may not be uploaded yet after cycle change, or cycle may change during download
    max_retries = 4
    retry_delay = 2.0  # Start with 2 seconds
    simulator = None
    
    for attempt in range(max_retries):
        try:
            # CRITICAL: Re-validate currgefs at start of each attempt to catch cycle changes
            # Cycle could change between attempts or during file download
            current_gefs = get_currgefs()
            if not current_gefs or current_gefs == "Unavailable":
                raise RuntimeError(f"GEFS timestamp not available (currgefs='{current_gefs}')")
            
            # If cycle changed, update and invalidate cache
            if current_gefs != currgefs:
                print(f"INFO: GEFS cycle changed for model {model} (attempt {attempt + 1}): {currgefs} -> {current_gefs}", flush=True)
                currgefs = current_gefs
                # Remove stale cache entry if it exists (from old cycle)
                with _cache_lock:
                    if model in _simulator_cache:
                        del _simulator_cache[model]
                        if model in _simulator_access_times:
                            del _simulator_access_times[model]
            
            # Load file and create simulator
            model_file = f'{currgefs}_{str(model).zfill(2)}.npz'
            wind_file_path = load_gefs(model_file)
            
            # CRITICAL: Re-validate cycle AFTER file download but BEFORE creating simulator
            # This catches cycle changes during the download (which can take 10-30s for large files)
            post_download_gefs = get_currgefs()
            if post_download_gefs != currgefs:
                # Cycle changed during download - retry with new cycle
                started_gefs = currgefs  # Save for error message
                print(f"WARNING: GEFS cycle changed during file download for model {model}: {started_gefs} -> {post_download_gefs}. Retrying...", flush=True)
                currgefs = post_download_gefs
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
                else:
                    raise RuntimeError(f"GEFS cycle changed during download for model {model}: started with {started_gefs}, ended with {post_download_gefs}")
            
            # Create simulator (cycle validated, file downloaded)
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
            
            # Final validation: ensure cycle hasn't changed during simulator creation
            final_gefs = get_currgefs()
            if final_gefs != currgefs:
                # Cycle changed during simulator creation - retry
                started_gefs = currgefs  # Save for error message
                print(f"WARNING: GEFS cycle changed during simulator creation for model {model}: {started_gefs} -> {final_gefs}. Retrying...", flush=True)
                currgefs = final_gefs
                simulator = None
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
                else:
                    raise RuntimeError(f"GEFS cycle changed during simulator creation for model {model}: started with {started_gefs}, ended with {final_gefs}")
            
            break  # Success - exit retry loop
        except FileNotFoundError as e:
            if attempt < max_retries - 1:
                # Exponential backoff: 2s, 4s, 8s, 16s (30s total)
                wait_time = retry_delay * (2 ** attempt)
                print(f"WARNING: Model {model} file not found (attempt {attempt + 1}/{max_retries}), retrying in {wait_time:.1f}s...", flush=True)
                time.sleep(wait_time)
            else:
                # Final attempt failed - re-raise
                print(f"ERROR: Failed to load simulator for model {model} after {max_retries} attempts: {e}", flush=True)
                raise
        except Exception as e:
            # Non-FileNotFoundError - don't retry, fail immediately
            print(f"ERROR: Failed to load simulator for model {model}: {e}", flush=True)
            raise
    
    # Ensure simulator was successfully created
    if simulator is None:
        raise RuntimeError(f"Failed to create simulator for model {model}: simulator is None after retry loop")
    
    if forced_preload_hint:
        _consume_forced_preload_slot()
    
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
    func_start = time.time()
    
    # Check cache first
    cache_key = _cache_key(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient)
    cached_result = _get_cached_prediction(cache_key)
    if cached_result is not None:
        return cached_result
    
    # Acquire simulator reference (prevents cleanup while in use)
    _acquire_simulator_ref(model)
    try:
        simulator = _get_simulator(model)
        
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
        
        return path
        
    except Exception as e:
        # Don't cache errors
        total_time = time.time() - func_start
        print(f"ERROR: [PERF] simulate() FAILED: model={model}, time={total_time:.2f}s, error={e}", flush=True)
        raise e
    finally:
        # Release simulator reference
        _release_simulator_ref(model)


