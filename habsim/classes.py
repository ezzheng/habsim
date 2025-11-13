"""
Core balloon simulation classes.

Provides Balloon (state tracking), Simulator (physics engine with Runge-Kutta),
Location (geographic coordinates), Trajectory (path container), and ElevationFile
(ground elevation data). Used by simulate.py for trajectory calculations.
"""
import math
import numpy as np
from windfile import WindFile
from datetime import timedelta, datetime

EARTH_RADIUS = float(6.371e6)

def _rowcol_from_transform(rows, cols, lon, lat):
    """Convert lon/lat to row/col for global grid transform."""
    col_f = (lon + 180.0) / (360.0 / cols)
    row_f = (lat - 90.0) / (-180.0 / rows)
    return float(row_f), float(col_f)

class Trajectory(list):
    def __init__(self, data=list()):
        super().__init__(data)
        self.data = data

    def duration(self):
        """Returns duration in hours."""
        if len(self.data) < 2:
            return 0.0
        return (self.data[-1].time - self.data[0].time).total_seconds() / 3600

    def length(self):
        """Distance travelled by trajectory in km."""
        return sum(i.location.distance(j.location) for i, j in zip(self[:-1], self[1:]))

    def interpolate(self, time):
        """Interpolate position at given time. Not implemented."""
        pass

class Record:
    def __init__(self, time=None, location=None, alt=None, ascent_rate=None, air_vector=None, wind_vector=None, ground_elev=None):
        self.time = time
        self.location = location
        self.alt = alt
        self.ascent_rate = ascent_rate
        self.air_vector = air_vector
        self.wind_vector = wind_vector
        self.ground_elev = ground_elev

class Location(tuple):
    EARTH_RADIUS = 6371.0

    def __new__(cls, lat, lon):
        return tuple.__new__(cls, (lat, lon))

    def getLon(self):
        return self[1]

    def getLat(self):
        return self[0]

    def distance(self, other):
        return self.haversine(self[0], self[1], other[0], other[1])

    def haversine(self, lat1, lon1, lat2, lon2):
        """Returns great circle distance between two points in km."""
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return self.EARTH_RADIUS * c

class ElevationFile:
    def __init__(self, path):
        self.data = np.load(path, mmap_mode='r')
        self.MIN_LON = -180.00013888888893
        self.MAX_LON = 179.99985967111152
        self.MAX_LAT = 83.99986041511133
        self.MIN_LAT = -90.0001388888889

    def elev(self, lat, lon):
        """Return bilinearly interpolated elevation for (lat, lon)."""
        try:
            rows, cols = self.data.shape
            lat = np.clip(lat, self.MIN_LAT, self.MAX_LAT)
            lon = ((lon + 180) % 360) - 180
            
            col_f = (lon - self.MIN_LON) / (self.MAX_LON - self.MIN_LON) * (cols - 1)
            row_f = (self.MAX_LAT - lat) / (self.MAX_LAT - self.MIN_LAT) * (rows - 1)
            
            x0, y0 = int(np.floor(col_f)), int(np.floor(row_f))
            x1, y1 = min(x0 + 1, cols - 1), min(y0 + 1, rows - 1)
            fx, fy = col_f - x0, row_f - y0
            
            v00, v10 = self.data[y0, x0], self.data[y0, x1]
            v01, v11 = self.data[y1, x0], self.data[y1, x1]
            v_top = v00 * (1 - fx) + v10 * fx
            v_bottom = v01 * (1 - fx) + v11 * fx
            elev = v_top * (1 - fy) + v_bottom * fy
            return float(max(0, elev))
        except Exception:
            return 0.0

