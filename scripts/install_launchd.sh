#!/bin/zsh
# Install the every-5-minutes local poller (survives reboots; runs whenever the Mac is awake).
set -e
PLIST_SRC="$(cd "$(dirname "$0")/.." && pwd)/launchd/com.srikar.internship-sniper.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.srikar.internship-sniper.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "installed — poller runs every 5 min. Logs: out/launchd.log"
echo "to uninstall: launchctl unload $PLIST_DST && rm $PLIST_DST"
