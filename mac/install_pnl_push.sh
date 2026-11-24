#!/bin/bash
# Install the P&L push job (every 10 min) on this Mac. Read-only on every broker.
# Prereq: the same token in Keychain here AND as PNL_PUSH_TOKEN on the server:
#   security add-generic-password -U -s alpha-markets-pnl-push -a "$USER" -w "<token>"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.alpha-markets.pnl-push
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/alpha-markets"; mkdir -p "$LOGDIR"
security find-generic-password -s alpha-markets-pnl-push -w >/dev/null || { echo "no push token in Keychain"; exit 1; }
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$HERE/pnl_push.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>ALPHA_PNL_URL</key><string>${ALPHA_PNL_URL:-https://alpha-markets.onrender.com}</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/pnl_push.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/pnl_push.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; heartbeat: ~/Library/Application Support/alpha-markets/pnl_push.heartbeat.json"
