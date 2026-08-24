# Gunicorn configuration optimized for fast large file uploads
import os

# Render and other PaaS set PORT; default 5000 for local. Read in Python so PORT is always correct.
_port = os.environ.get("PORT", "5000")
bind = f"0.0.0.0:{_port}"

workers = 1  # Single worker to avoid memory contention
worker_class = "gthread"  # Threaded worker so status polling works during background processing
threads = 4  # Allow 4 concurrent requests per worker
timeout = 600  # 10 minutes for very large file uploads
keepalive = 10  # Longer keepalive for upload connections
max_requests = 0  # DISABLED — recycling kills the background queue worker thread and loses in-memory task state
max_requests_jitter = 0
preload_app = False  # Must be False: app.py starts background threads at module level,
                      # and preload_app=True loads the app in the master *before* fork,
                      # causing a POSIX fork+threads deadlock (child inherits locked mutexes).
                      # With False, the app loads inside the worker (after fork) so threads
                      # start safely in the process that will actually serve requests.


def post_fork(server, worker):
    """With preload_app=False the app hasn't been imported yet in the child.
    Module-level code in app.py handles all initialization (queue, threads,
    cleanup scheduler) when gunicorn imports the WSGI app, so nothing extra
    is needed here."""
    pass

# Disable reload in production (Render sets no FLASK_ENV or PRODUCTION)
reload = os.environ.get("FLASK_ENV") == "development"
# Disable reuse_port on Render so the port scanner can detect the listening socket
reuse_port = False

# Memory and resource limits optimized for uploads (skip /dev/shm if missing)
worker_tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else None
tmp_upload_dir = None
worker_rlimit_nofile = 65535  # Increase file descriptor limit

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Security - optimized for large uploads
limit_request_line = 8190
limit_request_fields = 200  # More fields for complex uploads
limit_request_field_size = 16380  # Larger field size for file metadata