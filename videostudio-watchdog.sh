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
ROOT="$(cd "$(dirname "$0")" && pwd)"
FAILURE_FILE="${VIDEOSTUDIO_WATCHDOG_STATE_FILE:-$ROOT/service/.watchdog-failures}"
FAILURES_REQUIRED="${VIDEOSTUDIO_WATCHDOG_FAILURES_REQUIRED:-3}"
CURL_BIN="${VIDEOSTUDIO_WATCHDOG_CURL_BIN:-curl}"
LAUNCHCTL_BIN="${VIDEOSTUDIO_WATCHDOG_LAUNCHCTL_BIN:-launchctl}"

case "$FAILURES_REQUIRED" in
  ''|*[!0-9]*) FAILURES_REQUIRED=3 ;;
esac
if [ "$FAILURES_REQUIRED" -lt 2 ]; then FAILURES_REQUIRED=3; fi

if "$CURL_BIN" -fsS --max-time 10 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  rm -f "$FAILURE_FILE"
  exit 0
fi

failures=0
if [ -f "$FAILURE_FILE" ]; then
  read -r failures < "$FAILURE_FILE" || failures=0
fi
case "$failures" in
  ''|*[!0-9]*) failures=0 ;;
esac
failures=$((failures + 1))
mkdir -p "$(dirname "$FAILURE_FILE")"
tmp="${FAILURE_FILE}.$$"
printf '%s\n' "$failures" > "$tmp"
mv "$tmp" "$FAILURE_FILE"

if [ "$failures" -lt "$FAILURES_REQUIRED" ]; then
  echo "[watchdog] $(date '+%Y-%m-%d %H:%M:%S') health probe failed (${failures}/${FAILURES_REQUIRED}); waiting for confirmation"
else
  echo "[watchdog] $(date '+%Y-%m-%d %H:%M:%S') health probe failed ${failures} consecutive times — restarting ${LABEL}"
  "$LAUNCHCTL_BIN" kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true
fi
