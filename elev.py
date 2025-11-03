import threading
import numpy as np
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION = 120  # points per degree

def _get_elev_data():
    global _ELEV_DATA
    if _ELEV_DATA is not None:
        return _ELEV_DATA
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            return _ELEV_DATA
        # Use memory-mapped read to avoid loading the whole array into RAM repeatedly
        path = load_gefs('worldelev.npy')
        _ELEV_DATA = np.load(path, mmap_mode='r')
        return _ELEV_DATA

def getElevation(lat, lon):
    data = _get_elev_data()
    x = int(round((lon + 180) * _RESOLUTION))
    y = int(round((90 - lat) * _RESOLUTION)) - 1
    # Clamp to data bounds for safety
    if y < 0: y = 0
    if x < 0: x = 0
    if y >= data.shape[0]: y = data.shape[0] - 1
    if x >= data.shape[1]: x = data.shape[1] - 1
    try:
        return max(0, float(data[y, x]))
    except Exception:
        return 0