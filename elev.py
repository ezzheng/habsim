import threading
import numpy as np
from gefs import load_gefs

_ELEV_DATA = None
_ELEV_SHAPE = None
_ELEV_LOCK = threading.Lock()
_RESOLUTION_LAT = 60  # points per degree for latitude (will be calculated from file)
_RESOLUTION_LON = 60  # points per degree for longitude (will be calculated from file)

def _get_elev_data():
    global _ELEV_DATA, _ELEV_SHAPE, _RESOLUTION_LAT, _RESOLUTION_LON
    with _ELEV_LOCK:
        if _ELEV_DATA is not None and _ELEV_SHAPE is not None:
            # Data already loaded - always recalculate resolutions from shape to ensure correctness
            actual_height, actual_width = _ELEV_SHAPE
            _RESOLUTION_LAT = actual_height / 180.0
            _RESOLUTION_LON = actual_width / 360.0
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
            
            # Use the actual resolutions from the file (they may differ)
            # This ensures coordinate calculations match the actual data
            _RESOLUTION_LAT = actual_resolution_lat
            _RESOLUTION_LON = actual_resolution_lon
            
            if abs(actual_resolution_lat - actual_resolution_lon) > 0.01:
                import logging
                logging.info(f"Elevation file has different lat/lon resolutions: {actual_resolution_lat:.2f} (lat) vs {actual_resolution_lon:.2f} (lon) - using both correctly")
            if abs(actual_resolution_lat - 60) > 1 or abs(actual_resolution_lon - 60) > 1:
                import logging
                logging.info(f"Elevation file resolutions (lat: {actual_resolution_lat:.2f}, lon: {actual_resolution_lon:.2f} pts/deg) differ from expected (60). Using actual resolutions from file.")
            
            return _ELEV_DATA, _ELEV_SHAPE
        except Exception as e:
            import logging
            logging.error(f"Failed to load elevation data: {e}", exc_info=True)
            return None, None

def getElevation(lat, lon):
    """Get elevation with bilinear interpolation for smoother results"""
    import logging
    # Use root logger to ensure logs appear
    logger = logging.getLogger()
    logger.info(f"[ELEV DEBUG] getElevation called with lat={lat}, lon={lon}")
    try:
        data, shape = _get_elev_data()
        logger.info(f"[ELEV DEBUG] _get_elev_data returned: data is None={data is None}, shape={shape}")
        
        # Validate data and shape are loaded correctly
        if data is None or shape is None:
            logger.error("Elevation data not loaded: data=%s, shape=%s", data is None, shape is None)
            return 0
        
        logger.info(f"[ELEV DEBUG] Data loaded: shape={shape}, resolutions: lat={_RESOLUTION_LAT:.2f}, lon={_RESOLUTION_LON:.2f}")
        
        # Convert lat/lon to grid coordinates (continuous)
        # Use separate resolutions for latitude and longitude
        # Formula: array covers -90 to +90 lat (180 degrees) and -180 to +180 lon (360 degrees)
        # For latitude: 90 (north) -> index 0, -90 (south) -> index (height-1)
        # For longitude: -180 -> index 0, +180 -> index (width-1)
        x_float = (lon + 180) * _RESOLUTION_LON
        # The -1 offset is needed: at lat=-90, (90-(-90))*58 = 10440, but max index is 10439
        # So we subtract 1 to get the correct index range [0, 10439]
        y_float = (90 - lat) * _RESOLUTION_LAT - 1
        
        # Get integer indices and fractional parts for interpolation
        x0 = int(np.floor(x_float))
        y0 = int(np.floor(y_float))
        x1 = x0 + 1
        y1 = y0 + 1
        fx = x_float - x0  # fractional part in x
        fy = y_float - y0  # fractional part in y
        
        # Clamp indices to valid range
        x0_clamped = max(0, min(x0, shape[1] - 1))
        y0_clamped = max(0, min(y0, shape[0] - 1))
        x1_clamped = max(0, min(x1, shape[1] - 1))
        y1_clamped = max(0, min(y1, shape[0] - 1))
        
        # Debug logging - always log for now
        logger.info(f"[ELEV DEBUG] Coordinate calculation: lat={lat}, lon={lon}, res_lat={_RESOLUTION_LAT:.2f}, res_lon={_RESOLUTION_LON:.2f}, "
                    f"x_float={x_float:.2f}, y_float={y_float:.2f}, x0={x0}, y0={y0}, shape={shape}")
        
        try:
            # Bilinear interpolation: sample 4 corners and blend
            v00 = float(data[y0_clamped, x0_clamped])  # bottom-left
            v10 = float(data[y0_clamped, x1_clamped])  # bottom-right
            v01 = float(data[y1_clamped, x0_clamped])  # top-left
            v11 = float(data[y1_clamped, x1_clamped])  # top-right
            
            logger.info(f"[ELEV DEBUG] Elevation values: v00={v00}, v10={v10}, v01={v01}, v11={v11}, "
                        f"indices: ({y0_clamped},{x0_clamped}), ({y0_clamped},{x1_clamped}), ({y1_clamped},{x0_clamped}), ({y1_clamped},{x1_clamped})")
            
            # Interpolate in x direction
            v0 = v00 * (1 - fx) + v10 * fx  # bottom edge
            v1 = v01 * (1 - fx) + v11 * fx  # top edge
            
            # Interpolate in y direction
            result = v0 * (1 - fy) + v1 * fy
            
            logger.info(f"[ELEV DEBUG] Elevation interpolation: fx={fx:.3f}, fy={fy:.3f}, v0={v0:.2f}, v1={v1:.2f}, result={result:.2f}")
            
            final_result = max(0, round(result, 2))
            logger.info(f"[ELEV DEBUG] Returning elevation: {final_result}")
            return final_result
        except Exception as e:
            # Fallback to nearest neighbor if interpolation fails
            logger.warning(f"Bilinear interpolation failed for ({lat}, {lon}): {e}, falling back to nearest neighbor")
            x = int(round(x_float))
            y = int(round(y_float))
            x_clamped = max(0, min(x, shape[1] - 1))
            y_clamped = max(0, min(y, shape[0] - 1))
            try:
                result = float(data[y_clamped, x_clamped])
                logger.info(f"[ELEV DEBUG] Nearest neighbor fallback: x={x}, y={y}, clamped to ({y_clamped},{x_clamped}), value={result}")
                return max(0, round(result, 2))
            except Exception as e2:
                logger.error(f"Nearest neighbor fallback also failed for ({lat}, {lon}): {e2}")
                return 0
    except Exception as e:
        logger.error(f"Failed to get elevation data for ({lat}, {lon}): {e}", exc_info=True)
        return 0