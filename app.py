from flask import Flask, jsonify, request, Response, render_template, send_from_directory, make_response, session, redirect, url_for
from flask_cors import CORS
from flask_compress import Compress
import threading
from functools import wraps
import random
import time
import os
import secrets

app = Flask(__name__)
# Configure session for authentication (frontend only)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
# Don't set SESSION_COOKIE_MAX_AGE - this makes it a session cookie that expires when browser closes
CORS(app)
Compress(app)  # Automatically compress responses (10x size reduction)

# Password for authentication
LOGIN_PASSWORD = os.environ.get('HABSIM_PASSWORD')

# Log password status at startup (without revealing the actual password)
if LOGIN_PASSWORD:
    # Use a simple print since app.logger might not be ready yet
    print(f"[AUTH] HABSIM_PASSWORD is set (length: {len(LOGIN_PASSWORD)})")
else:
    print("[AUTH] WARNING: HABSIM_PASSWORD environment variable is NOT set - login will fail!")

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


def _is_authenticated():
    """Check if user is authenticated"""
    return session.get('authenticated', False)

@app.before_request
def _record_worker_activity():
    """Mark the worker as active so idle cleanup waits until the user is gone.
    Excludes status/health endpoints that poll continuously."""
    # Don't reset idle timer for status/health endpoints that poll frequently
    excluded_paths = ['/sim/status', '/sim/models', '/sim/cache-status', '/', '/favicon.ico']
    path = request.path
    # Also exclude static file requests (CSS, JS, images) and Railway health checks
    if (path.startswith('/static/') or 
        path.endswith(('.css', '.js', '.png', '.jpg', '.ico')) or
        path == '/health' or  # Common health check path
        request.headers.get('User-Agent', '').startswith('Railway')):  # Railway health checks
        return
    if path not in excluded_paths:
        try:
            # Get idle time BEFORE recording (more accurate)
            idle_before = 0
            if hasattr(simulate, '_last_activity_timestamp'):
                idle_before = time.time() - simulate._last_activity_timestamp
            simulate.record_activity()
            # Log which endpoint triggered activity (helps debug)
            if idle_before > 30:  # Only log if was idle for meaningful time
                app.logger.info(f"Activity recorded from {path} (was idle for {idle_before:.1f}s)")
        except Exception:
            # Non-critical; if simulate isn't ready yet we just skip recording.
            pass

# Progress tracking for ensemble + Monte Carlo simulations
# Key: request_id (hash of parameters), Value: {completed: int, total: int, ensemble_completed: int, ensemble_total: int}
_progress_tracking = {}
_progress_lock = threading.Lock()

# File pre-warming removed to reduce S3 egress costs
# Files will download on-demand when needed (still fast due to file cache)

def _start_cache_trim_thread():
    """Start cache trim thread early in each worker to ensure memory management is active"""
    try:
        import simulate
        # Access _get_simulator to trigger thread start
        # This ensures the background trimming thread is running even before first request
        simulate._start_cache_trim_thread()
        app.logger.info("Cache trim thread startup triggered")
    except Exception as e:
        app.logger.warning(f"Failed to start cache trim thread (non-critical): {e}")

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
        
        # Pre-download worldelev.npy file before elevation lookup
        # This ensures the 451MB file is cached before users click on the map
        try:
            import gefs
            app.logger.info("Pre-downloading worldelev.npy (451MB) to avoid on-demand download failures...")
            worldelev_path = gefs.load_gefs('worldelev.npy')
            app.logger.info(f"worldelev.npy pre-downloaded successfully: {worldelev_path}")
        except Exception as e:
            app.logger.warning(f"Failed to pre-download worldelev.npy (non-critical, will download on-demand): {e}")
        
        # Pre-warm elevation memmap used by /elev endpoint
        # This loads the file into memory-mapped mode
        try:
            _ = elev.getElevation(0, 0)
            app.logger.info("Elevation pre-warmed successfully")
        except Exception as ee:
            app.logger.info(f"Elevation pre-warm failed (non-critical, will retry on-demand): {ee}")
        
        app.logger.info(f"Cache pre-warming complete! All {len(model_ids)} models pre-warmed")
    except Exception as e:
        app.logger.info(f"Cache pre-warming failed (non-critical, will retry on-demand): {e}")

# Start cache trim thread early (ensures memory management is active from startup)
# Only do this on Railway where the Flask app actually runs
# Vercel just proxies requests, so skip initialization there
is_railway = os.environ.get('RAILWAY_ENVIRONMENT') is not None or os.environ.get('RAILWAY_SERVICE_NAME') is not None
if is_railway:
    # This is critical - the cleanup thread must start even if no simulators are accessed
    # The thread in simulate.py also starts at module import, but we ensure it here as well
    try:
        _start_cache_trim_thread()
    except Exception as e:
        app.logger.warning(f"Failed to start cache trim thread from app startup (non-critical): {e}")

# Start pre-warming in background thread
    _cache_warmer_thread = threading.Thread(target=_prewarm_cache, daemon=True)
    _cache_warmer_thread.start()
