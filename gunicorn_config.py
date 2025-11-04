# Gunicorn configuration file optimized for Railway (32GB RAM, 32 vCPU)
import os

# Server socket
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 512

# Worker processes - OPTIMIZED for SPEED (we have 32GB RAM, 32 CPUs - use them!)
# Balance between workers and threads for optimal parallelism
# More workers = better CPU utilization, but more RAM duplication
workers = 4  # 4 workers for better parallelism (4 workers × 8 threads = 32 concurrent capacity)
worker_class = 'gthread'  # Use threads for I/O-bound tasks
threads = 8  # 8 threads per worker = 32 concurrent capacity (matches 32 CPUs)
worker_connections = 500
max_requests = 1000  # Higher limit since we have more memory headroom
max_requests_jitter = 100
timeout = 300  # 5 minutes for long-running ensemble simulations
keepalive = 10

# Memory optimization for 32GB RAM
preload_app = True  # Share memory between workers (efficient for large caches)
reuse_port = True

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'habsim'

def on_starting(server):
    """Called just before the master process is initialized."""
    server.log.info("Starting HABSIM server with Railway configuration (32GB RAM, 32 CPUs - optimized for speed)")
    server.log.info(f"Workers: {workers}, Threads per worker: {threads}, Max concurrent: {workers * threads}")
    server.log.info("Optimized for speed using allocated resources (32GB RAM, 32 CPUs)")

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")

