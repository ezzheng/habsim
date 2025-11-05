import io
import os
import tempfile
import time
import logging
from pathlib import Path
from typing import Iterator
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Try to load from .env and/or supabase.env if available (non-fatal)
def _load_env_file():
    """Load environment variables from .env or supabase.env file if present.
    Does not override existing environment variables and does not raise on failure.
    """
    env_files = [Path('.env'), Path('supabase.env')]
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

_BASE_URL = os.environ.get("SUPABASE_URL", "").rstrip('/')
_KEY = os.environ.get("SUPABASE_SECRET", "")
_BUCKET = "habsim"

# Validate that Supabase credentials are set
if not _BASE_URL:
    raise ValueError("SUPABASE_URL environment variable is not set. Please configure it in Railway settings.")
if not _KEY:
    raise ValueError("SUPABASE_SECRET environment variable is not set. Please configure it in Railway settings.")

_COMMON_HEADERS = {
    "Authorization": f"Bearer {_KEY}",
    "apikey": _KEY,
}

_SESSION = requests.Session()
_RETRY = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=("GET", "POST"),
)
_ADAPTER = HTTPAdapter(max_retries=_RETRY, pool_connections=8, pool_maxsize=32)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)

_DEFAULT_TIMEOUT = (3, 60)
_CACHEABLE_SUFFIXES = (".npz", ".npy")
# Use persistent directory on Railway or Render, fallback to tempdir
# Check for Railway first (persistent volume), then Render, then tempdir
_default_cache_dir = None
if Path("/opt/render/project/src").exists():
    _default_cache_dir = Path("/opt/render/project/src/data/gefs")
elif Path("/app/data").exists():  # Railway default app directory
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

def _object_url(path: str) -> str:
    return f"{_BASE_URL}/storage/v1/object/{path}"

def _list_url(bucket: str) -> str:
    return f"{_BASE_URL}/storage/v1/object/list/{bucket}"

