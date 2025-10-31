import numpy as np 
import math
import elev
from datetime import datetime, timedelta, timezone
import math
import bisect
import time
from windfile import WindFile
from habsim import Simulator, Balloon
from gefs import open_gefs, load_gefs

# Note: .replace(tzinfo=utc) is needed because we need to call .timestamp

EARTH_RADIUS = float(6.371e6)
DATA_STEP = 6 # hrs

### Cache of datacubes and files. ###
### filecache maps model number -> Simulator instance ###
filecache = {}
elevation_cache = None

currgefs = "Unavailable"

def refresh():
    global currgefs
    f = open_gefs('whichgefs')
    s = f.readline()
    f.close()
    if s != currgefs:
        currgefs = s
        reset()
        return True
    return False

# opens and stores 20 Simulators in filecache
def reset():
    global filecache, elevation_cache
    filecache = {}
    elevation_cache = None


def _get_elevation_data():
    global elevation_cache
    if elevation_cache is None:
        elevation_cache = load_gefs('worldelev.npy')
    return elevation_cache


def _get_simulator(model):
    if model not in filecache:
        wind_file = WindFile(load_gefs(f'{currgefs}_{str(model).zfill(2)}.npz'))
        filecache[model] = Simulator(wind_file, _get_elevation_data())
    return filecache[model]


def lin_to_angular_velocities(lat, lon, u, v): 
    dlat = math.degrees(v / EARTH_RADIUS)
    dlon = math.degrees(u / (EARTH_RADIUS * math.cos(math.radians(lat))))
    return dlat, dlon

def simulate(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient=1, elevation=True):
    simulator = _get_simulator(model)
    balloon = Balloon(location=(lat, lon), alt=alt, time=simtime, ascent_rate=rate)
    traj = simulator.simulate(balloon, step, coefficient, elevation, dur=max_duration)
    path = list()
    for i in traj:
        if i.wind_vector is None:
            raise Exception("alt out of range")
        path.append(((i.time - datetime(1970, 1, 1).replace(tzinfo=timezone.utc)).total_seconds(), i.location.getLat(), i.location.getLon(), i.alt, i.wind_vector[0], i.wind_vector[1], 0, 0))
    return path


            
#def simulate(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient=1, elevation=True):
#    end = simtime + timedelta(hours=max_duration)
#    path = list()
#
#    while True:
#        u, v = filecache[model-1].get(lat, lon, alt, simtime)
#        path.append((simtime.timestamp(), lat, lon, alt, u, v, 0, 0))
#        if simtime >= end or (elevation and elev.getElevation(lat, lon) > alt):
#            break
#        dlat, dlon = lin_to_angular_velocities(lat, lon, u, v)
#        alt = alt + step * rate
#        lat = lat + dlat * step * coefficient
#        lon = lon + dlon * step * coefficient
#        simtime = simtime + timedelta(seconds = step)
#    
#    return path
