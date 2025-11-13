"""
WindFile class for loading and accessing GEFS wind data.

Supports memory-mapped (efficient) and preloaded (fast) access modes.
Provides 4D interpolation for wind vectors at arbitrary lat/lon/alt/time.
Thread-safe file loading with per-file locks to prevent zipfile contention.
"""
import math
import os
import time
import threading
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Union
from functools import lru_cache

import numpy as np
from numpy.lib.format import open_memmap

_MEMMAP_LOCKS: dict[Path, threading.Lock] = {}
_MEMMAP_LOCKS_LOCK = threading.Lock()
# Per-file locks for entire file loading (prevents zipfile contention)
_FILE_LOAD_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOAD_LOCKS_LOCK = threading.Lock()

def _normalize_path(path: Union[BytesIO, str]) -> Union[BytesIO, Path]:
    if isinstance(path, (str, Path)):
        return Path(path)
    return path

def _get_memmap_lock(path: Path) -> threading.Lock:
    with _MEMMAP_LOCKS_LOCK:
        lock = _MEMMAP_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _MEMMAP_LOCKS[path] = lock
        return lock

def _get_file_load_lock(path: Union[Path, BytesIO]) -> threading.Lock:
    """Get per-file lock for entire file loading process (prevents zipfile contention)"""
    # BytesIO objects don't need locking (they're already in memory)
    if isinstance(path, BytesIO):
        # Return a no-op lock for BytesIO (no contention possible)
        class NoOpLock:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return NoOpLock()
    
    # For Path objects, use per-file locking
    with _FILE_LOAD_LOCKS_LOCK:
        lock = _FILE_LOAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOAD_LOCKS[path] = lock
        return lock

# Cache altitude-to-pressure conversions (common altitudes in HAB simulations)
@lru_cache(maxsize=10000)
def _alt_to_hpa_cached(altitude_rounded):
    """Cached altitude to pressure conversion"""
    pa_to_hpa = 1.0/100.0
    if altitude_rounded < 11000:
        return pa_to_hpa * (1-altitude_rounded/44330.7)**5.2558 * 101325
    else:
        return pa_to_hpa * math.exp(altitude_rounded / -6341.73) * 128241

