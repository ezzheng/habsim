# Gunicorn configuration file optimized for Railway (32GB RAM, 32 vCPU)
import os

# Server socket
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 512

# Worker processes - OPTIMIZED for CPU without increasing memory
# Balance: Fewer workers (less memory) + More threads (shared memory, better CPU usage)
# Threads share memory within a process, so this maximizes CPU usage without RAM cost
workers = 4  # 4 workers to minimize memory duplication (4 workers × 8 threads = 32 concurrent capacity)
worker_class = 'gthread'  # Use threads for I/O-bound tasks (NumPy releases GIL for computation)
threads = 8  # 8 threads per worker = 32 concurrent capacity (matches 32 CPUs, shared memory)
worker_connections = 500
max_requests = 1000  # Higher limit since we have more memory headroom
max_requests_jitter = 100
timeout = 300  # 5 minutes for long-running ensemble simulations
keepalive = 10

# Memory optimization for 32GB RAM
preload_app = True  # Share memory between workers (efficient for large caches)
reuse_port = True

# Logging
# Railway treats stderr as errors, so we set loglevel to 'warning' to only log warnings/errors to stderr
# INFO logs (normal operation) won't appear in Railway's error view
accesslog = '-'  # Access logs go to stdout (Railway treats as normal)
errorlog = '-'   # Error logs go to stderr (Railway treats as errors) - only WARNING/ERROR now
loglevel = 'warning'  # Only log WARNING and ERROR to stderr (Railway error view)
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'habsim'

def on_starting(server):
    """Called just before the master process is initialized."""
    # Note: These startup messages will still appear but Railway may show them as errors
    # This is expected - Railway treats all stderr output as errors, but these are normal startup logs
    # With loglevel='warning', INFO logs are suppressed, but on_starting uses server.log which bypasses filters
    # Consider these startup messages as informational only
    pass  # Suppress startup INFO logs to avoid Railway showing them as errors

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    # Reduced verbosity - worker spawn is normal, no need to log each one
    pass

