"""
GEFS (Global Ensemble Forecast System) data management from AWS S3.

Handles downloading, caching, and serving GEFS weather model files.
Uses S3 TransferManager for multipart parallel downloads (faster, more resilient).
Implements LRU cache with disk persistence and connection pooling.
Provides load_gefs() for memory-mapped file access and open_gefs() for text files.
"""
import io
import os
import tempfile
import time
import logging
from pathlib import Path
import threading

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

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

# Log AWS configuration (without exposing secrets)
print(f"INFO: AWS S3 configured: region={_AWS_REGION}, bucket={_BUCKET}", flush=True)

# Configure boto3 with retries and connection pooling
# Increased to 64 connections for ensemble workloads (2 devices × 21 models = 42 concurrent downloads)
_S3_CONFIG = Config(
    retries={'max_attempts': 3, 'mode': 'adaptive'},
    max_pool_connections=64,  # Increased from 32 for high concurrency
    connect_timeout=15,
    read_timeout=60,
)

# Main S3 client for large file downloads (simulations)
# Explicitly pass credentials to avoid boto3 credential chain picking up wrong credentials
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

# S3 TransferManager configuration for multipart parallel downloads
# Optimized for large files (300-450MB): uses 8MB chunks with 16 parallel threads
# TransferManager handles retries automatically, so we don't need manual retry logic
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=1024 * 1024 * 8,  # 8MB - files larger than this use multipart
    max_concurrency=16,  # Parallel threads for multipart downloads
    multipart_chunksize=1024 * 1024 * 8,  # 8MB chunks
    use_threads=True  # Use threads for parallel chunk downloads
)

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
_MAX_CACHED_FILES = 30  # Allow 30 weather files (~9.2GB) - increased for 32GB RAM system, handles full 21-model ensemble + buffer

# Lightweight whichgefs cache: we still check the ETag every poll, but only download the body
# when the hash changes so we can detect cycle flips without hammering S3.
_whichgefs_cache = {"value": None, "timestamp": 0, "ttl": 15, "etag": None}
_whichgefs_lock = threading.Lock()

# Track files currently being downloaded to prevent premature deletion during cleanup
# Critical for multi-worker environments where cleanup could delete files mid-download
_downloading_files = set()
_downloading_lock = threading.Lock()

# Track recently downloaded files with timestamps to protect them from cleanup
# Files are protected for 5 minutes after download to prevent race conditions where
# cleanup deletes files before other workers can use them
_recently_downloaded = {}  # {file_name: download_timestamp}
_recently_downloaded_lock = threading.Lock()
_RECENT_DOWNLOAD_GRACE_PERIOD = 300  # 5 minutes protection

def _finalize_cached_file(file_name: str, cache_path: Path) -> Path:
    """Finalize a cached file: touch it, remove from downloading set, and protect from cleanup.
    
    This helper function consolidates the common pattern of finalizing a file that was
    downloaded (either by this worker or another worker). It ensures the file is:
    1. Touched to update access time
    2. Removed from downloading set
    3. Marked as recently downloaded to protect from cleanup
    
    Args:
        file_name: Name of the file
        cache_path: Path to the cached file
        
    Returns:
        The cache_path (for chaining)
    """
    cache_path.touch()  # Update access time
    with _downloading_lock:
        _downloading_files.discard(file_name)
    with _recently_downloaded_lock:
        _recently_downloaded[file_name] = time.time()
    return cache_path

# Limit concurrent downloads to prevent connection pool exhaustion
# During ensemble, 21 models try to download simultaneously - too many for S3
# Increased from 8 to 16 to reduce prefetch starvation (H3 from code review)
# Progressive prefetch waits for 12 models - with 16 concurrent, less blocking
# With 4 workers × 16 downloads = 64 max concurrent, at connection pool limit
_download_semaphore = threading.Semaphore(16)

def _release_file_lock(lock_fd):
    """Release file lock and close file descriptor. Safe to call multiple times."""
    if lock_fd:
        try:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass

def listdir_gefs():
    """List all files in S3 bucket."""
    try:
        response = _S3_CLIENT.list_objects_v2(Bucket=_BUCKET)
        if 'Contents' not in response:
            return []
        return [obj['Key'] for obj in response['Contents']]
    except ClientError as e:
        print(f"ERROR: Failed to list S3 bucket {_BUCKET}: {e}", flush=True)
        return []

