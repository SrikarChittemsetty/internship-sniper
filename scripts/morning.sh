#!/bin/zsh
# Morning routine:
#   1. pull the cloud runner's overnight state + brief (caught while Mac slept)
#   2. run a fresh local poll (catches the last few minutes)
#   3. open both briefs
set -e
cd "$(dirname "$0")/.."
git pull -q --rebase origin main || echo "(git pull failed — showing local data only)"
python3 -m sniper.main --once
open cloud-out/brief.md 2>/dev/null || true
open out/brief.md
