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
    # independent community-maintained list — second wide net; anything it
    # shares with Simplify dedups by URL/id in the seen-store anyway
    ("vanshb03/Summer2027-Internships", "dev"),
]

RAW = "https://raw.githubusercontent.com/%s/%s/.github/scripts/listings.json"


def fetch_simplify():
    """Return normalized active job dicts from the feed(s).

    uid is keyed on the posting URL, not the feed's internal id — the feeds
    don't share ids, so the URL is the only stable cross-feed key. Entries
    whose URL already appeared in an earlier feed are skipped."""
    jobs, seen_urls = [], set()
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
            if url in seen_urls:
                continue
            seen_urls.add(url)
            jobs.append({
                "uid": "simplify:%s" % (url or j.get("id")),
                "ats": "simplify",
                "company": j.get("company_name", ""),
                "title": j.get("title", ""),
                "url": url,
                "locations": j.get("locations") or [],
                "posted_at": j.get("date_posted"),  # epoch seconds
            })
    return jobs
