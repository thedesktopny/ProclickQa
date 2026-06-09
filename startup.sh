#!/bin/bash
# Start background worker
python worker.py &

# Start Flask server
gunicorn --bind=0.0.0.0:8000 --timeout 600 --workers 2 server:app
