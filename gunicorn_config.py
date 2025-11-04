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
# Note: Gunicorn's "errorlog" is actually for all application logs (INFO/WARNING/ERROR),
# not just errors. Railway may display these as "error" level in their UI, but they're normal.
accesslog = '-'
errorlog = '-'
loglevel = 'info'  # Log INFO and above (INFO, WARNING, ERROR)
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'habsim'

def on_starting(server):
    """Called just before the master process is initialized."""
    # Using server.log instead of app.logger to avoid duplicate logs
    server.log.info("Starting HABSIM server with Railway configuration (32GB RAM, 32 CPUs - optimized for speed)")
    server.log.info(f"Workers: {workers}, Threads per worker: {threads}, Max concurrent: {workers * threads}")

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    # Reduced verbosity - worker spawn is normal, no need to log each one
    pass