class Balloon:
    def __init__(self, time=None, location=None, alt=0, ascent_rate=0, air_vector=(0,0), wind_vector=None, ground_elev=None):
        record = Record(
            time=time,
            location=Location(*location) if location else None,
            alt=alt,
            ascent_rate=ascent_rate,
            air_vector=np.array(air_vector) if air_vector is not None else None,
            wind_vector=np.array(wind_vector) if wind_vector is not None else None,
            ground_elev=ground_elev
        )
        self.history = Trajectory([record])

    def update(self, time=None, location=None, alt=0, ascent_rate=0, air_vector=(0,0), wind_vector=None, ground_elev=None):
        record = Record(
            time=time or self.time,
            location=Location(*location) if location else self.location,
            alt=alt or self.alt,
            ascent_rate=ascent_rate or self.ascent_rate,
            air_vector=np.array(air_vector) if air_vector is not None else self.air_vector,
            wind_vector=np.array(wind_vector) if wind_vector is not None else self.wind_vector,
            ground_elev=ground_elev or self.ground_elev
        )
        self.history.append(record)
    
    def __getattr__(self, name):
        if name == "history":
            return super().__getattr__(name)
        return self.history[-1].__getattribute__(name)

    def __setattr__(self, name, value):
        if name != "history":
            self.history[-1].__setattr__(name, value)
        else:
            super().__setattr__(name, value)

