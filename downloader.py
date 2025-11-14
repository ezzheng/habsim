"""
GEFS data downloader from NOAA NOMADS.

Downloads GRIB2 files, converts to NumPy arrays, and combines into NPZ format.
Supports control run (gec00) and perturbed ensemble members (gep01-gep20).
Can be run directly or imported for programmatic use.
"""
import urllib.request
import time
import logging
import socket
import os
import argparse
import shutil
import glob
from datetime import datetime, timedelta

socket.setdefaulttimeout(10)

levels = [1, 2, 3, 5, 7, 20, 30, 70, 150, 350, 450, 550, 600, 650, 750, 800, 900, 950, 975]
NUM_PERTURBED_MEMBERS = 20
DOWNLOAD_CONTROL = True
MAX_HOURS = 384
FORECAST_INTERVAL = 6
TIMEOUT = timedelta(hours=12)

args = None
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("timestamp", 
            help='Model timestamp in the format "yyyymmddhh"')
    parser.add_argument("--logfile", default=None, 
            help="Target path for logs; prints to stdout by default.")
    parser.add_argument("--savedir", default="/Applications/Emmanuel Zheng/habsim/data/gefs", 
            help="Destination directory for intermediate and final files")
    args = parser.parse_args()
    
    logging.basicConfig(
            filename=args.logfile,
            level=logging.DEBUG,
            format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s', 
            datefmt='%Y-%m-%d %H:%M:%S'
    )

def _get_numpy():
    """Lazy import numpy."""
    import numpy as np
    return np

def _get_pygrib():
    """Lazy import pygrib."""
    import pygrib
    return pygrib

def get_model_ids():
    """Get list of available model IDs based on configuration."""
    model_ids = []
    if DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, NUM_PERTURBED_MEMBERS + 1))
    return model_ids

def main():
    if args is None:
        raise ValueError("downloader.py must be run directly, not imported")
    model_timestamp = datetime.strptime(args.timestamp, "%Y%m%d%H")
    try:
        complete_run(model_timestamp)
    except Exception as e:
        logger.exception(f"Uncaught exception {e}")
        exit(1)

def complete_run(model_timestamp, timestamp_str=None, savedir=None):
    """Download all GEFS models for a given timestamp."""
    if timestamp_str is None:
        if args is None:
            raise ValueError("timestamp_str must be provided when args is not available")
        timestamp_str = args.timestamp
    if savedir is None:
        savedir = args.savedir if args else "./gefs"
    
    logger.info(f'Starting run {timestamp_str}')
    y, m, d, h = model_timestamp.year, model_timestamp.month, model_timestamp.day, model_timestamp.hour
    
    temp_dir = f'{savedir}/temp'
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.mkdir(temp_dir)

    for t in range(0, FORECAST_INTERVAL + MAX_HOURS, FORECAST_INTERVAL):
        success = True
        for model_id in get_model_ids():
            try:
                single_run(y, m, d, h, t, model_id, is_control=(model_id == 0), savedir=savedir)
            except Exception as e:
                logger.warning(f'Failed to download model {model_id} at +{t}h: {e}')
                success = False
        
        if success:
            logger.info(f'Successfully completed {timestamp_str}+{t}')
        else:
            logger.warning(f'Partially completed {timestamp_str}+{t} (some members failed)')

    combine_files(timestamp_str, savedir)
    shutil.rmtree(temp_dir)
    logger.info(f'Downloader finished run {timestamp_str}')

def single_run(y,m,d,h,t,n,is_control=False,savedir=None):
    np = _get_numpy()  # Lazy import
    if savedir is None:
        savedir = args.savedir if args else "./gefs"
    savename = get_savename(y,m,d,h,t,n)
    
    if os.path.exists(f"{savedir}/{savename}.npy"): 
        logger.debug("{} exists; skipping.".format(savename))
        return

    url = get_url(y,m,d,h,t,n,is_control)
    logger.debug("Downloading {}".format(savename))

    download(url, f"{savedir}/temp/{savename}.grb2")
    logger.debug("Unpacking {}".format(savename))
    data = grb2_to_array(f"{savedir}/temp/{savename}")
    data = np.float16(data)
    np.save(f"{savedir}/temp/{savename}.npy", data)
    os.remove(f"{savedir}/temp/{savename}.grb2")

