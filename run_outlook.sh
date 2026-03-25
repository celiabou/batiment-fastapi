#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PY_BIN=""
if [ -x ".venv/bin/python" ]; then
  PY_BIN=".venv/bin/python"
elif [ -x "../.venv/bin/python" ]; then
  PY_BIN="../.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="$(command -v python3)"
else
  echo "Python introuvable. Installe Python ou active ton environnement virtuel."
  exit 1
fi

export SMTP_HOST="${SMTP_HOST:-smtp.office365.com}"
export SMTP_PORT="${SMTP_PORT:-587}"
export SMTP_STARTTLS="${SMTP_STARTTLS:-true}"
export SMTP_USER="${SMTP_USER:-celia.b@keythinkers.fr}"
export SMTP_FROM_EMAIL="${SMTP_FROM_EMAIL:-celia.b@keythinkers.fr}"
export SMTP_FROM_NAME="${SMTP_FROM_NAME:-Renovation Batiment IA}"
export INTERNAL_REPORT_EMAIL="${INTERNAL_REPORT_EMAIL:-celia.b@keythinkers.fr}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:8081}"

if [ -z "${SMTP_PASSWORD:-}" ]; then
  read -r -s -p "Mot de passe SMTP Outlook (celia.b@keythinkers.fr): " SMTP_PASSWORD
  echo
  export SMTP_PASSWORD
fi

exec "$PY_BIN" -m uvicorn app:app --host 127.0.0.1 --port 8081