class WindFile:
    def __init__(self, path: Union[BytesIO, str], preload: bool = False):
        """
        Initialize WindFile with wind data.
        
        Args:
            path: Path to NPZ file or BytesIO object
            preload: If True, load full array into RAM (faster, uses more RAM).
                    If False, use memory-mapping (slower but memory-efficient).
                    Default: False (memory-efficient mode)
        """
        normalized_path = _normalize_path(path)
        
        # Lock entire file loading to prevent zipfile contention
        file_lock = _get_file_load_lock(normalized_path)
        with file_lock:
            npz = np.load(normalized_path)
            try:
                self.time = float(npz['timestamp'][()])
                self.levels = np.array(npz['levels'], copy=True)
                self.interval = float(npz['interval'][()])

                if isinstance(normalized_path, Path) and normalized_path.suffix == '.npz':
                    if preload:
                        self.data = np.array(npz['data'], copy=True)
                    else:
                        self.data = self._load_memmap_data(npz, normalized_path)
                else:
                    self.data = np.array(npz['data'], copy=True)
            finally:
                npz.close()

        # Validate that data was loaded successfully
        if self.data is None:
            raise ValueError(f"Failed to load wind data from {normalized_path}: data is None")
        if not hasattr(self.data, 'shape') or len(self.data.shape) < 5:
            raise ValueError(f"Invalid wind data shape from {normalized_path}: expected 5D array, got {type(self.data)}")

        self.resolution_lat_multiplier = (self.data.shape[-5] - 1) / 180
        self.resolution_lon_multiplier = (self.data.shape[-4] - 1) / 360

        level_array = np.asarray(self.levels, dtype=np.float32)
        level_indices = np.arange(level_array.size, dtype=np.float32)

        if level_array.size == 0:
            raise ValueError("WindFile levels array is empty")

        level_diff = np.diff(level_array)
        if np.all(level_diff > 0):  # already ascending
            interp_levels = level_array
            interp_indices = level_indices
        elif np.all(level_diff < 0):  # descending
            interp_levels = level_array[::-1]
            interp_indices = level_indices[::-1]
        else:  # unordered, fall back to argsort
            sort_idx = np.argsort(level_array)
            interp_levels = level_array[sort_idx]
            interp_indices = level_indices[sort_idx]

        self._interp_levels = interp_levels
        self._interp_indices = interp_indices
        self._interp_min = float(interp_levels[0])
        self._interp_max = float(interp_levels[-1])
        
        # Pre-compute time bounds for faster validation
        self._time_max = self.time + self.interval * (self.data.shape[-2]-1)
    
    def cleanup(self):
        """Cleanup numpy arrays to free memory. Only call when WindFile is not in use."""
        if hasattr(self, 'data') and self.data is not None:
            if isinstance(self.data, np.ndarray) and not hasattr(self.data, 'filename'):
                try:
                    del self.data
                    self.data = None
                except:
                    pass
        
        # Clear other numpy arrays (these are smaller but still consume memory)
        if hasattr(self, 'levels') and isinstance(self.levels, np.ndarray):
            try:
                del self.levels
                self.levels = None
            except:
                pass
        if hasattr(self, '_interp_levels') and isinstance(self._interp_levels, np.ndarray):
            try:
                del self._interp_levels
                self._interp_levels = None
            except:
                pass
        if hasattr(self, '_interp_indices') and isinstance(self._interp_indices, np.ndarray):
            try:
                del self._interp_indices
                self._interp_indices = None
            except:
                pass

    def _load_memmap_data(self, npz: np.lib.npyio.NpzFile, path: Path):
        """Load data using memory-mapping for memory efficiency.
        
        PERFORMANCE: NPZ files are compressed (zip), so accessing npz['data'] requires
        decompression which takes 6-9 seconds. We extract to an uncompressed .npy file
        ONCE, then memory-map that file for fast subsequent access.
        """
        import os
        import logging
        memmap_path = Path(f"{path}.data.npy")
        temp_path = Path(f"{path}.data.npy.tmp")
        
        # CRITICAL: Always use the lock when checking/loading the file to prevent race conditions
        # Multiple threads might try to load the same file simultaneously
        lock = _get_memmap_lock(memmap_path)
        with lock:
            # Check if extracted .npy file already exists (fast path)
            if memmap_path.exists():
                try:
                    # Verify file is not corrupted by checking its size
                    file_size = memmap_path.stat().st_size
                    if file_size > 0:
                        memmap_data = np.load(memmap_path, mmap_mode='r')
                        if memmap_data is not None:
                            logging.debug(f"[MMAP] Using cached extraction: {memmap_path.name}")
                            return memmap_data
                except (EOFError, OSError, ValueError) as e:
                    # Corrupted extraction file - delete and re-extract
                    logging.warning(f"[MMAP] Cached extraction corrupted: {memmap_path.name}, re-extracting: {e}")
                    try:
                        memmap_path.unlink()
                    except:
                        pass
                except Exception as e:
                    # Other errors - log and re-extract
                    logging.warning(f"[MMAP] Error loading cached extraction: {memmap_path.name}, re-extracting: {e}")
                    try:
                        memmap_path.unlink()
                    except:
                        pass
            
            if 'data' not in npz:
                raise KeyError(f"NPZ file {path} is missing 'data' key")
            
            # Extract data array to uncompressed .npy file for fast memory-mapping
            # Use temporary file first, then rename atomically to prevent corruption
            extract_start = time.time()
            array = npz['data']
            memmap_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write to temporary file first
            try:
                # Remove temp file if it exists (from previous failed extraction)
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except:
                        pass
                
                mm = open_memmap(temp_path, mode='w+', dtype=array.dtype, shape=array.shape)
                mm[...] = array
                mm.flush()  # Flush memory-mapped array to disk
                del mm
                del array
                
                # Sync file to disk to ensure it's fully written before rename
                try:
                    with open(temp_path, 'rb') as f:
                        os.fsync(f.fileno())
                except:
                    pass
                
                # Atomically rename temp file to final file (prevents partial reads)
                temp_path.rename(memmap_path)
                
                extract_time = time.time() - extract_start
                logging.info(f"[PERF] Extracted NPZ to .npy: {memmap_path.name}, time={extract_time:.1f}s (one-time cost)")
            except Exception as e:
                # Clean up temp file on error
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except:
                    pass
                raise RuntimeError(f"Failed to extract NPZ data to {memmap_path}: {e}")
            
            # Now memory-map the extracted file (still inside lock)
            try:
                memmap_data = np.load(memmap_path, mmap_mode='r')
                if memmap_data is None:
                    raise RuntimeError(f"Failed to load memory-mapped data from {memmap_path}")
                return memmap_data
            except (EOFError, OSError, ValueError) as e:
                # File is corrupted - delete and raise error (will be retried by caller)
                logging.error(f"[MMAP] Extracted file is corrupted: {memmap_path.name}, error: {e}")
                try:
                    memmap_path.unlink()
                except:
                    pass
                raise RuntimeError(f"Extracted file is corrupted: {e}")

    def get(self, lat, lon, altitude, time):
        """Optimized wind data retrieval with bounds checking"""
        if lat < -90 or lat > 90:
            raise Exception(f"Latitude {lat} out of bounds")
        if lon < -180 or lon > 360:
            raise Exception(f"Longitude {lon} out of bounds")
        
        if lon < 0:
            lon = 360 + lon

        if isinstance(time, datetime.datetime):
            time = time.timestamp()
        
        # Use pre-computed time bound
        if time < self.time or time > self._time_max:
            raise Exception(f"Time {time} out of bounds")

        indices = self.get_indices(lat, lon, altitude, time)
        
        return self.interpolate(*indices)

    def get_indices(self, lat, lon, alt, time):
        """Convert physical coordinates to data indices"""
        lat = (90 - lat) * self.resolution_lat_multiplier
        lon = (lon % 360) * self.resolution_lon_multiplier
        time = (time - self.time)/self.interval
        pressure = self.get_pressure_index(alt)
        
        return lat, lon, pressure, time

    def get_pressure_index(self, alt):
        """Convert altitude to pressure level index with caching"""
        # Round altitude to nearest meter for caching
        alt_rounded = round(alt)
        pressure = _alt_to_hpa_cached(alt_rounded)

        if pressure <= self._interp_min:
            return float(self._interp_indices[0])
        if pressure >= self._interp_max:
            return float(self._interp_indices[-1])

        return float(np.interp(pressure, self._interp_levels, self._interp_indices))

    def interpolate(self, lat, lon, level, time):
        """Optimized 4D interpolation"""
        if self.data is None:
            raise RuntimeError("WindFile data is None - file may not have loaded correctly or was cleaned up")
        
        lat_i, lon_i, level_i, time_i = int(lat), int(lon), int(level), int(time)
        lat_frac = lat - lat_i
        lon_frac = lon - lon_i
        level_frac = level - level_i
        time_frac = time - time_i
        
        pressure_filter = np.array([1-level_frac, level_frac], dtype=np.float32).reshape(1, 1, 2, 1, 1)
        time_filter = np.array([1-time_frac, time_frac], dtype=np.float32).reshape(1, 1, 1, 2, 1) 
        lat_filter = np.array([1-lat_frac, lat_frac], dtype=np.float32).reshape(2, 1, 1, 1, 1)
        lon_filter = np.array([1-lon_frac, lon_frac], dtype=np.float32).reshape(1, 2, 1, 1, 1)

        cube = self.data[lat_i:lat_i+2, lon_i:lon_i+2, level_i:level_i+2, time_i:time_i+2, :]
        return np.sum(cube * lat_filter * lon_filter * pressure_filter * time_filter, axis=(0,1,2,3))

    def alt_to_hpa(self, altitude):
        """Convert altitude to hectopascals (with caching)"""
        alt_rounded = round(altitude)
        return _alt_to_hpa_cached(alt_rounded)

    def hpa_to_alt(self, p):
        """Convert hectopascals to altitude"""
        if p > 226.325:
            return 44330.7 * (1 - (p / 1013.25) ** 0.190266)
        else:
            return -6341.73 * (math.log(p) - 7.1565)

