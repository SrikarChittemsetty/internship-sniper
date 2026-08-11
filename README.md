# Internship Sniper

Detects new internship postings within minutes of going live and pushes an
alert to your phone with a direct application link — so you're applying while
everyone else is still waiting for a newsletter.

## Why this is as fast as it gets

- **Polls the source, not aggregators.** Every 5 minutes it hits the public
  JSON APIs behind ~1,500 company career pages (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workable, Workday) — the same endpoints the career pages
  themselves load. Aggregators like Simplify scrape these hourly and publish
  daily; you're reading them directly at 5-minute cadence.
- **Wide-net backstop.** Every 4th run also diffs SimplifyJobs'
  machine-readable feed (14k+ listings, two independent community feeds), so companies outside the direct-poll
  list still get caught.
- **Genuine deltas only.** SQLite seen-store: you're alerted once per posting,
  ever. First run baselines silently; stale postings (>7 days) never alert.
- **A full sweep of all ~1,500 boards takes ~90 seconds.**

## Dashboard & application tracking

`http://localhost:8777` — always-on local dashboard (launchd-managed):
pipeline stat tiles, search/filter/sort over every tracked posting, per-row
application status (interested → applied → OA → interview → offer) and notes,
CSV export. Statuses live in the gitignored local store — never pushed.

## Quick start

```bash
# 1. install the every-5-min local poller
./scripts/install_launchd.sh

# 2. get phone push alerts: install the ntfy app (App Store), subscribe to a
#    long random topic name, then put that topic in config.json under
#    notify.ntfy_topic. Done — no account needed.

# 3. (optional) fill in profile.md, then per alert:
python3 scripts/dossier.py --latest   # tailored bullets + drafted answers

# morning routine: pull overnight cloud catches, fresh poll, open dashboard
./scripts/morning.sh
```

## Layout

| path | what |
|---|---|
| `sniper/` | poller package (stdlib only, no deps) |
| `targets.json` | ~1,500 company boards, seeded from community feeds |
| `config.json` | filters, scoring keywords, notification channels |
| `state/seen.sqlite` | dedup store |
| `out/alerts.log`, `out/digest.md` | high-score alerts / everything else |
| `scripts/seed_targets.py` | re-mine targets.json (run monthly) |
| `scripts/dossier.py` | per-posting application prep via Claude |
| `scripts/dashboard.py` | localhost:8777 dashboard + application tracker |
| `pages.json` + `sniper/pages.py` | program-page change watcher (STEP, Explore…) |
| `.github/workflows/poll.yml` | cloud runner every 15 min (laptop-closed safety net) |

## Commands

```bash
python3 -m sniper.main --once        # single poll
python3 -m sniper.main --loop 300    # foreground loop, every 5 min
python3 -m sniper.main --once --baseline   # re-baseline (no alerts)
python3 scripts/seed_targets.py --max 1500 # refresh target list
```

## Cloud runner (optional but recommended)

Push this repo to GitHub (private is fine) and the included workflow polls
every 15 min even when your Mac is asleep. Add repo secrets
`SNIPER_NTFY_TOPIC` / `SNIPER_DISCORD_WEBHOOK` for notifications. Note:
GitHub cron has 5–15 min jitter — the local launchd poller is the fast path.

## Tuning

- `config.json → filters.title_exclude / location_exclude`: trim noise.
- `scoring.keywords`: regex → weight. Score ≥ `notify_threshold` ⇒ instant
  push; below ⇒ `out/digest.md`. Raise the threshold if you get too many
  pings; keep it at 0 while calibrating.
- `scoring.hot_companies`: +10 to companies you'd drop everything for.

## What it deliberately does NOT do

No auto-submitting. Auto-apply bots get accounts restricted (LinkedIn now
flags high-velocity applying) and botch company-specific questions. The edge
that matters is *knowing in minutes + having materials ready* — the dossier
script gets you from alert to submitted in ~10 minutes, with a human (you)
clicking submit.