else:
    # On Vercel or local dev - skip heavy initialization
    app.logger.info("Skipping Railway-specific initialization (Vercel/local dev)")

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and authentication handler"""
    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        expected_password = (LOGIN_PASSWORD or '').strip()
        
        # Debug logging - detailed comparison
        app.logger.info(f"=== LOGIN ATTEMPT ===")
        app.logger.info(f"Expected password length: {len(expected_password)}")
        app.logger.info(f"Received password length: {len(password)}")
        app.logger.info(f"Expected password set: {bool(expected_password)}")
        app.logger.info(f"Expected password repr: {repr(expected_password[:5])}...{repr(expected_password[-5:]) if len(expected_password) > 10 else ''}")
        app.logger.info(f"Received password repr: {repr(password[:5])}...{repr(password[-5:]) if len(password) > 10 else ''}")
        app.logger.info(f"Passwords match: {password == expected_password}")
        
        if not expected_password:
            app.logger.error("HABSIM_PASSWORD environment variable is not set!")
            return redirect('/login?error=1')
        
        # Compare passwords (case-sensitive, exact match)
        if password == expected_password:
            session['authenticated'] = True
            session.permanent = False  # Explicitly set to non-permanent (expires when browser closes)
            app.logger.info("✓ Login successful")
            # Session is NOT permanent - expires when browser closes (requires login every time)
            # Redirect to next page or home
            next_page = request.args.get('next', '/')
            return redirect(next_page)
        else:
            # Wrong password - redirect back to login with error
            app.logger.warning(f"✗ Login failed - password mismatch")
            # Log detailed comparison
            if expected_password and password:
                app.logger.warning(f"Expected: length={len(expected_password)}, first_char='{expected_password[0]}', last_char='{expected_password[-1]}'")
                app.logger.warning(f"Received: length={len(password)}, first_char='{password[0]}', last_char='{password[-1]}'")
                # Check for common issues
                if expected_password.lower() == password.lower():
                    app.logger.warning("⚠ Case sensitivity issue detected!")
                if expected_password.replace(' ', '') == password.replace(' ', ''):
                    app.logger.warning("⚠ Whitespace issue detected!")
            return redirect('/login?error=1')
    
    # GET request - serve login page
    login_html_path = os.path.join(os.path.dirname(__file__), 'www', 'login.html')
    try:
        with open(login_html_path, 'r', encoding='utf-8') as f:
            response = make_response(f.read())
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            return response
    except FileNotFoundError:
        return "Login page not found", 404

@app.route('/logout')
def logout():
    """Logout handler - clears both server session and client sessionStorage"""
    session.pop('authenticated', None)
    # Redirect with query param so client-side can clear sessionStorage
    return redirect('/login?logout=1')

@app.route('/')
def index():
    """Serve main application page (requires authentication)"""
    # Authentication is checked client-side via sessionStorage (expires on tab close)
    # Backend doesn't need to check - sessionStorage handles it
    index_html_path = os.path.join(os.path.dirname(__file__), 'www', 'index.html')
    try:
        with open(index_html_path, 'r', encoding='utf-8') as f:
            response = make_response(f.read())
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            return response
    except FileNotFoundError:
        return "Application not found", 404

@app.route('/sim/which')
def whichgefs():
    # Read directly from storage to avoid importing heavy modules on cold start
    f = open_gefs('whichgefs')
    s = f.readline()
    f.close()
    return s

@app.route('/sim/cache-status')
def cache_status():
    """Debug endpoint to see what's in the simulator cache and memory usage"""
    import simulate
    import os
    import time
    from pathlib import Path
    import gefs
    
    # Get cache info
    with simulate._cache_lock:
        cache_size = len(simulate._simulator_cache)
        cache_limit = simulate._current_max_cache
        ensemble_active_by_timestamp = simulate._is_ensemble_mode()
        ensemble_until = simulate._ensemble_mode_until
        ensemble_started = simulate._ensemble_mode_started
        cached_models = list(simulate._simulator_cache.keys())
        # Ensemble mode is active if timestamp says so OR if cache limit is expanded to ensemble size
        # (cache limit stays expanded until trim happens, even if timestamp expired)
        ensemble_active = ensemble_active_by_timestamp or (cache_limit >= simulate.MAX_SIMULATOR_CACHE_ENSEMBLE)
    
    now = time.time()
    
    # Check persistent volume usage
    cache_dir = getattr(gefs, '_CACHE_DIR', None)
    persistent_volume_mounted = Path("/app/data").exists()
    cache_dir_path = str(cache_dir) if cache_dir else "unknown"
    cache_dir_exists = cache_dir.exists() if cache_dir else False
    
    # Count cached files on disk
    disk_cache_files = 0
    disk_cache_size_mb = 0
    if cache_dir and cache_dir.exists():
        try:
            for file in cache_dir.glob("*.npz"):
                disk_cache_files += 1
                disk_cache_size_mb += file.stat().st_size / (1024 * 1024)
            for file in cache_dir.glob("*.npy"):
                disk_cache_files += 1
                disk_cache_size_mb += file.stat().st_size / (1024 * 1024)
        except Exception as e:
            pass
    
    # Check idle status
    idle_duration = now - simulate._last_activity_timestamp
    # Handle case where cleanup hasn't run yet (_last_idle_cleanup is 0)
    last_idle_cleanup = getattr(simulate, '_last_idle_cleanup', 0)
    last_cleanup = now - last_idle_cleanup if last_idle_cleanup > 0 else 0
    
    # Determine why cache might be empty or why ensemble mode might not show as active
    cache_status_note = None
    if cache_size == 0:
        if ensemble_active:
            cache_status_note = 'Cache is empty but ensemble mode is active (simulators may be building)'
        elif idle_duration > 300:
            cache_status_note = f'Cache is empty - worker has been idle for {round(idle_duration)}s (may have been cleaned up)'
        else:
            cache_status_note = 'Cache is empty - this worker may not have handled any requests yet'
    elif cache_limit == simulate.MAX_SIMULATOR_CACHE_NORMAL and cache_size < cache_limit:
        cache_status_note = 'Cache is below normal limit (normal operation)'
    elif cache_limit >= simulate.MAX_SIMULATOR_CACHE_ENSEMBLE:
        if not ensemble_active_by_timestamp:
            cache_status_note = 'Cache limit is expanded but timestamp expired (trim pending)'
        else:
            cache_status_note = 'Ensemble mode active - cache limit expanded'
    
    status = {
        'worker_pid': os.getpid(),
        'worker_info': {
            'pid': os.getpid(),
            'note': 'Gunicorn uses 4 workers. Each request may hit a different worker. This shows status for THIS worker only.',
            'tip': 'Refresh multiple times to see different workers, or check logs for which worker handled your request'
        },
        'cache': {
            'size': cache_size,
            'limit': cache_limit,
            'normal_limit': simulate.MAX_SIMULATOR_CACHE_NORMAL,
            'ensemble_limit': simulate.MAX_SIMULATOR_CACHE_ENSEMBLE,
            'cached_models': cached_models,
            'status_note': cache_status_note
        },
        'disk_cache': {
            'directory': cache_dir_path,
            'directory_exists': cache_dir_exists,
            'persistent_volume_mounted': persistent_volume_mounted,
            'files_count': disk_cache_files,
            'size_mb': round(disk_cache_size_mb, 2),
            'note': 'Disk cache is shared across all workers (persistent volume)'
        },
        'ensemble_mode': {
            'active': ensemble_active,
            'active_by_timestamp': ensemble_active_by_timestamp,
            'active_by_cache_limit': cache_limit >= simulate.MAX_SIMULATOR_CACHE_ENSEMBLE,
            'started': ensemble_started,
            'expires_at': ensemble_until,
            'seconds_until_expiry': max(0, round(ensemble_until - now, 1)) if ensemble_until > 0 else 0,
            'seconds_since_start': round(now - ensemble_started, 1) if ensemble_started > 0 else 0,
            'seconds_since_expiry': round(now - ensemble_until, 1) if ensemble_until > 0 and now > ensemble_until else None,
            'note': 'Status is per-worker. Ensemble requests may be handled by a different worker (check worker_pid in logs).',
            'diagnostic': f'This worker (PID {os.getpid()}) has ensemble_started={ensemble_started}, ensemble_until={ensemble_until}, cache_limit={cache_limit}'
        },
        'idle_cleanup': {
            'idle_duration_seconds': round(idle_duration, 1),
            'threshold_seconds': simulate._IDLE_RESET_TIMEOUT,
            'seconds_until_cleanup': max(0, round(simulate._IDLE_RESET_TIMEOUT - idle_duration, 1)),
            'last_cleanup_ago_seconds': round(last_cleanup, 1) if last_cleanup > 0 else None,
            'cleanup_has_run': last_idle_cleanup > 0
        },
        'note': 'Check Railway metrics for actual memory usage. This endpoint shows status for ONE worker only.'
    }
    
    return jsonify(status)

