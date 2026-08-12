"""Internship Sniper — orchestrator.

Usage:
    python3 -m sniper.main --once            # single poll
    python3 -m sniper.main --loop 300        # poll every 300s forever
    python3 -m sniper.main --once --baseline # force re-baseline (no alerts)

First run auto-baselines: every currently-open posting is recorded silently
so you only ever get alerted on postings that are genuinely NEW after that.
Stale postings (older than max_age_days, when the ATS reports a date) are
recorded without an alert too — so adding a new target company later doesn't
flood you with its historical listings.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from .fetchers import fetch_all
from .filters import Matcher
from .notify import Notifier
from .simplify import fetch_simplify
from .store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir(env_key, default_name):
    """Resolve a state/output dir; env override may be relative to ROOT.

    The cloud runner sets SNIPER_STATE_DIR=cloud-state / SNIPER_OUT_DIR=cloud-out
    so the two pollers never touch the same files (local dirs are gitignored,
    cloud dirs are committed by the workflow)."""
    val = os.environ.get(env_key) or default_name
    return val if os.path.isabs(val) else os.path.join(ROOT, val)


STATE_DIR = _dir("SNIPER_STATE_DIR", "state")
OUT_DIR = _dir("SNIPER_OUT_DIR", "out")


def load_json(name, default):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def parse_posted(val):
    """Best-effort: normalize posted_at to epoch seconds, else None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return val / 1000.0 if val > 1e11 else float(val)  # ms vs s
    s = str(val).strip()
    if s.lower() in ("posted today", "posted yesterday"):
        return time.time() - (0 if "today" in s.lower() else 86400)
    m = re.match(r"posted (\d+)\+? days ago", s.lower())
    if m:
        return time.time() - int(m.group(1)) * 86400
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def write_brief(store, cfg):
    """Regenerate out/brief.md — everything caught in the last N hours,
    best first. Refreshed after every poll, so whenever you open the laptop
    the first wake-up poll (within ~5 min) brings it current."""
    hours = cfg.get("brief_hours", 48)
    rows = store.recent(time.time() - hours * 3600)
    path = os.path.join(OUT_DIR, "brief.md")
    with open(path, "w") as f:
        f.write("# Morning brief — new postings, last %dh\n\n" % hours)
        f.write("_Generated %s. Best-scored first. ✅ = you were pinged, "
                "👀 = digest-only (below threshold)._\n\n"
                % time.strftime("%Y-%m-%d %H:%M"))
        if not rows:
            f.write("Nothing new in this window.\n")
        for company, title, url, first_seen, notified, score, posted, locs in rows:
            age_h = (time.time() - first_seen) / 3600
            f.write("- %s **[%d]** [%s — %s](%s)%s _(caught %.0fh ago)_\n" % (
                "✅" if notified else "👀", score, company, title, url,
                (" — " + locs[:80]) if locs else "", age_h))
    return path


def run_once(cfg, targets, store, matcher, notifier, force_baseline=False):
    t0 = time.time()
    baselined = store.get_meta("baselined") == "1" and not force_baseline

    # ------------------------------------------------ fetch direct ATS boards
    jobs, failures = fetch_all(targets, max_workers=cfg.get("max_workers", 24))

    # ------------------------------------- Simplify wide-net feed (throttled)
    every_n = cfg.get("simplify_every_n_runs", 4)
    run_no = int(store.get_meta("run_no", "0")) + 1
    store.set_meta("run_no", run_no)
    if every_n and run_no % every_n == 1:
        try:
            jobs += fetch_simplify()
        except Exception as e:
            failures.append(({"ats": "simplify", "slug": "-"}, str(e)))
        # program-page watcher rides the same slow cadence; its signals are
        # heuristic, so they land in out/watch.md — never the phone
        try:
            from .pages import check_pages
            check_pages(ROOT, store, OUT_DIR)
        except Exception as e:
            failures.append(({"ats": "pages", "slug": "-"}, str(e)))

    # ------------------------------------------------------- delta + alerting
    matched = [j for j in jobs if matcher.matches(j)]
    fresh = store.new_jobs(matched)

    max_age = cfg.get("max_age_days", 7) * 86400
    alerts, digest_rows, quiet = [], [], 0
    for j in fresh:
        posted = parse_posted(j.get("posted_at"))
        stale = posted is not None and (time.time() - posted) > max_age
        sc = matcher.score(j)
        if not baselined or stale:
            store.mark_seen(j, notified=False, score=sc, posted_at=posted)
            quiet += 1
            continue
        if sc >= matcher.threshold:
            alerts.append((sc, j))
        else:
            digest_rows.append((sc, j))
        store.mark_seen(j, notified=sc >= matcher.threshold, score=sc, posted_at=posted)

    for sc, j in sorted(alerts, key=lambda x: -x[0]):
        notifier.alert(j, sc)
    notifier.digest(digest_rows, os.path.join(OUT_DIR, "digest.md"))

    if not baselined:
        store.set_meta("baselined", "1")
    store.commit()
    write_brief(store, cfg)

    notifier.summary(
        "poll done in %.1fs: %d targets (%d failed), %d postings, %d matched filters, "
        "%d new (%d alerted, %d digested, %d baselined/stale), %d total tracked"
        % (time.time() - t0, len(targets), len(failures), len(jobs), len(matched),
           len(fresh), len(alerts), len(digest_rows), quiet, store.count()))
    if failures:
        for t, err in failures[:15]:
            print("  fail: %s:%s — %s" % (t.get("ats"), t.get("slug"), err), file=sys.stderr)
        if len(failures) > 15:
            print("  ... and %d more failures" % (len(failures) - 15), file=sys.stderr)
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, metavar="SECONDS")
    ap.add_argument("--baseline", action="store_true",
                    help="record all current postings without alerting")
    args = ap.parse_args()

    cfg = load_json("config.json", {})
    targets = load_json("targets.json", [])
    if not targets:
        print("targets.json is empty — run scripts/seed_targets.py first", file=sys.stderr)
        sys.exit(1)

    store = Store(os.path.join(STATE_DIR, "seen.sqlite"))
    matcher = Matcher(cfg)
    notifier = Notifier(cfg, OUT_DIR)

    try:
        if args.loop:
            while True:
                try:
                    run_once(cfg, targets, store, matcher, notifier, args.baseline)
                except Exception as e:
                    print("poll error: %s" % e, file=sys.stderr)
                args.baseline = False
                time.sleep(args.loop)
        else:
            run_once(cfg, targets, store, matcher, notifier, args.baseline)
    finally:
        store.close()


if __name__ == "__main__":
    main()
