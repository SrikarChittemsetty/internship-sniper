#!/bin/zsh
# Morning routine: force a fresh poll (catches everything since the Mac slept),
# then open the brief. Run this — or just open out/brief.md, which the
# background poller refreshes every 5 minutes anyway.
set -e
cd "$(dirname "$0")/.."
python3 -m sniper.main --once
open out/brief.md