@app.route('/sim/status')
def status():
    """Status endpoint - should be fast and non-blocking even during heavy load."""
    try:
        f = open_gefs('whichgefs')
        line = f.readline()
        f.close()
        # If we got a valid line (non-empty), server is ready
        if line and line.strip():
            return "Ready"
        else:
            # Empty response - might be temporary during S3 operations
            # Still return "Ready" to avoid false "Unavailable" during ensemble
            return "Ready"
    except Exception as e:
        # Log but don't fail - status checks shouldn't block
        app.logger.debug(f"Status check failed (non-critical): {e}")
        # During ensemble mode, server might be busy but still functional
        # Return "Ready" to avoid false negatives
        return "Ready"

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


def singlezpb(timestamp, lat, lon, alt, equil, eqtime, asc, desc, model, coefficient=1.0):
    """
    Simulate a zero-pressure balloon (ZPB) flight in three phases: ascent, coast/float, and descent.
    
    Parameters:
    - timestamp: Launch time (datetime object)
    - lat, lon: Launch location coordinates
    - alt: Launch altitude (meters)
    - equil: Burst/equilibrium altitude (meters) - balloon reaches this altitude and floats
    - eqtime: Equilibrium time (hours) - how long balloon floats at burst altitude before descent
    - asc: Ascent rate (m/s) - vertical velocity during ascent phase
    - desc: Descent rate (m/s) - vertical velocity during descent phase (positive value, will be negated)
    - model: GEFS weather model number (0-20)
    - coefficient: Floating coefficient (default 1.0) - scales horizontal wind effect
    
    Returns:
    - Tuple of (rise, coast, fall) - three trajectory arrays for each flight phase
    """
    try:
        # Note: refresh() is now called by _get_simulator() with 5-minute throttle
        
        # ========================================================================
        # PHASE 1: ASCENT - From launch altitude to burst altitude
        # ========================================================================
        # Calculate ascent duration: time to climb from launch (alt) to burst (equil)
        # Formula: distance / rate / 3600 (convert seconds to hours)
        #   - (equil - alt): Vertical distance to climb (meters)
        #   - asc: Ascent rate (m/s)
        #   - Divide by 3600 to convert seconds to hours
        # If already at burst altitude, duration is 0
        dur = 0 if equil == alt else (equil - alt) / asc / 3600
        
        # Simulate ascent phase:
        # - timestamp, lat, lon: Starting position and time
        # - asc: Ascent rate (positive, m/s)
        # - 120: Step size (seconds) - simulation time interval
        # - dur: Maximum duration (hours) - stops when burst altitude reached
        # - alt: Starting altitude (meters)
        # - model: Weather model to use
        # - elevation=False: Skip ground elevation checks during ascent (balloon is going up,
        #   so it won't hit ground. This avoids unnecessary elevation lookups for performance)
        # - coefficient: Floating coefficient - scales horizontal wind effect
        rise = simulate.simulate(timestamp, lat, lon, asc, 120, dur, alt, model, coefficient=coefficient, elevation=False)
        
        # Extract final position from ascent phase to use as starting point for coast
        if len(rise) > 0:
            timestamp, lat, lon, alt, __, __, __, __= rise[-1]
            timestamp = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
        
        # ========================================================================
        # PHASE 2: COAST/FLOAT - Balloon floats at burst altitude
        # ========================================================================
        # Simulate coast/floating phase:
        # - timestamp, lat, lon, alt: Final position from ascent (at burst altitude)
        # - 0: Vertical rate (m/s) - zero means floating, no vertical movement
        # - 120: Step size (seconds) - same as ascent
        # - eqtime: Duration (hours) - how long to float at equilibrium
        # - alt: Current altitude (burst altitude, stays constant)
        # - model: Same weather model
        # - elevation=True (default): Use elevation checks (though not needed at high altitude,
        #   this is the default behavior for consistency)
        # - coefficient: Floating coefficient - scales horizontal wind effect
        coast = simulate.simulate(timestamp, lat, lon, 0, 120, eqtime, alt, model, coefficient=coefficient)
        
        # Extract final position from coast phase to use as starting point for descent
        if len(coast) > 0:
            timestamp, lat, lon, alt, __, __, __, __ = coast[-1]
            timestamp = datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
        
        # ========================================================================
        # PHASE 3: DESCENT - From burst altitude to ground
        # ========================================================================
        # Calculate descent duration: time to fall from burst altitude to ground
        # Formula: altitude / descent_rate / 3600 (convert seconds to hours)
        #   - alt: Current altitude (burst altitude in meters)
        #   - desc: Descent rate (m/s, positive value)
        #   - Divide by 3600 to convert seconds to hours
        # Note: This is an estimate assuming ground is at 0m. The actual simulation
        # will stop when elevation=True detects the balloon hits the ground.
        dur = (alt) / desc / 3600
        
        # Simulate descent phase:
        # - timestamp, lat, lon: Final position from coast phase
        # - -desc: Descent rate (negative, m/s) - negative because descending
        # - 120: Step size (seconds) - same as other phases
        # - dur: Maximum duration (hours) - estimate, actual stop is when ground is hit
        # - alt: Starting altitude (burst altitude)
        # - model: Same weather model
        # - elevation=True (default): CRITICAL - Check ground elevation and stop simulation
        #   when balloon.alt < ground_elevation. This ensures the balloon stops at the actual
        #   ground level (which may be above sea level) rather than continuing below ground.
        # - coefficient: Floating coefficient - scales horizontal wind effect
        fall = simulate.simulate(timestamp, lat, lon, -desc, 120, dur, alt, model, coefficient=coefficient)
        
        # Return all three trajectory phases
        return (rise, coast, fall)
    except FileNotFoundError as e:
        # File not found in S3 - model may not exist yet
        app.logger.warning(f"Model file not found: {e}")
        raise  # Re-raise to be handled by route handler
    except Exception as e:
        app.logger.error(f"singlezpb failed for model {model}: {str(e)}", exc_info=True)
        if str(e) == "alt out of range":
            return "alt error"
        return "error"


