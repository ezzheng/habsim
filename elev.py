import threading
import numpy as np
from gefs import load_gefs
import logging

# Get logger - will use app.logger if available, otherwise root logger
_logger = None

def set_logger(logger):
    """Set the logger to use for elevation logging"""
    global _logger
    _logger = logger

def _get_logger():
    """Get the logger, using app.logger if available"""
    global _logger
    if _logger is not None:
        return _logger
    return logging.getLogger(__name__)

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION_LAT = 60  # points per degree for latitude (will be calculated from file)
_RESOLUTION_LON = 60  # points per degree for longitude (will be calculated from file)

def _get_elev_data():
    global _ELEV_DATA, _ELEV_SHAPE, _RESOLUTION_LAT, _RESOLUTION_LON
    if _ELEV_DATA is not None:
        # Always recalculate resolutions from shape to ensure correctness
        actual_height, actual_width = _ELEV_SHAPE
        _RESOLUTION_LAT = actual_height / 180.0
        _RESOLUTION_LON = actual_width / 360.0
        return _ELEV_DATA, _ELEV_SHAPE
    with _ELEV_LOCK:
        if _ELEV_DATA is not None:
            actual_height, actual_width = _ELEV_SHAPE
            _RESOLUTION_LAT = actual_height / 180.0
            _RESOLUTION_LON = actual_width / 360.0
            return _ELEV_DATA, _ELEV_SHAPE
        # Use memory-mapped read to avoid loading the whole array into RAM repeatedly
        path = load_gefs('worldelev.npy')
        if path is None:
            _get_logger().error("load_gefs('worldelev.npy') returned None")
            return None, None
        _ELEV_DATA = np.load(path, mmap_mode='r')
        if _ELEV_DATA is None:
            _get_logger().error("np.load() returned None for worldelev.npy")
            return None, None
        _ELEV_SHAPE = _ELEV_DATA.shape
        if _ELEV_SHAPE is None or len(_ELEV_SHAPE) != 2:
            _get_logger().error(f"Invalid shape for worldelev.npy: {_ELEV_SHAPE}")
            return None, None
        # Calculate actual resolution from file dimensions
        actual_height, actual_width = _ELEV_SHAPE
        _RESOLUTION_LAT = actual_height / 180.0  # points per degree for latitude
        _RESOLUTION_LON = actual_width / 360.0  # points per degree for longitude
        return _ELEV_DATA, _ELEV_SHAPE

def getElevation(lat, lon):
    """Get elevation with bilinear interpolation for smoother results"""
    data, shape = _get_elev_data()
    
    # Check if data loaded successfully
    if data is None or shape is None:
        _get_logger().error(f"Elevation data not loaded for ({lat}, {lon})")
        return 0
    
    # Convert lat/lon to grid coordinates (continuous)
    # Use separate resolutions for latitude and longitude
    x_float = (lon + 180) * _RESOLUTION_LON
    y_float = (90 - lat) * _RESOLUTION_LAT - 1
    
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
        _get_logger().warning(f"Bilinear interpolation failed for ({lat}, {lon}): {e}, falling back to nearest neighbor")
        # Fallback to nearest neighbor if interpolation fails
        x = int(round(x_float))
        y = int(round(y_float))
        x = max(0, min(x, shape[1] - 1))
        y = max(0, min(y, shape[0] - 1))
        try:
            return max(0, round(float(data[y, x]), 2))
        except Exception as e2:
            _get_logger().error(f"Nearest neighbor fallback also failed for ({lat}, {lon}): {e2}")
            return 0
