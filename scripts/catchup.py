"""Catch-up report: every internship posted in the last N days that's still
open RIGHT NOW — including everything from before this tool existed.

Does a full live sweep (all boards + the Simplify wide-net feed), keeps
matches with a post date inside the window, and writes out/catchup.md sorted
best-fit first. Ignores the seen-store entirely: this is for reviewing what
you already missed, not for alerting.

Usage:  python3 scripts/catchup.py [--days 14]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.fetchers import fetch_all           # noqa: E402
from sniper.filters import Matcher              # noqa: E402
from sniper.main import parse_posted, load_json  # noqa: E402
from sniper.simplify import fetch_simplify      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    cfg = load_json("config.json", {})
    targets = load_json("targets.json", [])
    matcher = Matcher(cfg)

    print("sweeping %d boards + Simplify feed ..." % len(targets))
    jobs, failures = fetch_all(targets, max_workers=cfg.get("max_workers", 24))
    try:
        jobs += fetch_simplify()
    except Exception as e:
        print("simplify feed failed: %s" % e, file=sys.stderr)

    cutoff = time.time() - args.days * 86400
    rows, undated = [], 0
    seen_urls = set()
    for j in jobs:
        if not matcher.matches(j):
            continue
        if j["url"] in seen_urls:      # same posting via direct poll AND Simplify
            continue
        seen_urls.add(j["url"])
        posted = parse_posted(j.get("posted_at"))
        if posted is None:
            undated += 1
            continue
        if posted >= cutoff:
            rows.append((matcher.score(j), posted, j))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    path = os.path.join(ROOT, "out", "catchup.md")
    with open(path, "w") as f:
        f.write("# Catch-up — internships posted in the last %d days, still open\n\n" % args.days)
        f.write("_Generated %s · %d postings · sorted best-fit first · "
                "[score] company — title (days ago)_\n\n" % (
                    time.strftime("%Y-%m-%d %H:%M"), len(rows)))
        for sc, posted, j in rows:
            days = (time.time() - posted) / 86400
            locs = ", ".join(j.get("locations") or [])[:80]
            f.write("- **[%d]** [%s — %s](%s)%s _(%.0fd ago)_\n" % (
                sc, j["company"], j["title"], j["url"],
                (" — " + locs) if locs else "", days))
    print("wrote %s: %d postings in window (%d matches had no parseable date, "
          "%d board failures)" % (path, len(rows), undated, len(failures)))


if __name__ == "__main__":
    main()