def listdir_gefs():
    resp = _SESSION.post(
        _list_url(_BUCKET),
        headers=_COMMON_HEADERS,
        json={"prefix": ""},
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json()
    return [item.get('name') for item in items]

def open_gefs(file_name):
    # Cache whichgefs locally to reduce connection pool pressure (status checks every 5 seconds)
    if file_name == 'whichgefs':
        now = time.time()
        with _whichgefs_lock:
            # Check if cached value is still valid (60 second TTL)
            if (_whichgefs_cache["value"] is not None and 
                now - _whichgefs_cache["timestamp"] < _whichgefs_cache["ttl"]):
                return io.StringIO(_whichgefs_cache["value"])
            
            # Cache miss or expired - download from Supabase
            resp = _SESSION.get(
                _object_url(f"{_BUCKET}/{file_name}"),
                headers=_COMMON_HEADERS,
                timeout=_DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.content.decode("utf-8")
            
            # Update cache
            _whichgefs_cache["value"] = content
            _whichgefs_cache["timestamp"] = now
            
            return io.StringIO(content)
    
    # Non-whichgefs files: download directly (no caching needed)
    resp = _SESSION.get(
        _object_url(f"{_BUCKET}/{file_name}"),
        headers=_COMMON_HEADERS,
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return io.StringIO(resp.content.decode("utf-8"))

def load_gefs(file_name):
    if _should_cache(file_name):
        return str(_ensure_cached(file_name))

    resp = _SESSION.get(
        _object_url(f"{_BUCKET}/{file_name}"),
        headers=_COMMON_HEADERS,
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return io.BytesIO(resp.content)

def download_gefs(file_name):
    if _should_cache(file_name):
        path = _ensure_cached(file_name)
        with open(path, 'rb') as fp:
            return fp.read()

    resp = _SESSION.get(
        _object_url(f"{_BUCKET}/{file_name}"),
        headers=_COMMON_HEADERS,
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


def _should_cache(file_name: str) -> bool:
    return file_name.endswith(_CACHEABLE_SUFFIXES)


def _cleanup_old_cache_files():
    """Remove least recently used cache files if we exceed the limit"""
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
        
        # If worldelev.npy exists, ensure it's not too old (touch it to update access time)
        # This prevents it from being considered for eviction even if cleanup logic changes
        if worldelev_file and worldelev_file.exists():
            try:
                worldelev_file.touch()
            except:
                pass
        
        # If under limit, no cleanup needed
        if len(cached_files) < _MAX_CACHED_FILES:
            return
        
        # Sort by access time (oldest first)
        cached_files.sort(key=lambda f: f.stat().st_atime)
        
        # Remove oldest files until we're under the limit
        files_to_remove = len(cached_files) - _MAX_CACHED_FILES + 1  # +1 to make room for new file
        for i in range(files_to_remove):
            try:
                cached_files[i].unlink()
            except Exception:
                pass  # File might have been removed by another thread
    except Exception:
        pass  # Don't fail if cleanup fails


def _ensure_cached(file_name: str) -> Path:
    cache_path = _CACHE_DIR / file_name
    
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
                    logging.debug(f"File cache HIT: {file_name} (no Supabase egress)")
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
        # Update access time to mark as recently used
        cache_path.touch()
        logging.debug(f"File cache HIT: {file_name} (no Supabase egress)")
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with _CACHE_LOCK:
        if cache_path.exists():
            cache_path.touch()
            logging.debug(f"File cache HIT: {file_name} (no Supabase egress)")
            return cache_path

        # Clean up old files before downloading new one
        _cleanup_old_cache_files()

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
        
        # Log download attempt (this will use Supabase egress)
        logging.info(f"File cache MISS: {file_name} - downloading from Supabase (will use egress)")
        
        # Use longer timeout for large files
        # NPZ wind files are ~300MB, worldelev.npy is 451MB
        # Calculate timeout: 10s connect + generous read timeout
        is_large_file = file_name == 'worldelev.npy' or file_name.endswith('.npz') or file_name.endswith('.npy')
        if is_large_file:
            # Use much longer timeout for large files (30 min read timeout)
            # Increased from 20 to 30 minutes to handle Railway-Supabase network issues
            download_timeout = (15, 1800)  # 15s connect, 1800s (30 min) read
        else:
            download_timeout = _DEFAULT_TIMEOUT
        
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
                resp = _SESSION.get(
                    _object_url(f"{_BUCKET}/{file_name}"),
                    headers=_COMMON_HEADERS,
                    stream=True,
                    timeout=download_timeout,
                )
                
                # Handle file not found errors with helpful message
                if resp.status_code == 400 or resp.status_code == 404:
                    error_msg = f"File not found in Supabase: {file_name} (status {resp.status_code})"
                    logging.error(error_msg)
                    raise FileNotFoundError(f"{error_msg}. The model file may not have been uploaded yet, or the model timestamp may be incorrect. Check Supabase storage or verify the model timestamp in 'whichgefs'.")
                
                resp.raise_for_status()
                
                # Get expected content length if available
                expected_size = resp.headers.get('Content-Length')
                if expected_size:
                    expected_size = int(expected_size)
                
                bytes_written = 0
                last_chunk_time = time.time()
                try:
                    with open(tmp_path, 'wb') as fh:
                        for chunk in _iter_content(resp):
                            current_time = time.time()
                            
                            # Check for connection timeout (no data for 120 seconds)
                            # Increased from 60s to 120s to tolerate Railway-Supabase network slowness
                            if is_large_file and (current_time - last_chunk_time) > 120:
                                raise IOError(f"Download stalled: no data received for 120 seconds")
                            
                            if chunk:
                                fh.write(chunk)
                                bytes_written += len(chunk)
                                last_chunk_time = current_time
                                
                                # For large files, log progress every 50MB
                                if is_large_file and bytes_written % (50 * 1024 * 1024) < _CHUNK_SIZE:
                                    mb_written = bytes_written / (1024 * 1024)
                                    if expected_size:
                                        mb_total = expected_size / (1024 * 1024)
                                        logging.info(f"Downloading {file_name}: {mb_written:.1f}MB / {mb_total:.1f}MB ({100 * bytes_written / expected_size:.1f}%)")
                                    else:
                                        logging.info(f"Downloading {file_name}: {mb_written:.1f}MB")
                except Exception as write_error:
                    # If file was created but write failed, clean it up
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except:
                            pass
                    raise IOError(f"Download failed: error writing {file_name}: {write_error}")
                
                # Verify download completed successfully
                if not tmp_path.exists():
                    raise IOError(f"Download failed: temp file not created for {file_name}")
                
                actual_size = tmp_path.stat().st_size
                if actual_size == 0:
                    raise IOError(f"Download failed: file {file_name} is empty")
                
                if expected_size and actual_size != expected_size:
                    raise IOError(f"Download incomplete: {file_name} expected {expected_size} bytes, got {actual_size}")
                
                # Log successful download with size (for egress tracking)
                size_mb = actual_size / (1024 * 1024)
                logging.info(f"Downloaded {file_name} from Supabase: {size_mb:.2f} MB (egress used)")
                
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
            except IOError as e:
                # Clean up incomplete download
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                last_error = e
                if attempt < max_retries - 1:
                    # Wait before retry (exponential backoff: 2s, 4s, 8s)
                    wait_time = 2 ** (attempt + 1)
                    logging.warning(f"Download attempt {attempt + 1}/{max_retries} failed for {file_name}: {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    # Last attempt failed - release lock and clean up
                    if lock_fd:
                        try:
                            import fcntl
                            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                            lock_fd.close()
                        except:
                            pass
                    raise
            except Exception as e:
                # Clean up incomplete download
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except:
                        pass
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logging.warning(f"Download attempt {attempt + 1}/{max_retries} failed for {file_name}: {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    # Last attempt failed - release lock and clean up
                    if lock_fd:
                        try:
                            import fcntl
                            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                            lock_fd.close()
                        except:
                            pass
                    raise
        
        # If we get here, download succeeded (tmp_path exists from successful attempt)
        if not tmp_path.exists():
            raise IOError(f"Download failed: temp file not created for {file_name} after {max_retries} attempts")
        
        # Rename temp file to final cache location
        try:
            os.replace(tmp_path, cache_path)
            
            # Verify final file exists and is not empty
            if not cache_path.exists() or cache_path.stat().st_size == 0:
                raise IOError(f"Downloaded file {file_name} is missing or empty after rename")
            
            logging.info(f"Cached {file_name} to disk: {cache_path} (future reads will use zero egress)")
            
            # Release the file lock after successful download and rename
            if lock_fd:
                try:
                    import fcntl
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                    lock_fd.close()
                    logging.debug(f"Released download lock for {file_name}")
                except Exception as lock_error:
                    logging.warning(f"Error releasing lock for {file_name}: {lock_error}")
        except Exception as e:
            # Clean up partial download and release lock
            if lock_fd:
                try:
                    import fcntl
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                    lock_fd.close()
                except:
                    pass
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except:
                    pass
            if cache_path.exists() and cache_path.stat().st_size == 0:
                try:
                    cache_path.unlink()
                except:
                    pass
            raise

    # Final check that file exists
    if not cache_path.exists():
        raise FileNotFoundError(f"Cached file {file_name} not found at {cache_path}")
    
    return cache_path


def _iter_content(resp: requests.Response) -> Iterator[bytes]:
    for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
        if chunk:
            yield chunk


def upload_gefs(file_path: Path, file_name: str) -> bool:
    """Upload a file to Supabase storage bucket.
    
    Args:
        file_path: Local path to file to upload
        file_name: Name to store file as in bucket
        
    Returns:
        True if successful, False otherwise
    """
    try:
        file_size = file_path.stat().st_size
        # For large files, use streaming upload
        with open(file_path, 'rb') as f:
            # Supabase storage uses PUT for uploads
            resp = _SESSION.put(
                _object_url(f"{_BUCKET}/{file_name}"),
                headers={
                    **_COMMON_HEADERS,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                },
                data=f,
                timeout=(10, 600),  # Longer timeout for large uploads (10 min)
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logging.error(f"Failed to upload {file_name}: {e}")
        return False


def delete_gefs(file_name: str) -> bool:
    """Delete a file from Supabase storage bucket.
    
    Args:
        file_name: Name of file to delete from bucket
        
    Returns:
        True if successful, False otherwise
    """
    try:
        resp = _SESSION.delete(
            _object_url(f"{_BUCKET}/{file_name}"),
            headers=_COMMON_HEADERS,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logging.warning(f"Failed to delete {file_name}: {e}")
        return False
