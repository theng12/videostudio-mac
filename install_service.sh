#!/bin/bash
#
# Install Video Studio KH as an always-on macOS service (launchd LaunchAgent).
#
# Idempotent — safe to run repeatedly; it re-bootstraps cleanly. No sudo needed
# (LaunchAgents are per-user). The one-time system settings for full power-cut
# recovery (auto power-on, auto-login, FileVault off) are admin-level and are
# explained at the end + in the README; they are NOT done here.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
UID_NUM="$(id -u)"
LA="$HOME/Library/LaunchAgents"
SRV="com.kh.videostudio.server"
WD="com.kh.videostudio.watchdog"
PORT=47872
APPNAME="Video Studio KH"

mkdir -p "$LA" "$ROOT/logs/service" "$ROOT/service"
chmod +x "$ROOT/videostudio-serve.sh" "$ROOT/videostudio-watchdog.sh"

# ── server agent: boot-start + auto-restart on crash ──
cat > "$LA/$SRV.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SRV</string>
  <key>ProgramArguments</key>
  <array><string>$ROOT/videostudio-serve.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/service/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/service/server.err.log</string>
</dict>
</plist>
PLIST

# ── watchdog agent: every 60s, restart the server if /api/health is dead ──
cat > "$LA/$WD.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$WD</string>
  <key>ProgramArguments</key>
  <array><string>$ROOT/videostudio-watchdog.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/service/watchdog.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/service/watchdog.err.log</string>
</dict>
</plist>
PLIST

# (re)load both agents — bootout first so re-running picks up any changes
launchctl bootout  "gui/$UID_NUM/$SRV" 2>/dev/null || true
launchctl bootout  "gui/$UID_NUM/$WD"  2>/dev/null || true

# bootout is asynchronous — wait for each label to fully unload before we
# bootstrap again, or launchd returns "Bootstrap failed: 5: Input/output error".
_wait_gone() { for _ in $(seq 1 25); do launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1 || return 0; sleep 0.2; done; }
_wait_gone "$SRV"; _wait_gone "$WD"

# Take over the port: if you started the app via Pinokio's "Start", that
# instance is still holding port $PORT. The whole point of converting to a
# service is for the service to own it, so stop the old listener now (graceful
# TERM, then KILL any straggler). This makes "Start, then Install Service" just
# work instead of silently crash-looping on a port clash.
PORT_PIDS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PORT_PIDS" ]; then
  echo "Taking over port $PORT — stopping the current instance (Pinokio 'Start'):"
  for p in $PORT_PIDS; do echo "   • stopping pid $p"; kill "$p" 2>/dev/null || true; done
  sleep 2
  STRAGGLERS="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$STRAGGLERS" ]; then
    for p in $STRAGGLERS; do kill -9 "$p" 2>/dev/null || true; done
    sleep 1
  fi
  echo ""
fi

# retry once — bootstrap can still transiently fail right after a bootout.
_bootstrap() { launchctl bootstrap "gui/$UID_NUM" "$1" 2>/dev/null || { sleep 1; launchctl bootstrap "gui/$UID_NUM" "$1"; }; }
_bootstrap "$LA/$SRV.plist"
_bootstrap "$LA/$WD.plist"
launchctl kickstart "gui/$UID_NUM/$SRV" 2>/dev/null || true

touch "$ROOT/service/.installed"

echo ""
echo "✅ $APPNAME is now an always-on service on port $PORT."
echo "   • Starts automatically at login, restarts itself if it crashes, and a"
echo "     watchdog re-launches it if it ever stops responding to /api/health."
echo "   • Logs: $ROOT/logs/service/"
echo "   • Reach it over Tailscale/LAN at  http://<this-mac>:$PORT"
echo ""
echo "──────────────────────────────────────────────────────────────────────────"
echo "ONE-TIME Mac settings for full hands-off recovery after a POWER CUT"
echo "(admin-level — do these once per machine; NOT done by this button):"
echo ""
echo "  1. Power back on automatically when electricity returns:"
echo "         sudo pmset -a autorestart 1"
echo ""
echo "  2. Enable Automatic login"
echo "         System Settings ▸ Users & Groups ▸ Automatically log in as …"
echo "     WHY: the Apple GPU (Metal/MLX) is only available inside a logged-in"
echo "     session. Without auto-login the Mac boots to the login screen and the"
echo "     models can't use the GPU."
echo ""
echo "  3. Turn FileVault OFF"
echo "         System Settings ▸ Privacy & Security ▸ FileVault"
echo "     WHY: with FileVault on, a reboot stops at the encrypted-disk password"
echo "     screen and never reaches auto-login — so the server never comes back."
echo ""
echo "  Use the service OR Pinokio's Start button — not both (they share port $PORT)."
echo "──────────────────────────────────────────────────────────────────────────"