@app.route('/sim/singlezpb')
@cache_for(600)  # Cache for 10 minutes
def singlezpbh():
    worker_pid = os.getpid()
    print(f"[WORKER {worker_pid}] /sim/singlezpb called", flush=True)
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
        # Model file not found in S3
        app.logger.warning(f"Model file not found for request: {e}")
        return make_response(jsonify({"error": "Model file not available. The requested model may not have been uploaded yet. Please check if the model timestamp is correct."}), 404)


@app.route('/sim/spaceshot')
# NO CACHING - This is a real-time simulation with progress tracking
def spaceshot():
    """
    Run all available ensemble models with Monte Carlo analysis.
    Returns both the 21 main ensemble paths AND Monte Carlo landing positions for heatmap.
    Respects DOWNLOAD_CONTROL and NUM_PERTURBED_MEMBERS.
    
    Weighting: Ensemble landing points are weighted more heavily than Monte Carlo points
    to reflect that weather model uncertainty (different forecast scenarios) is typically
    more significant than parameter uncertainty (measurement/launch variations).
    Default: ensemble_weight = 2.0 (ensemble points count 2× in density calculation)
    """
    import sys
    import os
    worker_pid = os.getpid()
    # CRITICAL: Use print() to stdout so it appears in Railway logs (same as access logs)
    # app.logger goes to stderr which Railway may filter or show separately
    print(f"[WORKER {worker_pid}] ===== SPACESHOT ENDPOINT CALLED ===== (stdout for Railway visibility)", flush=True)
    app.logger.info(f"[WORKER {worker_pid}] ===== SPACESHOT ENDPOINT CALLED ===== (spaceshot endpoint called)")
    sys.stdout.flush()
    sys.stderr.flush()
    app.logger.info(f"[WORKER {worker_pid}] Ensemble run with Monte Carlo started: /sim/spaceshot endpoint called")
    sys.stdout.flush()
    sys.stderr.flush()
    
    # Weighting factor for ensemble points relative to Monte Carlo points
    # Higher values give more weight to weather model uncertainty vs parameter uncertainty
    # Reasonable range: 1.5-3.0 (2.0 = ensemble points count twice as much)
    ENSEMBLE_WEIGHT = 2.0
    import time
    start_time = time.time()
    args = request.args
    timestamp = datetime.utcfromtimestamp(float(args['timestamp'])).replace(tzinfo=timezone.utc)
    base_lat, base_lon = float(args['lat']), float(args['lon'])
    base_alt = float(args['alt'])
    base_equil = float(args['equil'])
    base_eqtime = float(args['eqtime'])
    base_asc, base_desc = float(args['asc']), float(args['desc'])
    # Coefficient defaults to 1.0 if not provided (STANDARD/ZPB modes don't send it)
    base_coeff = float(args.get('coeff', 1.0))
    
    # Optional: number of perturbations (default 20)
    num_perturbations = int(args.get('num_perturbations', 20))
    
    # Generate unique request ID for progress tracking
    # Use simple hash that's easy to replicate on client side
    request_key = f"{args['timestamp']}_{args['lat']}_{args['lon']}_{args['alt']}_{args['equil']}_{args['eqtime']}_{args['asc']}_{args['desc']}_{base_coeff}"
    # Simple hash function (same as client-side)
    hash_val = 0
    for char in request_key:
        hash_val = ((hash_val << 5) - hash_val) + ord(char)
        hash_val = hash_val & 0xFFFFFFFF  # Convert to 32-bit integer
    request_id = format(abs(hash_val), 'x').zfill(16)[:16]
    
    # Enable ensemble mode (expanded cache) for 60 seconds
    # NOTE: This is the ONLY endpoint that extends ensemble mode.
    # This endpoint is only called when user explicitly clicks "Simulate" with ensemble toggle enabled.
    # Single model requests (/sim/singlezpb) do NOT extend ensemble mode.
    # Legacy /sim/montecarlo endpoint also does NOT extend ensemble mode.
    # This prevents memory bloat from non-ensemble requests.
    worker_pid = os.getpid()
    # Use print() to stdout so it appears in Railway logs (same as access logs)
    print(f"[WORKER {worker_pid}] /sim/spaceshot called - activating ensemble mode on THIS worker", flush=True)
    # Also log to app.logger (stderr) for completeness
    app.logger.info(f"[WORKER {worker_pid}] /sim/spaceshot called - activating ensemble mode on THIS worker")
    import sys
    sys.stdout.flush()  # Ensure log appears immediately
    sys.stderr.flush()
    
    # CRITICAL: Activate ensemble mode BEFORE any simulations start
    # This must happen synchronously before ThreadPoolExecutor begins
    # Use longer duration (5 minutes) to ensure ensemble mode doesn't expire during long simulations
    # Ensemble runs can take 5-15 minutes, especially with slow S3 downloads on first run
    simulate.set_ensemble_mode(duration_seconds=300)  # 5 minutes to cover slow downloads + simulations
    
    # Verify ensemble mode was activated (for debugging)
    with simulate._cache_lock:
        cache_limit_after = simulate._current_max_cache
        ensemble_until_after = simulate._ensemble_mode_until
    print(f"[WORKER {worker_pid}] Ensemble mode enabled: cache_limit={cache_limit_after}, expires_at={ensemble_until_after:.1f}", flush=True)
    app.logger.info(f"[WORKER {worker_pid}] Ensemble mode enabled: cache_limit={cache_limit_after}, expires_at={ensemble_until_after:.1f}")
    sys.stdout.flush()  # Ensure log appears immediately
    sys.stderr.flush()
    
    # Build model list based on configuration
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    
    # Initialize progress tracking
    total_ensemble = len(model_ids)
    total_montecarlo = num_perturbations * len(model_ids)
    total_simulations = total_ensemble + total_montecarlo
    
    with _progress_lock:
        _progress_tracking[request_id] = {
            'completed': 0,
            'total': total_simulations,
            'ensemble_completed': 0,
            'ensemble_total': total_ensemble,
            'montecarlo_completed': 0,
            'montecarlo_total': total_montecarlo
        }
    
    print(f"[WORKER {worker_pid}] Ensemble run: Processing {len(model_ids)} models + Monte Carlo ({num_perturbations} perturbations × {len(model_ids)} models), request_id={request_id}", flush=True)
    app.logger.info(f"Ensemble run: Processing {len(model_ids)} models + Monte Carlo ({num_perturbations} perturbations × {len(model_ids)} models), request_id={request_id}")
    
    # ============================================================================
    # MONTE CARLO SIMULATION: Generate Parameter Perturbations
    # ============================================================================
    # Monte Carlo simulation creates multiple variations of input parameters to
    # explore uncertainty in landing position predictions. Each perturbation
    # represents a plausible variation in launch conditions (position, altitude,
    # timing, ascent/descent rates) that could occur in real-world launches.
    #
    # Process:
    # 1. Generate N perturbations (default 20) with random variations
    # 2. Run each perturbation through all ensemble models (21 models)
    # 3. Collect final landing positions: 21 ensemble + 420 Monte Carlo = 441 total
    # 4. Return landing positions for heatmap visualization
    #
    # Perturbation ranges (designed for high-altitude balloon launches):
    # - Latitude/Longitude: ±0.001° (≈ ±111m) - accounts for launch site uncertainty
    # - Altitude: ±50m - launch altitude variation
    # - Equilibrium altitude: ±200m - burst altitude uncertainty
    # - Equilibrium time: ±0.5 hours - timing variation at equilibrium (absolute, works even when base = 0)
    # - Ascent/Descent rate: ±0.5 m/s - rate measurement uncertainty
    # - Floating coefficient: 0.9 to 1.0, weighted towards 0.95-1.0 (70% in [0.95, 1.0], 30% in [0.9, 0.95])
    #
    # Note: Using uniform random distribution for most perturbations. Coefficient uses weighted
    # distribution to favor values closer to 1.0. If landing positions appear circular/concentric,
    # it's likely due to Google Maps heatmap smoothing (applies Gaussian-like aggregation),
    # not the perturbation distribution itself.
    # ============================================================================
    perturbations = []
    # Use random seed based on request parameters for reproducibility while maintaining randomness
    # This ensures perturbations are different each time but consistent within a request
    random.seed(hash(request_key) & 0xFFFFFFFF)
    for i in range(num_perturbations):
        # Generate random perturbations within reasonable bounds
        # Using uniform distribution - each parameter varies independently
        pert_lat = base_lat + random.uniform(-0.001, 0.001)  # ±0.001° ≈ ±111m
        pert_lon = (base_lon + random.uniform(-0.001, 0.001)) % 360  # Wrap longitude to [0, 360)
        pert_alt = max(0, base_alt + random.uniform(-50, 50))  # ±50m, min 0
        pert_equil = max(pert_alt, base_equil + random.uniform(-200, 200))  # ±200m, must be >= alt
        # Use absolute perturbation for eqtime: ±0.5 hours (works even when base_eqtime = 0)
        # For non-zero base_eqtime, this provides ±0.5h variation; for Standard mode (0), adds small coasting time variations
        pert_eqtime = max(0, base_eqtime + random.uniform(-0.5, 0.5))  # ±0.5 hours, min 0
        pert_asc = max(0.1, base_asc + random.uniform(-0.5, 0.5))  # ±0.5 m/s, min 0.1
        pert_desc = max(0.1, base_desc + random.uniform(-0.5, 0.5))  # ±0.5 m/s, min 0.1
        
        # Floating coefficient perturbation: 0.9 to 1.0, weighted towards 0.95-1.0
        # 90% chance: uniform in [0.95, 1.0] (higher values more likely)
        # 10% chance: uniform in [0.9, 0.95] (lower values less likely)
        # Note: Absolute range [0.9, 1.0], not relative to base_coeff
        if random.random() < 0.9:
            pert_coeff = random.uniform(0.95, 1.0)
        else:
            pert_coeff = random.uniform(0.9, 0.95)
        
        perturbations.append({
            'perturbation_id': i,
            'lat': pert_lat,
            'lon': pert_lon,
            'alt': pert_alt,
            'equil': pert_equil,
            'eqtime': pert_eqtime,
            'asc': pert_asc,
            'desc': pert_desc,
            'coeff': pert_coeff
        })
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    paths = [None] * len(model_ids)  # Pre-allocate to preserve order for 21 ensemble paths
    landing_positions = []  # Landing positions: 21 ensemble + 420 Monte Carlo = 441 total
    
    def run_ensemble_simulation(model):
        """Run standard ensemble simulation for one model.
        
        Returns full trajectory path for line plotting on map.
        """
        try:
            return singlezpb(timestamp, base_lat, base_lon, base_alt, base_equil, base_eqtime, base_asc, base_desc, model)
        except FileNotFoundError as e:
            app.logger.warning(f"Model {model} file not found: {e}")
            return "error"
        except Exception as e:
            app.logger.exception(f"Model {model} failed with error: {e}")
            return "error"
    
    def run_montecarlo_simulation(pert, model):
        """Run a single Monte Carlo simulation and extract final landing position.
        
        This function runs a full trajectory simulation with perturbed parameters,
        then extracts only the final landing position (lat, lon) from the descent
        phase. This landing position will be aggregated with all other Monte Carlo
        results to create a probability density heatmap.
        
        Args:
            pert: Dictionary with perturbed parameters (lat, lon, alt, equil, eqtime, asc, desc)
            model: Model ID (0-20) to use for weather data
            
        Returns:
            Dictionary with landing position {'lat': float, 'lon': float, ...} or None if failed
        """
        try:
            # Run full trajectory simulation with perturbed parameters
            result = singlezpb(timestamp, pert['lat'], pert['lon'], pert['alt'], 
                             pert['equil'], pert['eqtime'], pert['asc'], pert['desc'], model,
                             coefficient=pert.get('coeff', 1.0))
            
            if result == "error" or result == "alt error":
                return None
            
            # Extract final landing position from descent phase
            # Result format: (rise, coast, fall) where fall is list of [timestamp, lat, lon, alt, ...]
            rise, coast, fall = result
            if len(fall) > 0:
                # Get last point in descent phase (final landing position)
                __, final_lat, final_lon, __, __, __, __, __ = fall[-1]
                return {
                    'lat': float(final_lat),
                    'lon': float(final_lon),
                    'perturbation_id': pert['perturbation_id'],
                    'model_id': model,
                    'weight': 1.0  # Standard weight for Monte Carlo (parameter uncertainty)
                }
            return None
        except Exception as e:
            app.logger.warning(f"Monte Carlo simulation failed: pert={pert['perturbation_id']}, model={model}, error={e}")
            return None
    
    try:
        # Verify ensemble mode is active before starting simulations
        with simulate._cache_lock:
            verify_cache_limit = simulate._current_max_cache
            verify_ensemble_active = simulate._is_ensemble_mode()
        if verify_cache_limit < simulate.MAX_SIMULATOR_CACHE_ENSEMBLE:
            print(f"[WORKER {worker_pid}] WARNING: Starting simulations but cache_limit={verify_cache_limit} < {simulate.MAX_SIMULATOR_CACHE_ENSEMBLE}! Ensemble mode may not be active!", flush=True)
            app.logger.warning(f"[WORKER {worker_pid}] WARNING: Starting simulations but cache_limit={verify_cache_limit} < {simulate.MAX_SIMULATOR_CACHE_ENSEMBLE}! Ensemble mode may not be active!")
        else:
            print(f"[WORKER {worker_pid}] Verified: cache_limit={verify_cache_limit}, ensemble_active={verify_ensemble_active} - ready to start simulations", flush=True)
            app.logger.info(f"[WORKER {worker_pid}] Verified: cache_limit={verify_cache_limit}, ensemble_active={verify_ensemble_active} - ready to start simulations")
        sys.stdout.flush()
        sys.stderr.flush()
        
        # ========================================================================
        # PARALLEL EXECUTION: Ensemble + Monte Carlo Simulations
        # ========================================================================
        # Run both ensemble paths (for line plotting) and Monte Carlo simulations
        # (for heatmap) in parallel using the same thread pool. This maximizes
        # CPU utilization and minimizes total execution time.
        #
        # Total tasks: 21 ensemble + 420 Monte Carlo = 441 simulations
        # Thread pool: 32 workers
        # ========================================================================
        with ThreadPoolExecutor(max_workers=32) as executor:
            # Submit ALL tasks first (both ensemble and Monte Carlo)
            # This ensures progress tracking starts immediately as tasks complete
            ensemble_futures = {
                executor.submit(run_ensemble_simulation, model): model
                for model in model_ids
            }
            
            # Submit Monte Carlo tasks (420 simulations: 20 perturbations × 21 models)
            # Each task returns only the final landing position (lat, lon)
            montecarlo_futures = []
            for pert in perturbations:
                for model in model_ids:
                    montecarlo_futures.append(executor.submit(run_montecarlo_simulation, pert, model))
            
            # Combine all futures into one list for unified progress tracking
            # This allows us to track progress as ANY simulation completes, not just ensemble or Monte Carlo separately
            all_futures = list(ensemble_futures.keys()) + montecarlo_futures
            total_futures = len(all_futures)
            
            # Track which futures are ensemble vs Monte Carlo
            ensemble_future_set = set(ensemble_futures.keys())
            
            # Collect results as they complete (unified progress tracking)
            ensemble_completed = 0
            montecarlo_completed = 0
            total_completed = 0
            
            for future in as_completed(all_futures):
                total_completed += 1
                
                # Update progress immediately as each task completes
                # This provides real-time progress updates instead of waiting for batches
                with _progress_lock:
                    if request_id in _progress_tracking:
                        _progress_tracking[request_id]['completed'] = total_completed
                
                # Check if this is an ensemble or Monte Carlo future
                if future in ensemble_future_set:
                    # Ensemble simulation
                    model = ensemble_futures[future]
                    try:
                        idx = model_ids.index(model)
                        result = future.result()
                        paths[idx] = result
                        
                        # Extract landing position from ensemble path and add to heatmap data
                        # Result format: (rise, coast, fall) where fall is list of [timestamp, lat, lon, alt, ...]
                        if result != "error" and result is not None:
                            try:
                                rise, coast, fall = result
                                if len(fall) > 0:
                                    # Get last point in descent phase (final landing position)
                                    __, final_lat, final_lon, __, __, __, __, __ = fall[-1]
                                    landing_positions.append({
                                        'lat': float(final_lat),
                                        'lon': float(final_lon),
                                        'perturbation_id': -1,  # -1 indicates ensemble (not Monte Carlo)
                                        'model_id': model,
                                        'weight': ENSEMBLE_WEIGHT  # Higher weight for weather model uncertainty
                                    })
                            except Exception as e:
                                app.logger.warning(f"Failed to extract landing position from ensemble model {model}: {e}")
                        
                        ensemble_completed += 1
                        with _progress_lock:
                            if request_id in _progress_tracking:
                                _progress_tracking[request_id]['ensemble_completed'] = ensemble_completed
                        app.logger.info(f"Ensemble model {model} completed ({ensemble_completed}/{len(model_ids)})")
                    except Exception as e:
                        app.logger.exception(f"Ensemble model {model} result processing failed: {e}")
                        idx = model_ids.index(model)
                        paths[idx] = "error"
                        ensemble_completed += 1
                        with _progress_lock:
                            if request_id in _progress_tracking:
                                _progress_tracking[request_id]['ensemble_completed'] = ensemble_completed
                else:
                    # Monte Carlo simulation
                    try:
                        result = future.result()
                        if result is not None:
                            landing_positions.append(result)
                        montecarlo_completed += 1
                        with _progress_lock:
                            if request_id in _progress_tracking:
                                _progress_tracking[request_id]['montecarlo_completed'] = montecarlo_completed
                        # Log progress every 50 simulations for visibility
                        if montecarlo_completed % 50 == 0:
                            app.logger.info(f"Monte Carlo progress: {montecarlo_completed}/{len(montecarlo_futures)} simulations completed")
                    except Exception as e:
                        app.logger.warning(f"Monte Carlo simulation result processing failed: {e}")
                        montecarlo_completed += 1
                        with _progress_lock:
                            if request_id in _progress_tracking:
                                _progress_tracking[request_id]['montecarlo_completed'] = montecarlo_completed
            
            # Ensure all ensemble models have results
            for i, path in enumerate(paths):
                if path is None:
                    app.logger.warning(f"Model {model_ids[i]} did not complete (timeout or missing)")
                    paths[i] = "error"
        
        # Log summary
        ensemble_success = sum(1 for p in paths if p != "error" and p is not None)
        elapsed = time.time() - start_time
        ensemble_landings = sum(1 for p in landing_positions if p.get('perturbation_id') == -1)
        montecarlo_landings = len(landing_positions) - ensemble_landings
        print(f"[WORKER {worker_pid}] Ensemble + Monte Carlo complete: {ensemble_success}/{len(model_ids)} ensemble paths, {ensemble_landings} ensemble + {montecarlo_landings} Monte Carlo = {len(landing_positions)} total landing positions in {elapsed:.1f} seconds", flush=True)
        app.logger.info(f"Ensemble + Monte Carlo complete: {ensemble_success}/{len(model_ids)} ensemble paths, {ensemble_landings} ensemble + {montecarlo_landings} Monte Carlo = {len(landing_positions)} total landing positions in {elapsed:.1f} seconds")
        
    except Exception as e:
        print(f"[WORKER {worker_pid}] Ensemble + Monte Carlo run FAILED: {e}", flush=True)
        app.logger.exception(f"Ensemble + Monte Carlo run failed with unexpected error: {e}")
        paths = ["error"] * len(model_ids)
        landing_positions = []
    finally:
        # Trim cache after ensemble completes (in finally block so it always runs)
        print(f"[WORKER {worker_pid}] Spaceshot complete - trimming cache to normal", flush=True)
        simulate._trim_cache_to_normal()
        # Mark progress as completed and schedule cleanup after 30 seconds
        # This allows clients to poll one last time to see 100% completion
        # before the progress entry is deleted
        with _progress_lock:
            if request_id in _progress_tracking:
                _progress_tracking[request_id]['completed'] = _progress_tracking[request_id]['total']
                # Schedule cleanup in background thread
                def cleanup_progress():
                    import time
                    time.sleep(30)  # Wait 30 seconds
        with _progress_lock:
            if request_id in _progress_tracking:
                del _progress_tracking[request_id]
                app.logger.debug(f"Cleaned up progress tracking for {request_id}")
                # Schedule cleanup in background thread
                cleanup_thread = threading.Thread(target=cleanup_progress, daemon=True)
                cleanup_thread.start()
    
    # ========================================================================
    # RESPONSE FORMAT
    # ========================================================================
    # Returns both ensemble paths (for line plotting) and landing positions (for heatmap):
    # - paths: 21 full trajectory paths for Polyline rendering
    # - heatmap_data: 441 landing positions (21 ensemble + 420 Monte Carlo) for density visualization
    # - request_id: Unique ID for progress tracking (client polls /sim/progress)
    # ========================================================================
    return jsonify({
        'paths': paths,  # Original 21 ensemble paths for line plotting
        'heatmap_data': landing_positions,  # 441 landing positions (21 ensemble + 420 Monte Carlo) for heatmap
        'request_id': request_id  # For progress polling
    })

