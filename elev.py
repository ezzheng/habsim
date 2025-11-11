import threading
import numpy as np
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION = 60  # points per degree (30 arc-second source halved = 60 arc-seconds)

def _get_elev_data():
    global _ELEV_DATA, _ELEV_SHAPE
    if _ELEV_DATA is not None:
        return _ELEV_DATA, _ELEV_SHAPE
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            return _ELEV_DATA, _ELEV_SHAPE
        # Use memory-mapped read to avoid loading the whole array into RAM repeatedly
        try:
            path = load_gefs('worldelev.npy')
            if path is None:
                import logging
                logging.error("load_gefs('worldelev.npy') returned None")
                return None, None
            _ELEV_DATA = np.load(path, mmap_mode='r')
            if _ELEV_DATA is None:
                import logging
                logging.error("np.load() returned None for worldelev.npy")
                return None, None
            _ELEV_SHAPE = _ELEV_DATA.shape
            if _ELEV_SHAPE is None or len(_ELEV_SHAPE) != 2:
                import logging
                logging.error(f"Invalid shape for worldelev.npy: {_ELEV_SHAPE}")
                return None, None
            
            # Validate array dimensions match expected resolution (60 points per degree)
            # Expected: 180 degrees latitude * 60 = 10800 points, 360 degrees longitude * 60 = 21600 points
            expected_height = 180 * _RESOLUTION  # 10800 for 60 resolution
            expected_width = 360 * _RESOLUTION   # 21600 for 60 resolution
            actual_height, actual_width = _ELEV_SHAPE
            
            if actual_height != expected_height or actual_width != expected_width:
                import logging
                logging.error(f"Resolution mismatch! Expected shape ({expected_height}, {expected_width}) for resolution {_RESOLUTION}, but got {_ELEV_SHAPE}. "
                            f"This will cause incorrect elevation lookups. Check if data file was created with different resolution.")
                # Don't return None - still try to use it, but log the error
                # The coordinate calculation might still work if we adjust, but it's risky
            
            return _ELEV_DATA, _ELEV_SHAPE
        except Exception as e:
            import logging
            logging.error(f"Failed to load elevation data: {e}", exc_info=True)
            return None, None

def getElevation(lat, lon):
    """Get elevation with bilinear interpolation for smoother results"""
    try:
        data, shape = _get_elev_data()
        
        # Validate data and shape are loaded correctly
        if data is None or shape is None:
            import logging
            logging.error("Elevation data not loaded: data=%s, shape=%s", data is None, shape is None)
            return 0
        
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
        except Exception as e:
            # Fallback to nearest neighbor if interpolation fails
            import logging
            logging.warning(f"Bilinear interpolation failed for ({lat}, {lon}): {e}, falling back to nearest neighbor")
            x = int(round(x_float))
            y = int(round(y_float))
            x = max(0, min(x, shape[1] - 1))
            y = max(0, min(y, shape[0] - 1))
            try:
                return max(0, round(float(data[y, x]), 2))
            except Exception as e2:
                import logging
                logging.error(f"Nearest neighbor fallback also failed for ({lat}, {lon}): {e2}")
                return 0
    except Exception as e:
        import logging
        logging.error(f"Failed to get elevation data for ({lat}, {lon}): {e}")
        return 0