#!/usr/bin/env bash
# Macht das AKTUELLE Verzeichnis zur sicheren Entwicklungsumgebung (nach dem Umzug in
# ~/pvcontroller ausführen). Nichts davon betrifft Produktion (~/pvcontroller-prod).
#   - runtime.dry_run = true, Log in <dir>/dev.log
#   - Zendure aus (enabled/control_enabled = false)
#   - Kaskade aus (cascade_enabled = 0 in der DB dieses Verzeichnisses)
#   - Web-Port 8001 (nur über deploy/dev-web.sh)
set -euo pipefail
DEV="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DEV"
case "$DEV" in *-prod|*/pvcontroller-prod) echo "Das ist das Produktionsverzeichnis – Abbruch"; exit 1;; esac
umask 077   # config.json und Backups enthalten Secrets: neue Dateien nur für den Besitzer (600)
[ -f config.json ] && install -m 600 config.json "config.backup-$(date +%Y%m%d-%H%M%S).json"
python3 - <<PY
import json
p="config.json"; c=json.load(open(p,encoding="utf-8"))
c.setdefault("runtime",{})["dry_run"]=True
c["runtime"]["log_file"]="$DEV/dev.log"
if "zendure" in c:
    c["zendure"]["enabled"]=False; c["zendure"]["control_enabled"]=False
json.dump(c,open(p,"w",encoding="utf-8"),indent=2,ensure_ascii=False); open(p,"a").write("\n")
import os; os.chmod(p, 0o600)
print("config.json: dry_run=true, Zendure aus, Log → dev.log")
PY
if [ -f pvcontroller.db ]; then
  sqlite3 pvcontroller.db "UPDATE cascade_settings SET value='0' WHERE key='cascade_enabled';" 2>/dev/null \
    || python3 -c "import sqlite3;c=sqlite3.connect('pvcontroller.db');c.execute(\"UPDATE cascade_settings SET value='0' WHERE key='cascade_enabled'\");c.commit()"
  echo "pvcontroller.db: cascade_enabled=0"
fi
echo "Fertig. Dev-Web: deploy/dev-web.sh (Port 8001). Den Cron-Eintrag für dieses Verzeichnis entfernen!"
