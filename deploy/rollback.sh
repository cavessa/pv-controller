#!/usr/bin/env bash
# Zurück auf den vorherigen (oder einen angegebenen) Stand: deploy/rollback.sh [sha]
set -euo pipefail
PROD="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$PROD"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  [ "$(wc -l <.deploy_history)" -ge 2 ] || { echo "Kein vorheriger Stand in .deploy_history"; exit 1; }
  TARGET="$(tail -n 2 .deploy_history | head -n 1)"
fi
echo "Rollback auf ${TARGET:0:9}"
FORCE=1 exec "$PROD/deploy/deploy.sh" "$TARGET"
