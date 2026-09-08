#!/usr/bin/env bash
# Alpha Markets — one-command launcher.
set -euo pipefail
cd "$(dirname "$0")/backend"

if [ ! -d ".venv" ]; then
  echo "→ Creating virtual environment…"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Installing dependencies…"
pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "→ Created .env. No API keys are needed — the research side runs on free public data."
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)
cat <<EOF

  Alpha Markets — NFL research terminal
  ─────────────────────────────────────────────
  On this machine:  http://localhost:8000
  On your phone:    http://$IP:8000   (same WiFi)

  First start downloads a few seasons of play-by-play (~100MB) and fits the
  model in the background. That takes about a minute; the interface says
  "calibrating" until it finishes.

EOF
exec uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
