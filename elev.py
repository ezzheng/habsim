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
    Returns interpolated elevation (meters) for given lat/lon using downsampled array.
    """
    data, shape = _get_elev_data()
    rows, cols = shape
    
    # Convert lat/lon to fractional row/col
    # Equivalent to: row_f, col_f = rowcol(transform, lon, lat, op=float)
    # For global grid: transform maps pixel (0,0) at top-left to (-180, 90)
    row_f = (90.0 - lat) / 180.0 * rows
    col_f = (lon + 180.0) / 360.0 * cols
    
    # Clamp to valid range
    row_f = np.clip(row_f, 0, rows - 1)
    col_f = np.clip(col_f, 0, cols - 1)
    
    # Integer indices and fractional offsets
    row0 = int(np.floor(row_f))
    col0 = int(np.floor(col_f))
    row1 = min(row0 + 1, rows - 1)
    col1 = min(col0 + 1, cols - 1)
    dr = row_f - row0
    dc = col_f - col0
    
    # Bilinear interpolation
    v00 = data[row0, col0]
    v10 = data[row0, col1]
    v01 = data[row1, col0]
    v11 = data[row1, col1]
    v_top = v00 * (1 - dc) + v10 * dc
    v_bottom = v01 * (1 - dc) + v11 * dc
    elev = v_top * (1 - dr) + v_bottom * dr
    
    return float(max(0, elev))