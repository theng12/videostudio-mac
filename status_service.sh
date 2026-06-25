#!/bin/bash
#
# Show whether the Video Studio KH always-on service is actually running:
#   - is the server agent loaded + running (or crash-looping on a port clash)?
#   - is the watchdog scheduled?  (PERIODIC — runs ~1s every 60s; "not running"
#     between checks is normal, not broken.)
#   - is the server answering /api/health right now?
#   - port-conflict detection (both the service AND Pinokio's Start running).
#   - the tail of the service log.
#
ROOT="$(cd "$(dirname "$0")" && pwd)"
UID_NUM="$(id -u)"
SRV="com.kh.videostudio.server"
WD="com.kh.videostudio.watchdog"
PORT=47872

echo "═══════════ Video Studio KH — service status ═══════════"
echo ""

if [ ! -f "$ROOT/service/.installed" ]; then
  echo "Startup service is NOT installed on this Mac."
  echo "Use the 'Install as Startup Service' button to set it up."
  exit 0
fi

# Port conflict? The service can't bind if another process (almost always
# Pinokio's own "Start") already holds the port — launchd then crash-loops.
CONFLICT=0
if tail -n 30 "$ROOT/logs/service/server.err.log" 2>/dev/null | grep -q "address already in use"; then
  CONFLICT=1
fi

echo "▸ Server agent  ($SRV)"
if launchctl print "gui/$UID_NUM/$SRV" >/dev/null 2>&1; then
  pid="$(launchctl print "gui/$UID_NUM/$SRV" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{print $2; exit}')"
  if [ "$CONFLICT" -eq 1 ]; then
    echo "   ⚠️  loaded, but CAN'T BIND port $PORT — crash-looping (see conflict note below)"
  elif [ -n "$pid" ]; then
    echo "   ✓ loaded · running (pid $pid)"
  else
    echo "   ✓ loaded · not running right now — launchd will (re)start it"
  fi
else
  echo "   ✗ not loaded"
fi
echo ""
echo "▸ Watchdog agent ($WD)  — periodic, fires every 60s"
if launchctl print "gui/$UID_NUM/$WD" >/dev/null 2>&1; then
  echo "   ✓ loaded · scheduled (it runs for ~1s each minute, so 'not running'"
  echo "             between checks is normal — it's working)"
else
  echo "   ✗ not loaded"
fi
echo ""
echo "▸ Live health check — http://127.0.0.1:$PORT/api/health   ← the real test"
if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
  echo ""
  if [ "$CONFLICT" -eq 1 ]; then
    echo "   ⚠️  Port $PORT is answering — but it's the OTHER instance (Pinokio's"
    echo "      Start), NOT the service. See the conflict fix below."
  else
    echo "   ✅ RUNNING — the server is up and responding on port $PORT."
  fi
else
  echo "   ⏳/❌ Not responding yet on port $PORT."
  echo "      (Still starting, stopped, or the port is taken by Pinokio's Start.)"
fi
echo ""
if [ "$CONFLICT" -eq 1 ]; then
  echo "──────────────────────────────────────────────────────────────"
  echo "⚠️  PORT CONFLICT — you're running BOTH the service AND Pinokio's Start."
  echo "   The service can't take port $PORT because Pinokio's Start already has"
  echo "   it, so the service is stuck restarting. Pick ONE runner:"
  echo ""
  echo "   ▸ To let the SERVICE run it (recommended): stop Pinokio's Start —"
  echo "       pkill -f 'uvicorn backend.main:app.*$PORT'"
  echo "     …or in Pinokio click the running app and Stop it. Within ~10s the"
  echo "     service grabs the port and runs on its own (and survives reboots)."
  echo ""
  echo "   ▸ To go back to manual Pinokio Start instead: click 'Uninstall"
  echo "     Startup Service'."
  echo "──────────────────────────────────────────────────────────────"
  echo ""
fi
echo "▸ Recent server log (logs/service/server.log)"
tail -n 15 "$ROOT/logs/service/server.log" 2>/dev/null | sed 's/^/   /' || echo "   (no log yet)"
echo ""
echo "════════════════════════════════════════════════════════"
