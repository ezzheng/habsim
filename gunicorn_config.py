"""
Gunicorn configuration for HABSIM deployment on Railway.

Optimized for high-concurrency ensemble simulations with 4 workers × 8 threads = 32 concurrent capacity.
Each worker runs its own cache trim thread for memory management.
"""
import os
import logging

# Network binding
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"  # Bind to all interfaces, use PORT env var or default 8000
backlog = 512  # Maximum pending connections

# Worker configuration
workers = 4  # 4 worker processes for parallel request handling
worker_class = 'gthread'  # Thread-based workers (better for I/O-bound operations)
threads = 8  # 8 threads per worker (4 workers × 8 threads = 32 concurrent capacity)
worker_connections = 200  # Maximum simultaneous clients per worker

# Worker lifecycle
max_requests = 800  # Restart worker after handling this many requests (prevents memory leaks)
max_requests_jitter = 80  # Randomize restart to avoid all workers restarting simultaneously

# Timeouts
timeout = 900  # 15 minutes - ensemble simulations can take 5-15 minutes
keepalive = 30  # Keep connections alive for 30 seconds

# Application loading
preload_app = True  # Load app before forking workers (faster startup, shared memory)
reuse_port = True  # Enable SO_REUSEPORT for better load distribution

# Logging
accesslog = '-'  # Log to stdout (Railway captures this)
errorlog = '-'  # Log errors to stdout
loglevel = 'warning'  # Only log warnings and errors
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

proc_name = 'habsim'  # Process name for system monitoring

def post_fork(server, worker):
    """Initialize each worker process after forking.
    
    Each worker needs its own cache trim thread since they have separate memory spaces.
    The flag is reset so each worker starts its own thread (prevents conflicts).
    Also sets up access log filtering to suppress /sim/status requests.
    """
    # Set up access log filtering for this worker
    class StatusLogFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return '/sim/status' not in msg
    
    access_logger = logging.getLogger('gunicorn.access')
    access_logger.addFilter(StatusLogFilter())
    
    try:
        import simulate
        was_already_started = simulate._cache_trim_thread_started
        simulate._cache_trim_thread_started = False
        simulate._start_cache_trim_thread()
        if was_already_started:
            print(f"[WORKER {worker.pid}] Cache trim thread restarted in post_fork", flush=True)
        else:
            print(f"[WORKER {worker.pid}] Cache trim thread started in post_fork", flush=True)
    except Exception as e:
        print(f"[WORKER {worker.pid}] Failed to start cache trim thread: {e}", flush=True)

