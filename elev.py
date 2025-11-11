import threading
import warnings
import numpy as np
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION_LAT = 58  # points per degree for latitude (10440 / 180)
_RESOLUTION_LON = 60  # points per degree for longitude (21600 / 360)

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
    """
    Bilinear-interpolated elevation lookup that treats array pixels as CELL CENTERS.
    
    Works for arbitrary (h, w) shapes that span [-90..+90] lat and [-180..+180] lon.
    
    Returns elevation rounded to 2 decimals, min 0.
    """
    data, shape = _get_elev_data()
    h, w = shape
    
    # Diagnostic: compute and warn about coarse resolution
    res_lat_deg = 180.0 / h  # degrees per pixel in latitude
    res_lon_deg = 360.0 / w  # degrees per pixel in longitude
    if (res_lat_deg > 1.0 or res_lon_deg > 1.0):
        # warn once or log; using warnings.warn so it can be captured
        warnings.warn(
            f"Elevation grid is coarse: lat_res={res_lat_deg:.4f}°, lon_res={res_lon_deg:.4f}°. "
            "Consider using higher-resolution elevation for accurate results."
        )
    
    # Normalize inputs
    lon = ((lon + 180.0) % 360.0) - 180.0
    lat = max(-90.0, min(90.0, lat))
    
    # Pixel-center mapping:
    # Pixel centers are located at:
    #   lon_center_i = -180 + (i + 0.5) * (360 / w)  for i in [0..w-1]
    # So invert that mapping to get continuous index coordinate:
    x_float = ( (lon + 180.0) / 360.0 ) * w - 0.5
    y_float = ( (90.0 - lat) / 180.0 ) * h - 0.5
    
    # Now standard bilinear interpolation using floor indices
    x0 = int(np.floor(x_float))
    y0 = int(np.floor(y_float))
    fx = x_float - x0
    fy = y_float - y0
    
    # Neighbor indices (clamped)
    x0_clamped = max(0, min(x0, w - 1))
    y0_clamped = max(0, min(y0, h - 1))
    x1_clamped = max(0, min(x0 + 1, w - 1))
    y1_clamped = max(0, min(y0 + 1, h - 1))
    
    try:
        v00 = float(data[y0_clamped, x0_clamped])
        v10 = float(data[y0_clamped, x1_clamped])
        v01 = float(data[y1_clamped, x0_clamped])
        v11 = float(data[y1_clamped, x1_clamped])
        
        # If original x0/y0 were out-of-bounds, fx/fy might be outside [0,1].
        # Clamp interpolation fractions to [0,1] to avoid weird extrapolation.
        fx = min(max(fx, 0.0), 1.0)
        fy = min(max(fy, 0.0), 1.0)
        
        v0 = v00 * (1.0 - fx) + v10 * fx
        v1 = v01 * (1.0 - fx) + v11 * fx
        val = v0 * (1.0 - fy) + v1 * fy
        
        return max(0.0, round(float(val), 2))
    except Exception:
        # Fallback to nearest neighbor
        xi = int(round(x_float))
        yi = int(round(y_float))
        xi = max(0, min(xi, w - 1))
        yi = max(0, min(yi, h - 1))
        try:
            return max(0.0, round(float(data[yi, xi]), 2))
        except Exception:
            return 0.0
