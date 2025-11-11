import io
import os
import tempfile
import time
import logging
from pathlib import Path
from typing import Iterator
import threading

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.config import Config

# Try to load from .env file if available (non-fatal)
def _load_env_file():
    """Load environment variables from .env file if present.
    Does not override existing environment variables and does not raise on failure.
    """
    env_files = [Path('.env')]
    for env_file in env_files:
        if env_file.exists():
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            os.environ.setdefault(key.strip(), value.strip())
            except Exception:
                # Non-fatal: rely on existing os.environ
                pass

_load_env_file()

# AWS S3 configuration
_AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
_AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
_AWS_REGION = os.environ.get("AWS_REGION", "us-west-1")
_BUCKET = os.environ.get("S3_BUCKET_NAME", "habsim-storage")

# Validate that AWS credentials are set
if not _AWS_ACCESS_KEY_ID:
    raise ValueError("AWS_ACCESS_KEY_ID environment variable is not set. Please configure it in Railway settings.")
if not _AWS_SECRET_ACCESS_KEY:
    raise ValueError("AWS_SECRET_ACCESS_KEY environment variable is not set. Please configure it in Railway settings.")

# Configure boto3 with retries and connection pooling
# Increased to 64 connections for ensemble workloads (2 devices × 21 models = 42 concurrent downloads)
_S3_CONFIG = Config(
    retries={'max_attempts': 3, 'mode': 'adaptive'},
    max_pool_connections=64,  # Increased from 32 for high concurrency
    connect_timeout=15,
    read_timeout=60,
)

# Main S3 client for large file downloads (simulations)
_S3_CLIENT = boto3.client(
    's3',
    aws_access_key_id=_AWS_ACCESS_KEY_ID,
    aws_secret_access_key=_AWS_SECRET_ACCESS_KEY,
    region_name=_AWS_REGION,
    config=_S3_CONFIG,
)

# Separate S3 client for status checks (small files like whichgefs)
# This ensures status checks never wait behind large file downloads
_STATUS_S3_CONFIG = Config(
    retries={'max_attempts': 2, 'mode': 'adaptive'},
    max_pool_connections=4,
    connect_timeout=3,
    read_timeout=10,
)
_STATUS_S3_CLIENT = boto3.client(
    's3',
    aws_access_key_id=_AWS_ACCESS_KEY_ID,
    aws_secret_access_key=_AWS_SECRET_ACCESS_KEY,
    region_name=_AWS_REGION,
    config=_STATUS_S3_CONFIG,
)

# Timeout constants (kept for compatibility, but S3 uses boto3 config)
_DEFAULT_TIMEOUT = (3, 60)
_CACHEABLE_SUFFIXES = (".npz", ".npy")
# Use Railway persistent volume if available, fallback to tempdir
_default_cache_dir = None
if Path("/app/data").exists():  # Railway persistent volume mount
    _default_cache_dir = Path("/app/data/gefs")
else:
    _default_cache_dir = Path(tempfile.gettempdir()) / "habsim-gefs"
_CACHE_DIR = Path(os.environ.get("HABSIM_CACHE_DIR", _default_cache_dir))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_LOCK = threading.Lock()
_CHUNK_SIZE = 1024 * 1024
_MAX_CACHED_FILES = 25  # Allow 25 weather files (~7.7GB) - handles full 21-model ensemble + buffer

# Cache for whichgefs to reduce connection pool pressure (updates every 6 hours, but status checks every 5 seconds)
_whichgefs_cache = {"value": None, "timestamp": 0, "ttl": 60}  # Cache for 60 seconds
_whichgefs_lock = threading.Lock()

# Track files currently being downloaded to prevent premature deletion during cleanup
# Critical for multi-worker environments where cleanup could delete files mid-download
_downloading_files = set()
_downloading_lock = threading.Lock()

# Limit concurrent downloads to prevent connection pool exhaustion
# During ensemble, 21 models try to download simultaneously - too many for S3
# Semaphore limits to 4 concurrent downloads at a time across all workers
_download_semaphore = threading.Semaphore(4)

