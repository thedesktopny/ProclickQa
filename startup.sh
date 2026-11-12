#!/bin/bash
# Start background worker
python worker.py &

# Start the web server.
# 2 processes x 8 threads each: before, each process answered ONE request at a
# time, so one slow page (or a stuck scoring batch) made everyone else wait.
# --max-requests restarts each process now and then, which hands back any
# memory it has crept up to; --timeout 180 restarts a process that is stuck.
gunicorn --bind=0.0.0.0:8000 --timeout 180 --workers 2 --threads 8 --worker-class gthread --max-requests 3000 --max-requests-jitter 300 server:app
