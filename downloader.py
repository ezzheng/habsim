# Lightweight imports (always available)
import urllib.request
import time, logging, socket, sys, os, argparse, shutil, glob
from datetime import datetime, timedelta
socket.setdefaulttimeout(10)

# Configuration constants (available when module is imported)
levels = [1, 2, 3, 5, 7, 20, 30, 70, 150, 350, 450, 550, 600, 650, 750, 800, 900, 950, 975]
NUM_PERTURBED_MEMBERS = 2  # Number of perturbed ensemble members (gep01, gep02, etc.)
DOWNLOAD_CONTROL = True     # Whether to download control run (gec00)
MAX_HOURS = 384
FORECAST_INTERVAL = 6
TIMEOUT = timedelta(hours=12)
start = datetime.now()

# Argument parsing only when run directly (not when imported)
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

# Lazy imports for heavy dependencies (only loaded when functions are called)
def _get_numpy():
    """Lazy import numpy - only loaded when actually needed"""
    import numpy as np
    return np

def _get_pygrib():
    """Lazy import pygrib - only loaded when actually needed"""
    import pygrib
    return pygrib

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
    # Allow parameters to be passed when called programmatically
    if timestamp_str is None:
        if args is None:
            raise ValueError("timestamp_str must be provided when args is not available")
        timestamp_str = args.timestamp
    if savedir is None:
        savedir = args.savedir if args else "./gefs"
    logger.info(f'Starting run {timestamp_str}')
    y, m = model_timestamp.year, model_timestamp.month
    d, h = model_timestamp.day, model_timestamp.hour
    
    if os.path.exists(f'{savedir}/temp'):
        shutil.rmtree(f'{savedir}/temp')
        
    os.mkdir(f'{savedir}/temp')

    for t in range(0, FORECAST_INTERVAL+MAX_HOURS, FORECAST_INTERVAL):
        success = True
        # Download control run (member 0)
        if DOWNLOAD_CONTROL:
            try:
                single_run(y, m, d, h, t, 0, is_control=True, savedir=savedir)
            except Exception as e:
                logger.warning(f'Failed to download control run at +{t}h: {e}')
                success = False
        # Download perturbed ensemble members
        for n in range(1, 1+NUM_PERTURBED_MEMBERS):
            try:
                single_run(y, m, d, h, t, n, is_control=False, savedir=savedir)
            except Exception as e:
                logger.warning(f'Failed to download member {n} at +{t}h: {e}')
                success = False
        
        if success:
            logger.info(f'Successfully completed {timestamp_str}+{t}')
        else:
            logger.warning(f'Partially completed {timestamp_str}+{t} (some members failed, continuing...)')

    combine_files(timestamp_str, savedir)
    shutil.rmtree(f'{savedir}/temp')
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

def get_savename(y,m,d,h,t,n):
    base = datetime(y, m, d, h)
    base_string = base.strftime("%Y%m%d%H")
    # Intermediate filename format: {base}_{forecastHour}_{modelId}
    # Example: 2025110312_000_00.npy (no repeated date string)
    savename = base_string + "_" + str(t).zfill(3) + "_" + str(n).zfill(2)
    return savename

def get_url(y,m,d,h,t,n,is_control=False):
    m, d, h = map(lambda x: str(x).zfill(2), [m, d, h])
    n_str = str(n).zfill(2)
    t = str(t).zfill(3)
    
    # Control run uses 'gec00', perturbed members use 'gep01', 'gep02', etc.
    model_prefix = 'gec' if is_control else 'gep'
    model_num = '00' if is_control else n_str
    
    url = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gens/prod/gefs.{y}{m}{d}/{h}/atmos/pgrb2bp5/{model_prefix}{model_num}.t{h}z.pgrb2b.0p50.f{t}"
    return url
    
def grb2_to_array(filename): 
    np = _get_numpy()  # Lazy import
    pygrib = _get_pygrib()  # Lazy import
    ## Array format: array[u,v][Pressure][Lat][Lon] ##
    ## Currently [lat 90 to -90][lon 0 to 359]
    grbs = pygrib.open(filename + ".grb2")
    dataset = np.zeros((2, len(levels), 181, 360)) # CHANGE: (181, 360, 19, 65, 2), need to add timestamp
    u = grbs.select(shortName='u', typeOfLevel='isobaricInhPa') # gets the wind data array, which comes in 181x360
    v = grbs.select(shortName='v', typeOfLevel='isobaricInhPa')
    grbs.close()
    
    assert(len(u) == len(levels))
    
    for i, level in enumerate(levels):
        assert(u[i]['level'] == level)
        assert(v[i]['level'] == level)
        dataset[0][i] = u[i].data()[0][::2, ::2] # Takes the second element of every second array in data
        dataset[1][i] = v[i].data()[0][::2, ::2]
    return dataset

## save data as npz file of ['data', 'timestamp (unix)', 'interval', 'levels']
def combine_files(timestamp_str=None, savedir=None):
    np = _get_numpy()  # Lazy import
    if timestamp_str is None:
        timestamp_str = args.timestamp if args else None
    if savedir is None:
        savedir = args.savedir if args else "./gefs"
    if timestamp_str is None:
        raise ValueError("timestamp_str must be provided")
    
    filesets = []
    
    # Collect all model files (control + perturbed members)
    model_ids = []
    if DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, NUM_PERTURBED_MEMBERS+1))
    
    for i in model_ids:
        files = glob.glob(f'{savedir}/temp/{timestamp_str}_*_{str(i).zfill(2)}.npy')
        files.sort()
        filesets.append(files)

    for i in range(len(filesets)):
        data = combine_npy_for_member(filesets[i])
        
        # Use actual model ID (0, 1, 2) not index+1
        savename = timestamp_str + "_" + str(model_ids[i]).zfill(2) + ".npz"
        dt = datetime.strptime(timestamp_str, "%Y%m%d%H")
        timestamp = (dt - datetime(1970, 1, 1)).total_seconds()
        
        np.savez(f'{savedir}/' + savename, data=data, timestamp=timestamp, interval=FORECAST_INTERVAL*3600, levels=levels)
        logger.info(f'Combined file for member {i+1} saved as {savename}')

    logger.info('Completed combining files')

## change shape of data from (2, 19, 181, 360) to (181, 360, 19, 65, 2), with the 65 timestamps added
def combine_npy_for_member(file_list):
    np = _get_numpy()  # Lazy import
    data = np.stack(list(map(np.load, file_list)))
    data = np.transpose(data, (3, 4, 2, 0, 1))
    data = np.append(data, data[:, 0:1], axis=1)
    return data

if __name__ == "__main__":
    main()
