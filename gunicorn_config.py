import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
backlog = 512

workers = 4
worker_class = 'gthread'
threads = 8

worker_connections = 200

max_requests = 800
max_requests_jitter = 80

timeout = 900

keepalive = 30

preload_app = True
reuse_port = True

accesslog = '-'
errorlog = '-'
loglevel = 'warning'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Custom access log filter to suppress /sim/status requests
def access_log_filter(status_code, path):
    """Filter out /sim/status requests from access logs"""
    if '/sim/status' in path:
        return False
    return True

proc_name = 'habsim'

def on_starting(server):
    pass

def post_fork(server, worker):
    """Reset cache trim thread flag so each worker starts its own thread."""
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

