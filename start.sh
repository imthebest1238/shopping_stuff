#!/usr/bin/env bash
# Start the shopping agent (macOS / Linux). First run sets everything up.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (this takes a minute or two)..."
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
if [ -z "${SHOP_BROWSER_CHANNEL:-}" ] && ! grep -qs '^SHOP_BROWSER_CHANNEL=.' .env; then
  .venv/bin/python -m playwright install chromium
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! grep -qs '^ANTHROPIC_API_KEY=.' .env; then
  echo
  echo "One more step: open the file .env in this folder, paste your Anthropic API key"
  echo "after ANTHROPIC_API_KEY=  and run ./start.sh again."
  exit 1
fi

exec .venv/bin/python -m shopping_agent
