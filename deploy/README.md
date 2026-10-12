# Deploy (Produktion ↔ Entwicklung)

| | Entwicklung | Produktion |
|---|---|---|
| Verzeichnis | `~/pvcontroller` | `~/pvcontroller-prod` (Clone von GitHub, Stand = `origin/main`) |
| Cron | keiner | `deploy/cron.example` (Shared-Lock) |
| Web-Dienst | `deploy/dev-web.sh` (Port 8001) | `pvcontroller-web` (Port 8000) |
| Config/DB | eigene Kopien: `dry_run`, Zendure aus, `cascade_enabled=0` | echte Daten (nicht in Git) |

## Deploy

```
~/pvcontroller-prod/deploy/deploy.sh            # origin/main
~/pvcontroller-prod/deploy/deploy.sh <sha|tag>  # bestimmter Stand
~/pvcontroller-prod/deploy/rollback.sh          # vorheriger Stand
```
Ablauf: fetch → Wegwerf-Worktree: Unit-Tests + produktive `config.json` gegen den **neuen** Code laden
(bei Fehler bleibt Produktion unberührt) → Umschalten unter exklusivem Lock (wartet auf einen laufenden
Cron-Lauf) → `pip install` falls `requirements.txt` geändert → Neustart `pvcontroller-web` → Smoke-Test
(`/api/health` + ein vollständiger Cron-Lauf ohne „Unerwarteter Fehler"/Importfehler). Fehlschlag ⇒ automatisch
zurück auf den vorherigen Stand. Protokoll: `deploy.log`, Verlauf: `.deploy_history`.

Voraussetzung: `/etc/sudoers.d/pvcontroller` (siehe `sudoers.pvcontroller`) – erlaubt nur
`systemctl restart pvcontroller-web`.

## Umzug (einmalig)

1. **Du:** sudoers anlegen: `sudo visudo -f /etc/sudoers.d/pvcontroller` (Inhalt: `deploy/sudoers.pvcontroller`).
2. Clone + Venv (kein Eingriff in Produktion):
   `git clone git@github.com:cavessa/pv-controller.git ~/pvcontroller-prod && cd ~/pvcontroller-prod && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m unittest discover -s tests -t .`
3. Cutover (≈ 1 min ohne Cron):
   1. Cron-Zeile für `~/pvcontroller` auskommentieren, laufenden Lauf abwarten (`pgrep -f main.py`).
   2. Zustand kopieren: `config.json`, `temp_history.json`, `data/`; DB per Python-Backup-API (das `sqlite3`-CLI ist auf dem Pi nicht installiert): `python3 -c "import sqlite3;s=sqlite3.connect('pvcontroller.db');d=sqlite3.connect('$HOME/pvcontroller-prod/pvcontroller.db');s.backup(d)"`.
   3. **Du:** `sudo cp ~/pvcontroller-prod/deploy/pvcontroller-web.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart pvcontroller-web`.
   4. Neue Cron-Zeile aus `deploy/cron.example` eintragen.
   5. Prüfen: `/api/health`, nächster Cron-Lauf im Log, Dashboard, `deploy.sh` (sollte „Bereits aktuell" melden).
4. Auch `pvlog.py --mode hourly/daily` im Cron auf `~/pvcontroller-prod` umstellen.
5. Entwicklung: in `~/pvcontroller` `deploy/make-dev.sh` ausführen (Dry-Run, Zendure aus, Kaskade aus, eigenes Log).
6. Backup: `~/pvcontroller-prod/{config.json,pvcontroller.db,temp_history.json}` in `backup.sh` aufnehmen.

**Rückweg:** Cron-Zeile und `/etc/systemd/system/pvcontroller-web.service` wieder auf `~/pvcontroller` zeigen lassen (Dateien dort bleiben bis zur Freigabe unverändert), `daemon-reload`, Neustart.


**Alltag nach dem Umzug:** in `~/pvcontroller` (Dev, Dry-Run) entwickeln → committen → pushen (nach Freigabe) → `~/pvcontroller-prod/deploy/deploy.sh`.
