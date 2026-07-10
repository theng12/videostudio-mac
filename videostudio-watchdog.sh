#!/bin/bash
#
# Video Studio KH — health watchdog for the always-on server.
#
# launchd's KeepAlive restarts the server if the PROCESS dies, but it can't tell
# when the server is alive-but-hung. This script (run every 60s by the watchdog
# LaunchAgent) pings /api/health and force-restarts the service if it stops
# answering — belt-and-suspenders self-healing.
#
PORT=47872
LABEL="com.kh.videostudio.server"

if ! curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "[watchdog] $(date '+%Y-%m-%d %H:%M:%S') no /api/health on :${PORT} — restarting ${LABEL}"
  launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true
fi
