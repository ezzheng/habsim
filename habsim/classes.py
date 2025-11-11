import datetime
#from . import util
import math
import random
import bisect
import numpy as np
from windfile import WindFile
from datetime import timedelta, datetime
EARTH_RADIUS = float(6.371e6)
import pdb

class Trajectory(list):
    # superclass of list
    def __init__(self, data=list()):
        super().__init__(data)
        self.data = data

    def duration(self):
        '''
        Returns duration in hours, assuming the first field of each tuple is a UNIX timestamp.
        '''
        # these are datetime objects, call .seconds()
        # rolls over with days
        return (self.data[len(self.data) - 1].time - self.data[0].time).total_seconds() / 3600

    def length(self):
        '''
        Distance travelled by trajectory in km.
        '''
        res = 0
        for i, j in zip(self[:-1], self[1:]):
            res += i.location.distance(j.location)
        return res

    def interpolate(self, time):
        # find where it is between locations
        # return location and altitude
        pass

class Record:
    def __init__(self, time=None, location=None, alt=None, ascent_rate=None, air_vector=None, wind_vector=None, ground_elev=None):
        self.time = time
        self.location = location
        self.alt = alt
        # naming
        self.ascent_rate = ascent_rate
        self.air_vector = air_vector
        self.wind_vector = wind_vector
        #added 3/23
        self.ground_elev = ground_elev

