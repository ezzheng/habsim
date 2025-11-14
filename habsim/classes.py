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

# Earth radius in meters (used for coordinate transformations)
EARTH_RADIUS = float(6.371e6)

def _rowcol_from_transform(rows, cols, lon, lat):
    """Convert lon/lat to row/col indices for global grid transform."""
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
    """
    Geographic location as immutable tuple (lat, lon).
    
    Provides distance calculation using haversine formula for great circle
    distance (accounts for Earth's curvature, accurate for long distances).
    """
    EARTH_RADIUS = 6371.0  # Earth radius in km (for distance calculations)

    def __new__(cls, lat, lon):
        return tuple.__new__(cls, (lat, lon))

    def getLon(self):
        return self[1]

    def getLat(self):
        return self[0]

    def distance(self, other):
        """Calculate great circle distance to another location in km."""
        return self.haversine(self[0], self[1], other[0], other[1])

    def haversine(self, lat1, lon1, lat2, lon2):
        """
        Calculate great circle distance between two points using haversine formula.
        
        Returns distance in km. More accurate than simple Euclidean distance for
        long distances because it accounts for Earth's curvature.
        
        Formula: a = sin²(Δlat/2) + cos(lat1) × cos(lat2) × sin²(Δlon/2)
                 c = 2 × atan2(√a, √(1-a))
                 distance = R × c
        """
        # Convert degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        # Haversine formula
        a = math.sin(dlat/2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return self.EARTH_RADIUS * c

class ElevationFile:
    """
    Ground elevation data access with bilinear interpolation.
    
    Loads worldelev.npy (memory-mapped) and provides bilinear interpolation
    for elevation at arbitrary lat/lon coordinates. Same algorithm as elev.py
    but as a class instance (allows multiple instances if needed).
    """
    def __init__(self, path):
        # Memory-map elevation data (451MB file, but only accessed pages loaded)
        self.data = np.load(path, mmap_mode='r')
        # Geographic bounds of elevation data
        self.MIN_LON = -180.00013888888893
        self.MAX_LON = 179.99985967111152
        self.MAX_LAT = 83.99986041511133
        self.MIN_LAT = -90.0001388888889

    def elev(self, lat, lon):
        """
        Return bilinearly interpolated elevation for (lat, lon).
        
        Performs bilinear interpolation on 2D elevation grid. Clips coordinates
        to valid range and handles longitude wrap-around. Returns 0.0 on error.
        """
        try:
            rows, cols = self.data.shape
            # Clip latitude to valid range (prevents out-of-bounds access)
            lat = np.clip(lat, self.MIN_LAT, self.MAX_LAT)
            # Normalize longitude to [-180, 180] range (handles wrap-around)
            lon = ((lon + 180) % 360) - 180
            
            # Convert lat/lon to grid coordinates (floating-point indices)
            col_f = (lon - self.MIN_LON) / (self.MAX_LON - self.MIN_LON) * (cols - 1)
            row_f = (self.MAX_LAT - lat) / (self.MAX_LAT - self.MIN_LAT) * (rows - 1)
            
            # Get integer indices of 2×2 grid cell surrounding target point
            x0, y0 = int(np.floor(col_f)), int(np.floor(row_f))
            x1, y1 = min(x0 + 1, cols - 1), min(y0 + 1, rows - 1)  # Clamp to grid bounds
            # Fractional parts for interpolation weights
            fx, fy = col_f - x0, row_f - y0
            
            # Extract 4 corner values of grid cell
            v00, v10 = self.data[y0, x0], self.data[y0, x1]  # Top row
            v01, v11 = self.data[y1, x0], self.data[y1, x1]  # Bottom row
            
            # Bilinear interpolation: interpolate horizontally first, then vertically
            v_top = v00 * (1 - fx) + v10 * fx      # Interpolate top row
            v_bottom = v01 * (1 - fx) + v11 * fx   # Interpolate bottom row
            elev = v_top * (1 - fy) + v_bottom * fy  # Interpolate vertically
            
            # Return non-negative elevation (ocean/sea level is 0)
            return float(max(0, elev))
        except Exception:
            # Return 0.0 on any error (safe fallback - simulation continues)
            return 0.0

class Balloon:
    """
    Balloon state tracking with trajectory history.
    
    Uses history-based state: current state is always the last Record in history.
    This allows tracking full trajectory while maintaining simple attribute access
    (balloon.alt, balloon.location, etc. always refer to current state).
    """
    def __init__(self, time=None, location=None, alt=0, ascent_rate=0, air_vector=(0,0), wind_vector=None, ground_elev=None):
        # Create initial state record
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
        """
        Add new state record to trajectory history.
        
        Uses current values as defaults (allows partial updates). New record becomes
        the current state (accessible via balloon.alt, balloon.location, etc.).
        """
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
        """
        Delegate attribute access to current state (last record in history).
        
        This allows balloon.alt, balloon.location, etc. to work naturally
        while state is actually stored in history list.
        """
        if name == "history":
            return super().__getattr__(name)
        return self.history[-1].__getattribute__(name)

    def __setattr__(self, name, value):
        """
        Delegate attribute setting to current state (last record in history).
        
        Setting balloon.alt = 1000 actually updates self.history[-1].alt.
        """
        if name != "history":
            self.history[-1].__setattr__(name, value)
        else:
            super().__setattr__(name, value)

class Simulator:
    """
    Physics engine for balloon trajectory simulation.
    
    Uses Runge-Kutta 2nd order (RK2) integrator to solve differential equations
    for balloon motion. Accounts for wind, ascent/descent rates, and ground elevation.
    """
    def __init__(self, wind_file, elev_file):
        # Accept either a path (str/Path) or an ElevationFile object
        # This allows sharing ElevationFile instances across simulators (ensemble mode)
        if isinstance(elev_file, ElevationFile):
            self.elev_file = elev_file
        else:
            self.elev_file = ElevationFile(elev_file)
        self.wind_file = wind_file
        # Cache for elevation lookups (rounded to 4 decimal places ≈ 11m precision)
        # Reduces redundant elevation calculations during simulation
        self._elev_cache = {}
    
    def _cached_elev(self, lat_rounded, lon_rounded):
        """
        Cached elevation lookup with rounded coordinates.
        
        Rounds coordinates to 4 decimal places (≈11m precision) to increase
        cache hit rate. Elevation doesn't change significantly over 11m.
        """
        cache_key = (lat_rounded, lon_rounded)
        if cache_key not in self._elev_cache:
            if len(self._elev_cache) >= 1000:  # Limit cache size
                self._elev_cache.clear()  # Simple eviction (FIFO)
            self._elev_cache[cache_key] = self.elev_file.elev(lat_rounded, lon_rounded)
        return self._elev_cache[cache_key]

    def step(self, balloon, step_size: float, coefficient):
        """
        Runge-Kutta 2nd order (RK2) integrator for one simulation step.
        
        RK2 method: evaluates derivative at start (k1) and midpoint (k2), then
        uses k2 to advance state. More accurate than Euler method for same step size.
        
        Args:
            balloon: Balloon object (state is updated in-place)
            step_size: Time step in seconds
            coefficient: Floating coefficient (scales horizontal wind effect)
        
        Returns:
            New Record object with updated state
        """
        if self.wind_file is None:
            raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
        
        # Initialize ground elevation on first step
        if not balloon.ground_elev:
            balloon.ground_elev = self.elev_file.elev(*balloon.location)
            balloon.alt = max(balloon.alt, balloon.ground_elev)  # Ensure altitude >= ground
        
        # Get wind vector if not already cached
        if balloon.wind_vector is None:
            balloon.wind_vector = self.wind_file.get(*balloon.location, balloon.alt, balloon.time)

        # Current state
        lat0 = balloon.location.getLat()
        lon0 = balloon.location.getLon()
        alt0 = balloon.alt
        t0 = balloon.time
        asc = balloon.ascent_rate
        h = float(step_size)

        def sample_rates(lat, lon, alt, t):
            """
            Sample rate of change (derivative) at given position/time.
            
            Returns (dlat_dt, dlon_dt, dalt_dt) - angular velocities in deg/s
            and vertical velocity in m/s.
            """
            if self.wind_file is None:
                raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
            # Get wind vector at this position/time
            temp = self.wind_file.get(lat, lon, alt, t)
            u, v = float(temp[0]), float(temp[1])  # u = eastward, v = northward
            # Add air vector if present (balloon's own motion relative to wind)
            if balloon.air_vector is not None:
                u += float(balloon.air_vector[0])
                v += float(balloon.air_vector[1])
            # Convert linear velocities (m/s) to angular velocities (deg/s)
            dlat_dt, dlon_dt = self.lin_to_angular_velocities(lat, lon, u, v)
            # Apply floating coefficient (scales horizontal wind effect)
            dlat_dt *= coefficient
            dlon_dt *= coefficient
            return dlat_dt, dlon_dt, asc  # asc is already in m/s

        # RK2 Step 1: Evaluate derivative at start (k1)
        k1_lat, k1_lon, k1_alt = sample_rates(lat0, lon0, alt0, t0)
        
        # RK2 Step 2: Evaluate derivative at midpoint (k2)
        # Midpoint is calculated using k1: state_mid = state0 + 0.5 * h * k1
        lat_mid = lat0 + 0.5 * h * k1_lat
        lon_mid = lon0 + 0.5 * h * k1_lon
        alt_mid = alt0 + 0.5 * h * k1_alt
        t_mid = t0 + timedelta(seconds=0.5 * h)
        
        k2_lat, k2_lon, k2_alt = sample_rates(lat_mid, lon_mid, alt_mid, t_mid)
        
        # RK2 Step 3: Advance state using k2 (more accurate than using k1)
        # new_state = old_state + h * k2
        newLat = lat0 + h * k2_lat
        newLon = lon0 + h * k2_lon
        newAlt = alt0 + h * k2_alt
        newTime = t0 + timedelta(seconds=h)
        newLoc = (newLat, newLon)

        # Get wind vector at new position (for next step)
        if self.wind_file is None:
            raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
        wind_vector = self.wind_file.get(*newLoc, newAlt, newTime)
        
        # OPTIMIZATION: Only compute elevation if near ground or first step
        # When high altitude (>1000m above ground), elevation doesn't change much
        # This avoids expensive elevation lookups during most of the flight
        if newAlt < (balloon.ground_elev or 0) + 1000 or not balloon.ground_elev:
            # Use cached elevation lookup with rounded coordinates (reduces redundant calculations)
            ground_elev = self._cached_elev(round(newLat, 4), round(newLon, 4))
        else:
            ground_elev = balloon.ground_elev  # Reuse existing elevation (high altitude)
        
        # Update balloon state with new position
        balloon.update(
            location=newLoc,
            ground_elev=ground_elev,
            wind_vector=wind_vector,
            time=newTime,
            alt=newAlt,
        )
        return balloon.history[-1]
    
    def lin_to_angular_velocities(self, lat, lon, u, v):
        """
        Convert linear velocities (m/s) to angular velocities (deg/s).
        
        Accounts for Earth's curvature: longitude velocity depends on latitude
        (lines of longitude get closer together near poles). Uses cosine of latitude
        to correct for this effect.
        
        Formula:
            dlat/dt = v / R (degrees per second)
            dlon/dt = u / (R * cos(lat)) (degrees per second)
        """
        dlat = math.degrees(v / EARTH_RADIUS)
        dlon = math.degrees(u / (EARTH_RADIUS * math.cos(math.radians(lat))))
        return dlat, dlon

    def simulate(self, balloon, step_size, coefficient, elevation, target_alt=None, dur=None):
        """
        Run full trajectory simulation until target altitude or duration reached.
        
        Simulates balloon flight using RK2 integrator, stopping when:
        - Target altitude reached (if target_alt specified)
        - Duration elapsed (if dur specified)
        - Ground is hit (if elevation=True and descending)
        
        CRITICAL: When descending with elevation=True, dynamically adjusts step size
        to stop exactly at ground level (prevents balloon from going underground).
        
        Args:
            balloon: Balloon object (state is updated during simulation)
            step_size: Base time step in seconds (may be reduced near ground/end_time)
            coefficient: Floating coefficient (scales horizontal wind effect)
            elevation: If True, check ground elevation and stop when hitting ground
            target_alt: Target altitude in meters (simulation stops when reached)
            dur: Duration in hours (simulation stops after this time)
        """
        if step_size < 0:
            raise Exception("step size cannot be negative")
        
        # Validate arguments: must specify either target_alt OR dur, not both
        if (target_alt and dur != None) or not (target_alt or dur != None):
            raise Exception("Trajectory simulation must either have a max altitude or specified duration, not both")
        
        step_history = Trajectory([balloon.history[-1]])
        
        # Calculate duration from target altitude if not specified
        if dur == None:
            # Duration = (target_alt - current_alt) / ascent_rate / 3600 (convert to hours)
            dur = ((target_alt - balloon.alt) / balloon.ascent_rate) / 3600
        
        # Handle zero-duration case (already at target)
        if dur == 0:
            step_history.append(self.step(balloon, 0, coefficient))
            return step_history
        
        end_time = balloon.time + timedelta(hours=dur)
        
        # Main simulation loop: step until end_time or ground is hit
        while (end_time - balloon.time).total_seconds() > 1:
            # Calculate step size (may be reduced if hitting end_time or ground)
            current_step_size = step_size
            
            # Reduce step size if next step would exceed end_time
            # This ensures we stop exactly at end_time, not overshoot
            if balloon.time + timedelta(seconds=step_size) >= end_time:
                current_step_size = (end_time - balloon.time).seconds
            
            # GROUND COLLISION DETECTION: If descending, check if we'll hit ground
            # This prevents balloon from going underground (physically impossible)
            if elevation and balloon.ascent_rate < 0:  # Descending (negative ascent_rate)
                current_ground_elev = self.elev_file.elev(*balloon.location)
                # Estimate altitude after full step (simple linear estimate)
                # More accurate than RK2 for this check (faster, sufficient accuracy)
                estimated_alt_after = balloon.alt + balloon.ascent_rate * current_step_size
                
                # If we would go below ground, calculate exact time to reach ground
                if estimated_alt_after < current_ground_elev:
                    # Calculate time to reach ground: distance / descent_rate
                    # descent_rate is negative, so we need absolute value
                    time_to_ground = (balloon.alt - current_ground_elev) / abs(balloon.ascent_rate)
                    # Use smaller of: time to ground or remaining step time
                    # This ensures we stop exactly at ground, not overshoot
                    current_step_size = min(time_to_ground, current_step_size)
                    # Minimum step size to avoid numerical issues (very small steps cause errors)
                    if current_step_size < 0.1:
                        current_step_size = 0.1
            
            # Perform one RK2 integration step
            newRecord = self.step(balloon, current_step_size, coefficient)
            step_history.append(newRecord)
            
            # GROUND COLLISION CHECK: Break if balloon hits the ground
            # Check after step to catch any overshoot (RK2 might slightly overshoot)
            if elevation and balloon.alt <= self.elev_file.elev(*balloon.location):
                break
        
        return step_history