@app.route('/sim/progress')
def get_progress():
    """Get progress for a running simulation (polling endpoint)"""
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id required'}), 400
    
    with _progress_lock:
        progress = _progress_tracking.get(request_id)
    
    if progress is None:
        return jsonify({'error': 'Progress not found or completed'}), 404
    
    # Calculate percentage with integer rounding for cleaner display
    percentage = round((progress['completed'] / progress['total']) * 100) if progress['total'] > 0 else 0
    
    return jsonify({
        'completed': progress['completed'],
        'total': progress['total'],
        'ensemble_completed': progress['ensemble_completed'],
        'ensemble_total': progress['ensemble_total'],
        'montecarlo_completed': progress['montecarlo_completed'],
        'montecarlo_total': progress['montecarlo_total'],
        'percentage': percentage
    })

@app.route('/sim/progress-stream')
def progress_stream():
    """Server-Sent Events stream for real-time progress updates"""
    from flask import Response, stream_with_context
    import json
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id required'}), 400
    
    def generate():
        last_completed = -1
        while True:
            with _progress_lock:
                progress = _progress_tracking.get(request_id)
            
            if progress is None:
                # Progress not found or completed
                yield f"data: {json.dumps({'error': 'Progress not found or completed'})}\n\n"
                break
            
            current_completed = progress['completed']
            total = progress['total']
            percentage = round((current_completed / total) * 100) if total > 0 else 0
            
            # Only send update if progress changed
            if current_completed != last_completed:
                data = {
                    'completed': current_completed,
                    'total': total,
                    'ensemble_completed': progress['ensemble_completed'],
                    'ensemble_total': progress['ensemble_total'],
                    'montecarlo_completed': progress['montecarlo_completed'],
                    'montecarlo_total': progress['montecarlo_total'],
                    'percentage': percentage
                }
                yield f"data: {json.dumps(data)}\n\n"
                last_completed = current_completed
                
                # If completed, send final update and close
                if current_completed >= total:
                    break
            
            # Sleep briefly before next check
            import time
            time.sleep(0.5)  # Check every 500ms
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'  # Disable nginx buffering
    })

'''
Given a lat and lon, returns the elevation as a string
'''
@app.route('/sim/elev')
def elevation():
    lat, lon = float(request.args['lat']), float(request.args['lon'])
    app.logger.info(f"[ELEV ENDPOINT] Called with lat={lat}, lon={lon}")
    try:
        result = elev.getElevation(lat, lon)
        app.logger.info(f"[ELEV ENDPOINT] getElevation returned: {result} (type: {type(result)})")
        return str(result)
    except Exception as e:
        app.logger.error(f"[ELEV ENDPOINT] Exception in getElevation: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        return "0"


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
