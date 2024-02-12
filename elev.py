import numpy as np
from gefs import load_gefs

def getElevation(lat, lon):
    data = np.load(load_gefs('worldelev.npy'))
    resolution = 120 ## points per degree

    x = int(round((lon + 180) * resolution))
    y = int(round((90 - lat) * resolution)) - 1
    try: return max(0, data[y, x])
    except: return 0