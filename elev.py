import threading
import numpy as np
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION = 120  # points per degree

def _get_elev_data():
    global _ELEV_DATA, _ELEV_SHAPE
    if _ELEV_DATA is not None:
        return _ELEV_DATA, _ELEV_SHAPE
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            return _ELEV_DATA, _ELEV_SHAPE
        # Use memory-mapped read to avoid loading the whole array into RAM repeatedly
        path = load_gefs('worldelev.npy')
        _ELEV_DATA = np.load(path, mmap_mode='r')
        _ELEV_SHAPE = _ELEV_DATA.shape
        return _ELEV_DATA, _ELEV_SHAPE

def getElevation(lat, lon):
    """Get elevation with bilinear interpolation for smoother results"""
    data, shape = _get_elev_data()
    
    # Convert lat/lon to grid coordinates (continuous)
    x_float = (lon + 180) * _RESOLUTION
    y_float = (90 - lat) * _RESOLUTION - 1
    
    # Get integer indices and fractional parts for interpolation
    x0 = int(np.floor(x_float))
    y0 = int(np.floor(y_float))
    x1 = x0 + 1
    y1 = y0 + 1
    fx = x_float - x0  # fractional part in x
    fy = y_float - y0  # fractional part in y
    
    # Clamp indices to valid range
    x0 = max(0, min(x0, shape[1] - 1))
    y0 = max(0, min(y0, shape[0] - 1))
    x1 = max(0, min(x1, shape[1] - 1))
    y1 = max(0, min(y1, shape[0] - 1))
    
    try:
        # Bilinear interpolation: sample 4 corners and blend
        v00 = float(data[y0, x0])  # bottom-left
        v10 = float(data[y0, x1])  # bottom-right
        v01 = float(data[y1, x0])  # top-left
        v11 = float(data[y1, x1])  # top-right
        
        # Interpolate in x direction
        v0 = v00 * (1 - fx) + v10 * fx  # bottom edge
        v1 = v01 * (1 - fx) + v11 * fx  # top edge
        
        # Interpolate in y direction
        result = v0 * (1 - fy) + v1 * fy
        
        return max(0, round(result, 2))
    except Exception:
        # Fallback to nearest neighbor if interpolation fails
        x = int(round(x_float))
        y = int(round(y_float))
        x = max(0, min(x, shape[1] - 1))
        y = max(0, min(y, shape[0] - 1))
        try:
            return max(0, round(float(data[y, x]), 2))
        except Exception:
            return 0