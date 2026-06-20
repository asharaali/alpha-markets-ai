#!/usr/bin/env bash
# Alpha Markets AI — one-command launcher.
set -e
cd "$(dirname "$0")/backend"

if [ ! -d ".venv" ]; then
  echo "→ Creating virtual environment…"
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "→ Installing dependencies…"
pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "→ Created .env (running in DEMO mode; add ODDS_API_KEY for live odds)."
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "localhost")
echo "→ Starting Alpha Markets AI"
echo "   On this Mac:     http://localhost:8000"
echo "   On your phone:   http://$IP:8000   (same WiFi)"
exec uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
