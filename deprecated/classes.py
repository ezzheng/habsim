from windfile import WindFile
import math
import numpy
EARTH_RADIUS = float(6.371e6)
from datetime import timedelta, datetime

class Balloon:

    def __init__(self, lat, lon, alt, time, ascent_rate=0, air_vector=(0,0)):
        self.lat = lat
        self.lon = lon
        self.alt = alt
        self.time = time
        self.ascent_rate = ascent_rate
        self.air_vector = np.array(air_vector)
        self.history = []

    def set_airvector(u, v):
        self.air_vector = np.array([u, v])

    # bearing of the airvector
    def set_bearing(self, bearing, airspeed: float):
        self.ascent_rate = ascent_rate
        NotImplemented 

#testing
wf = WindFile("2021012806_01.npz")
print(wf.get(30, 120, 50, 1612143049))

class Simulator:
    def __init__(self, wind_file):
        self.wind_file = wind_file
    def step(self, balloon, step_size: float):
        windvector = self.wind_file.get(balloon.lat, balloon.lon, balloon.alt, balloon.time)
        distance_moved = (windvector + balloon.air_vector) * step_size
        balloon.alt = balloon.ascent_rate * step_size
        balloon.time += timedelta(seconds=step_size)
        dlat, dlon = self.lin_to_angular_velocities(balloon.lat, balloon.lon, *distance_moved) 
        balloon.lat += dlat
        balloon.lon += dlon
        balloon.history.append((balloon.lat, balloon.lon))
        return balloon.lat, balloon.lon
		

    def lin_to_angular_velocities(self, lat, lon, u, v): 
        dlat = math.degrees(v / EARTH_RADIUS)
        dlon = math.degrees(u / (EARTH_RADIUS * math.cos(math.radians(lat))))
        return dlat, dlon

    def simulate(self, balloon, step_size, target_alt=None, dur=None): 
        if step_size < 0:
            raise Exception("step size cannot be negative")
        if (target_alt and dur) or not (target_alt or dur):
            raise Exception("Trajectory simulation must either have a max altitude or specified duration, not both")
        step_history = [(balloon.lat, balloon.lon)]
        if not dur:
            dur = ((target_alt - balloon.alt) / balloon.ascent_rate) / 3600
        end_time = balloon.time + timedelta(hours=dur)
        while balloon.time < end_time:
            if balloon.time + timedelta(seconds=step_size) >= end_time:
                step_size = (end_time - balloon.time).seconds
            newLocation = step(balloon, step_size)
            total_airtime += step_size
            step_history.append(newLoaction)
        return step_history

#testing output code blow this point
balloon = Balloon(0, 30, 40, datetime.utcfromtimestamp(1612143049))
simulate = Simulator(wf)
for i in range(1000):
    simulate.step(balloon, 1)
print(balloon.history)



