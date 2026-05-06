#!/bin/sh
set -e

mkdir -p /data/logs

# Redirect persistent files into /data via symlinks
ln -sf /data/pvcontroller.db     /app/pvcontroller.db
ln -sf /data/temp_history.json   /app/temp_history.json
rm -rf /app/logs && ln -sf /data/logs /app/logs

cron

exec uvicorn web:app --host 0.0.0.0 --port 8000
