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
# Shared filter cache for ensemble mode (all WindFiles share same cache when preloaded)
_shared_filter_cache = {}

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
        
        # Store source path for GEFS cycle validation (extract timestamp from filename)
        # Format: YYYYMMDDHH_NN.npz -> extract YYYYMMDDHH to validate against currgefs
        if isinstance(normalized_path, Path):
            self._source_path = normalized_path
        else:
            self._source_path = None
        
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
        
        # Cache for interpolation filter arrays (reduces allocations)
        # In ensemble mode (preload=True), use shared global cache since all models have similar patterns
        if preload:
            global _shared_filter_cache
            if not _shared_filter_cache:
                _shared_filter_cache = {}
            self._filter_cache = _shared_filter_cache
            self._filter_cache_max_size = 2000  # Larger cache for ensemble mode
        else:
            self._filter_cache = {}
            self._filter_cache_max_size = 1000  # Limit cache size

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
        """
        Load data using memory-mapping for memory efficiency.
        
        PERFORMANCE OPTIMIZATION: NPZ files are compressed (zip format), so accessing
        npz['data'] requires decompression which takes 6-9 seconds per access. This is
        too slow for repeated access during simulation.
        
        SOLUTION: Extract the data array to an uncompressed .npy file ONCE, then
        memory-map that file for fast subsequent access. The extraction takes 6-9 seconds
        the first time, but subsequent loads are instant (just memory-mapping).
        
        THREAD SAFETY: Uses per-file locks to prevent multiple threads from extracting
        the same file simultaneously (which would waste time and disk I/O).
        """
        memmap_path = Path(f"{path}.data.npy")
        temp_path = Path(f"{path}.data.npy.tmp")
        
        # THREAD SAFETY: Always use the lock when checking/loading the file
        # Multiple threads might try to load the same file simultaneously
        # Without locking, we'd extract the same file multiple times (wasteful)
        lock = _get_memmap_lock(memmap_path)
        with lock:
            # FAST PATH: Check if extracted .npy file already exists
            # Another thread may have already extracted it
            if memmap_path.exists():
                try:
                    # Verify file is not corrupted by checking its size
                    # Corrupted files would cause errors during memory-mapping
                    file_size = memmap_path.stat().st_size
                    if file_size > 0:
                        # Memory-map the existing file (instant, no decompression needed)
                        memmap_data = np.load(memmap_path, mmap_mode='r')
                        if memmap_data is not None:
                            return memmap_data
                except (EOFError, OSError, ValueError) as e:
                    # Corrupted extraction file - delete and re-extract
                    # This handles cases where extraction was interrupted or file system error
                    print(f"WARNING: Cached extraction corrupted: {memmap_path.name}, re-extracting", flush=True)
                    try:
                        memmap_path.unlink()
                    except:
                        pass
                except Exception as e:
                    # Other errors - log and re-extract
                    # Unknown error, safest to re-extract
                    try:
                        memmap_path.unlink()
                    except:
                        pass
            
            if 'data' not in npz:
                raise KeyError(f"NPZ file {path} is missing 'data' key")
            
            # EXTRACTION: Extract data array to uncompressed .npy file for fast memory-mapping
            # This is the expensive operation (6-9 seconds) but only happens once per file
            array = npz['data']
            memmap_path.parent.mkdir(parents=True, exist_ok=True)
            
            # ATOMIC WRITE: Write to temporary file first, then rename atomically
            # This prevents other threads from seeing a partially-written file
            # If extraction fails, temp file is cleaned up and final file is never created
            try:
                # Remove temp file if it exists (from previous failed extraction)
                # Stale temp files indicate a previous crash or error
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except:
                        pass
                
                # Create memory-mapped array in write mode
                # This allows us to write the large array efficiently
                mm = open_memmap(temp_path, mode='w+', dtype=array.dtype, shape=array.shape)
                mm[...] = array  # Copy data from compressed NPZ to uncompressed .npy
                mm.flush()  # Flush memory-mapped array to disk (ensures data is written)
                del mm  # Close memory-mapped file
                del array  # Free memory from NPZ array
                
                # CRITICAL: Sync file to disk to ensure it's fully written before rename
                # Without this, rename might happen before all data is flushed to disk
                # This could cause corruption if process crashes between flush and rename
                try:
                    with open(temp_path, 'rb') as f:
                        os.fsync(f.fileno())  # Force write to disk
                except:
                    pass
                
                # ATOMIC RENAME: Rename temp file to final file (prevents partial reads)
                # On most filesystems, rename is atomic - either succeeds completely or fails
                # This ensures other threads never see a partially-written file
                temp_path.rename(memmap_path)
            except Exception as e:
                # Clean up temp file on error (prevents accumulation of failed extractions)
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except:
                    pass
                raise RuntimeError(f"Failed to extract NPZ data to {memmap_path}: {e}")
            
            # MEMORY-MAP: Now memory-map the extracted file (still inside lock)
            # This is fast (just creates a memory mapping, no data copy)
            # The file is now available for fast access by this and other threads
            try:
                memmap_data = np.load(memmap_path, mmap_mode='r')
                if memmap_data is None:
                    raise RuntimeError(f"Failed to load memory-mapped data from {memmap_path}")
                return memmap_data
            except (EOFError, OSError, ValueError) as e:
                # File is corrupted - delete and raise error (will be retried by caller)
                # This handles rare cases where extraction succeeded but file is unreadable
                print(f"ERROR: Extracted file corrupted: {memmap_path.name}", flush=True)
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

        if isinstance(time, datetime):
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
        """
        Optimized 4D trilinear interpolation with filter caching.
        
        Performs trilinear interpolation across 4 dimensions (lat, lon, pressure level, time)
        to get wind vector at arbitrary coordinates. Uses cached filter arrays to avoid
        repeated allocations (major performance win for ensemble workloads).
        
        ALGORITHM: Trilinear interpolation in 4D space
        - Takes 2×2×2×2 = 16 data points surrounding the target coordinates
        - Computes weighted average using fractional parts as weights
        - Returns 2D wind vector (u, v components)
        """
        if self.data is None:
            raise RuntimeError("WindFile data is None - file may not have loaded correctly or was cleaned up")
        
        # Convert continuous coordinates to integer indices and fractional parts
        lat_i, lon_i, level_i, time_i = int(lat), int(lon), int(level), int(time)
        lat_frac = lat - lat_i
        lon_frac = lon - lon_i
        level_frac = level - level_i
        time_frac = time - time_i
        
        # CACHE KEY: Round fractional parts to reduce cache size
        # 0.001 precision ≈ 100m for lat/lon, sufficient for interpolation accuracy
        # Without rounding, cache would have millions of unique keys (wasteful)
        frac_key = (round(lat_frac, 3), round(lon_frac, 3), round(level_frac, 3), round(time_frac, 3))
        
        # Get or create filter arrays from cache
        # Filter arrays are the interpolation weights - expensive to compute, cheap to cache
        if frac_key not in self._filter_cache:
            # Limit cache size to prevent memory growth
            # In ensemble mode, cache is larger (2000 vs 1000) since all models share it
            if len(self._filter_cache) >= self._filter_cache_max_size:
                # Clear cache if too large (simple FIFO eviction)
                # More sophisticated eviction (LRU) would be slower
                self._filter_cache.clear()
            
            # Create interpolation filter arrays for each dimension
            # Each filter is a 2-element array [1-frac, frac] reshaped for broadcasting
            # These are the weights for trilinear interpolation
            self._filter_cache[frac_key] = (
                np.array([1-level_frac, level_frac], dtype=np.float32).reshape(1, 1, 2, 1, 1),  # Pressure level filter
                np.array([1-time_frac, time_frac], dtype=np.float32).reshape(1, 1, 1, 2, 1),   # Time filter
                np.array([1-lat_frac, lat_frac], dtype=np.float32).reshape(2, 1, 1, 1, 1),     # Latitude filter
                np.array([1-lon_frac, lon_frac], dtype=np.float32).reshape(1, 2, 1, 1, 1)      # Longitude filter
            )
        
        pressure_filter, time_filter, lat_filter, lon_filter = self._filter_cache[frac_key]

        # Extract 2×2×2×2 cube of data points surrounding target coordinates
        # Shape: (2, 2, 2, 2, 2) where last dimension is [u, v] wind components
        cube = self.data[lat_i:lat_i+2, lon_i:lon_i+2, level_i:level_i+2, time_i:time_i+2, :]
        
        # Perform trilinear interpolation: multiply cube by filters and sum
        # Broadcasting automatically handles the 4D interpolation
        # Result is 2D vector [u, v] wind components
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

