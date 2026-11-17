# Gunicorn settings for VoiceGuard / the CMS portal (Azure App Service).
#
# Azure startup command:   gunicorn --config gunicorn.conf.py server:app
# (Anything also written on the command line overrides this file, so keep the
#  command exactly that and change the numbers here.)
#
# Nov 17 2026 the site hung for two hours. What changed here because of it:
#   * workers 3 (was 2): ~50 agents now poll the portal all day.
#     Each worker keeps its own database connections, and the Postgres server
#     has a connection limit, so more is not automatically better — this can
#     be raised with WEB_CONCURRENCY in the app settings.
#   * max_requests 50000 (was 3000): workers were being replaced every 2–3
#     minutes, and every replacement re-ran start-up work.
#   * timeout 120 (was 600): a worker whose main loop stops is replaced after
#     2 minutes instead of 10. (Stuck REQUESTS inside a live worker are handled
#     by the watchdog in server.py, which gunicorn itself cannot see.)
#   * worker_tmp_dir /dev/shm: gunicorn's heartbeat file in memory instead of
#     Azure's slow shared disk, which can cause false "WORKER TIMEOUT"s.
import os

bind = '0.0.0.0:' + os.getenv('PORT', '8000')

workers = int(os.getenv('WEB_CONCURRENCY', '3'))
worker_class = 'gthread'
threads = int(os.getenv('GUNICORN_THREADS', '8'))

timeout = int(os.getenv('GUNICORN_TIMEOUT', '120'))
graceful_timeout = 30          # a worker asked to restart gets 30 s to finish what it is doing
keepalive = 5

max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '50000'))
max_requests_jitter = 5000     # so the workers don't all restart at the same moment

worker_tmp_dir = '/dev/shm'

# the watchdog in server.py reads this to know how many threads a worker has
raw_env = ['GUNICORN_THREADS=%d' % threads]

accesslog = None               # Azure already logs every request
errorlog = '-'
loglevel = 'info'
