#!/bin/bash
set -e

# Start Xvfb virtual display on :99
Xvfb :99 -screen 0 1920x1080x24 -ac &
export DISPLAY=:99
echo "Xvfb started on DISPLAY=:99"
sleep 2

# Start the webhook server (Railway keeps this process alive)
echo "Starting AVIBM webhook server..."
exec python3 master_monitor.py
