#!/bin/bash
#
# Force-restart the Video Studio KH always-on service (launchd kickstart -k).
# Useful if it's wedged or after you change something. KeepAlive normally
# handles crashes on its own; this is the manual "kick".
#
set -euo pipefail
UID_NUM="$(id -u)"
SRV="com.kh.videostudio.server"

if launchctl kickstart -k "gui/$UID_NUM/$SRV" 2>/dev/null; then
  echo "🔄 Restart signal sent to $SRV."
  echo "   Give it ~10s, then use 'Check Service Status' to confirm it's up."
else
  echo "⚠️  Couldn't kick the service — is it installed?"
  echo "   Use 'Install as Startup Service' first."
fi
