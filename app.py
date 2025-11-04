from flask import Flask, jsonify, request, Response, render_template, send_from_directory, make_response
from flask_cors import CORS
from flask_compress import Compress
import threading
from functools import wraps
import random

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
    Run all available ensemble models with Monte Carlo analysis.
    Returns both the 21 main ensemble paths AND Monte Carlo landing positions for heatmap.
    Respects DOWNLOAD_CONTROL and NUM_PERTURBED_MEMBERS.
    """
    app.logger.info("Ensemble run with Monte Carlo started: /sim/spaceshot endpoint called")
    import time
    start_time = time.time()
    args = request.args
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    base_lat, base_lon = float(args['lat']), float(args['lon'])
    base_alt = float(args['alt'])
    base_equil = float(args['equil'])
    base_eqtime = float(args['eqtime'])
    base_asc, base_desc = float(args['asc']), float(args['desc'])
    
    # Optional: number of perturbations (default 20)
    num_perturbations = int(args.get('num_perturbations', 20))
    
    # Enable ensemble mode (expanded cache) for longer duration to accommodate Monte Carlo
    simulate.set_ensemble_mode(duration_seconds=120)
    app.logger.info("Ensemble mode enabled: expanded cache for 120 seconds (ensemble + Monte Carlo)")
    
    # Build model list based on configuration
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    
    app.logger.info(f"Ensemble run: Processing {len(model_ids)} models + Monte Carlo ({num_perturbations} perturbations × {len(model_ids)} models)")
    
    # Generate Monte Carlo perturbations
    perturbations = []
    for i in range(num_perturbations):
        pert_lat = base_lat + random.uniform(-0.1, 0.1)  # ±0.1° ≈ ±11km
        pert_lon = (base_lon + random.uniform(-0.1, 0.1)) % 360  # Wrap longitude
        pert_alt = max(0, base_alt + random.uniform(-50, 50))  # ±50m, min 0
        pert_equil = max(pert_alt, base_equil + random.uniform(-200, 200))  # ±200m, must be >= alt
        pert_eqtime = max(0, base_eqtime * random.uniform(0.9, 1.1))  # ±10%, min 0
        pert_asc = max(0.1, base_asc + random.uniform(-0.1, 0.1))  # ±0.1 m/s, min 0.1
        pert_desc = max(0.1, base_desc + random.uniform(-0.1, 0.1))  # ±0.1 m/s, min 0.1
        
        perturbations.append({
            'perturbation_id': i,
            'lat': pert_lat,
            'lon': pert_lon,
            'alt': pert_alt,
            'equil': pert_equil,
            'eqtime': pert_eqtime,
            'asc': pert_asc,
            'desc': pert_desc
        })
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    paths = [None] * len(model_ids)  # Pre-allocate to preserve order
    landing_positions = []  # Monte Carlo landing positions
    
    def run_ensemble_simulation(model):
        """Run standard ensemble simulation"""
        try:
            return singlezpb(timestamp, base_lat, base_lon, base_alt, base_equil, base_eqtime, base_asc, base_desc, model)
        except FileNotFoundError as e:
            app.logger.warning(f"Model {model} file not found: {e}")
            return "error"
        except Exception as e:
            app.logger.exception(f"Model {model} failed with error: {e}")
            return "error"
    
    def run_montecarlo_simulation(pert, model):
        """Run a single Monte Carlo simulation and return landing position"""
        try:
            result = singlezpb(timestamp, pert['lat'], pert['lon'], pert['alt'], 
                              pert['equil'], pert['eqtime'], pert['asc'], pert['desc'], model)
            
            if result == "error" or result == "alt error":
                return None
            
            rise, coast, fall = result
            if len(fall) > 0:
                __, final_lat, final_lon, __, __, __, __, __ = fall[-1]
                return {
                    'lat': float(final_lat),
                    'lon': float(final_lon),
                    'perturbation_id': pert['perturbation_id'],
                    'model_id': model
                }
            return None
        except Exception as e:
            app.logger.warning(f"Monte Carlo simulation failed: pert={pert['perturbation_id']}, model={model}, error={e}")
            return None
    
    try:
        # Run both ensemble and Monte Carlo in parallel
        with ThreadPoolExecutor(max_workers=32) as executor:
            # Submit ensemble tasks (21 models)
            ensemble_futures = {
                executor.submit(run_ensemble_simulation, model): model
                for model in model_ids
            }
            
            # Submit Monte Carlo tasks (420 simulations)
            montecarlo_futures = []
            for pert in perturbations:
                for model in model_ids:
                    montecarlo_futures.append(executor.submit(run_montecarlo_simulation, pert, model))
            
            # Collect ensemble results
            ensemble_completed = 0
            for future in as_completed(ensemble_futures):
                model = ensemble_futures[future]
                try:
                    idx = model_ids.index(model)
                    paths[idx] = future.result()
                    ensemble_completed += 1
                    app.logger.info(f"Ensemble model {model} completed ({ensemble_completed}/{len(model_ids)})")
                except Exception as e:
                    app.logger.exception(f"Ensemble model {model} result processing failed: {e}")
                    idx = model_ids.index(model)
                    paths[idx] = "error"
            
            # Collect Monte Carlo results
            montecarlo_completed = 0
            for future in as_completed(montecarlo_futures):
                result = future.result()
                if result is not None:
                    landing_positions.append(result)
                montecarlo_completed += 1
                if montecarlo_completed % 100 == 0:
                    app.logger.info(f"Monte Carlo progress: {montecarlo_completed}/{len(montecarlo_futures)} simulations completed")
            
            # Ensure all ensemble models have results
            for i, path in enumerate(paths):
                if path is None:
                    app.logger.warning(f"Model {model_ids[i]} did not complete (timeout or missing)")
                    paths[i] = "error"
        
        # Log summary
        ensemble_success = sum(1 for p in paths if p != "error" and p is not None)
        elapsed = time.time() - start_time
        app.logger.info(f"Ensemble + Monte Carlo complete: {ensemble_success}/{len(model_ids)} ensemble paths, {len(landing_positions)} Monte Carlo positions in {elapsed:.1f} seconds")
        
    except Exception as e:
        app.logger.exception(f"Ensemble + Monte Carlo run failed with unexpected error: {e}")
        paths = ["error"] * len(model_ids)
        landing_positions = []
    finally:
        simulate._trim_cache_to_normal()
    
    # Return both paths and heatmap data
    return jsonify({
        'paths': paths,  # Original 21 ensemble paths for line plotting
        'heatmap_data': landing_positions  # Monte Carlo landing positions for heatmap
    })

@app.route('/sim/montecarlo')
@cache_for(600)  # Cache for 10 minutes
def montecarlo():
    """
    Monte Carlo simulation: Run 20 perturbations of input parameters across all 21 ensemble models.
    Returns final landing positions for heatmap visualization.
    
    Total simulations: 20 perturbations × 21 models = 420 simulations
    
    Performance: ~5-15 minutes (vs ~30-60 seconds for normal ensemble)
    - 20× more simulations than normal ensemble (420 vs 21)
    - Uses same 32-worker parallelization
    - Gunicorn timeout is 900s (15 minutes) to safely accommodate Monte Carlo runs
    
    Perturbation ranges (realistic for high-altitude balloon uncertainty):
    - lat/lon: ±0.1° (~11km) - launch position uncertainty
    - alt: ±50m - launch altitude uncertainty
    - equil: ±200m - equilibrium altitude variation
    - eqtime: ±10% - equilibrium time variation
    - asc: ±0.1 m/s - ascent rate uncertainty
    - desc: ±0.1 m/s - descent rate uncertainty
    """
    app.logger.info("Monte Carlo simulation started: /sim/montecarlo endpoint called")
    import time
    start_time = time.time()
    args = request.args
    
    # Base parameters
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    base_lat, base_lon = float(args['lat']), float(args['lon'])
    base_alt = float(args['alt'])
    base_equil = float(args['equil'])
    base_eqtime = float(args['eqtime'])
    base_asc, base_desc = float(args['asc']), float(args['desc'])
    
    # Optional: number of perturbations (default 20)
    num_perturbations = int(args.get('num_perturbations', 20))
    
    # Enable ensemble mode (expanded cache) for longer duration
    # Monte Carlo takes longer, so extend ensemble mode
    simulate.set_ensemble_mode(duration_seconds=120)
    app.logger.info(f"Ensemble mode enabled: expanded cache for 120 seconds (Monte Carlo run)")
    
    # Build model list based on configuration
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    
    app.logger.info(f"Monte Carlo: {num_perturbations} perturbations × {len(model_ids)} models = {num_perturbations * len(model_ids)} total simulations")
    
    # Generate perturbations
    perturbations = []
    for i in range(num_perturbations):
        # Perturbation ranges (realistic for high-altitude balloon simulation)
        pert_lat = base_lat + random.uniform(-0.1, 0.1)  # ±0.1° ≈ ±11km
        pert_lon = (base_lon + random.uniform(-0.1, 0.1)) % 360  # Wrap longitude
        pert_alt = max(0, base_alt + random.uniform(-50, 50))  # ±50m, min 0
        pert_equil = max(pert_alt, base_equil + random.uniform(-200, 200))  # ±200m, must be >= alt
        pert_eqtime = max(0, base_eqtime * random.uniform(0.9, 1.1))  # ±10%, min 0
        pert_asc = max(0.1, base_asc + random.uniform(-0.1, 0.1))  # ±0.1 m/s, min 0.1
        pert_desc = max(0.1, base_desc + random.uniform(-0.1, 0.1))  # ±0.1 m/s, min 0.1
        
        perturbations.append({
            'perturbation_id': i,
            'lat': pert_lat,
            'lon': pert_lon,
            'alt': pert_alt,
            'equil': pert_equil,
            'eqtime': pert_eqtime,
            'asc': pert_asc,
            'desc': pert_desc
        })
    
    # Parallel execution: Run all perturbations × all models
    from concurrent.futures import ThreadPoolExecutor, as_completed
    landing_positions = []  # List of {lat, lon, perturbation_id, model_id}
    
    def run_montecarlo_simulation(pert, model):
        """Run a single Monte Carlo simulation and return landing position"""
        try:
            result = singlezpb(timestamp, pert['lat'], pert['lon'], pert['alt'], 
                              pert['equil'], pert['eqtime'], pert['asc'], pert['desc'], model)
            
            # Extract final landing position from fall trajectory
            if result == "error" or result == "alt error":
                return None
            
            rise, coast, fall = result
            if len(fall) > 0:
                # Extract final lat/lon from fall trajectory (last point)
                __, final_lat, final_lon, __, __, __, __, __ = fall[-1]
                return {
                    'lat': float(final_lat),
                    'lon': float(final_lon),
                    'perturbation_id': pert['perturbation_id'],
                    'model_id': model,
                    'success': True
                }
            else:
                return None
        except Exception as e:
            app.logger.warning(f"Monte Carlo simulation failed: pert={pert['perturbation_id']}, model={model}, error={e}")
            return None
    
    try:
        # Submit all tasks (420 total: 20 perturbations × 21 models)
        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = []
            for pert in perturbations:
                for model in model_ids:
                    future = executor.submit(run_montecarlo_simulation, pert, model)
                    futures.append(future)
            
            # Collect results as they complete
            completed_count = 0
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    landing_positions.append(result)
                completed_count += 1
                if completed_count % 50 == 0:
                    app.logger.info(f"Monte Carlo progress: {completed_count}/{len(futures)} simulations completed")
        
        # Summary
        elapsed = time.time() - start_time
        success_count = len(landing_positions)
        total_expected = num_perturbations * len(model_ids)
        app.logger.info(f"Monte Carlo complete: {success_count}/{total_expected} successful in {elapsed:.1f} seconds")
        
        return jsonify({
            'landing_positions': landing_positions,
            'summary': {
                'total_simulations': total_expected,
                'successful': success_count,
                'failed': total_expected - success_count,
                'duration_seconds': round(elapsed, 2),
                'num_perturbations': num_perturbations,
                'num_models': len(model_ids)
            }
        })
        
    except Exception as e:
        app.logger.exception(f"Monte Carlo simulation failed: {e}")
        return make_response(jsonify({"error": str(e)}), 500)
    finally:
        # Trim cache back to normal size after Monte Carlo run completes
        simulate._trim_cache_to_normal()

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
