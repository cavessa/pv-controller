#!/bin/sh
set -e

# Controller-Loop im Hintergrund: läuft jede Minute wie der bisherige Cron-Job
(while true; do
    python main.py >> /app/logs/pv-controller.log 2>&1
    sleep 60
done) &

# Web-Server im Vordergrund (PID 1, empfängt SIGTERM von Docker)
exec uvicorn web:app --host 0.0.0.0 --port 8000
