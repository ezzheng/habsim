"""
Flask WSGI application serving REST API and static assets for HABSIM.

Provides endpoints for:
- Single and ensemble trajectory simulations (/sim/singlezpb, /sim/spaceshot) - STANDARD mode only
- Real-time progress tracking (/sim/progress-stream via SSE)
- Elevation data lookup (/sim/elev)
- Cache and model status (/sim/status, /sim/models, /sim/cache-status)
- Authentication (login/logout)

Manages ensemble mode activation, Monte Carlo perturbations, and parallel execution
using ThreadPoolExecutor. Handles progress tracking via Server-Sent Events (SSE).
"""
from flask import Flask, jsonify, request, Response, render_template, send_from_directory, make_response, session, redirect, url_for
from flask_cors import CORS
from flask_compress import Compress
import threading
from functools import wraps
import random
import time
import os
import secrets
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
CORS(app)
Compress(app)

# Suppress /sim/status access logs (polled every 5s, creates log spam)
class StatusLogFilter(logging.Filter):
    def filter(self, record):
        return '/sim/status' not in record.getMessage()

logging.getLogger('werkzeug').addFilter(StatusLogFilter())
logging.getLogger('gunicorn.access').addFilter(StatusLogFilter())

LOGIN_PASSWORD = os.environ.get('HABSIM_PASSWORD')
MAX_CONCURRENT_ENSEMBLE_CALLS = 4
_ENSEMBLE_COUNTER_FILE = '/tmp/ensemble_active_count'
_ENSEMBLE_COUNTER_LOCK_FILE = '/tmp/ensemble_active_count.lock'

if not LOGIN_PASSWORD:
    print("WARNING: HABSIM_PASSWORD not set - login will fail", flush=True)