def listdir_gefs():
    """List all files in S3 bucket."""
    try:
        response = _S3_CLIENT.list_objects_v2(Bucket=_BUCKET)
        if 'Contents' not in response:
            return []
        return [obj['Key'] for obj in response['Contents']]
    except ClientError as e:
        logging.error(f"Failed to list S3 bucket {_BUCKET}: {e}")
        return []

def open_gefs(file_name):
    # Cache whichgefs locally to reduce connection pool pressure (status checks every 5 seconds)
    # Use separate status client to avoid blocking on large file downloads
    if file_name == 'whichgefs':
        now = time.time()
        
        # Fast path: check cache without lock (read-only)
        if (_whichgefs_cache["value"] is not None and 
            now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
            return io.StringIO(_whichgefs_cache["value"])
        
        # Cache miss or expired - need to download
        # Try to acquire lock, but if it's already being downloaded, return stale cache
        # This prevents status requests from blocking behind each other
        if not _whichgefs_lock.acquire(blocking=False):
            # Lock held by another thread downloading - return stale cache if available
            if _whichgefs_cache["value"] is not None:
                logging.debug("whichgefs cache expired but download in progress, returning stale cache")
                return io.StringIO(_whichgefs_cache["value"])
            # No stale cache available - wait briefly for lock (max 1 second)
            if _whichgefs_lock.acquire(blocking=True, timeout=1.0):
                try:
                    # Double-check cache (might have been updated while waiting)
                    if (_whichgefs_cache["value"] is not None and 
                        now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
                        return io.StringIO(_whichgefs_cache["value"])
                finally:
                    _whichgefs_lock.release()
            # If still can't get lock or cache still stale, return stale cache or fail gracefully
            if _whichgefs_cache["value"] is not None:
                return io.StringIO(_whichgefs_cache["value"])
            # Last resort: return empty string (will show as "Unavailable")
            return io.StringIO("")
        
        try:
            # Double-check cache after acquiring lock (another thread might have updated it)
            if (_whichgefs_cache["value"] is not None and 
                now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
                return io.StringIO(_whichgefs_cache["value"])
            
            # Cache miss or expired - download from S3 using status client
            # Status client has its own connection pool (4 connections) separate from main pool
            # Use shorter timeout for status requests (10s instead of 60s)
            try:
                response = _STATUS_S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
                content = response['Body'].read().decode("utf-8")
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey':
                    logging.warning(f"File not found in S3: {file_name}")
                    return io.StringIO("")
                raise
            
            # Update cache
            _whichgefs_cache["value"] = content
            _whichgefs_cache["timestamp"] = now
            
            return io.StringIO(content)
        finally:
            _whichgefs_lock.release()
    
    # Non-whichgefs files: download directly using main client (no caching needed)
    try:
        response = _S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
        content = response['Body'].read().decode("utf-8")
        return io.StringIO(content)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise FileNotFoundError(f"File not found in S3: {file_name}")
        raise

def load_gefs(file_name):
    """Load GEFS file from cache or download from S3.
    Returns path to cached file for memory-mapped access."""
    load_start = time.time()
    
    if _should_cache(file_name):
        result = str(_ensure_cached(file_name))
        load_time = time.time() - load_start
        if load_time > 5.0:
            logging.warning(f"[PERF] load_gefs() slow: {file_name}, time={load_time:.2f}s")
        elif load_time > 0.1:
            logging.debug(f"[PERF] load_gefs(): {file_name}, time={load_time:.3f}s")
        return result

    try:
        response = _S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
        return io.BytesIO(response['Body'].read())
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise FileNotFoundError(f"File not found in S3: {file_name}")
        raise

def download_gefs(file_name):
    if _should_cache(file_name):
        path = _ensure_cached(file_name)
        with open(path, 'rb') as fp:
            return fp.read()

    try:
        response = _S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
        return response['Body'].read()
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise FileNotFoundError(f"File not found in S3: {file_name}")
        raise


def _should_cache(file_name: str) -> bool:
    return file_name.endswith(_CACHEABLE_SUFFIXES)


def _cleanup_old_cache_files():
    """Remove least recently used cache files if we exceed file count OR size limits.
    This prevents disk bloat especially after GEFS cycle changes."""
    try:
        # Get all cached files with their access times
        cached_files = []
        for suffix in _CACHEABLE_SUFFIXES:
            cached_files.extend(_CACHE_DIR.glob(f"*{suffix}"))
        
        # Never evict worldelev.npy - it's required and large (451MB)
        # This file is loaded on-demand when users click on the map, so it must stay cached
        worldelev_file = None
        for f in cached_files:
            if f.name == 'worldelev.npy':
                worldelev_file = f
                break
        cached_files = [f for f in cached_files if f.name != 'worldelev.npy']
        
        # CRITICAL: Exclude files currently being downloaded by other workers
        # Without this, worker A could delete a file that worker B is actively downloading
        with _downloading_lock:
            downloading_names = _downloading_files.copy()
        cached_files = [f for f in cached_files if f.name not in downloading_names]
        
        # If worldelev.npy exists, ensure it's not too old (touch it to update access time)
        # This prevents it from being considered for eviction even if cleanup logic changes
        if worldelev_file and worldelev_file.exists():
            try:
                worldelev_file.touch()
            except:
                pass
        
        # Calculate total cache size (excluding worldelev.npy)
        # PERFORMANCE: Only calculate size if we're close to the file limit
        # Avoids expensive stat() calls on every download
        if len(cached_files) < _MAX_CACHED_FILES - 5:
            # Well under limit, skip size calculation
            return
        
        total_size_gb = sum(f.stat().st_size for f in cached_files) / (1024**3)
        
        # Sort by access time (oldest first) for LRU eviction
        cached_files.sort(key=lambda f: f.stat().st_atime)
        
        # Determine how many files to remove based on both count and size limits
        files_to_remove = 0
        
        # Check file count limit
        if len(cached_files) >= _MAX_CACHED_FILES:
            files_to_remove = len(cached_files) - _MAX_CACHED_FILES + 1  # +1 to make room for new file
        
        # Check size limit: Allow more headroom for concurrent old/new GEFS cycles
        # During GEFS change: old 21 files (~6.5GB) + new 21 files (~6.5GB) = ~13GB temporarily
        # Increased limits for 32GB RAM system
        MAX_NPZ_SIZE_GB = 25  # Trigger cleanup at 25GB (increased from 21GB)
        if total_size_gb > MAX_NPZ_SIZE_GB:
            # Remove files until we're under 24GB (leaves room for growth)
            TARGET_SIZE_GB = 24
            current_size = total_size_gb
            for i, f in enumerate(cached_files):
                if current_size <= TARGET_SIZE_GB:
                    break
                try:
                    file_size_gb = f.stat().st_size / (1024**3)
                    current_size -= file_size_gb
                    files_to_remove = max(files_to_remove, i + 1)
                except:
                    pass
        
        # Remove the determined number of oldest files
        if files_to_remove > 0:
            import logging
            removed_count = 0
            removed_size = 0
            for i in range(min(files_to_remove, len(cached_files))):
                try:
                    file_size = cached_files[i].stat().st_size
                    cached_files[i].unlink()
                    removed_count += 1
                    removed_size += file_size
                except Exception:
                    pass  # File might have been removed by another thread
            
            if removed_count > 0:
                logging.info(f"Cache cleanup: removed {removed_count} files ({removed_size/(1024**3):.2f}GB), "
                           f"cache now {(total_size_gb - removed_size/(1024**3)):.2f}GB")
    except Exception as e:
        import logging
        logging.debug(f"Cache cleanup error (non-critical): {e}")


def _ensure_cached(file_name: str) -> Path:
    """
    Ensure a GEFS file is cached on disk, downloading from S3 if necessary.
    
    This function implements robust S3 download with the following reliability features:
    
    **File Handle Management:**
    - Uses context managers (`with open()`) to ensure files are always closed
    - Proper cleanup of file handles even on exceptions
    - os.fsync() to ensure data is synced to disk (with error handling)
    
    **Exception Handling:**
    - Distinguishes fatal errors (file not found) from retryable errors (network issues)
    - Catches ClientError (S3 API errors), IOError (write/stall errors), and unexpected errors
    - All errors are properly logged with retry attempt information
    
    **Flushing and Syncing:**
    - Periodic flushing every 10MB during download to prevent data loss
    - os.fsync() before closing to ensure data is persisted to disk
    - Error handling for fsync failures (non-critical on some file systems)
    
    **Socket Timeout:**
    - Configures socket timeout for large files (30 minutes) if supported
    - Gracefully handles cases where timeout is not supported
    
    **Temporary File Cleanup:**
    - Partial downloads are always cleaned up on failure
    - Cleanup happens in all error paths (IOError, ClientError, unexpected)
    - Previous incomplete downloads are cleaned up before retry attempts
    
    **File Integrity Verification:**
    - Verifies file size matches expected ContentLength from S3
    - Validates file is non-empty and not suspiciously small
    - NPZ file structure validation before committing to cache
    
    **Retry Logic:**
    - Up to 5 retries for NPZ files, 3 for other large files
    - Exponential backoff (2s, 4s, 8s, 16s, 32s)
    - Distinguishes retryable vs fatal errors in logs
    
    Returns:
        Path to cached file (ready for memory-mapped access)
        
    Raises:
        FileNotFoundError: If file doesn't exist in S3 (fatal, no retry)
        IOError: For retryable errors (network, incomplete downloads, etc.)
    """
    cache_path = _CACHE_DIR / file_name
    
    # Fast path: Check if file exists BEFORE any locking
    # This is the common case and should be as fast as possible
    # Special handling for worldelev.npy - always check if it exists and is valid
    # This file is critical for elevation lookups when users click on the map
    if file_name == 'worldelev.npy':
        if cache_path.exists():
            # Verify file is not corrupted (check size)
            try:
                file_size = cache_path.stat().st_size
                expected_size = 451008128  # Expected size for worldelev.npy
                if file_size == expected_size:
                    cache_path.touch()  # Update access time
                    logging.debug(f"File cache HIT: {file_name} (no S3 egress)")
                    return cache_path
                else:
                    # File exists but is wrong size - delete it and re-download
                    logging.warning(f"{file_name} exists but is corrupted (expected {expected_size} bytes, got {file_size}). Re-downloading...")
                    cache_path.unlink()
            except Exception as e:
                logging.warning(f"Error checking {file_name}: {e}. Re-downloading...")
                try:
                    cache_path.unlink()
                except:
                    pass
    
    if cache_path.exists():
        # Validate NPZ integrity on cache hit; delete and re-download if corrupted
        try:
            if file_name.endswith('.npz'):
                import numpy as np
                with np.load(cache_path) as _:
                    pass
        except Exception as e:
            logging.warning(f"Cached file appears corrupted ({file_name}): {e}. Re-downloading...")
            try:
                cache_path.unlink()
            except Exception:
                pass
        else:
            # Update access time to mark as recently used
            cache_path.touch()
            logging.debug(f"[CACHE] HIT: {file_name} (no S3 egress)")
            return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with _CACHE_LOCK:
        if cache_path.exists():
            cache_path.touch()
            logging.debug(f"[CACHE] HIT (double-check): {file_name}")
            return cache_path

        # Clean up old files before downloading new one
        cleanup_start = time.time()
        _cleanup_old_cache_files()
        cleanup_time = time.time() - cleanup_start
        if cleanup_time > 1.0:
            logging.warning(f"[PERF] _cleanup_old_cache_files() slow: time={cleanup_time:.2f}s")
    
    # CRITICAL: Mark file as downloading ONLY after cache miss confirmed
    # This prevents false positives where cache hits would leave files in the set
    with _downloading_lock:
        _downloading_files.add(file_name)
    
    # For large files, prevent concurrent downloads of the same file ACROSS PROCESSES
    # This avoids connection pool exhaustion and partial download conflicts
    # Use file-based locking (fcntl) which works across Gunicorn worker processes
    is_large_file = file_name == 'worldelev.npy' or file_name.endswith('.npy')
    lock_file = None
    lock_fd = None
    if is_large_file:
        # Create a lock file for inter-process coordination
        lock_file = _CACHE_DIR / f".{file_name}.lock"
        try:
            # Open lock file (create if doesn't exist)
            lock_fd = open(lock_file, 'a')
            
            # Try to acquire exclusive lock (non-blocking)
            import fcntl
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                logging.debug(f"Acquired download lock for {file_name}")
            except BlockingIOError:
                # Another process is downloading - wait and check if it completed
                logging.info(f"Another process is downloading {file_name}, waiting for completion...")
                
                # Wait up to 5 minutes for the download to complete
                for i in range(300):  # 300 * 1s = 5 minutes
                    time.sleep(1)
                    if cache_path.exists() and cache_path.stat().st_size > 0:
                        # File was downloaded by another process
                        lock_fd.close()
                        cache_path.touch()
                        # Remove from downloading set before returning
                        with _downloading_lock:
                            _downloading_files.discard(file_name)
                        logging.info(f"File {file_name} was downloaded by another process (cache HIT)")
                        return cache_path
                    
                    # Log progress every 30 seconds
                    if i % 30 == 0 and i > 0:
                        logging.info(f"Still waiting for {file_name} download ({i}s elapsed)...")
                
                # After 5 minutes, acquire lock (blocking) to download ourselves
                logging.warning(f"Timeout waiting for {file_name} download, acquiring lock...")
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            
            # Double-check file wasn't created while waiting for lock
            if cache_path.exists() and cache_path.stat().st_size > 0:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
                cache_path.touch()
                # Remove from downloading set before returning
                with _downloading_lock:
                    _downloading_files.discard(file_name)
                logging.debug(f"File {file_name} exists after acquiring lock (cache HIT)")
                return cache_path
        except Exception as e:
            logging.warning(f"File locking error for {file_name} (non-critical): {e}")
            if lock_fd:
                try:
                    lock_fd.close()
                except:
                    pass
            lock_fd = None

    # CRITICAL: Acquire download semaphore to limit concurrent downloads
    # This prevents connection pool exhaustion when ensemble downloads 21 files
    # Semaphore allows max 4 concurrent downloads across all workers
    _download_semaphore.acquire()
    try:
        # Log download attempt (this will use S3 egress)
        download_start = time.time()
        logging.info(f"[CACHE] MISS: {file_name} - downloading from S3 (will use egress)")
        
        # Use longer timeout for large files
        # NPZ wind files are ~300MB, worldelev.npy is 451MB
        is_large_file = file_name == 'worldelev.npy' or file_name.endswith('.npz') or file_name.endswith('.npy')
        
        # Retry logic for large file downloads (up to 5 attempts for NPZ files)
        # NPZ wind files often have network interruptions due to size (~300MB)
        max_retries = 5 if file_name.endswith('.npz') else (3 if is_large_file else 1)
        last_error = None
        
        for attempt in range(max_retries):
            # Clean up any incomplete temp files from previous attempts
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                    logging.debug(f"Cleaned up incomplete download: {file_name}")
                except:
                    pass
            
            try:
                # Get object metadata first to check if it exists and get size
                # This helps distinguish between fatal (file not found) and retryable errors
                try:
                    head_response = _S3_CLIENT.head_object(Bucket=_BUCKET, Key=file_name)
                    expected_size = head_response.get('ContentLength')
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    if error_code in ('404', 'NoSuchKey'):
                        # File not found - fatal error, don't retry
                        error_msg = f"File not found in S3: {file_name}"
                        logging.error(error_msg)
                        raise FileNotFoundError(f"{error_msg}. The model file may not have been uploaded yet, or the model timestamp may be incorrect. Check S3 storage or verify the model timestamp in 'whichgefs'.")
                    # Other S3 errors (403, 500, etc.) - retryable
                    logging.warning(f"S3 head_object failed for {file_name}: {e} (retryable, attempt {attempt + 1}/{max_retries})")
                    raise IOError(f"S3 metadata error: {e}")
                
                # Download file with streaming
                # Use streaming mode to handle large files efficiently
                response = None
                body = None
                try:
                    response = _S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
                    body = response['Body']
                    
                    # Configure socket timeout for large files (if supported)
                    # Some response bodies may not support this, so check first
                    if hasattr(body, 'set_socket_timeout'):
                        try:
                            body.set_socket_timeout(1800)  # 30 minutes
                        except (AttributeError, TypeError):
                            # Non-critical: socket timeout not supported, continue anyway
                            pass
                    
                    # Create temp file and download with proper resource management
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    bytes_written = 0
                    last_chunk_time = time.time()
                    
                    # Use context manager for file handle to ensure it's always closed
                    with open(tmp_path, 'wb') as fh:
                        try:
                            while True:
                                chunk = body.read(_CHUNK_SIZE)
                                if not chunk:
                                    break
                                
                                current_time = time.time()
                                
                                # Check for connection timeout (no data for 120 seconds)
                                # This detects stalled downloads (retryable)
                                if is_large_file and (current_time - last_chunk_time) > 120:
                                    raise IOError(f"Download stalled: no data received for 120 seconds (retryable)")
                                
                                fh.write(chunk)
                                bytes_written += len(chunk)
                                last_chunk_time = current_time
                                
                                # Flush periodically for large files to ensure data is written to disk
                                # This prevents data loss if process crashes mid-download
                                if is_large_file and bytes_written % (10 * 1024 * 1024) < _CHUNK_SIZE:
                                    fh.flush()
                                
                                # For large files, log progress every 50MB
                                if is_large_file and bytes_written % (50 * 1024 * 1024) < _CHUNK_SIZE:
                                    mb_written = bytes_written / (1024 * 1024)
                                    if expected_size:
                                        mb_total = expected_size / (1024 * 1024)
                                        logging.info(f"Downloading {file_name}: {mb_written:.1f}MB / {mb_total:.1f}MB ({100 * bytes_written / expected_size:.1f}%)")
                                    else:
                                        logging.info(f"Downloading {file_name}: {mb_written:.1f}MB")
                            
                            # Flush and sync to disk before closing (ensures data is persisted)
                            fh.flush()
                            try:
                                # os.fsync() ensures data is written to disk, not just buffer
                                # This is critical for large files to prevent data loss
                                os.fsync(fh.fileno())
                            except (OSError, AttributeError):
                                # Non-critical: fsync failed (e.g., on some file systems)
                                # Data should still be written due to flush()
                                pass
                        except Exception as write_error:
                            # Write error during download - clean up partial file
                            # This is a retryable error
                            raise IOError(f"Error writing {file_name} (wrote {bytes_written} bytes): {write_error}")
                except ClientError as s3_error:
                    # S3 API errors (throttling, network issues, etc.) - retryable
                    error_code = s3_error.response.get('Error', {}).get('Code', '')
                    if error_code in ('403', '429', '500', '502', '503', '504'):
                        # Throttling or server errors - definitely retryable
                        logging.warning(f"S3 download error for {file_name}: {error_code} (retryable, attempt {attempt + 1}/{max_retries})")
                        raise IOError(f"S3 error ({error_code}): {s3_error}")
                    else:
                        # Other S3 errors - log and retry
                        logging.warning(f"S3 download error for {file_name}: {s3_error} (retryable, attempt {attempt + 1}/{max_retries})")
                        raise IOError(f"S3 error: {s3_error}")
                except IOError:
                    # Re-raise IOErrors (stall detection, write errors) - already retryable
                    raise
                except Exception as unexpected_error:
                    # Unexpected errors - log and treat as retryable
                    logging.warning(f"Unexpected error downloading {file_name}: {unexpected_error} (retryable, attempt {attempt + 1}/{max_retries})")
                    raise IOError(f"Unexpected download error: {unexpected_error}")
                finally:
                    # Ensure S3 response body is closed to free resources
                    if body is not None:
                        try:
                            body.close()
                        except:
                            pass
                
                # Verify download completed successfully
                # This is a critical step to ensure file integrity before committing
                if not tmp_path.exists():
                    raise IOError(f"Download failed: temp file not created for {file_name} (request succeeded but no file written)")
                
                actual_size = tmp_path.stat().st_size
                if actual_size == 0:
                    # Empty file - fatal error, don't retry (file exists but is corrupted)
                    raise IOError(f"Download failed: file {file_name} is empty (fatal)")
                
                # Verify file size matches expected size (if available)
                # This catches incomplete downloads that weren't detected during streaming
                if expected_size and actual_size != expected_size:
                    # Size mismatch - retryable error (download was incomplete)
                    raise IOError(f"Download incomplete: {file_name} expected {expected_size} bytes, got {actual_size} (retryable)")
                
                # Final verification: ensure file is readable and non-empty
                # This catches edge cases where file exists but is corrupted
                if actual_size < 1024:  # Files should be at least 1KB
                    raise IOError(f"Download failed: file {file_name} is suspiciously small ({actual_size} bytes) (fatal)")
                
                # Log successful download with size (for egress tracking)
                size_mb = actual_size / (1024 * 1024)
                logging.info(f"Downloaded {file_name} from S3: {size_mb:.2f} MB (egress used)")
                
                # Validate NPZ file structure before committing
                if file_name.endswith('.npz'):
                    try:
                        import numpy as np
                        test_npz = np.load(tmp_path)
                        test_npz.close()  # Close immediately after validation
                    except Exception as e:
                        raise IOError(f"Downloaded file {file_name} is corrupted (invalid NPZ): {e}")
                
                # Success - break out of retry loop
                break
            except FileNotFoundError:
                # File not found in S3 - fatal error, don't retry
                # Clean up any partial download and release lock
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                if lock_fd:
                    try:
                        import fcntl
                        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                        lock_fd.close()
                    except:
                        pass
                # Re-raise immediately (no retry for missing files)
                raise
            except IOError as e:
                # Retryable errors: network issues, incomplete downloads, write errors, stalls
                # Clean up incomplete download before retry
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                last_error = e
                if attempt < max_retries - 1:
                    # Wait before retry (exponential backoff: 2s, 4s, 8s, 16s, 32s)
                    wait_time = 2 ** (attempt + 1)
                    # Check if error message indicates fatal vs retryable
                    error_str = str(e).lower()
                    if 'fatal' in error_str:
                        # Fatal error - don't retry
                        logging.error(f"Download failed with fatal error for {file_name}: {e}")
                        if lock_fd:
                            try:
                                import fcntl
                                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                                lock_fd.close()
                            except:
                                pass
                        raise
                    else:
                        # Retryable error - log and retry
                        logging.warning(f"Download attempt {attempt + 1}/{max_retries} failed for {file_name}: {e}. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                else:
                    # Last attempt failed - release lock and clean up
                    logging.error(f"Download failed after {max_retries} attempts for {file_name}: {last_error}")
                    if lock_fd:
                        try:
                            import fcntl
                            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                            lock_fd.close()
                        except:
                            pass
                    raise
            except Exception as e:
                # Unexpected errors - treat as retryable but log as warning
                # Clean up incomplete download
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logging.warning(f"Unexpected error on download attempt {attempt + 1}/{max_retries} for {file_name}: {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    # Last attempt failed - release lock and clean up
                    logging.error(f"Download failed after {max_retries} attempts for {file_name} (unexpected error): {last_error}")
                    if lock_fd:
                        try:
                            import fcntl
                            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                            lock_fd.close()
                        except:
                            pass
                    raise
        
        # If we get here, download succeeded (tmp_path exists from successful attempt)
        # Note: tmp_path is defined in the loop, so we need to reconstruct it here
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        if not tmp_path.exists():
            raise IOError(f"Download failed: temp file not created for {file_name} after {max_retries} attempts")
        
        # Rename temp file to final cache location
        # Double-check temp file exists right before rename to avoid race conditions
        try:
            if not tmp_path.exists():
                raise IOError(f"Download failed: temp file {tmp_path} was deleted before rename (race condition or cleanup issue)")
            
            # Get file size before rename for verification
            tmp_size = tmp_path.stat().st_size
            if tmp_size == 0:
                raise IOError(f"Download failed: temp file {file_name} is empty before rename")
            
            os.replace(tmp_path, cache_path)
            
            # Verify final file exists and is not empty
            if not cache_path.exists():
                raise IOError(f"Downloaded file {file_name} is missing after rename (temp file was {tmp_size} bytes)")
            
            if cache_path.stat().st_size == 0:
                raise IOError(f"Downloaded file {file_name} is empty after rename (temp file was {tmp_size} bytes)")
            
            if cache_path.stat().st_size != tmp_size:
                raise IOError(f"Downloaded file {file_name} size mismatch after rename (expected {tmp_size} bytes, got {cache_path.stat().st_size} bytes)")
            
            download_time = time.time() - download_start
            logging.info(f"Cached {file_name} to disk in {download_time:.1f}s: {cache_path} (future reads will use zero egress)")
        except FileNotFoundError as e:
            # Temp file was deleted between check and rename (race condition)
            error_msg = f"Download failed: temp file {tmp_path} not found during rename for {file_name}. This may indicate a race condition or premature cleanup."
            logging.error(error_msg)
            raise IOError(error_msg) from e
        except OSError as e:
            # File system error during rename
            error_msg = f"Download failed: error renaming temp file for {file_name}: {e}"
            logging.error(error_msg)
            raise IOError(error_msg) from e
        
        # Release the file lock after successful download and rename
        if lock_fd:
            try:
                import fcntl
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
                logging.debug(f"Released download lock for {file_name}")
                
                # Clean up lock file on successful download to prevent accumulation
                # Lock files are small but can accumulate over time
                if lock_file and lock_file.exists():
                    try:
                        lock_file.unlink()
                        logging.debug(f"Removed lock file: {lock_file.name}")
                    except:
                        pass  # Non-critical if cleanup fails
            except Exception as lock_error:
                logging.warning(f"Error releasing lock for {file_name}: {lock_error}")

        # Final check that file exists
        if not cache_path.exists():
            raise FileNotFoundError(f"Cached file {file_name} not found at {cache_path}")
        
        return cache_path
    finally:
        # CRITICAL: Always remove from downloading set, even on error
        # This ensures the set doesn't grow unbounded with failed download attempts
        with _downloading_lock:
            _downloading_files.discard(file_name)
        
        # Always release download semaphore, even on error
        _download_semaphore.release()


def upload_gefs(file_path: Path, file_name: str) -> bool:
    """Upload a file to S3 bucket.
    
    Args:
        file_path: Local path to file to upload
        file_name: Name to store file as in bucket (S3 key)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Use upload_file which automatically handles multipart uploads for large files
        _S3_CLIENT.upload_file(
            str(file_path),
            _BUCKET,
            file_name,
            ExtraArgs={'ContentType': 'application/octet-stream'}
        )
        return True
    except Exception as e:
        logging.error(f"Failed to upload {file_name} to S3: {e}")
        return False


def delete_gefs(file_name: str) -> bool:
    """Delete a file from S3 bucket.
    
    Args:
        file_name: Name of file to delete from bucket (S3 key)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        _S3_CLIENT.delete_object(Bucket=_BUCKET, Key=file_name)
        return True
    except Exception as e:
        logging.warning(f"Failed to delete {file_name} from S3: {e}")
        return False
