#!/bin/bash
set -e

# Start Xvfb virtual display on :99
Xvfb :99 -screen 0 1920x1080x24 -ac &
export DISPLAY=:99

echo "Xvfb started on DISPLAY=:99"

# Wait for Xvfb to initialise
sleep 2

# Start cron daemon
service cron start
echo "Cron started"

# Keep container alive and stream logs
touch /var/log/avibm.log
echo "AVIBM monitor running — tailing log..."
tail -f /var/log/avibm.log
