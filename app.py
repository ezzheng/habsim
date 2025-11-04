from flask import Flask, jsonify, request, Response, render_template, send_from_directory, make_response
from flask_cors import CORS
from flask_compress import Compress
import threading
from functools import wraps

app = Flask(__name__)
CORS(app)
Compress(app)  # Automatically compress responses (10x size reduction)

# Cache decorator for GET requests
def cache_for(seconds=300):
    """Add HTTP cache headers to responses"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            response = make_response(f(*args, **kwargs))
            response.headers['Cache-Control'] = f'public, max-age={seconds}'
            return response
        return decorated_function
    return decorator

import logging
import elev
from datetime import datetime, timezone
from gefs import listdir_gefs, open_gefs

# Import simulate at module level to avoid circular import issues
import simulate
import downloader  # Import to access model configuration

# File pre-warming removed to reduce Supabase egress costs
# Files will download on-demand when needed (still fast due to file cache)

def _prewarm_cache():
    """Pre-load only model 0 for fast single requests (cost-optimized)"""
    try:
        import time
        time.sleep(2)  # Give the app a moment to fully initialize
        
        # Get configured model IDs
        model_ids = []
        if downloader.DOWNLOAD_CONTROL:
            model_ids.append(0)
        model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
        
        app.logger.info(f"Pre-warming cache: configured models {model_ids}...")
        
        # Pre-warm only model 0 for fast single requests (cost-optimized)
        # Ensemble runs will build simulators on-demand from file cache (files pre-downloaded to disk)
        models_to_prewarm = model_ids[:1]  # Pre-warm only model 0 (fast path for single requests)
        app.logger.info(f"Pre-warming {len(models_to_prewarm)} model(s) for fast single requests: {models_to_prewarm}")
        
        for model_id in models_to_prewarm:
            try:
                simulate._get_simulator(model_id)
                app.logger.info(f"Model {model_id} pre-warmed")
                time.sleep(0.5)
            except Exception as e:
                app.logger.info(f"Failed to pre-warm model {model_id} (non-critical, will retry on-demand): {e}")
        
        app.logger.info(f"Cache pre-warming complete! Model 0 ready. Ensemble runs will build simulators from file cache on-demand.")
        
        # Pre-warm elevation memmap used by /elev endpoint
        try:
            _ = elev.getElevation(0, 0)
            app.logger.info("Elevation pre-warmed successfully")
        except Exception as ee:
            app.logger.info(f"Elevation pre-warm failed (non-critical, will retry on-demand): {ee}")
        
        app.logger.info(f"Cache pre-warming complete! All {len(model_ids)} models pre-warmed")
    except Exception as e:
        app.logger.info(f"Cache pre-warming failed (non-critical, will retry on-demand): {e}")

# Start pre-warming in background thread
_cache_warmer_thread = threading.Thread(target=_prewarm_cache, daemon=True)
_cache_warmer_thread.start()

@app.route('/sim/which')
def whichgefs():
    # Read directly from storage to avoid importing heavy modules on cold start
    f = open_gefs('whichgefs')
    s = f.readline()
    f.close()
    return s

@app.route('/sim/status')
def status():
    try:
        f = open_gefs('whichgefs')
        _ = f.readline()
        f.close()
        return "Ready"
    except Exception as e:
        app.logger.info(f"Status check failed (non-critical): {e}")
        return "Unavailable"

@app.route('/sim/models')
def models():
    """Return available model IDs based on configuration"""
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    return jsonify({
        "models": model_ids,
        "download_control": downloader.DOWNLOAD_CONTROL,
        "num_perturbed": downloader.NUM_PERTURBED_MEMBERS
    })

@app.route('/sim/ls')
def ls():
    files = listdir_gefs()
    return jsonify({
        "count": len(files),
        "files": files
    })


'''
Returns a json object representing the flight path, given a UTC launch time (yr, mo, day, hr, mn),
a location (lat, lon), a launch elevation (alt), a drift coefficient (coeff),
a maximum duration in hrs (dur), a step interval in seconds (step), and a GEFS model number (model)


Return format is a list of [loc1, loc2 ...] where each loc is a list [lat, lon, altitude, u-wind, v-wind]

u-wind is wind towards the EAST: wind vector in the positive X direction
v-wind is wind towards the NORTH: wind vector in the positve Y direction
'''
@app.route('/sim/singlepredicth')
@cache_for(600)  # Cache for 10 minutes
def singlepredicth():
    args = request.args
    yr, mo, day, hr, mn = int(args['yr']), int(args['mo']), int(args['day']), int(args['hr']), int(args['mn'])
    lat, lon = float(args['lat']), float(args['lon'])
    rate, dur, step = float(args['rate']), float(args['dur']), float(args['step'])
    model = int(args['model'])
    coeff = float(args['coeff'])
    alt = float(args['alt'])
    #simulate.refresh()
    try:
        path = simulate.simulate(datetime(yr, mo, day, hr, mn).replace(tzinfo=timezone.utc), lat, lon, rate, step, dur, alt, model, coefficient=coeff)
    except:
        return jsonify("error")
    return jsonify(path)

@app.route('/sim/singlepredict')
@cache_for(600)  # Cache for 10 minutes
def singlepredict():
    args = request.args
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    lat, lon = float(args['lat']), float(args['lon'])
    rate, dur, step = float(args['rate']), float(args['dur']), float(args['step'])
    model = int(args['model'])
    coeff = float(args['coeff'])
    alt = float(args['alt'])
    #simulate.refresh()

    try:
        path = simulate.simulate(timestamp, lat, lon, rate, step, dur, alt, model, coefficient=coeff)
    except:
        return jsonify("error")
    return jsonify(path)


def singlezpb(timestamp, lat, lon, alt, equil, eqtime, asc, desc, model):
    try:
        # Note: refresh() is now called by _get_simulator() with 5-minute throttle
        dur = 0 if equil == alt else (equil - alt) / asc / 3600
        rise = simulate.simulate(timestamp, lat, lon, asc, 240, dur, alt, model, elevation=False)
        if len(rise) > 0:
            timestamp, lat, lon, alt, __, __, __, __= rise[-1]
            timestamp = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
        coast = simulate.simulate(timestamp, lat, lon, 0, 240, eqtime, alt, model)
        if len(coast) > 0:
            timestamp, lat, lon, alt, __, __, __, __ = coast[-1]
            timestamp = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
        dur = (alt) / desc / 3600
        fall = simulate.simulate(timestamp, lat, lon, -desc, 240, dur, alt, model)
        return (rise, coast, fall)
    except FileNotFoundError as e:
        # File not found in Supabase - model may not exist yet
        app.logger.warning(f"Model file not found: {e}")
        raise  # Re-raise to be handled by route handler
    except Exception as e:
        app.logger.exception("singlezpb failed")
        if str(e) == "alt out of range":
            return "alt error"
        return "error"


@app.route('/sim/singlezpb')
@cache_for(600)  # Cache for 10 minutes
def singlezpbh():
    args = request.args
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    lat, lon = float(args['lat']), float(args['lon'])
    alt = float(args['alt'])
    equil = float(args['equil'])
    eqtime = float(args['eqtime'])
    asc, desc = float(args['asc']), float(args['desc'])
    model = int(args['model'])
    try:
        path = singlezpb(timestamp, lat, lon, alt, equil, eqtime, asc, desc, model)
        return jsonify(path)
    except FileNotFoundError as e:
        # Model file not found in Supabase
        app.logger.warning(f"Model file not found for request: {e}")
        return make_response(jsonify({"error": "Model file not available. The requested model may not have been uploaded yet. Please check if the model timestamp is correct."}), 404)


@app.route('/sim/spaceshot')
@cache_for(600)  # Cache for 10 minutes
def spaceshot():
    """
    Run all available ensemble models (respects DOWNLOAD_CONTROL and NUM_PERTURBED_MEMBERS).
    Note: This endpoint is designed for the full ensemble spread, so it uses all configured models.
    Enables ensemble mode (expanded cache) for 10 minutes to speed up ensemble runs.
    """
    app.logger.info("Ensemble run started: /sim/spaceshot endpoint called")
    import time
    start_time = time.time()
    args = request.args
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    lat, lon = float(args['lat']), float(args['lon'])
    alt = float(args['alt'])
    equil = float(args['equil'])
    eqtime = float(args['eqtime'])
    asc, desc = float(args['asc']), float(args['desc'])
    
    # Enable ensemble mode (expanded cache) for 10 minutes
    # This allows all 21 models to be cached during ensemble runs
    # If another ensemble run happens within 10 min, it extends the duration
    simulate.set_ensemble_mode(duration_seconds=600)
    app.logger.info("Ensemble mode enabled: expanded cache for 10 minutes (extends with each ensemble run)")
    
    # Build model list based on configuration
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    
    app.logger.info(f"Ensemble run: Processing {len(model_ids)} models: {model_ids}")
    
    # Parallel execution for faster ensemble runs
    # max_workers=16 for maximum parallelism (we have 32 CPUs, use them!)
    # Since we're paying for 32 CPUs, might as well use them for speed
    from concurrent.futures import ThreadPoolExecutor, as_completed
    paths = [None] * len(model_ids)  # Pre-allocate to preserve order
    
    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            # Submit all tasks
            future_to_model = {
                executor.submit(singlezpb, timestamp, lat, lon, alt, equil, eqtime, asc, desc, model): model
                for model in model_ids
            }
            
            # Collect results as they complete
            completed_count = 0
            for future in as_completed(future_to_model):
                model = future_to_model[future]
                try:
                    idx = model_ids.index(model)
                    paths[idx] = future.result()
                    completed_count += 1
                    app.logger.info(f"Model {model} completed ({completed_count}/{len(model_ids)})")
                except FileNotFoundError as e:
                    # Model file not found in Supabase
                    app.logger.warning(f"Model {model} file not found: {e}")
                    idx = model_ids.index(model)
                    paths[idx] = "error"
                    completed_count += 1
                except Exception as e:
                    app.logger.exception(f"Model {model} failed with error: {e}")
                    idx = model_ids.index(model)
                    paths[idx] = "error"
                    completed_count += 1
            
            # Log summary
            success_count = sum(1 for p in paths if p != "error" and p is not None)
            error_count = sum(1 for p in paths if p == "error")
            app.logger.info(f"Ensemble run summary: {success_count} successful, {error_count} failed out of {len(model_ids)} models")
            
            # Ensure all models have results (even if errors)
            for i, path in enumerate(paths):
                if path is None:
                    app.logger.warning(f"Model {model_ids[i]} did not complete (timeout or missing)")
                    paths[i] = "error"
    except Exception as e:
        app.logger.exception(f"Ensemble run failed with unexpected error: {e}")
        # Return error for all models if ensemble completely fails
        paths = ["error"] * len(model_ids)
    finally:
        # Trim cache back to normal size after ensemble run completes
        # This happens automatically via _trim_cache_to_normal() but we can trigger it
        simulate._trim_cache_to_normal()
        elapsed = time.time() - start_time
        app.logger.info(f"Ensemble run complete in {elapsed:.1f} seconds, cache will trim to normal size 10 minutes after last ensemble run")
    
    return jsonify(paths)

'''
Given a lat and lon, returns the elevation as a string
'''
@app.route('/sim/elev')
def elevation():
    lat, lon = float(request.args['lat']), float(request.args['lon'])
    return str(elev.getElevation(lat, lon))


'''
Given a time (yr, mo, day, hr, mn), a location (lat, lon), and an altitude (alt)
returns a json object of [u-wind, v-wind, du/dh, dv/dh], where

u-wind = [u-wind-1, u-wind-2, u-wind-3...u-wind-20]
v-wind = [v-wind-1, v-wind-2, v-wind-3...v-wind-20]
du/dh = [du/dh-1, du/dh-2, du/dh-3...du/dh-20]
dv/dh = [dv/dh-1, dv/dh-2, dv/dh-3...dv/dh-20]

where the numbers are the GEFS model from which the data is extracted.
'''
@app.route('/sim/windensemble')
def windensemble():
    args = request.args
    lat, lon = float(args['lat']), float(args['lon'])
    alt = float(args['alt'])
    yr, mo, day, hr, mn = int(args['yr']), int(args['mo']), int(args['day']), int(args['hr']), int(args['mn'])
    time = datetime(yr, mo, day, hr, mn).replace(tzinfo=timezone.utc)
    uList = list()
    vList = list()
    duList = list()
    dvList = list()

    levels = simulate.GFSHIST if yr < 2019 else simulate.GEFS

    for i in range(1, 21):
        u, v, du, dv = simulate.get_wind(time,lat,lon,alt, i, levels)
        uList.append(u)
        vList.append(v)
        duList.append(du)
        dvList.append(dv)
    
    return jsonify([uList, vList, duList, dvList])

'''
Given a time (yr, mo, day, hr, mn), a location (lat, lon), an altitude (alt),
and a model (model) returns a json object of u-wind, v-wind, du/dh, dv/dh for that location
extracted from that model.
'''
@app.route('/sim/wind')
def wind():
    args = request.args
    lat, lon = float(args['lat']), float(args['lon'])
    model = int(args['model'])
    alt = float(args['alt'])
    yr, mo, day, hr, mn = int(args['yr']), int(args['mo']), int(args['day']), int(args['hr']), int(args['mn'])
    levels = simulate.GFSHIST if yr < 2019 else simulate.GEFS
    time = datetime(yr, mo, day, hr, mn).replace(tzinfo=timezone.utc)
    u, v, du, dv = simulate.get_wind(time,lat,lon,alt, model, levels)
    return jsonify([u, v, du, dv])
