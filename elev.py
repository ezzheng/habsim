"""
Elevation data access using bilinear interpolation.

Loads worldelev.npy (memory-mapped, 451MB) and provides getElevation() for
bilinearly interpolated elevation at arbitrary lat/lon coordinates.
Thread-safe singleton pattern ensures data is loaded only once per process.
"""
import numpy as np
import threading
from gefs import load_gefs

# Module-level cache for elevation data (shared across all threads in process)
_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()

# Geographic bounds of elevation data (from worldelev.npy metadata)
MIN_LON = -180.00013888888893
MAX_LON = 179.99985967111152
MAX_LAT = 83.99986041511133
MIN_LAT = -90.0001388888889

def _get_elev_data():
    """
    Load elevation data with memory mapping. Thread-safe singleton.
    
    Uses double-checked locking pattern: check cache first (fast path),
    then acquire lock and check again (prevents race condition where multiple
    threads try to load simultaneously). Data is memory-mapped for efficiency
    (451MB file, but only pages actually accessed are loaded into RAM).
    """
    global _ELEV_DATA, _ELEV_SHAPE
    # Fast path: check cache without lock (most common case)
    if _ELEV_DATA is not None:
        return _ELEV_DATA, _ELEV_SHAPE
    # Slow path: acquire lock and check again (double-checked locking)
    with _ELEV_LOCK:
        # Another thread may have loaded it while we waited for lock
        if _ELEV_DATA is not None:
            return _ELEV_DATA, _ELEV_SHAPE
        # Load from S3 cache (downloads if not cached)
        path = load_gefs('worldelev.npy')
        # Memory-map the file (doesn't load entire 451MB into RAM)
        _ELEV_DATA = np.load(path, mmap_mode='r')
        _ELEV_SHAPE = _ELEV_DATA.shape
        return _ELEV_DATA, _ELEV_SHAPE

def getElevation(lat, lon):
    """
    Return bilinearly interpolated elevation for (lat, lon).
    
    Performs bilinear interpolation on 2D elevation grid. Clips coordinates
    to valid range and handles longitude wrap-around (lon -180 to 180).
    Returns 0.0 on error (safe fallback for simulation).
    """
    try:
        data, shape = _get_elev_data()
        rows, cols = shape
        
        # Clip latitude to valid range (prevents out-of-bounds access)
        lat = np.clip(lat, MIN_LAT, MAX_LAT)
        # Normalize longitude to [-180, 180] range (handles wrap-around)
        lon = ((lon + 180) % 360) - 180
        
        # Convert lat/lon to grid coordinates (floating-point indices)
        # col_f: column index (0 to cols-1)
        col_f = (lon - MIN_LON) / (MAX_LON - MIN_LON) * (cols - 1)
        # row_f: row index (0 to rows-1), inverted because latitude decreases upward
        row_f = (MAX_LAT - lat) / (MAX_LAT - MIN_LAT) * (rows - 1)
        
        # Get integer indices of 2×2 grid cell surrounding target point
        x0, y0 = int(np.floor(col_f)), int(np.floor(row_f))
        x1, y1 = min(x0 + 1, cols - 1), min(y0 + 1, rows - 1)  # Clamp to grid bounds
        # Fractional parts for interpolation weights
        fx, fy = col_f - x0, row_f - y0
        
        # Extract 4 corner values of grid cell
        v00, v10 = data[y0, x0], data[y0, x1]  # Top row
        v01, v11 = data[y1, x0], data[y1, x1]  # Bottom row
        
        # Bilinear interpolation: interpolate horizontally first, then vertically
        v_top = v00 * (1 - fx) + v10 * fx      # Interpolate top row
        v_bottom = v01 * (1 - fx) + v11 * fx   # Interpolate bottom row
        elev = v_top * (1 - fy) + v_bottom * fy  # Interpolate vertically
        
        # Return non-negative elevation (ocean/sea level is 0)
        return float(max(0, elev))
    except Exception:
        # Return 0.0 on any error (safe fallback - simulation continues)
        return 0.0