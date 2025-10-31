import math
import datetime
import threading
from io import BytesIO
from pathlib import Path
from typing import Union

import numpy as np
from numpy.lib.format import open_memmap

_MEMMAP_LOCKS: dict[Path, threading.Lock] = {}
_MEMMAP_LOCKS_LOCK = threading.Lock()


def _normalize_path(path: Union[BytesIO, str]) -> Union[BytesIO, Path]:
    if isinstance(path, (str, Path)):
        return Path(path)
    return path


def _get_memmap_lock(path: Path) -> threading.Lock:
    with _MEMMAP_LOCKS_LOCK:
        lock = _MEMMAP_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _MEMMAP_LOCKS[path] = lock
        return lock


class WindFile:
    def __init__(self, path: Union[BytesIO, str]):
        normalized_path = _normalize_path(path)
        npz = np.load(normalized_path)

        try:
            self.time = float(npz['timestamp'][()])
            self.levels = np.array(npz['levels'], copy=True)
            self.interval = float(npz['interval'][()])

            if isinstance(normalized_path, Path) and normalized_path.suffix == '.npz':
                self.data = self._load_memmap_data(npz, normalized_path)
            else:
                self.data = np.array(npz['data'], copy=True)
        finally:
            npz.close()

        self.resolution_lat_multiplier = (self.data.shape[-5] - 1) / 180
        self.resolution_lon_multiplier = (self.data.shape[-4] - 1) / 360

        level_array = np.asarray(self.levels, dtype=np.float32)
        level_indices = np.arange(level_array.size, dtype=np.float32)

        if level_array.size == 0:
            raise ValueError("WindFile levels array is empty")

        level_diff = np.diff(level_array)
        if np.all(level_diff > 0):  # already ascending
            interp_levels = level_array
            interp_indices = level_indices
        elif np.all(level_diff < 0):  # descending
            interp_levels = level_array[::-1]
            interp_indices = level_indices[::-1]
        else:  # unordered, fall back to argsort
            sort_idx = np.argsort(level_array)
            interp_levels = level_array[sort_idx]
            interp_indices = level_indices[sort_idx]

        self._interp_levels = interp_levels
        self._interp_indices = interp_indices
        self._interp_min = float(interp_levels[0])
        self._interp_max = float(interp_levels[-1])

    def _load_memmap_data(self, npz: np.lib.npyio.NpzFile, path: Path):
        memmap_path = Path(f"{path}.data.npy")
        if not memmap_path.exists():
            lock = _get_memmap_lock(memmap_path)
            with lock:
                if not memmap_path.exists():
                    array = npz['data']
                    memmap_path.parent.mkdir(parents=True, exist_ok=True)
                    mm = open_memmap(memmap_path, mode='w+', dtype=array.dtype, shape=array.shape)
                    mm[...] = array
                    mm.flush()
                    del mm
                    del array
        return np.load(memmap_path, mmap_mode='r')

    def get(self, lat, lon, altitude, time):
        if lat < -90 or lat > 90:
            raise Exception(f"Latitude {lat} out of bounds")
        if lon < -180 or lon > 360:
            raise Exception(f"Longitude {lon} out of bounds")
        
        if lon < 0:
            lon = 360 + lon

        if isinstance(time, datetime.datetime):
            time = time.timestamp()
        
        tmax = self.time + self.interval * (self.data.shape[-2]-1)
        if time < self.time or time > tmax:
            raise Exception(f"Time {time} out of bounds")

        indices = self.get_indices(lat, lon, altitude, time)
        
        return self.interpolate(*indices)

    def get_indices(self, lat, lon, alt, time):
        lat = (90 - lat) * self.resolution_lat_multiplier
        lon = (lon % 360) * self.resolution_lon_multiplier

        time = (time - self.time)/self.interval
        pressure = self.get_pressure_index(alt)
        
        return lat, lon, pressure, time

    def get_pressure_index(self, alt):
        pressure = self.alt_to_hpa(alt)

        if pressure < self._interp_min or pressure > self._interp_max:
            raise Exception(f"Altitude {alt} out of bounds")

        return float(np.interp(pressure, self._interp_levels, self._interp_indices))

    def interpolate(self, lat, lon, level, time):
        pressure_filter = np.array([1-level % 1, level % 1]).reshape(1, 1, 2, 1, 1)
        time_filter = np.array([1-time % 1, time % 1]).reshape(1, 1, 1, 2, 1) 
        lat_filter = np.array([1-lat % 1, lat % 1]).reshape(2, 1, 1, 1, 1)
        lon_filter = np.array([1-lon % 1, lon % 1]).reshape(1, 2, 1, 1, 1)

        lat = int(lat)
        lon = int(lon)
        level = int(level)
        time = int(time)

        cube = self.data[lat:lat+2, lon:lon+2, level:level+2, time:time+2, :]
       
        return np.sum(cube * lat_filter * lon_filter * pressure_filter * time_filter, axis=(0,1,2,3))

    def alt_to_hpa(self, altitude):
        pa_to_hpa = 1.0/100.0
        if altitude < 11000:
            return pa_to_hpa * (1-altitude/44330.7)**5.2558 * 101325
        else:
            return pa_to_hpa * math.exp(altitude / -6341.73) * 128241

    def hpa_to_alt(self, p):
        if p >  226.325:
            return 44330.7 * (1 - (p / 1013.25) ** 0.190266)
        else:
            return -6341.73 * (math.log(p) - 7.1565)
