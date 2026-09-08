#!/usr/bin/env bash
# Install Kalshi API credentials into backend/.env.
#
# The private key is read straight from the file Kalshi gave you and written into .env.
# It is never printed, echoed, or passed as a shell argument, so it does not end up in
# your shell history or in a Claude Code transcript.
#
#   ./install_kalshi_key.sh <key-id> <path-to-private-key.pem>
#
set -euo pipefail
cd "$(dirname "$0")"

KEY_ID="${1:-}"
KEY_PATH="${2:-}"

if [ -z "$KEY_ID" ] || [ -z "$KEY_PATH" ]; then
  echo "usage: ./install_kalshi_key.sh <key-id> <path-to-private-key.pem>" >&2
  exit 1
fi
if [ ! -f "$KEY_PATH" ]; then
  echo "error: no such file: $KEY_PATH" >&2
  exit 1
fi
if ! grep -q "BEGIN .*PRIVATE KEY" "$KEY_PATH"; then
  echo "error: $KEY_PATH does not look like a PEM private key." >&2
  echo "       Kalshi gives you a file starting with -----BEGIN RSA PRIVATE KEY-----" >&2
  exit 1
fi

[ -f .env ] || cp .env.example .env

# Back up the existing .env, but only ever to a path the gitignore already covers, and
# lock it down immediately. A backup taken AFTER a previous successful install contains
# the private key, so a stray `git add -A` would otherwise publish it.
BACKUP=".env.backup.$(date +%Y%m%d%H%M%S)"
cp .env "$BACKUP"
chmod 600 "$BACKUP"
# Keep only the two most recent backups; old copies of a rotated key are pure liability.
ls -1t .env.backup.* 2>/dev/null | tail -n +3 | while read -r old; do rm -f "$old"; done

# Fold the PEM into a single line with literal \n, which is what the config loader expects.
PEM_ONELINE="$(awk 'BEGIN{ORS="\\n"} {print}' "$KEY_PATH")"

PEM_ONELINE="$PEM_ONELINE" python3 - "$KEY_ID" <<'PY'
import os, sys, pathlib
key_id = sys.argv[1]
pem = os.environ["PEM_ONELINE"]
path = pathlib.Path(".env")
lines = [l for l in path.read_text().splitlines()
         if not l.startswith(("KALSHI_KEY_ID=", "KALSHI_PRIVATE_KEY="))]
lines += [f"KALSHI_KEY_ID={key_id}", f"KALSHI_PRIVATE_KEY={pem}"]
path.write_text("\n".join(lines) + "\n")
print("Wrote KALSHI_KEY_ID and KALSHI_PRIVATE_KEY to backend/.env")
PY

chmod 600 .env
echo
echo "Permissions on .env set to 600 (owner-only)."
echo "backend/.env is gitignored — verify with: git check-ignore -v backend/.env"
echo
echo "Next: ./verify_kalshi.sh   (read-only balance check, places nothing)"