def cache_for(seconds=300):
    """Add HTTP cache headers to responses."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            response = make_response(f(*args, **kwargs))
            response.headers['Cache-Control'] = f'public, max-age={seconds}'
            return response
        return decorated_function
    return decorator

import logging
import hashlib
import elev
from datetime import datetime, timezone
from gefs import listdir_gefs, open_gefs
import simulate
import downloader
from pathlib import Path
import tempfile
import json

def _log(msg, level='info', worker_pid=None):
    """Print to stdout (Railway logs) with optional worker PID prefix."""
    if worker_pid is not None:
        msg = f"[WORKER {worker_pid}] {msg}"
    prefix = {
        'info': 'INFO',
        'warning': 'WARNING',
        'error': 'ERROR',
        'debug': 'DEBUG'
    }.get(level, 'INFO')
    print(f"{prefix}: {msg}", flush=True)

def get_arg(args, key, type_func=float, default=None, required=True):
    """Parse and validate request argument with type conversion and NaN/Inf checks."""
    val = args.get(key, default)
    if required and val is None:
        raise ValueError(f"Missing required parameter: {key}")
    if val is None:
        return None
    try:
        result = type_func(val)
        # Reject NaN/Inf: comparisons with inf always return False, so this catches all non-finite values
        if isinstance(result, (int, float)) and not (float('-inf') < result < float('inf')):
            raise ValueError(f"Parameter {key} is not a finite number")
        return result
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid parameter {key}: {e}")

def parse_datetime(args):
    """Parse datetime from request arguments (yr, mo, day, hr, mn)."""
    return datetime(
        int(args['yr']), int(args['mo']), int(args['day']), 
        int(args['hr']), int(args['mn'])
    ).replace(tzinfo=timezone.utc)

def generate_request_id(args, base_coeff):
    """Generate unique request ID using MD5 hash. Formats base_coeff consistently (1.0 not 1) to match client."""
    base_coeff_str = f"{base_coeff:.1f}" if isinstance(base_coeff, float) else str(base_coeff)
    request_key = f"{args['timestamp']}_{args['lat']}_{args['lon']}_{args['alt']}_{args['equil']}_{args['eqtime']}_{args['asc']}_{args['desc']}_{base_coeff_str}"
    return hashlib.md5(request_key.encode()).hexdigest()[:16]

def _ensure_ensemble_optimizations(worker_pid):
    """No-op: cache automatically adapts when 10+ models detected."""
    pass

def _prefetch_model(model_id, worker_pid, expected_gefs=None):
    """Prefetch a single model (downloads file and builds simulator).
    
    Args:
        model_id: Model ID to prefetch
        worker_pid: Worker process ID for logging
        expected_gefs: Expected GEFS timestamp (validates cycle hasn't changed)
    """
    try:
        # Check if GEFS cycle changed during prefetch (abort if so)
        if expected_gefs:
            current_gefs = simulate.get_currgefs()
            if current_gefs and current_gefs != expected_gefs:
                raise RuntimeError(f"GEFS cycle changed during prefetch: expected {expected_gefs}, got {current_gefs}")
        simulate._get_simulator(model_id)
    except Exception as e:
        print(f"WARNING: [WORKER {worker_pid}] Prefetch failed for model {model_id}: {e}", flush=True)

def wait_for_prefetch(model_ids, worker_pid, timeout=120, min_models=12):
    """Progressive prefetch: wait for first N models, continue prefetching rest in background.
    
    Hybrid approach that balances fast startup with avoiding on-demand delays:
    - Waits for first 12 models to complete (fast simulation start)
    - Continues prefetching remaining models in background
    - When simulations need models 13-21, they're likely ready (avoiding 100+ second delays)
    
    Args:
        model_ids: List of model IDs to prefetch
        worker_pid: Worker process ID for logging
        timeout: Maximum time to wait for initial models (seconds)
        min_models: Number of models to wait for before returning (default: 12)
    
    Returns:
        Time spent waiting for initial models (seconds)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
    start_time = time.time()
    
    # Capture current GEFS cycle to detect changes during prefetch
    # If cycle changes mid-prefetch, abort to prevent loading stale data
    initial_gefs = simulate.get_currgefs()
    
    # Submit all models for prefetch in parallel
    # This ensures models 13-21 are actively downloading while simulations run
    executor = ThreadPoolExecutor(max_workers=min(10, len(model_ids)))
    try:
        prefetch_futures = {
            executor.submit(_prefetch_model, model_id, worker_pid, initial_gefs): model_id
            for model_id in model_ids
        }
        
        completed_count = 0
        failed_count = 0
        total_models = len(model_ids)
        models_to_wait = min(min_models, total_models)
        
        # Wait for first N models to complete, then return (simulations can start)
        # Remaining models continue prefetching in background (executor not shut down)
        try:
            for future in as_completed(prefetch_futures, timeout=timeout):
                model_id = prefetch_futures[future]
                try:
                    future.result()
                    completed_count += 1
                except Exception as e:
                    failed_count += 1
                    print(f"WARNING: [WORKER {worker_pid}] Prefetch failed for model {model_id}: {e}", flush=True)
                
                # Return after first N models complete (allow simulations to start)
                # Leave executor running so remaining prefetches continue in background
                if completed_count >= models_to_wait:
                    elapsed = time.time() - start_time
                    remaining = total_models - completed_count - failed_count
                    # Don't shutdown executor - let remaining prefetches continue
                    print(f"INFO: [WORKER {worker_pid}] Prefetch: {completed_count}/{total_models} ready in {elapsed:.1f}s, "
                          f"{remaining} continuing in background", flush=True)
                    return elapsed
                    
        except TimeoutError:
            # Timeout reached - return with whatever we have
            elapsed = time.time() - start_time
            # Don't shutdown executor - let remaining prefetches continue
            print(f"WARNING: [WORKER {worker_pid}] Prefetch timeout after {timeout}s: "
                  f"{completed_count}/{total_models} ready, {total_models - completed_count - failed_count} continuing", flush=True)
            return elapsed
    finally:
        # Shutdown executor without waiting (remaining prefetches continue as daemon threads)
        # This prevents executor from blocking on exit while allowing background work to finish
        executor.shutdown(wait=False)
    
    # All models completed (shouldn't reach here due to early return, but handle gracefully)
    elapsed = time.time() - start_time
    print(f"INFO: [WORKER {worker_pid}] Prefetch complete: {completed_count}/{total_models} models in {elapsed:.1f}s", flush=True)
    return elapsed

def perturb_lat(base_lat):
    """Perturb latitude: ±0.001° ≈ ±111m. Clamped to [-90, 90]."""
    result = base_lat + random.uniform(-0.001, 0.001)
    return max(-90.0, min(90.0, result))

def perturb_lon(base_lon):
    """Perturb longitude: ±0.001° with wrap to [0, 360)."""
    return (base_lon + random.uniform(-0.001, 0.001)) % 360

def perturb_alt(base_alt):
    """Perturb altitude: ±50m, minimum 0."""
    return max(0, base_alt + random.uniform(-50, 50))

def perturb_equil(base_equil, pert_alt):
    """Perturb equilibrium altitude: ±200m, must be >= alt."""
    return max(pert_alt, base_equil + random.uniform(-200, 200))

def perturb_eqtime(base_eqtime):
    """Perturb equilibrium time: ±0.5 hours, minimum 0."""
    return max(0, base_eqtime + random.uniform(-0.5, 0.5))

def perturb_rate(base_rate):
    """Perturb ascent/descent rate: ±0.5 m/s, minimum 0.1."""
    return max(0.1, base_rate + random.uniform(-0.5, 0.5))

def perturb_coefficient():
    """Perturb floating coefficient: 0.9-1.0, weighted 90% towards 0.95-1.0."""
    if random.random() < 0.9:
        return random.uniform(0.95, 1.0)
    return random.uniform(0.9, 0.95)

def extract_landing_position(result):
    """Extract landing position from singlezpb result. Returns dict or None."""
    if result is None or not isinstance(result, tuple) or len(result) != 3:
        return None
    
    try:
        rise, coast, fall = result
        if not fall or len(fall) == 0 or len(fall[-1]) < 3:
            return None
        
        # Tuple format: [timestamp, lat, lon, alt, u, v, __, __]
        final_lat = float(fall[-1][1])
        final_lon = float(fall[-1][2])
        return {'lat': final_lat, 'lon': final_lon}
    except (IndexError, ValueError, TypeError):
        return None

def _update_ensemble_progress(request_id, ensemble_completed, ensemble_total):
    """Update ensemble progress (batched every 5 completions or on completion)."""
    if ensemble_completed % 5 == 0 or ensemble_completed == ensemble_total:
        update_progress(request_id, ensemble_completed=ensemble_completed)

def _update_montecarlo_progress(request_id, montecarlo_completed, montecarlo_total):
    """Update Monte Carlo progress (batched every 20 completions or on completion)."""
    if montecarlo_completed % 20 == 0 or montecarlo_completed == montecarlo_total:
        update_progress(request_id, montecarlo_completed=montecarlo_completed)

def get_model_ids():
    """Get list of available model IDs based on configuration."""
    model_ids = []
    if downloader.DOWNLOAD_CONTROL:
        model_ids.append(0)
    model_ids.extend(range(1, 1 + downloader.NUM_PERTURBED_MEMBERS))
    return model_ids

def _generate_perturbations(args, base_lat, base_lon, base_alt, base_equil, 
                            base_asc, base_desc, base_eqtime, base_coeff, num_perturbations):
    """
    Generate Monte Carlo perturbations with deterministic seeding.
    
    Uses hash of request parameters as seed to ensure same request produces
    same perturbations (useful for caching/debugging). Validates all perturbations
    to ensure physical constraints are met (e.g., burst >= launch altitude).
    """
    perturbations = []
    # Create deterministic seed from request parameters
    # Same request always produces same perturbations
    request_key = f"{args['timestamp']}_{args['lat']}_{args['lon']}_{args['alt']}_{args['equil']}_{args['eqtime']}_{args['asc']}_{args['desc']}_{base_coeff}"
    random.seed(hash(request_key) & 0xFFFFFFFF)  # Mask to 32-bit for consistency
    
    for i in range(num_perturbations):
        # Generate perturbed values for each parameter
        pert_alt = perturb_alt(base_alt)
        pert_equil = perturb_equil(base_equil, pert_alt)
        pert_asc = perturb_rate(base_asc)
        pert_desc = perturb_rate(base_desc)
        pert_eqtime = perturb_eqtime(base_eqtime)
        
        # Validate perturbations to ensure physical constraints
        # These checks prevent invalid simulations that would fail later
        if pert_equil < pert_alt:
            pert_equil = pert_alt  # Burst altitude must be >= launch altitude
        if pert_asc <= 0:
            pert_asc = 0.1  # Ascent rate must be positive
        if pert_desc <= 0:
            pert_desc = 0.1  # Descent rate must be positive
        if pert_eqtime < 0:
            pert_eqtime = 0  # Equilibrium time must be non-negative
        
        perturbations.append({
            'perturbation_id': i,
            'lat': perturb_lat(base_lat),
            'lon': perturb_lon(base_lon),
            'alt': pert_alt,
            'equil': pert_equil,
            'eqtime': pert_eqtime,
            'asc': pert_asc,
            'desc': pert_desc,
            'coeff': perturb_coefficient()
        })
    
    return perturbations

def update_progress(request_id, completed=None, ensemble_completed=None, montecarlo_completed=None, status=None):
    """Update progress tracking atomically (both in-memory and file-based)."""
    with _progress_lock:
        if request_id in _progress_tracking:
            if completed is not None:
                _progress_tracking[request_id]['completed'] = completed
            if ensemble_completed is not None:
                _progress_tracking[request_id]['ensemble_completed'] = ensemble_completed
            if montecarlo_completed is not None:
                _progress_tracking[request_id]['montecarlo_completed'] = montecarlo_completed
            if status is not None:
                _progress_tracking[request_id]['status'] = status
            # Also update file-based cache for multi-worker access
            _write_progress(request_id, _progress_tracking[request_id])


def _is_authenticated():
    """Check if user is authenticated"""
    return session.get('authenticated', False)

@app.before_request
def _record_worker_activity():
    """Mark worker as active for idle cleanup tracking. Excludes polling endpoints."""
    excluded_paths = ['/sim/status', '/sim/models', '/sim/cache-status', '/', '/favicon.ico']
    path = request.path
    if (path.startswith('/static/') or 
        path.endswith(('.css', '.js', '.png', '.jpg', '.ico')) or
        path == '/health' or
        request.headers.get('User-Agent', '').startswith('Railway')):
        return
    if path not in excluded_paths:
        try:
            simulate.record_activity()
        except Exception:
            pass

_progress_tracking = {}
_progress_lock = threading.Lock()

_PROGRESS_CACHE_DIR = Path("/app/data/progress") if Path("/app/data").exists() else Path(tempfile.gettempdir()) / "habsim-progress"
_PROGRESS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _get_progress_file(request_id):
    """Get file path for progress tracking."""
    return _PROGRESS_CACHE_DIR / f"{request_id}.json"

def _read_progress(request_id):
    """Read progress from file (shared across workers)."""
    try:
        progress_file = _get_progress_file(request_id)
        if progress_file.exists():
            with open(progress_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error reading progress file for {request_id}: {e}", flush=True)
    return None

def _write_progress(request_id, progress_data):
    """Write progress to file (shared across workers)."""
    try:
        progress_file = _get_progress_file(request_id)
        temp_file = progress_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(progress_data, f)
        temp_file.replace(progress_file)
    except Exception as e:
        print(f"Error writing progress file for {request_id}: {e}", flush=True)

def _delete_progress(request_id):
    """Delete progress file."""
    try:
        progress_file = _get_progress_file(request_id)
        if progress_file.exists():
            progress_file.unlink()
    except Exception as e:
        print(f"Error deleting progress file for {request_id}: {e}", flush=True)

def _start_cache_trim_thread():
    """Start cache trim thread early in each worker."""
    try:
        simulate._start_cache_trim_thread()
    except Exception as e:
        print(f"WARNING: Failed to start cache trim thread: {e}", flush=True)

def _prewarm_cache():
    """Pre-load model 0 and worldelev.npy for fast single requests."""
    try:
        time.sleep(2)
        for model_id in get_model_ids()[:1]:
            try:
                simulate._get_simulator(model_id)
                time.sleep(0.5)
            except Exception:
                pass
        
        try:
            import gefs
            gefs.load_gefs('worldelev.npy')
        except Exception:
            pass
        
        try:
            _ = elev.getElevation(0, 0)
        except Exception:
            pass
    except Exception:
        pass

is_railway = os.environ.get('RAILWAY_ENVIRONMENT') is not None or os.environ.get('RAILWAY_SERVICE_NAME') is not None
if is_railway:
    try:
        _start_cache_trim_thread()
    except Exception as e:
        print(f"WARNING: Failed to start cache trim thread: {e}", flush=True)
    _cache_warmer_thread = threading.Thread(target=_prewarm_cache, daemon=True)
    _cache_warmer_thread.start()
else:
    print("INFO: Skipping Railway-specific initialization", flush=True)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and authentication handler."""
    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        expected_password = (LOGIN_PASSWORD or '').strip()
        
        if not expected_password:
            print("ERROR: HABSIM_PASSWORD environment variable is not set!", flush=True)
            return redirect('/login?error=1')
        
        if password == expected_password:
            session['authenticated'] = True
            session.permanent = False
            return redirect(request.args.get('next', '/'))
        else:
            print("WARNING: Login failed: invalid password", flush=True)
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
    """Logout handler."""
    session.pop('authenticated', None)
    return redirect('/login?logout=1')

@app.route('/')
def index():
    """Serve main application page."""
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
    """Get current GEFS timestamp."""
    f = open_gefs('whichgefs')
    s = f.readline()
    f.close()
    return s

@app.route('/sim/test-s3')
def test_s3():
    """Test S3 connectivity and credentials. Returns diagnostic info."""
    import gefs
    from botocore.exceptions import ClientError, NoCredentialsError
    import time
    
    result = {
        'credentials_configured': bool(gefs._AWS_ACCESS_KEY_ID and gefs._AWS_SECRET_ACCESS_KEY),
        'region': gefs._AWS_REGION,
        'bucket': gefs._BUCKET,
        'access_key_prefix': gefs._AWS_ACCESS_KEY_ID[:8] + '...' if gefs._AWS_ACCESS_KEY_ID else 'NOT SET',
        'tests': {}
    }
    
    # Test 1: Try to list bucket (requires s3:ListBucket permission)
    start = time.time()
    try:
        response = gefs._S3_CLIENT.list_objects_v2(Bucket=gefs._BUCKET, MaxKeys=1)
        result['tests']['list_bucket'] = {
            'success': True,
            'time': f"{time.time() - start:.2f}s",
            'message': 'Can list bucket'
        }
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_msg = e.response.get('Error', {}).get('Message', str(e))
        result['tests']['list_bucket'] = {
            'success': False,
            'time': f"{time.time() - start:.2f}s",
            'error_code': error_code,
            'message': error_msg
        }
    except Exception as e:
        result['tests']['list_bucket'] = {
            'success': False,
            'time': f"{time.time() - start:.2f}s",
            'error': f"{type(e).__name__}: {str(e)}"
        }
    
    # Test 2: Try to read whichgefs file
    start = time.time()
    try:
        response = gefs._STATUS_S3_CLIENT.get_object(Bucket=gefs._BUCKET, Key='whichgefs')
        content = response['Body'].read().decode("utf-8")
        result['tests']['read_whichgefs'] = {
            'success': True,
            'time': f"{time.time() - start:.2f}s",
            'content': content.strip(),
            'message': 'Can read whichgefs file'
        }
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_msg = e.response.get('Error', {}).get('Message', str(e))
        result['tests']['read_whichgefs'] = {
            'success': False,
            'time': f"{time.time() - start:.2f}s",
            'error_code': error_code,
            'message': error_msg
        }
    except Exception as e:
        result['tests']['read_whichgefs'] = {
            'success': False,
            'time': f"{time.time() - start:.2f}s",
            'error': f"{type(e).__name__}: {str(e)}"
        }
    
    return jsonify(result)

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
        cached_models = list(simulate._simulator_cache.keys())
        # Check if we're in ensemble workload (10+ ensemble models cached, matching simulate.py threshold)
        ensemble_models = simulate._count_ensemble_models() if hasattr(simulate, '_count_ensemble_models') else len([m for m in cached_models if isinstance(m, int) and m < 21])
        is_ensemble_workload = ensemble_models >= 10
        # Get target cache size while holding lock (required by _get_target_cache_size)
        target_cache_size = simulate._get_target_cache_size() if hasattr(simulate, '_get_target_cache_size') else cache_limit
    
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
    
    # Determine cache status
    cache_status_note = None
    if cache_size == 0:
        if idle_duration > 300:
            cache_status_note = f'Cache is empty - worker has been idle for {round(idle_duration)}s (may have been cleaned up)'
        else:
            cache_status_note = 'Cache is empty - this worker may not have handled any requests yet'
    elif cache_limit == simulate.MAX_SIMULATOR_CACHE_NORMAL and cache_size < cache_limit:
        cache_status_note = 'Cache is below normal limit (normal operation)'
    elif cache_limit >= simulate.MAX_SIMULATOR_CACHE_ENSEMBLE:
        if is_ensemble_workload:
            cache_status_note = 'Ensemble workload detected - cache limit expanded (adaptive sizing)'
        else:
            cache_status_note = 'Cache limit expanded but ensemble workload not detected (trim pending)'
    
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
        'workload': {
            'is_ensemble_workload': is_ensemble_workload,
            'ensemble_models_cached': ensemble_models,
            'cache_limit_expanded': cache_limit >= simulate.MAX_SIMULATOR_CACHE_ENSEMBLE,
            'note': 'Cache automatically adapts to workload. Ensemble workloads (10+ models) trigger preloading and expanded cache.',
            'adaptive_behavior': {
                'preload_arrays': is_ensemble_workload,
                'target_cache_size': target_cache_size,
                'note': 'Preloading and cache size are automatically adjusted based on cached model count'
            }
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
    """Status endpoint - should be fast and non-blocking even during heavy load.
    Access logging is suppressed for this endpoint to reduce Railway log noise.
    """
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
        return "Ready"

@app.route('/sim/models')
def models():
    """Return available model IDs based on configuration"""
    return jsonify({
        "models": get_model_ids(),
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
            timestamp, lat, lon, alt = rise[-1][0], rise[-1][1], rise[-1][2], rise[-1][3]
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
            timestamp, lat, lon, alt = coast[-1][0], coast[-1][1], coast[-1][2], coast[-1][3]
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
        print(f"WARNING: Model file not found: {e}", flush=True)
        raise  # Re-raise to be handled by route handler
    except Exception as e:
        print(f"ERROR: singlezpb failed for model {model}: {e}", flush=True)
        # Re-raise all exceptions - let route handlers decide how to format response
        raise


@app.route('/sim/singlezpb')
@cache_for(600)
def singlezpbh():
    worker_pid = os.getpid()
    args = request.args
    timestamp = datetime.utcfromtimestamp(get_arg(args, 'timestamp')).replace(tzinfo=timezone.utc)
    lat = get_arg(args, 'lat')
    lon = get_arg(args, 'lon')
    alt = get_arg(args, 'alt')
    equil = get_arg(args, 'equil')
    eqtime = get_arg(args, 'eqtime')
    asc = get_arg(args, 'asc')
    desc = get_arg(args, 'desc')
    model = get_arg(args, 'model', type_func=int)
    # Validate input ranges
    if not (-90 <= lat <= 90):
        return make_response(jsonify({"error": "Latitude must be between -90 and 90"}), 400)
    if not (0 <= alt < 50000):
        return make_response(jsonify({"error": "Launch altitude must be between 0 and 50000 meters"}), 400)
    if not (alt <= equil < 50000):
        return make_response(jsonify({"error": "Burst altitude must be >= launch altitude and < 50000 meters"}), 400)
    if not (0 <= asc <= 20):
        return make_response(jsonify({"error": "Ascent rate must be between 0 and 20 m/s"}), 400)
    if not (0 <= desc <= 20):
        return make_response(jsonify({"error": "Descent rate must be between 0 and 20 m/s"}), 400)
    if not (0 <= eqtime <= 48):
        return make_response(jsonify({"error": "Equilibrium time must be between 0 and 48 hours"}), 400)
    if not (0 <= model <= 20):
        return make_response(jsonify({"error": "Model ID must be between 0 and 20"}), 400)
    
    print(f"INFO: [WORKER {worker_pid}] Single simulate: model={model}, lat={lat}, lon={lon}, alt={alt}, "
          f"burst={equil}, ascent={asc}m/s, descent={desc}m/s", flush=True)
    try:
        path = singlezpb(timestamp, lat, lon, alt, equil, eqtime, asc, desc, model)
        return jsonify(path)
    except ValueError as e:
        return make_response(jsonify({"error": str(e)}), 400)
    except FileNotFoundError as e:
        print(f"WARNING: Model file not found: {e}", flush=True)
        return make_response(jsonify({"error": "Model file not available. The requested model may not have been uploaded yet. Please check if the model timestamp is correct."}), 404)
    except Exception as e:
        error_msg = str(e)
        if "alt out of range" in error_msg:
            return make_response(jsonify({"error": "Altitude out of range"}), 400)
        print(f"ERROR: singlezpbh failed: {e}", flush=True)
        return make_response(jsonify({"error": "Simulation failed"}), 500)


def _increment_ensemble_counter():
    """
    Atomically increment the ensemble call counter across all Gunicorn workers.
    
    Uses file-based locking (fcntl) to coordinate between worker processes since
    Gunicorn uses multiple processes, not threads. Returns True if under limit,
    False if limit exceeded.
    
    CRITICAL: Must use fcntl.flock() for inter-process locking (threading.Lock
    only works within a single process). The lock file ensures only one worker
    can read-modify-write the counter at a time.
    """
    lock_file = None
    try:
        import fcntl
        # Open lock file for fcntl-based inter-process coordination
        # Using 'w' mode creates file if it doesn't exist
        lock_file = open(_ENSEMBLE_COUNTER_LOCK_FILE, 'w')
        # Acquire exclusive lock (blocks other workers until released)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        
        # Read current count atomically (while holding lock)
        try:
            with open(_ENSEMBLE_COUNTER_FILE, 'r') as f:
                current_count = int(f.read().strip() or '0')
        except (FileNotFoundError, ValueError):
            # First time or corrupted file - start at 0
            current_count = 0
        
        # Check if we're at the limit before incrementing
        if current_count >= MAX_CONCURRENT_ENSEMBLE_CALLS:
            return False
        
        # Increment and write back atomically (still holding lock)
        current_count += 1
        with open(_ENSEMBLE_COUNTER_FILE, 'w') as f:
            f.write(str(current_count))
        
        return True
    except Exception as e:
        # Fail open: if locking fails, allow request rather than blocking
        # This prevents lock file issues from breaking the entire system
        print(f"WARNING: Failed to check ensemble counter: {e}. Allowing request.", flush=True)
        return True
    finally:
        # CRITICAL: Always release lock and close file, even on error
        # Failure to release lock causes deadlock for other workers
        if lock_file:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass

def _decrement_ensemble_counter():
    """
    Atomically decrement the ensemble call counter across all workers.
    
    Uses same file-based locking as _increment_ensemble_counter() to ensure
    thread-safe decrement. Called in finally block to ensure counter is always
    decremented even if ensemble run fails.
    """
    lock_file = None
    try:
        import fcntl
        lock_file = open(_ENSEMBLE_COUNTER_LOCK_FILE, 'w')
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        
        try:
            with open(_ENSEMBLE_COUNTER_FILE, 'r') as f:
                current_count = int(f.read().strip() or '0')
        except (FileNotFoundError, ValueError):
            current_count = 0
        
        # Decrement but don't go below 0 (prevents negative counts from errors)
        current_count = max(0, current_count - 1)
        
        with open(_ENSEMBLE_COUNTER_FILE, 'w') as f:
            f.write(str(current_count))
    except Exception as e:
        # Non-fatal: log but don't raise (counter will be slightly off but system continues)
        print(f"WARNING: Failed to decrement ensemble counter: {e}", flush=True)
    finally:
        # CRITICAL: Always release lock to prevent deadlock
        if lock_file:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass

@app.route('/sim/spaceshot')
def spaceshot():
    """
    Run all available ensemble models with Monte Carlo analysis.
    Returns both ensemble paths and Monte Carlo landing positions for heatmap.
    Ensemble points are weighted 2× more than Monte Carlo points.
    """
    worker_pid = os.getpid()
    simulate.record_activity()
    
    # CRITICAL: Check ensemble counter limit before processing
    # This prevents too many simultaneous ensemble requests from overwhelming the system
    counter_incremented = False
    if not _increment_ensemble_counter():
        return make_response(
            jsonify({
                "error": f"Too many concurrent ensemble requests. Maximum is {MAX_CONCURRENT_ENSEMBLE_CALLS}. Please try again later."
            }), 
            429  # Too Many Requests
        )
    counter_incremented = True  # Only set if increment succeeded
    
    ENSEMBLE_WEIGHT = 2.0
    start_time = time.time()
    args = request.args
    
    # Parse arguments using helper
    try:
        timestamp = datetime.utcfromtimestamp(get_arg(args, 'timestamp')).replace(tzinfo=timezone.utc)
        base_lat = get_arg(args, 'lat')
        base_lon = get_arg(args, 'lon')
        base_alt = get_arg(args, 'alt')
        base_equil = get_arg(args, 'equil')
        base_eqtime = get_arg(args, 'eqtime')
        base_asc = get_arg(args, 'asc')
        base_desc = get_arg(args, 'desc')
        base_coeff = get_arg(args, 'coeff', default=1.0)
        num_perturbations = get_arg(args, 'num_perturbations', type_func=int, default=20)
        
        # Validate input ranges
        if not (-90 <= base_lat <= 90):
            return make_response(jsonify({"error": "Latitude must be between -90 and 90"}), 400)
        if not (0 <= base_alt < 50000):
            return make_response(jsonify({"error": "Launch altitude must be between 0 and 50000 meters"}), 400)
        if not (base_alt <= base_equil < 50000):
            return make_response(jsonify({"error": "Burst altitude must be >= launch altitude and < 50000 meters"}), 400)
        if not (0 <= base_asc <= 20):
            return make_response(jsonify({"error": "Ascent rate must be between 0 and 20 m/s"}), 400)
        if not (0 <= base_desc <= 20):
            return make_response(jsonify({"error": "Descent rate must be between 0 and 20 m/s"}), 400)
        if not (0 <= base_eqtime <= 48):
            return make_response(jsonify({"error": "Equilibrium time must be between 0 and 48 hours"}), 400)
        if not (1 <= num_perturbations <= 100):
            return make_response(jsonify({"error": "Number of perturbations must be between 1 and 100"}), 400)
        if not (0.5 <= base_coeff <= 1.5):
            return make_response(jsonify({"error": "Coefficient must be between 0.5 and 1.5"}), 400)
    except ValueError as e:
        return make_response(jsonify({"error": str(e)}), 400)
    
    request_id = generate_request_id(args, base_coeff)
    
    # CRITICAL: Initialize progress tracking IMMEDIATELY before any other processing
    # Frontend may connect via SSE before simulations start, so progress must exist
    # or SSE will timeout waiting for progress to appear
    model_ids = get_model_ids()
    total_ensemble = len(model_ids)
    total_montecarlo = num_perturbations * len(model_ids)
    total_simulations = total_ensemble + total_montecarlo
    
    # Create progress structure with all counters initialized to 0
    progress_data = {
        'completed': 0,
        'total': total_simulations,
        'ensemble_completed': 0,
        'ensemble_total': total_ensemble,
        'montecarlo_completed': 0,
        'montecarlo_total': total_montecarlo,
        'status': 'loading'  # Initial status: loading models
    }
    # Store in both in-memory dict (fast) and file (shared across workers)
    with _progress_lock:
        _progress_tracking[request_id] = progress_data.copy()
    _write_progress(request_id, progress_data)
    
    # Log ensemble start immediately
    print(f"INFO: [WORKER {worker_pid}] Ensemble: request_id={request_id}, "
          f"lat={base_lat}, lon={base_lon}, alt={base_alt}, burst={base_equil}, "
          f"ascent={base_asc}m/s, descent={base_desc}m/s, "
          f"models={len(model_ids)}, montecarlo={num_perturbations}×{len(model_ids)}={total_montecarlo}", flush=True)
    
    _ensure_ensemble_optimizations(worker_pid)
    
    # Generate Monte Carlo perturbations
    perturbations = _generate_perturbations(args, base_lat, base_lon, base_alt, base_equil, 
                                            base_asc, base_desc, base_eqtime, base_coeff, 
                                            num_perturbations)
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    paths = [None] * len(model_ids)  # Pre-allocate to preserve order for 21 ensemble paths
    landing_positions = []  # Landing positions: 21 ensemble + 420 Monte Carlo = 441 total
    
    def run_ensemble_simulation(model):
        """Run ensemble simulation for one model. Returns trajectory path or None."""
        try:
            result = singlezpb(timestamp, base_lat, base_lon, base_alt, base_equil, base_eqtime, base_asc, base_desc, model)
            return result
        except FileNotFoundError as e:
            print(f"WARNING: Model {model} file not found: {e}", flush=True)
            return None
        except Exception as e:
            print(f"ERROR: Model {model} failed: {e}", flush=True)
            return None
    
    def run_montecarlo_simulation(pert, model):
        """Run Monte Carlo simulation and extract landing position. Returns dict or None."""
        try:
            result = singlezpb(timestamp, pert['lat'], pert['lon'], pert['alt'], 
                             pert['equil'], pert['eqtime'], pert['asc'], pert['desc'], model,
                             coefficient=pert.get('coeff', 1.0))
            
            landing = extract_landing_position(result)
            if landing:
                landing.update({
                    'perturbation_id': pert['perturbation_id'],
                    'model_id': model,
                    'weight': 1.0
                })
            return landing
        except FileNotFoundError as e:
            print(f"WARNING: Monte Carlo simulation file not found: pert={pert['perturbation_id']}, model={model}: {e}", flush=True)
            return None
        except Exception as e:
            print(f"WARNING: Monte Carlo simulation failed: pert={pert['perturbation_id']}, model={model}, error={e}", flush=True)
            return None
    
    try:
        # Progressive prefetch: wait for first 12 models, continue rest in background
        # This balances fast startup (simulations start after 12 models) with avoiding
        # on-demand delays (models 13-21 continue prefetching, ready when needed)
        # Update status to show we're loading
        update_progress(request_id, status='loading')
        wait_for_prefetch(model_ids, worker_pid)
        
        # Switch to simulating status once prefetch is done
        update_progress(request_id, status='simulating')
        
        # Run ensemble and Monte Carlo simulations in parallel with 10-minute timeout
        max_workers = min(32, os.cpu_count() or 4)
        timeout_seconds = 600  # 10 minutes
        
        # Create model-to-index mapping for O(1) lookup (replaces O(n) model_ids.index())
        # This is critical for performance: without it, finding model index in paths array
        # would be O(n) for each completion, making total complexity O(n²) instead of O(n)
        model_to_index = {model: idx for idx, model in enumerate(model_ids)}
        
        # Submit all simulations to thread pool (ensemble + Monte Carlo run in parallel)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Ensemble futures: one per model (21 total)
            # Use dict to track which model each future belongs to
            ensemble_futures = {
                executor.submit(run_ensemble_simulation, model): model
                for model in model_ids
            }
            
            # Monte Carlo futures: one per perturbation × model combination
            # Total: num_perturbations × len(model_ids) (e.g., 20 × 21 = 420)
            montecarlo_futures = [
                executor.submit(run_montecarlo_simulation, pert, model)
                for pert in perturbations
                for model in model_ids
            ]
            
            # Combine all futures for unified completion tracking
            all_futures = list(ensemble_futures.keys()) + montecarlo_futures
            # Use set for O(1) lookup to check if future is ensemble or Monte Carlo
            ensemble_future_set = set(ensemble_futures.keys())
            
            # Progress tracking counters
            ensemble_completed = 0
            montecarlo_completed = 0
            total_completed = 0
            last_progress_update = 0
            # Batch progress updates to reduce lock contention
            # Updating on every completion would cause excessive locking overhead
            progress_update_interval = 10
            
            try:
                # Process completions as they arrive (non-blocking)
                # as_completed() yields futures in order of completion, not submission order
                for future in as_completed(all_futures, timeout=timeout_seconds):
                    total_completed += 1
                    # Batch progress updates to reduce lock contention
                    # Updating on every completion would serialize all threads on the lock
                    if total_completed - last_progress_update >= progress_update_interval or total_completed == total_simulations:
                        update_progress(request_id, completed=total_completed)
                        last_progress_update = total_completed
                    
                    # Check if this is an ensemble or Monte Carlo future
                    if future in ensemble_future_set:
                        # Ensemble simulation completed
                        model = ensemble_futures[future]
                        try:
                            # Use O(1) lookup to find correct index in paths array
                            idx = model_to_index[model]
                            result = future.result()
                            paths[idx] = result  # Store in correct position to preserve order
                            
                            # Extract landing position for heatmap (ensemble points weighted 2×)
                            landing = extract_landing_position(result)
                            if landing:
                                landing.update({
                                    'perturbation_id': -1,  # -1 indicates ensemble (not Monte Carlo)
                                    'model_id': model,
                                    'weight': ENSEMBLE_WEIGHT  # 2.0× weight for ensemble
                                })
                                landing_positions.append(landing)
                            
                            ensemble_completed += 1
                            # Update ensemble-specific progress (batched internally)
                            _update_ensemble_progress(request_id, ensemble_completed, len(model_ids))
                        except Exception as e:
                            print(f"ERROR: Ensemble model {model} failed: {e}", flush=True)
                            # Store None to indicate failure (preserves array order)
                            idx = model_to_index.get(model)
                            if idx is not None:
                                paths[idx] = None
                            ensemble_completed += 1
                            _update_ensemble_progress(request_id, ensemble_completed, len(model_ids))
                    else:
                        # Monte Carlo simulation completed
                        try:
                            result = future.result()
                            if result is not None:
                                # Monte Carlo results are landing positions only (not full paths)
                                landing_positions.append(result)
                            montecarlo_completed += 1
                            # Update Monte Carlo-specific progress (batched internally)
                            _update_montecarlo_progress(request_id, montecarlo_completed, total_montecarlo)
                        except Exception as e:
                            # Monte Carlo failures are non-fatal (just one perturbation)
                            print(f"WARNING: Monte Carlo simulation failed: {e}", flush=True)
                            montecarlo_completed += 1
                            _update_montecarlo_progress(request_id, montecarlo_completed, total_montecarlo)
            except TimeoutError:
                # 10-minute timeout reached - cancel remaining work to prevent hanging
                print(f"WARNING: [WORKER {worker_pid}] Ensemble timeout after {timeout_seconds}s", flush=True)
                for f in all_futures:
                    if not f.done():
                        f.cancel()  # Cancel futures that haven't started yet
        
        # Log summary
        ensemble_success = sum(1 for p in paths if p is not None)
        elapsed = time.time() - start_time
        ensemble_landings = sum(1 for p in landing_positions if p.get('perturbation_id') == -1)
        montecarlo_landings = len(landing_positions) - ensemble_landings
        print(f"INFO: [WORKER {worker_pid}] Ensemble complete: request_id={request_id}, "
              f"result={ensemble_success}/{len(model_ids)} paths, {len(landing_positions)} landings, "
              f"time={elapsed:.0f}s", flush=True)
        
    except Exception as e:
        print(f"ERROR: Ensemble run failed: {e}", flush=True)
        paths = [None] * len(model_ids)
        landing_positions = []
    finally:
        # CRITICAL: Decrement counter only if we incremented it
        # Counter tracks active ensemble calls across all workers
        # If counter check failed (returned early), counter was never incremented, so don't decrement
        if counter_incremented:
            _decrement_ensemble_counter()
        
        # Trim cache back to normal size after ensemble completes
        # Cache automatically expanded during ensemble, now shrink it back
        simulate._trim_cache_to_normal()
        
        # Mark progress as 100% complete (for SSE connections that are still open)
        with _progress_lock:
            if request_id in _progress_tracking:
                _progress_tracking[request_id]['completed'] = _progress_tracking[request_id]['total']
                _write_progress(request_id, _progress_tracking[request_id])
        
        # Schedule cleanup after delay (allows SSE connections to read final progress)
        # Progress files are cleaned up after 30s to prevent disk bloat
        def cleanup_progress():
            time.sleep(30)  # Wait for SSE connections to finish
            with _progress_lock:
                if request_id in _progress_tracking:
                    del _progress_tracking[request_id]
            _delete_progress(request_id)  # Remove file-based progress cache
        
        cleanup_thread = threading.Thread(target=cleanup_progress, daemon=True)
        cleanup_thread.start()
    
    return jsonify({
        'paths': paths,
        'heatmap_data': landing_positions,
        'request_id': request_id
    })

@app.route('/sim/progress-stream')
def progress_stream():
    """
    Server-Sent Events (SSE) stream for real-time progress updates.
    
    Frontend connects to this endpoint and receives progress updates as simulations
    complete. Uses both in-memory dict (fast) and file-based cache (shared across
    workers) to ensure progress is available even if request hits different worker.
    """
    from flask import Response, stream_with_context
    import json
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id required'}), 400
    
    def generate():
        """
        Generator function that yields SSE events as progress updates.
        
        Checks both in-memory dict and file-based cache to handle multi-worker
        scenarios. Only sends updates when progress actually changes to reduce
        bandwidth. Waits up to 10s for progress to appear (handles race condition
        where SSE connects before ensemble starts).
        """
        last_completed = -1
        initial_sent = False
        wait_count = 0
        max_wait = 100  # Wait up to 10 seconds (100 * 0.1s) for progress to be created
        
        while True:
            # Check in-memory dict first (fastest, but only works within same worker)
            with _progress_lock:
                progress = _progress_tracking.get(request_id)
            
            # If not in memory, check file-based cache (works across workers)
            if progress is None:
                progress = _read_progress(request_id)
                if progress:
                    # Cache in memory for faster subsequent reads
                    with _progress_lock:
                        _progress_tracking[request_id] = progress
            
            # If still not found, wait a bit (handles race where SSE connects before ensemble starts)
            if progress is None:
                if wait_count < max_wait:
                    wait_count += 1
                    time.sleep(0.1)
                    continue
                # Progress still not found after waiting - request may have failed or wrong request_id
                # Log once for debugging (not on every check to avoid log spam)
                if wait_count == max_wait:
                    print(f"SSE: Progress not found for request_id: {request_id} after {max_wait * 0.1:.1f}s wait", flush=True)
                yield f"data: {json.dumps({'error': 'Progress not found. The request may have failed or the request_id is incorrect.'})}\n\n"
                break
            
            # Re-read from file to get latest updates (another worker may have updated it)
            file_progress = _read_progress(request_id)
            if file_progress:
                progress = file_progress
                # Update in-memory cache with latest data
                with _progress_lock:
                    _progress_tracking[request_id] = progress
            
            # Calculate progress percentage
            current_completed = progress['completed']
            total = progress['total']
            percentage = round((current_completed / total) * 100) if total > 0 else 0
            
            # Check if status changed (loading -> simulating -> complete)
            status = progress.get('status', 'simulating')
            status_changed = 'status' in progress and (not initial_sent or status != getattr(generate, '_last_status', None))
            
            # Only send update if progress changed or status changed (reduces bandwidth)
            if current_completed != last_completed or not initial_sent or status_changed:
                data = {
                    'completed': current_completed,
                    'total': total,
                    'ensemble_completed': progress['ensemble_completed'],
                    'ensemble_total': progress['ensemble_total'],
                    'montecarlo_completed': progress['montecarlo_completed'],
                    'montecarlo_total': progress['montecarlo_total'],
                    'percentage': percentage,
                    'status': status
                }
                # SSE format: "data: {json}\n\n"
                yield f"data: {json.dumps(data)}\n\n"
                last_completed = current_completed
                generate._last_status = status
                initial_sent = True
                # If complete, send final update and exit
                if current_completed >= total:
                    break
            else:
                # No change - sleep to avoid busy-waiting
                time.sleep(0.5)
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'  # Disable nginx buffering (ensures real-time updates)
    })

@app.route('/sim/elev')
def elevation():
    """Get elevation at specified coordinates."""
    try:
        lat = get_arg(request.args, 'lat')
        lon = get_arg(request.args, 'lon')
        
        # Validate coordinate ranges
        if not (-90 <= lat <= 90):
            return make_response(jsonify({"error": "Latitude must be between -90 and 90"}), 400)
        
        result = elev.getElevation(lat, lon)
        return str(result or 0)
    except ValueError as e:
        return make_response(jsonify({"error": str(e)}), 400)
    except Exception as e:
        print(f"WARNING: Elevation lookup failed: {e}", flush=True)
        return "0"