def open_gefs(file_name):
    """Open GEFS file from S3. Uses head_object for whichgefs to reduce bandwidth/cost."""
    if file_name == 'whichgefs':
        now = time.time()
        
        # Try to acquire lock (non-blocking first, then blocking with timeout)
        if not _whichgefs_lock.acquire(blocking=False):
            # Another thread is fetching - wait briefly and check if it completed
            if _whichgefs_lock.acquire(blocking=True, timeout=1.0):
                try:
                    # Check if cache was updated by other thread (it will have checked ETag)
                    if (_whichgefs_cache["value"] is not None and 
                        now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
                        return io.StringIO(_whichgefs_cache["value"])
                finally:
                    _whichgefs_lock.release()
            # If still no cache, return cached value if available (fallback)
            if _whichgefs_cache["value"] is not None:
                return io.StringIO(_whichgefs_cache["value"])
            return io.StringIO("")
        
        try:
            # CRITICAL: Always check ETag to detect cycle changes immediately
            # Even if cache is fresh, we must verify file hasn't changed
            try:
                # Use head_object to check ETag (reduces bandwidth/cost)
                response = _STATUS_S3_CLIENT.head_object(Bucket=_BUCKET, Key=file_name)
                etag = response.get('ETag', '').strip('"')
                
                # If cache is fresh AND ETag matches, return cached value
                if (_whichgefs_cache["value"] is not None and 
                    _whichgefs_cache["etag"] is not None and
                    _whichgefs_cache["etag"] == etag and
                    now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
                    # File hasn't changed - return cached value
                    _whichgefs_cache["timestamp"] = now  # Update timestamp
                    return io.StringIO(_whichgefs_cache["value"])
                
                # ETag mismatch or cache expired - fetch new content
                # whichgefs is tiny (~10 bytes), so this is still efficient
                content_response = _STATUS_S3_CLIENT.get_object(Bucket=_BUCKET, Key=file_name)
                content = content_response['Body'].read().decode("utf-8")
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                error_msg = e.response.get('Error', {}).get('Message', str(e))
                print(f"ERROR: S3 error reading {file_name}: Code={error_code}, Message={error_msg}", flush=True)
                if error_code == 'NoSuchKey':
                    print(f"WARNING: File not found in S3: {file_name}", flush=True)
                    return io.StringIO("")
                raise
            except Exception as e:
                print(f"ERROR: Unexpected error reading {file_name} from S3: {type(e).__name__}: {e}", flush=True)
                raise
            
            # Update cache with new content and ETag
            _whichgefs_cache["value"] = content
            _whichgefs_cache["timestamp"] = now
            _whichgefs_cache["etag"] = etag
            return io.StringIO(content)
        finally:
            _whichgefs_lock.release()
    
    # For non-whichgefs files, use get_object (normal text files)
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
        # Check if file is already cached (cache hit vs miss)
        cache_path = _CACHE_DIR / file_name
        was_cached = cache_path.exists()
        
        result = str(_ensure_cached(file_name))
        total_time = time.time() - load_start
        
        # Log performance warnings with context
        # Cache hits should be fast (<1s), cache misses include download time
        if was_cached and total_time > 1.0:
            print(f"WARNING: [PERF] load_gefs() slow (cache hit): {file_name}, time={total_time:.2f}s", flush=True)
        elif not was_cached and total_time > 30.0:
            # Cache miss includes download - warn if >30s (download + validation)
            print(f"WARNING: [PERF] load_gefs() slow (cache miss): {file_name}, time={total_time:.2f}s", flush=True)
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
        
        # CRITICAL: Exclude recently downloaded files (within grace period)
        # Prevents race condition where cleanup deletes files before other workers use them
        now = time.time()
        with _recently_downloaded_lock:
            # Remove expired entries in-place instead of reassigning the dict
            # Reassignment would require a global declaration and previously caused the
            # entire cleanup routine to raise UnboundLocalError (and silently abort).
            protected_files = set()
            expired_keys = []
            for file_name, download_time in list(_recently_downloaded.items()):
                if now - download_time < _RECENT_DOWNLOAD_GRACE_PERIOD:
                    protected_files.add(file_name)
                else:
                    expired_keys.append(file_name)
            for expired_name in expired_keys:
                _recently_downloaded.pop(expired_name, None)
        cached_files = [f for f in cached_files if f.name not in protected_files]
        
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
                # Recalculate actual cache size after cleanup (account for concurrent downloads)
                # Re-scan cache directory to get accurate size including any new files added during cleanup
                remaining_files = []
                for suffix in _CACHEABLE_SUFFIXES:
                    remaining_files.extend(_CACHE_DIR.glob(f"*{suffix}"))
                # Exclude worldelev.npy from size calculation
                remaining_files = [f for f in remaining_files if f.name != 'worldelev.npy' and f.exists()]
                actual_size_gb = sum(f.stat().st_size for f in remaining_files) / (1024**3)
                print(f"INFO: Cache cleanup: removed {removed_count} files ({removed_size/(1024**3):.2f}GB), "
                           f"cache now {actual_size_gb:.2f}GB")
    except Exception as e:
        pass


def _ensure_cached(file_name: str) -> Path:
    """Ensure a GEFS file is cached on disk, downloading from S3 if necessary.
    
    Uses S3 TransferManager for multipart parallel downloads (faster, more resilient).
    TransferManager handles retries automatically. Implements file integrity verification
    and proper cleanup. Returns path to cached file for memory-mapped access.
    
    Raises:
        FileNotFoundError: If file doesn't exist in S3 (fatal, no retry)
        IOError: For retryable errors (network, incomplete downloads, etc.)
    """
    cache_path = _CACHE_DIR / file_name
    
    if cache_path.exists():
        try:
            if file_name.endswith('.npz'):
                import numpy as np
                with np.load(cache_path) as _:
                    pass
        except Exception as e:
            print(f"WARNING: {file_name} corrupted, re-downloading", flush=True)
            try:
                cache_path.unlink()
            except Exception:
                pass
        else:
            cache_path.touch()
            return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with _CACHE_LOCK:
        if cache_path.exists():
            cache_path.touch()
            return cache_path

        # Clean up old files before downloading new one
        _cleanup_old_cache_files()
    
    # CRITICAL: Mark file as downloading ONLY after cache miss confirmed
    # This prevents false positives where cache hits would leave files in the set
    # The _downloading_files set is used by cleanup to avoid deleting files mid-download
    with _downloading_lock:
        _downloading_files.add(file_name)
    
    # INTER-PROCESS COORDINATION: For large files, prevent concurrent downloads across workers
    # 
    # PROBLEM: Multiple Gunicorn workers may try to download the same file simultaneously.
    # This causes:
    # 1. Connection pool exhaustion (too many concurrent S3 connections)
    # 2. Partial download conflicts (multiple workers writing to same file)
    # 3. Wasted bandwidth (downloading same file multiple times)
    #
    # SOLUTION: Use file-based locking (fcntl) which works across processes (not just threads).
    # Only one worker downloads, others wait and then use the completed file.
    #
    # Large files: worldelev.npy (451MB) and .npz files (model files are ~300MB each)
    is_large_file = file_name == 'worldelev.npy' or file_name.endswith(('.npz', '.npy'))
    lock_file = None
    lock_fd = None
    if is_large_file:
        # Create a lock file for inter-process coordination
        # Lock file name: .{filename}.lock (e.g., .2025110306_00.npz.lock)
        lock_file = _CACHE_DIR / f".{file_name}.lock"
        try:
            # Open lock file (create if doesn't exist)
            # Using 'a' mode ensures file exists even if empty
            lock_fd = open(lock_file, 'a')
            
            # Try to acquire exclusive lock (non-blocking)
            import fcntl
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Lock acquired - we're the downloader
            except BlockingIOError:
                # Another worker is downloading - wait and check if it completed
                # This avoids duplicate downloads and connection pool exhaustion
                
                # Wait up to 5 minutes for the download to complete
                # Poll every second to check if file was created by other worker
                for i in range(300):  # 300 * 1s = 5 minutes
                    time.sleep(1)
                    if cache_path.exists() and cache_path.stat().st_size > 0:
                        # File was downloaded by another worker - use it
                        # FIX C-4: Clean up before early return to prevent leak in _downloading_files set
                        lock_fd.close()
                        with _downloading_lock:
                            _downloading_files.discard(file_name)
                        return _finalize_cached_file(file_name, cache_path)
                # After 5 minutes, other worker may have failed - acquire lock (blocking) to download ourselves
                # This handles case where other worker crashed mid-download
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            
            # RACE CONDITION PROTECTION: Double-check file wasn't created while waiting for lock
            # Another worker may have completed the download between our check and acquiring lock
            if cache_path.exists() and cache_path.stat().st_size > 0:
                # File exists - release lock and use it (no need to download)
                # FIX C-4: Clean up before early return to prevent leak in _downloading_files set
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
                with _downloading_lock:
                    _downloading_files.discard(file_name)
                return _finalize_cached_file(file_name, cache_path)
        except Exception as e:
            # Lock acquisition failed - continue without lock (fallback to semaphore-only)
            # This prevents lock file issues from breaking downloads
            if lock_fd:
                try:
                    lock_fd.close()
                except:
                    pass
            lock_fd = None

    # CONNECTION POOL LIMITING: Use semaphore to limit concurrent downloads
    # During ensemble, 21 models try to download simultaneously - too many for S3
    # Semaphore limits to 4 concurrent downloads at a time across all workers
    # This prevents connection pool exhaustion and S3 throttling
    # NOTE: TransferManager's internal parallel chunking (16 threads) does NOT conflict
    # with this semaphore - the semaphore limits downloads, TransferManager parallelizes chunks within each download
    _download_semaphore.acquire()
    try:
        download_start = time.time()
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        
        # Clean up any stale temp files from previous failed attempts
        if tmp_path.exists():
            try:
                # Check file age - if it's very old (>5 minutes), it's probably stale
                file_age = time.time() - tmp_path.stat().st_mtime
                if file_age > 300:  # 5 minutes
                    tmp_path.unlink()
            except Exception:
                pass  # File might have been deleted by another worker
        
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
                print(f"ERROR: {error_msg}", flush=True)
                _release_file_lock(lock_fd)
                raise FileNotFoundError(f"{error_msg}. The model file may not have been uploaded yet, or the model timestamp may be incorrect. Check S3 storage or verify the model timestamp in 'whichgefs'.")
            # Other S3 errors (403, 500, etc.) - retryable
            _release_file_lock(lock_fd)
            raise IOError(f"S3 metadata error: {e}")
        
        # Use TransferManager for multipart parallel downloads
        # TransferManager handles retries automatically, so we don't need manual retry logic
        # It uses multipart downloads for files > 8MB with 16 parallel threads per download
        try:
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download file using TransferManager (multipart parallel download)
            # This is much faster and more resilient than manual streaming
            _S3_CLIENT.download_file(
                Bucket=_BUCKET,
                Key=file_name,
                Filename=str(tmp_path),
                Config=_TRANSFER_CONFIG
            )
        except ClientError as s3_error:
            # S3 API errors - clean up partial file and re-raise
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            error_code = s3_error.response.get('Error', {}).get('Code', '')
            if error_code in ('404', 'NoSuchKey'):
                error_msg = f"File not found in S3: {file_name}"
                print(f"ERROR: {error_msg}", flush=True)
                _release_file_lock(lock_fd)
                raise FileNotFoundError(f"{error_msg}. The model file may not have been uploaded yet, or the model timestamp may be incorrect. Check S3 storage or verify the model timestamp in 'whichgefs'.")
            # Other S3 errors - TransferManager will retry automatically, but if it fails, raise
            _release_file_lock(lock_fd)
            raise IOError(f"S3 download error ({error_code}): {s3_error}")
        except Exception as download_error:
            # Other errors (network, disk, etc.) - clean up and re-raise
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            _release_file_lock(lock_fd)
            raise IOError(f"Download error for {file_name}: {download_error}")
        
        # FILE INTEGRITY VERIFICATION: Verify download completed successfully
        # TransferManager handles retries, but we still verify file integrity
        if not tmp_path.exists():
            _release_file_lock(lock_fd)
            raise IOError(f"Download failed: temp file not created for {file_name}")
        
        actual_size = tmp_path.stat().st_size
        if actual_size == 0:
            # Empty file - fatal error
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            _release_file_lock(lock_fd)
            raise IOError(f"Download failed: file {file_name} is empty (fatal)")
        
        # Verify file size matches expected size (if available from S3 metadata)
        if expected_size and actual_size != expected_size:
            # Size mismatch - retryable error (download was incomplete)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            _release_file_lock(lock_fd)
            raise IOError(f"Download incomplete: {file_name} expected {expected_size} bytes, got {actual_size} (retryable)")
        
        # Final verification: ensure file is readable and non-empty
        # Model files are ~300MB, so anything < 1KB is definitely wrong
        if actual_size < 1024:  # Files should be at least 1KB
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            _release_file_lock(lock_fd)
            raise IOError(f"Download failed: file {file_name} is suspiciously small ({actual_size} bytes) (fatal)")
        
        # Log successful download
        size_mb = actual_size / (1024 * 1024)
        download_time = time.time() - download_start
        print(f"INFO: Downloaded {file_name} ({size_mb:.1f} MB) in {download_time:.1f}s", flush=True)
        
        # Validate NPZ file structure before committing
        if file_name.endswith('.npz'):
            try:
                import numpy as np
                test_npz = np.load(tmp_path)
                test_npz.close()  # Close immediately after validation
            except Exception as e:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                _release_file_lock(lock_fd)
                raise IOError(f"Downloaded file {file_name} is corrupted (invalid NPZ): {e}")
        
        # CRITICAL: Check if final file already exists (another worker might have completed the download)
        # This handles race conditions where multiple workers download the same file
        if cache_path.exists() and cache_path.stat().st_size > 0:
            # Another worker completed the download - finalize and return
            return _finalize_cached_file(file_name, cache_path)
        
        if not tmp_path.exists():
            # Temp file doesn't exist - check if another worker completed it
            if cache_path.exists() and cache_path.stat().st_size > 0:
                # Another worker completed it - finalize and return
                return _finalize_cached_file(file_name, cache_path)
            # Temp file missing and final file doesn't exist - this is an error
            raise IOError(f"Download logic error: loop completed but temp file not found for {file_name} (and final file doesn't exist)")
        
        # Rename temp file to final cache location
        # Double-check temp file exists right before rename to avoid race conditions
        try:
            # Atomic check: try to get file size (this will fail if file doesn't exist)
            # This is more reliable than exists() + stat() which has a race condition window
            try:
                tmp_size = tmp_path.stat().st_size
            except FileNotFoundError:
                # Temp file was deleted - check if another worker completed the download
                if cache_path.exists() and cache_path.stat().st_size > 0:
                    # Another worker completed it - finalize and return
                    return _finalize_cached_file(file_name, cache_path)
                # Temp file deleted and final file doesn't exist - error
                raise IOError(f"Download failed: temp file {tmp_path} was deleted before rename (race condition or cleanup issue)")
            
            if tmp_size == 0:
                raise IOError(f"Download failed: temp file {file_name} is empty before rename")
            
            # Perform rename atomically
            os.replace(tmp_path, cache_path)
            
            # Verify final file exists and is not empty
            if not cache_path.exists():
                raise IOError(f"Downloaded file {file_name} is missing after rename (temp file was {tmp_size} bytes)")
            
            if cache_path.stat().st_size == 0:
                raise IOError(f"Downloaded file {file_name} is empty after rename (temp file was {tmp_size} bytes)")
            
            if cache_path.stat().st_size != tmp_size:
                raise IOError(f"Downloaded file {file_name} size mismatch after rename (expected {tmp_size} bytes, got {cache_path.stat().st_size} bytes)")
        except FileNotFoundError as e:
            # Temp file was deleted between check and rename (race condition)
            error_msg = f"Download failed: temp file {tmp_path} not found during rename for {file_name}. This may indicate a race condition or premature cleanup."
            print(f"ERROR: {error_msg}", flush=True)
            raise IOError(error_msg) from e
        except OSError as e:
            # File system error during rename
            error_msg = f"Download failed: error renaming temp file for {file_name}: {e}"
            print(f"ERROR: {error_msg}", flush=True)
            raise IOError(error_msg) from e

        # Final check that file exists
        if not cache_path.exists():
            raise FileNotFoundError(f"Cached file {file_name} not found at {cache_path}")
        
        # CRITICAL: Finalize file to protect it from cleanup
        # This prevents race condition where cleanup deletes file before other workers use it
        return _finalize_cached_file(file_name, cache_path)
    finally:
        # CRITICAL: Always release file lock and clean up lock file, even on error
        # This prevents deadlocks on subsequent downloads
        _release_file_lock(lock_fd)
        
        # Clean up lock file on successful download to prevent accumulation
        if lock_file and lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass  # Non-critical if cleanup fails
        
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
        print(f"ERROR: Failed to upload {file_name} to S3: {e}", flush=True)
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
        print(f"WARNING: Failed to delete {file_name} from S3: {e}", flush=True)
        return False
