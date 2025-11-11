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
            
            # Calculate actual resolution from file dimensions
            # Resolution = points per degree = array_size / degrees
            actual_height, actual_width = _ELEV_SHAPE
            actual_resolution_lat = actual_height / 180.0  # points per degree for latitude
            actual_resolution_lon = actual_width / 360.0  # points per degree for longitude
            
            # Use the actual resolution from the file instead of hardcoded value
            # This ensures coordinate calculations match the actual data
            global _RESOLUTION
            if abs(actual_resolution_lat - actual_resolution_lon) > 0.01:
                import logging
                logging.warning(f"Elevation file has different lat/lon resolutions: {actual_resolution_lat} vs {actual_resolution_lon}, using lat resolution")
            _RESOLUTION = actual_resolution_lat
            
            if abs(_RESOLUTION - 60) > 1:
                import logging
                logging.warning(f"Elevation file resolution ({_RESOLUTION:.2f} points/degree) differs from expected (60). Using actual resolution from file.")
            
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