class Location(tuple): # subclass of tuple, override __iter__
    # unpack lat and lon as two arguments when passed into a function
    EARTH_RADIUS = 6371.0

    # super class
    def __new__(self, lat, lon):
        return tuple.__new__(Location, (lat, lon))

    def getLon(self):
        return self[1]

    def getLat(self):
        return self[0]

    def distance(self, other):
        # change to indices
        return self.haversine(self[0], self.lon, other.lat, other.lon)

    def haversine(self, lat1, lon1, lat2, lon2):
        '''
        Returns great circle distance between two points.
        '''
        # what will happen if distance called between invalid point (lat out of bounds)
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

        dlat = lat2-lat1
        dlon = lon2-lon1

        a = math.sin(dlat/2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        return EARTH_RADIUS * c

class ElevationFile:
    # res may not be 120
    resolution = 120 ## points per degree

    def __init__(self, path): # store
        # Use memory-mapped read-only mode to avoid loading 430MB into RAM
        # This allows OS to manage page cache instead of Python holding full array
        self.data = np.load(path, mmap_mode='r')
        self.resolution = 120

    def elev(self, lat, lon): # return elevation
        # Normalize longitude to [-180, 180] range
        lon = ((lon + 180) % 360) - 180
        
        # Clamp latitude to valid range
        lat = max(-90, min(90, lat))
        
        # Convert to array indices
        x = int(round((lon + 180) * self.resolution))
        y = int(round((90 - lat) * self.resolution)) - 1
        
        # Clamp indices to valid array bounds
        shape = self.data.shape
        x = max(0, min(x, shape[1] - 1))
        y = max(0, min(y, shape[0] - 1))
        
        return max(0, self.data[y, x])

class Balloon:
    def __init__(self, time=None, location=None, alt=0, ascent_rate=0, air_vector=(0,0), wind_vector=None, ground_elev=None):
        record = Record(time=time, location=Location(*location), alt=alt, ascent_rate=ascent_rate, air_vector=np.array(air_vector) if air_vector is not None else None, wind_vector=np.array(wind_vector) if wind_vector is not None else None, ground_elev=ground_elev)
        self.history = Trajectory([record])
    
    #def set_airvector(u, v):
       # self.air_vector = np.array([u, v])

    # bearing of the airvector
   # def set_bearing(self, bearing, airspeed: float):
        #self.ascent_rate = ascent_rate
        # airspeed * sin(bearing), airspeed *cos(bearing) (make 0 degrees be the north pole)

    def update(self, time=None, location=None, alt=0, ascent_rate=0, air_vector=(0,0), wind_vector=None, ground_elev=None):
        record = Record(time=time or self.time, 
                        location=Location(*location) or self.location, 
                        alt=alt or self.alt, 
                        ascent_rate=ascent_rate or self.ascent_rate, 
                        air_vector=np.array(air_vector) if air_vector is not None else self.air_vector,
                        wind_vector=np.array(wind_vector) if wind_vector is not None else self.wind_vector, 
                        ground_elev=ground_elev or self.ground_elev)
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

    def step(self, balloon, step_size: float, coefficient):
        # Preserve initial ground constraint
        if not balloon.ground_elev:
            balloon.ground_elev = self.elev_file.elev(*balloon.location)
            balloon.alt = max(balloon.alt, balloon.ground_elev)
        # if wind_vector is not set, get it from the wind_file
        if balloon.wind_vector is None:
            temp = self.wind_file.get(*balloon.location, balloon.alt, balloon.time)
            balloon.wind_vector = temp

        # Runge–Kutta 2nd order (Midpoint) integrator for (lat, lon, alt)
        # State at start of step
        lat0 = balloon.location.getLat()
        lon0 = balloon.location.getLon()
        alt0 = balloon.alt
        t0 = balloon.time
        asc = balloon.ascent_rate  # m/s (can be negative for descent)
        h = float(step_size)

        def sample_rates(lat, lon, alt, t):
            # Defensive check: wind_file might be None if simulator was cleaned up
            if self.wind_file is None:
                raise RuntimeError("Simulator wind_file is None - simulator was cleaned up during use")
            # wind_file.get returns [u, v, du/dh, dv/dh, ...]; use u, v and include air_vector
            temp = self.wind_file.get(lat, lon, alt, t)
            u = float(temp[0])
            v = float(temp[1])
            if balloon.air_vector is not None:
                u += float(balloon.air_vector[0])
                v += float(balloon.air_vector[1])
            # Convert linear m/s to angular deg/s at this latitude
            dlat_dt, dlon_dt = self.lin_to_angular_velocities(lat, lon, u, v)
            # Apply coefficient to horizontal (FLOAT scaling)
            dlat_dt *= coefficient
            dlon_dt *= coefficient
            # Altitude changes linearly with ascent_rate
            dalt_dt = asc
            return dlat_dt, dlon_dt, dalt_dt

        # k1 at start
        k1_lat, k1_lon, k1_alt = sample_rates(lat0, lon0, alt0, t0)

        # Midpoint state using k1
        lat_mid = lat0 + 0.5 * h * k1_lat
        lon_mid = lon0 + 0.5 * h * k1_lon
        alt_mid = alt0 + 0.5 * h * k1_alt
        t_mid = t0 + timedelta(seconds=0.5 * h)

        # k2 at midpoint
        k2_lat, k2_lon, k2_alt = sample_rates(lat_mid, lon_mid, alt_mid, t_mid)

        # Advance using k2
        newLat = lat0 + h * k2_lat
        newLon = lon0 + h * k2_lon
        newAlt = alt0 + h * k2_alt
        newTime = t0 + timedelta(seconds=h)
        newLoc = (newLat, newLon)

        # Update record at end of step
        balloon.update(
            location=newLoc,
            ground_elev=self.elev_file.elev(*newLoc),
            wind_vector=self.wind_file.get(*newLoc, newAlt, newTime),
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
            if balloon.time + timedelta(seconds=step_size) >= end_time:
                step_size = (end_time - balloon.time).seconds
            newRecord = self.step(balloon, step_size, coefficient)

            #total_airtime += step_size
            step_history.append(newRecord)
            
            # break if balloon hits the ground (last record will be below ground)
            if elevation and balloon.alt < self.elev_file.elev(*balloon.location):
                break
        return step_history

#testing output code below this point
#balloon = Balloon(0, 30, 40, datetime.utcfromtimestamp(1612143049))
#simulate = Simulator(wf)
#for i in range(1000):
#    simulate.step(balloon, 1)
#print(balloon.history)
