import io
import os
import tempfile
from pathlib import Path
from typing import Iterator
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_BASE_URL = os.environ.get("SUPABASE_URL", "").rstrip('/')
_KEY = os.environ.get("SUPABASE_SECRET", "")
_BUCKET = "habsim"
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
_ADAPTER = HTTPAdapter(max_retries=_RETRY, pool_connections=4, pool_maxsize=8)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)

_DEFAULT_TIMEOUT = (3, 60)
_CACHEABLE_SUFFIXES = (".npz", ".npy")
_CACHE_DIR = Path(os.environ.get("HABSIM_CACHE_DIR", Path(tempfile.gettempdir()) / "habsim-gefs"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_LOCK = threading.Lock()
_CHUNK_SIZE = 1024 * 1024
_MAX_CACHED_FILES = 3  # Keep at most 3 GEFS files (~450MB max, safe for 2GB RAM)

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
        
        # Never evict worldelev.npy - it's required and large
        cached_files = [f for f in cached_files if f.name != 'worldelev.npy']
        
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
    if cache_path.exists():
        # Update access time to mark as recently used
        cache_path.touch()
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with _CACHE_LOCK:
        if cache_path.exists():
            cache_path.touch()
            return cache_path

        # Clean up old files before downloading new one
        _cleanup_old_cache_files()

        resp = _SESSION.get(
            _object_url(f"{_BUCKET}/{file_name}"),
            headers=_COMMON_HEADERS,
            stream=True,
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()

        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        try:
            with open(tmp_path, 'wb') as fh:
                for chunk in _iter_content(resp):
                    fh.write(chunk)
            os.replace(tmp_path, cache_path)
            
            # Verify file exists and is not empty
            if not cache_path.exists() or cache_path.stat().st_size == 0:
                raise IOError(f"Downloaded file {file_name} is missing or empty")
        except Exception as e:
            # Clean up partial download
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
