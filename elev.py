"""
Elevation data access using bilinear interpolation.

Loads worldelev.npy (memory-mapped) and provides getElevation() for
bilinearly interpolated elevation at arbitrary lat/lon coordinates.
Thread-safe singleton pattern for data loading.
"""
import numpy as np
import threading
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()

MIN_LON = -180.00013888888893
MAX_LON = 179.99985967111152
MAX_LAT = 83.99986041511133
MIN_LAT = -90.0001388888889

def _get_elev_data():
    """Load elevation data with memory mapping. Thread-safe singleton."""
    global _ELEV_DATA, _ELEV_SHAPE
    if _ELEV_DATA is not None:
        return _ELEV_DATA, _ELEV_SHAPE
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            return _ELEV_DATA, _ELEV_SHAPE
        path = load_gefs('worldelev.npy')
        _ELEV_DATA = np.load(path, mmap_mode='r')
        _ELEV_SHAPE = _ELEV_DATA.shape
        return _ELEV_DATA, _ELEV_SHAPE

def getElevation(lat, lon):
    """Return bilinearly interpolated elevation for (lat, lon)."""
    try:
        data, shape = _get_elev_data()
        rows, cols = shape
        lat = np.clip(lat, MIN_LAT, MAX_LAT)
        lon = ((lon + 180) % 360) - 180
        
        col_f = (lon - MIN_LON) / (MAX_LON - MIN_LON) * (cols - 1)
        row_f = (MAX_LAT - lat) / (MAX_LAT - MIN_LAT) * (rows - 1)
        
        x0, y0 = int(np.floor(col_f)), int(np.floor(row_f))
        x1, y1 = min(x0 + 1, cols - 1), min(y0 + 1, rows - 1)
        fx, fy = col_f - x0, row_f - y0
        
        v00, v10 = data[y0, x0], data[y0, x1]
        v01, v11 = data[y1, x0], data[y1, x1]
        v_top = v00 * (1 - fx) + v10 * fx
        v_bottom = v01 * (1 - fx) + v11 * fx
        elev = v_top * (1 - fy) + v_bottom * fy
        return float(max(0, elev))
    except Exception:
        return 0.0