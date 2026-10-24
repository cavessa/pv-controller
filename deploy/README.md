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

Secrets: `config.json` und ihre Backups enthalten Tokens/API-Keys und sind nur für den Besitzer lesbar (`600`);
`deploy.sh` stellt das bei jedem Lauf sicher, die Weboberfläche und `make-dev.sh` legen Backups direkt so an.
Cron und `pvcontroller-web` müssen als Besitzer der Dateien laufen.

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

**Wartung:** `python3 deploy/maintenance/prune_once.py` (Dry-Run) bzw. `--apply` räumt `cascade_log` einmalig auf (Snapshot, Lock, VACUUM, integrity_check). Danach hält `db.prune()` im Minuten-Lauf (täglich ab 03:00) die Tabellen klein.

## SQLite im WAL-Modus

`db.init_db()` schaltet `pvcontroller.db` einmalig auf `journal_mode=WAL` (steckt danach in der Datei). Dadurch warten Web-Leser nicht auf den Minutenlauf, und das Schreiben braucht weniger fsync. Neben der DB liegen jetzt `pvcontroller.db-wal` und `-shm` (gleiche Rechte wie die DB, per `.gitignore` ausgeschlossen).
- **Nie die DB-Datei allein mit `cp`/`tar`/`rsync` kopieren** – Daten, die nur im `-wal` stehen, gehen sonst verloren. Immer die Python-Backup-API nehmen (`sqlite3.connect(quelle).backup(ziel)`, wie `backup.sh`, `prune_once.py` und der Umzug oben) oder vorher `PRAGMA wal_checkpoint(TRUNCATE)`.
- Ein per Backup-API erzeugter Snapshot ist eine ganz normale, eigenständige DB-Datei (`integrity_check`, Restore = Datei an die Stelle kopieren, solange nichts schreibt). Restore-Test: `tests/test_web_speed.py` (`WalTest`).
- Docker: `entrypoint.sh` verlinkt `/app/pvcontroller.db` auf `/data`; SQLite legt `-wal`/`-shm` neben die echte Datei – das Verzeichnis `/data` muss als Volume gemountet sein (nicht nur die einzelne Datei).

## Log-Rotation (Prod-`cron.log`, ohne root)

`deploy/logrotate.conf`: täglich, 14 Generationen, komprimiert. Der Cron hängt mit `>>` an und öffnet die Datei bei jedem Lauf neu, deshalb genügt Umbenennen (kein `copytruncate`). Einmalig in der User-Crontab eintragen (vorher `crontab -l > ~/crontab.bak`):

```
20 4 * * * /usr/sbin/logrotate -s $HOME/.logrotate.state $HOME/pvcontroller-prod/deploy/logrotate.conf >/dev/null 2>&1
```

Prüfen ohne Änderung: `logrotate -d -s /tmp/lr.state ~/pvcontroller-prod/deploy/logrotate.conf`.

## Offline-Meldungen der Wechselrichter

`clients/solax_client.py` meldet einen Totalausfall nur beim Wechsel (offline / wieder erreichbar) und danach höchstens alle 30 min („weiterhin nicht erreichbar, seit …"); die Einzelversuche stehen auf DEBUG. Der Zustand liegt je Prozessart in `data/offline_log_state.json` (jeder Cron-Lauf ist ein neuer Prozess). Nach einem Totalausfall wird das Gerät 30 s lang nicht erneut abgefragt (Web und Cron), und Cache-Daten gelten nur 180 s als Ersatz – ältere Werte zählen als „nicht erreichbar".
