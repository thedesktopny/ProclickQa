#!/bin/bash
# Start background worker
python worker.py &

# Start the web server. All its settings (workers, threads, time limits,
# restarts) live in gunicorn.conf.py — see the notes at the top of that file.
gunicorn --config gunicorn.conf.py server:app
