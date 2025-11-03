# Gunicorn configuration file optimized for Render (2GB RAM, 1 CPU)
import os

# Server socket
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 512

# Worker processes - CONSERVATIVE for 2GB RAM, 1 CPU
# With 1 CPU, we use 2 workers max to avoid context switching overhead
workers = 2  # 2 workers for 1 CPU (allows graceful restarts)
worker_class = 'gthread'  # Use threads for I/O-bound tasks
threads = 2  # 2 threads per worker = 4 concurrent requests total
worker_connections = 500
max_requests = 800  # Restart workers to prevent memory leaks
max_requests_jitter = 100
timeout = 120  # Allow 2 minutes for long-running simulations
keepalive = 5

# Memory optimization for 2GB limit
# Each worker: ~400-600MB (with GEFS cache)
# 2 workers = ~1.2GB, leaving 800MB buffer for OS + cache
preload_app = True  # Share memory between workers (critical for 2GB!)
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
    server.log.info("Starting HABSIM server with optimized configuration")
    server.log.info(f"Workers: {workers}, Threads per worker: {threads}")

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")

