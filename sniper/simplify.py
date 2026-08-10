"""Backstop source: the SimplifyJobs internship repo's machine-readable feed.

Direct ATS polling (fetchers.py) is the fast path — minutes of latency.
This feed is the wide net: Simplify scrapes thousands of career pages hourly
and commits to GitHub, so anything our target list misses shows up here.

The feed is ~10 MB, so main.py polls it on a slower cadence (see config
"simplify_every_n_runs").
"""
from .http import fetch_json

FEEDS = [
    # (repo, branch) — dev branch carries the freshest data
    ("SimplifyJobs/Summer2027-Internships", "dev"),
]

RAW = "https://raw.githubusercontent.com/%s/%s/.github/scripts/listings.json"


def fetch_simplify():
    """Return normalized active job dicts from the Simplify feed(s)."""
    jobs = []
    for repo, branch in FEEDS:
        data = fetch_json(RAW % (repo, branch), timeout=60)
        if not isinstance(data, list):
            continue
        for j in data:
            if not j.get("active", False):
                continue
            if j.get("is_visible") is False:
                continue
            url = j.get("url") or ""
            jobs.append({
                "uid": "simplify:%s" % (j.get("id") or url),
                "ats": "simplify",
                "company": j.get("company_name", ""),
                "title": j.get("title", ""),
                "url": url,
                "locations": j.get("locations") or [],
                "posted_at": j.get("date_posted"),  # epoch seconds
            })
    return jobs
