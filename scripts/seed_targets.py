"""Seed targets.json from the SimplifyJobs listings feed.

Mines every application URL in the feed, recognizes which ATS each company
uses, extracts the board slug, and writes a deduped targets.json. Companies
with the most recent activity are kept — these are the boards worth polling
directly every 5 minutes (everything else is still covered by the Simplify
wide-net feed).

Usage:  python3 scripts/seed_targets.py [--max 400]
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.http import fetch_json  # noqa: E402
from sniper.simplify import FEEDS, RAW  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATTERNS = [
    # (ats, regex with slug group)
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/([A-Za-z0-9_-]+)/jobs")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_%.-]+)")),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
]
WORKDAY = re.compile(r"https?://([a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=400)
    args = ap.parse_args()

    repo, branch = FEEDS[0]
    print("downloading %s (%s) ..." % (repo, branch))
    data = fetch_json(RAW % (repo, branch), timeout=120)
    if not isinstance(data, list):
        print("failed to download feed", file=sys.stderr)
        sys.exit(1)
    print("%d listings in feed" % len(data))

    # newest activity first so the cap keeps the liveliest boards
    def upd(j):
        return j.get("date_updated") or j.get("date_posted") or 0
    data.sort(key=upd, reverse=True)

    targets, seen = [], set()
    for j in data:
        url = j.get("url") or ""
        company = (j.get("company_name") or "").strip()
        hit = None
        for ats, pat in PATTERNS:
            m = pat.search(url)
            if m:
                hit = {"ats": ats, "slug": m.group(1), "company": company}
                break
        if hit is None:
            m = WORKDAY.search(url)
            if m:
                host, site = m.group(1), m.group(2)
                tenant = host.split(".")[0]
                hit = {"ats": "workday", "slug": tenant, "wd_host": host,
                       "wd_site": site, "company": company}
        if hit is None:
            continue
        if hit["slug"].lower() in ("embed", "job_board", "jobs"):
            continue  # mis-parsed embed URLs, not real board slugs
        key = (hit["ats"], hit["slug"].lower())
        if key in seen:
            continue
        seen.add(key)
        hit["last_active"] = upd(j)
        targets.append(hit)
        if len(targets) >= args.max:
            break

    by_ats = {}
    for t in targets:
        by_ats[t["ats"]] = by_ats.get(t["ats"], 0) + 1
    print("extracted %d unique boards: %s" % (len(targets), by_ats))

    out = os.path.join(ROOT, "targets.json")
    with open(out, "w") as f:
        json.dump(targets, f, indent=1)
    print("wrote %s (%s)" % (out, time.strftime("%Y-%m-%d %H:%M")))


if __name__ == "__main__":
    main()