def download(url, path, timeout=None):
    if timeout is None:
        timeout = TIMEOUT
    RETRY_INTERVAL = 10
    start_time = datetime.now()
    while datetime.now() - start_time < timeout:
        try:
            urllib.request.urlretrieve(url, path); return
        except Exception as e:
            logger.debug(f'{e} --- retrying in {RETRY_INTERVAL} seconds.')
        time.sleep(RETRY_INTERVAL)
    logger.warning(f"Download timed out on {url}.")
    raise TimeoutError(f"Download timed out on {url}")

def get_savename(y, m, d, h, t, n):
    """Generate filename: {base}_{forecastHour}_{modelId}"""
    base_string = datetime(y, m, d, h).strftime("%Y%m%d%H")
    return f"{base_string}_{str(t).zfill(3)}_{str(n).zfill(2)}"

def get_url(y, m, d, h, t, n, is_control=False):
    """Generate NOAA NOMADS URL for GEFS file."""
    m, d, h = map(lambda x: str(x).zfill(2), [m, d, h])
    model_prefix = 'gec' if is_control else 'gep'
    model_num = '00' if is_control else str(n).zfill(2)
    t_str = str(t).zfill(3)
    return f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/{model_prefix}{model_num}.t{h}z.pgrb2b.0p50.f{t_str}"
    
def grb2_to_array(filename):
    """Convert GRIB2 file to numpy array. Format: [u,v][Pressure][Lat][Lon]"""
    np = _get_numpy()
    pygrib = _get_pygrib()
    grbs = pygrib.open(filename + ".grb2")
    dataset = np.zeros((2, len(levels), 181, 360))
    u = grbs.select(shortName='u', typeOfLevel='isobaricInhPa')
    v = grbs.select(shortName='v', typeOfLevel='isobaricInhPa')
    grbs.close()
    
    assert len(u) == len(levels)
    for i, level in enumerate(levels):
        assert u[i]['level'] == level
        assert v[i]['level'] == level
        dataset[0][i] = u[i].data()[0][::2, ::2]
        dataset[1][i] = v[i].data()[0][::2, ::2]
    return dataset

def combine_files(timestamp_str=None, savedir=None):
    np = _get_numpy()  # Lazy import
    if timestamp_str is None:
        timestamp_str = args.timestamp if args else None
    if savedir is None:
        savedir = args.savedir if args else "./gefs"
    if timestamp_str is None:
        raise ValueError("timestamp_str must be provided")
    
    model_ids = get_model_ids()
    filesets = []
    for i in model_ids:
        files = sorted(glob.glob(f'{savedir}/temp/{timestamp_str}_*_{str(i).zfill(2)}.npy'))
        filesets.append(files)

    for i, files in enumerate(filesets):
        data = combine_npy_for_member(files)
        savename = f"{timestamp_str}_{str(model_ids[i]).zfill(2)}.npz"
        dt = datetime.strptime(timestamp_str, "%Y%m%d%H")
        timestamp = (dt - datetime(1970, 1, 1)).total_seconds()
        
        np.savez(f'{savedir}/{savename}', data=data, timestamp=timestamp, 
                 interval=FORECAST_INTERVAL * 3600, levels=levels)
        logger.info(f'Combined file for member {model_ids[i]} saved as {savename}')

    logger.info('Completed combining files')

def combine_npy_for_member(file_list):
    """Combine multiple .npy files into single array. Reshape from (2, 19, 181, 360) to (181, 360, 19, 65, 2)."""
    np = _get_numpy()
    data = np.stack([np.load(f) for f in file_list])
    data = np.transpose(data, (3, 4, 2, 0, 1))
    data = np.append(data, data[:, 0:1], axis=1)
    return data

if __name__ == "__main__":
    main()
