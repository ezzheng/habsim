"""
Gunicorn configuration for HABSIM deployment on Railway.

Optimized for high-concurrency ensemble simulations with 4 workers × 8 threads = 32 concurrent capacity.
Each worker runs its own cache trim thread for memory management.

ARCHITECTURE: Multi-process + multi-threaded
- 4 worker processes (isolated memory, crash isolation)
- 8 threads per worker (I/O-bound operations benefit from threading)
- Total: 32 concurrent request capacity
- Each worker has separate simulator cache (not shared across processes)
"""
import os
import logging

# Network binding
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"  # Bind to all interfaces, use PORT env var or default 8000
backlog = 512  # Maximum pending connections (queue size before rejecting)

# Worker configuration
workers = 4  # 4 worker processes for parallel request handling
# Use thread-based workers (gthread) instead of sync workers
# Threads share memory within process (faster), processes are isolated (safer)
worker_class = 'gthread'  # Thread-based workers (better for I/O-bound operations like S3 downloads)
threads = 8  # 8 threads per worker (4 workers × 8 threads = 32 concurrent capacity)
worker_connections = 200  # Maximum simultaneous clients per worker (rarely reached)

# Worker lifecycle
max_requests = 800  # Restart worker after handling this many requests (prevents memory leaks)
# Jitter randomizes restart timing to avoid thundering herd (all workers restarting at once)
max_requests_jitter = 80  # Randomize restart: 800 ± 80 requests

# Timeouts
timeout = 900  # 15 minutes - ensemble simulations can take 5-15 minutes
# Must be longer than longest expected ensemble run (10 minutes + buffer)
keepalive = 30  # Keep connections alive for 30 seconds (reduces connection overhead)

# Application loading
preload_app = True  # Load app before forking workers (faster startup, shared memory for code)
# CRITICAL: With preload_app=True, module-level code runs once in master process
# Each worker then forks, getting a copy of the preloaded app
reuse_port = True  # Enable SO_REUSEPORT for better load distribution across workers

# Logging
accesslog = '-'  # Log to stdout (Railway captures this)
errorlog = '-'  # Log errors to stdout
loglevel = 'warning'  # Only log warnings and errors (reduces log noise)
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

proc_name = 'habsim'  # Process name for system monitoring

def post_fork(server, worker):
    """
    Initialize each worker process after forking.
    
    CRITICAL: Each worker needs its own cache trim thread since they have separate
    memory spaces (Gunicorn uses processes, not just threads). The flag is reset
    so each worker starts its own thread (prevents conflicts from preload_app=True).
    
    Also sets up access log filtering to suppress /sim/status requests (polled every 5s).
    """
    # Set up access log filtering for this worker
    # Suppresses /sim/status logs (polled every 5s, creates log spam)
    class StatusLogFilter(logging.Filter):
        def filter(self, record):
            return '/sim/status' not in record.getMessage()
    
    access_logger = logging.getLogger('gunicorn.access')
    access_logger.addFilter(StatusLogFilter())
    
    # CRITICAL: Reset cache trim thread flag and start thread for this worker
    # With preload_app=True, module-level code runs in master process, so flag
    # might already be True. We reset it so each worker starts its own thread.
    try:
        import simulate
        was_already_started = simulate._cache_trim_thread_started
        simulate._cache_trim_thread_started = False  # Reset flag for this worker
        simulate._start_cache_trim_thread()  # Start thread in this worker process
        if was_already_started:
            print(f"[WORKER {worker.pid}] Cache trim thread restarted in post_fork", flush=True)
        else:
            print(f"[WORKER {worker.pid}] Cache trim thread started in post_fork", flush=True)
    except Exception as e:
        print(f"[WORKER {worker.pid}] Failed to start cache trim thread: {e}", flush=True)

