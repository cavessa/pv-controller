#!/usr/bin/env bash
# Entwicklungs-Weboberfläche auf Port 8001 (nicht Produktion)
DEV="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DEV"
exec "$DEV/.venv/bin/uvicorn" web:app --host 0.0.0.0 --port 8001
