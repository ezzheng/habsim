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
### Filecache is in the form (timestamp, modelnumber). ###
filecache = []

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
    global filecache
    filecache = []
    for i in range(1, 3): # TODO: change 3 back to 21
        filecache.append(Simulator(WindFile(load_gefs(f'{currgefs}_{str(i).zfill(2)}.npz')), load_gefs('worldelev.npy')))


def lin_to_angular_velocities(lat, lon, u, v): 
    dlat = math.degrees(v / EARTH_RADIUS)
    dlon = math.degrees(u / (EARTH_RADIUS * math.cos(math.radians(lat))))
    return dlat, dlon

def simulate(simtime, lat, lon, rate, step, max_duration, alt, model, coefficient=1, elevation=True):
    balloon = Balloon(location=(lat, lon), alt=alt, time=simtime, ascent_rate=rate)
    traj = filecache[model-1].simulate(balloon, step, coefficient, elevation, dur=max_duration)
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
