import numpy as np
import threading
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()

def _get_elev_data():
    """Load elevation data once with memory mapping for efficiency."""
    global _ELEV_DATA, _ELEV_SHAPE
    if _ELEV_DATA is not None:
        return _ELEV_DATA, _ELEV_SHAPE
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            return _ELEV_DATA, _ELEV_SHAPE
        # Use load_gefs to handle S3 downloads and caching
        path = load_gefs('worldelev.npy')
        _ELEV_DATA = np.load(path, mmap_mode='r')
        _ELEV_SHAPE = _ELEV_DATA.shape
        return _ELEV_DATA, _ELEV_SHAPE

def getElevation(lat, lon):
    """
    Returns interpolated elevation (in meters) for given lat/lon.
    Uses bilinear interpolation. Clamps to [0, max elevation].
    """
    data, shape = _get_elev_data()
    
    # Clamp latitude/longitude
    lat = max(-90.0, min(90.0, lat))
    lon = ((lon + 180.0) % 360.0) - 180.0  # normalize to [-180, 180]
    
    # Convert lat/lon to float pixel indices
    y_float = (90.0 - lat) / 180.0 * (shape[0] - 1)   # 0=top (90N), max=bottom (-90S)
    x_float = (lon + 180.0) / 360.0 * (shape[1] - 1)  # 0=left (-180), max=right (180)
    
    # Integer indices and fractional parts
    x0 = int(np.floor(x_float))
    y0 = int(np.floor(y_float))
    x1 = min(x0 + 1, shape[1] - 1)
    y1 = min(y0 + 1, shape[0] - 1)
    fx = x_float - x0
    fy = y_float - y0
    
    try:
        # Bilinear interpolation
        v00 = float(data[y0, x0])  # top-left
        v10 = float(data[y0, x1])  # top-right
        v01 = float(data[y1, x0])  # bottom-left
        v11 = float(data[y1, x1])  # bottom-right
        
        # Interpolate along x
        v_top = v00 * (1 - fx) + v10 * fx
        v_bottom = v01 * (1 - fx) + v11 * fx
        
        # Interpolate along y
        elev = v_top * (1 - fy) + v_bottom * fy
        
        return max(0.0, round(elev, 2))
    except Exception:
        # fallback to nearest neighbor
        xi = int(round(x_float))
        yi = int(round(y_float))
        xi = max(0, min(shape[1] - 1, xi))
        yi = max(0, min(shape[0] - 1, yi))
        return max(0.0, float(data[yi, xi]))
