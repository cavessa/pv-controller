#!/usr/bin/env bash
# Deploy nach ~/pvcontroller-prod: holt <ref> (Default origin/main) von GitHub, prüft ihn
# in einem Wegwerf-Worktree (Tests + produktive config.json gegen den NEUEN Code), schaltet
# dann atomar um (exklusiver Lock gegen den Cron), startet den Web-Dienst neu und prüft
# danach Health + den nächsten Cron-Lauf. Schlägt der Smoke-Test fehl, geht es automatisch
# zurück auf den vorherigen Stand.
#
#   deploy/deploy.sh            # origin/main
#   deploy/deploy.sh <sha|tag>  # bestimmter Stand
set -euo pipefail

PROD="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROD"
REF="${1:-origin/main}"
LOCK=/tmp/pvc-code.lock
SERVICE=pvcontroller-web
LOG="$PROD/deploy.log"
HIST="$PROD/.deploy_history"
PY="$PROD/.venv/bin/python"
PORT="${PVC_PORT:-8000}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }
die() { log "ABBRUCH: $*"; exit 1; }

# Neustart: sudoers erlaubt genau diesen Befehl (deploy/sudoers.pvcontroller). Für Tests überschreibbar.
RESTART_CMD="${PVC_RESTART_CMD:-sudo -n /usr/bin/systemctl restart $SERVICE}"
SMOKE_WAIT="${PVC_SMOKE_WAIT:-90}"     # Sekunden auf den nächsten Cron-Lauf warten
restart_service() { $RESTART_CMD; }
app_log() { grep -o '"log_file": *"[^"]*"' "$PROD/config.json" | sed 's/.*: *"//;s/"$//'; }

wait_health() {   # bis zu 30 s auf /api/health warten
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

[ -x "$PY" ] || die ".venv fehlt ($PY)"
[ -f "$PROD/config.json" ] || die "config.json fehlt"
git diff --quiet || die "Produktionsverzeichnis hat lokale Änderungen (git status) – nichts überschrieben"

OLD="$(git rev-parse HEAD)"
[ -s "$HIST" ] || echo "$OLD" >"$HIST"     # erster Lauf: aktueller Stand als Rollback-Ziel
git fetch --quiet --tags --prune origin
NEW="$(git rev-parse --verify "$REF^{commit}")" || die "Ref '$REF' nicht gefunden"
if [ "$NEW" = "$OLD" ] && [ -z "${FORCE:-}" ]; then log "Bereits aktuell: ${NEW:0:9}"; exit 0; fi
log "Deploy ${OLD:0:9} → ${NEW:0:9} ($REF)"
git log --oneline "$OLD..$NEW" 2>/dev/null | head -15 | sed 's/^/    /' | tee -a "$LOG" || true

# 1) Vorabprüfung im Wegwerf-Worktree (Prod bleibt unberührt)
TMP="$(mktemp -d /tmp/pvc-deploy.XXXXXX)"
# Bei jedem Ende (ok, Rollback, Abbruch): Worktree entfernen und verwaiste Verweise aufräumen
trap 'git -C "$PROD" worktree remove --force "$TMP/wt" >/dev/null 2>&1 || true; rm -rf "$TMP"; git -C "$PROD" worktree prune >/dev/null 2>&1 || true' EXIT
git worktree add --quiet --detach "$TMP/wt" "$NEW"
( cd "$TMP/wt"
  log "Tests …"
  "$PY" -m unittest discover -s tests -t . >"$TMP/tests.log" 2>&1 || { tail -25 "$TMP/tests.log"; exit 11; }
  log "Prüfe produktive config.json gegen neuen Code …"
  "$PY" -c "from config import load_config; load_config('$PROD/config.json')" || exit 12
) || die "Vorabprüfung fehlgeschlagen (Tests oder Config) – Produktion unverändert"

# 2) Umschalten (exklusiv: wartet auf laufenden Cron-Lauf, Cron wartet kurz)
if ! git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  log "requirements.txt geändert → pip install"
  "$PROD/.venv/bin/pip" install -q -r <(git show "$NEW:requirements.txt")
fi
exec 9>"$LOCK"
flock -x -w 180 9 || die "Lock nicht erhalten (Cron-Lauf hängt?)"
git checkout --quiet --detach "$NEW"
flock -u 9
echo "$NEW" >>"$HIST"

# 3) Dienst neu starten + Smoke-Test (Health + ein Cron-Lauf nach dem Neustart ohne Fehler)
restart_service || die "Neustart fehlgeschlagen (sudoers-Eintrag vorhanden? deploy/sudoers.pvcontroller)"
START="$(date '+%F %T')"
ok=""
if wait_health; then
  log "Health ok; warte auf den nächsten Cron-Lauf (max. ${SMOKE_WAIT} s) …"
  LF="$(app_log)"
  for _ in $(seq 1 "$SMOKE_WAIT"); do
    sleep 1
    rc=0; "$PY" "$PROD/deploy/smoke_check.py" "$LF" "$START" || rc=$?
    if [ "$rc" -eq 0 ]; then ok=1; break; fi
    if [ "$rc" -eq 2 ]; then break; fi      # Fehler im Lauf → sofort zurück
  done
fi

if [ -z "${ok:-}" ]; then
  log "Smoke-Test FEHLGESCHLAGEN → zurück auf ${OLD:0:9}"
  exec 9>"$LOCK"; flock -x -w 180 9
  git checkout --quiet --detach "$OLD"; flock -u 9
  restart_service || true
  head -n -1 "$HIST" >"$HIST.tmp" && mv "$HIST.tmp" "$HIST"
  die "Deploy zurückgerollt auf ${OLD:0:9}"
fi
log "Deploy ok: ${NEW:0:9}"