class Simulator:
    def __init__(self, wind_file, elev_file):
        self.elev_file = ElevationFile(elev_file)
        self.wind_file = wind_file
        # Cache for elevation lookups (rounded to 4 decimal places ~11m precision)
        self._elev_cache = {}
    
    def _cached_elev(self, lat_rounded, lon_rounded):
        """Cached elevation lookup with rounded coordinates."""
        cache_key = (lat_rounded, lon_rounded)
        if cache_key not in self._elev_cache:
            if len(self._elev_cache) >= 1000:  # Limit cache size
                self._elev_cache.clear()  # Simple eviction
            self._elev_cache[cache_key] = self.elev_file.elev(lat_rounded, lon_rounded)
        return self._elev_cache[cache_key]

    def step(self, balloon, step_size: float, coefficient):
        """Runge-Kutta 2nd order integrator for balloon trajectory."""
        if self.wind_file is None:
            raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
        
        if not balloon.ground_elev:
            balloon.ground_elev = self.elev_file.elev(*balloon.location)
            balloon.alt = max(balloon.alt, balloon.ground_elev)
        
        if balloon.wind_vector is None:
            balloon.wind_vector = self.wind_file.get(*balloon.location, balloon.alt, balloon.time)

        lat0 = balloon.location.getLat()
        lon0 = balloon.location.getLon()
        alt0 = balloon.alt
        t0 = balloon.time
        asc = balloon.ascent_rate
        h = float(step_size)

        def sample_rates(lat, lon, alt, t):
            if self.wind_file is None:
                raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
            temp = self.wind_file.get(lat, lon, alt, t)
            u, v = float(temp[0]), float(temp[1])
            if balloon.air_vector is not None:
                u += float(balloon.air_vector[0])
                v += float(balloon.air_vector[1])
            dlat_dt, dlon_dt = self.lin_to_angular_velocities(lat, lon, u, v)
            dlat_dt *= coefficient
            dlon_dt *= coefficient
            return dlat_dt, dlon_dt, asc

        k1_lat, k1_lon, k1_alt = sample_rates(lat0, lon0, alt0, t0)
        
        lat_mid = lat0 + 0.5 * h * k1_lat
        lon_mid = lon0 + 0.5 * h * k1_lon
        alt_mid = alt0 + 0.5 * h * k1_alt
        t_mid = t0 + timedelta(seconds=0.5 * h)
        
        k2_lat, k2_lon, k2_alt = sample_rates(lat_mid, lon_mid, alt_mid, t_mid)
        
        newLat = lat0 + h * k2_lat
        newLon = lon0 + h * k2_lon
        newAlt = alt0 + h * k2_alt
        newTime = t0 + timedelta(seconds=h)
        newLoc = (newLat, newLon)

        if self.wind_file is None:
            raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
        wind_vector = self.wind_file.get(*newLoc, newAlt, newTime)
        
        # Only compute elevation if near ground or first step (skip when high altitude)
        if newAlt < (balloon.ground_elev or 0) + 1000 or not balloon.ground_elev:
            # Use cached elevation lookup with rounded coordinates
            ground_elev = self._cached_elev(round(newLat, 4), round(newLon, 4))
        else:
            ground_elev = balloon.ground_elev  # Reuse existing elevation
        
        balloon.update(
            location=newLoc,
            ground_elev=ground_elev,
            wind_vector=wind_vector,
            time=newTime,
            alt=newAlt,
        )
        return balloon.history[-1]

		# Original Euler integrator (preserved for reference):
		# if not balloon.ground_elev:
		# 	balloon.ground_elev = self.elev_file.elev(*balloon.location)
		# 	balloon.alt = max(balloon.alt, balloon.ground_elev)
		# 
		# if balloon.wind_vector is None:
		# 	temp = self.wind_file.get(*balloon.location, balloon.alt, balloon.time)
		# 	balloon.wind_vector = temp
		# 
		# distance_moved = (balloon.wind_vector + balloon.air_vector) * step_size
		# alt = balloon.alt + balloon.ascent_rate * step_size
		# time = balloon.time + timedelta(seconds=step_size)
		# dlat, dlon = self.lin_to_angular_velocities(*balloon.location, *distance_moved)
		# 
		# # multiply by coeff to do FLOAT type balloon
		# newLat = balloon.location.getLat() + dlat * coefficient
		# newLon = balloon.location.getLon() + dlon * coefficient
		# newLoc = newLat, newLon
		# 
		# balloon.update(location=newLoc, 
		# 			ground_elev=self.elev_file.elev(*newLoc), 
		# 			wind_vector=self.wind_file.get(*newLoc, alt, time),
		# 			time=time, alt=alt)
		# return balloon.history[-1]
		
    def lin_to_angular_velocities(self, lat, lon, u, v): 
        dlat = math.degrees(v / EARTH_RADIUS)
        dlon = math.degrees(u / (EARTH_RADIUS * math.cos(math.radians(lat))))
        return dlat, dlon

    def simulate(self, balloon, step_size, coefficient, elevation, target_alt=None, dur=None): 
        if step_size < 0:
            raise Exception("step size cannot be negative")
        
        if (target_alt and dur != None) or not (target_alt or dur != None):
            raise Exception("Trajectory simulation must either have a max altitude or specified duration, not both")
        step_history =Trajectory([balloon.history[-1]])
        
        if dur == None:
            dur = ((target_alt - balloon.alt) / balloon.ascent_rate) / 3600
        
        if dur == 0:
            step_history.append(self.step(balloon, 0, coefficient))
        end_time = balloon.time + timedelta(hours=dur)
        while (end_time - balloon.time).total_seconds() > 1:
            # Calculate step size (may be reduced if hitting end_time or ground)
            current_step_size = step_size
            if balloon.time + timedelta(seconds=step_size) >= end_time:
                current_step_size = (end_time - balloon.time).seconds
            
            # If descending and elevation checking is enabled, check if we'll hit ground
            if elevation and balloon.ascent_rate < 0:  # Descending
                current_ground_elev = self.elev_file.elev(*balloon.location)
                # Estimate altitude after full step (simple linear estimate)
                estimated_alt_after = balloon.alt + balloon.ascent_rate * current_step_size
                
                # If we would go below ground, calculate exact time to reach ground
                if estimated_alt_after < current_ground_elev:
                    # Calculate time to reach ground: (alt - ground_elev) / descent_rate
                    # descent_rate is negative, so we need absolute value
                    time_to_ground = (balloon.alt - current_ground_elev) / abs(balloon.ascent_rate)
                    # Use smaller of: time to ground or remaining step time
                    current_step_size = min(time_to_ground, current_step_size)
                    # Minimum step size to avoid numerical issues
                    if current_step_size < 0.1:
                        current_step_size = 0.1
            
            newRecord = self.step(balloon, current_step_size, coefficient)

            #total_airtime += step_size
            step_history.append(newRecord)
            
            # break if balloon hits the ground (check after step to catch any overshoot)
            if elevation and balloon.alt <= self.elev_file.elev(*balloon.location):
                break
        return step_history

