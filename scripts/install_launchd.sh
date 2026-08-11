#!/bin/zsh
# Install the every-5-minutes local poller + always-on dashboard
# (survive reboots; run whenever the Mac is awake).
set -e
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/launchd"
mkdir -p "$HOME/Library/LaunchAgents"
for name in com.srikar.internship-sniper com.srikar.internship-sniper-dashboard; do
    DST="$HOME/Library/LaunchAgents/$name.plist"
    cp "$SRC_DIR/$name.plist" "$DST"
    launchctl unload "$DST" 2>/dev/null || true
    launchctl load "$DST"
done
echo "installed — poller every 5 min; dashboard at http://localhost:8777"
echo "to uninstall: launchctl unload ~/Library/LaunchAgents/com.srikar.internship-sniper*.plist && rm ~/Library/LaunchAgents/com.srikar.internship-sniper*.plist"
