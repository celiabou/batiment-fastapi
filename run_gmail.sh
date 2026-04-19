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

export SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"
export SMTP_PORT="${SMTP_PORT:-587}"
export SMTP_SSL="${SMTP_SSL:-false}"
export SMTP_STARTTLS="${SMTP_STARTTLS:-true}"
export SMTP_USER="${SMTP_USER:-divclass72@gmail.com}"
export SMTP_FROM_EMAIL="${SMTP_FROM_EMAIL:-devis@eurobatservices.com}"
export SMTP_FROM_NAME="${SMTP_FROM_NAME:-EUROBAT SERVICES}"
export SMTP_REPLY_TO="${SMTP_REPLY_TO:-devis@eurobatservices.com}"
export INTERNAL_REPORT_EMAIL="${INTERNAL_REPORT_EMAIL:-devis@eurobatservices.com}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:8081}"

if [ -z "${SMTP_PASSWORD:-}" ]; then
  read -r -s -p "Mot de passe d'application Gmail (16 caracteres): " SMTP_PASSWORD
  echo
  export SMTP_PASSWORD
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
  read -r -p "OPENAI_API_KEY: " OPENAI_API_KEY
  export OPENAI_API_KEY
fi

exec "$PY_BIN" -m uvicorn app:app --host 127.0.0.1 --port 8081
