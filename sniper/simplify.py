"""Backstop source: the SimplifyJobs internship repo's machine-readable feed.

Direct ATS polling (fetchers.py) is the fast path — minutes of latency.
This feed is the wide net: Simplify scrapes thousands of career pages hourly
and commits to GitHub, so anything our target list misses shows up here.

The feed is ~10 MB, so main.py polls it on a slower cadence (see config
"simplify_every_n_runs").
"""
import re

from .http import fetch, fetch_json

FEEDS = [
    # (repo, branch) — dev branch carries the freshest data
    ("SimplifyJobs/Summer2027-Internships", "dev"),
    # independent community-maintained list — second wide net; anything it
    # shares with Simplify dedups by URL/id in the seen-store anyway
    ("vanshb03/Summer2027-Internships", "dev"),
]

RAW = "https://raw.githubusercontent.com/%s/%s/.github/scripts/listings.json"

# README-only lists (no listings.json) — parsed from their markdown tables.
# Covers underclassmen-specific programs the ATS boards title differently.
README_FEEDS = [
    ("zapplyjobs/underclassmen-internships", "main"),
]
RAW_README = "https://raw.githubusercontent.com/%s/%s/README.md"


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
    jobs += _fetch_readme_feeds(seen_urls)
    return jobs


# program-tracker row: | [Name](url) | Status/Open Date | Year | Note |
_ROW = re.compile(
    r"^\|\s*(?:\[([^\]]+)\]\((https?://[^)\s]+)\)|([A-Za-z][^|\[]*?))\s*"
    r"\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|", re.M)


def _fetch_readme_feeds(seen_urls):
    """Emit a job dict per program the moment its row shows Open.

    The uid embeds the open-state, so a program tracked as '?' that later
    flips to '✅ Open' becomes a NEW uid — i.e. exactly one alert per
    program per opening."""
    jobs = []
    for repo, branch in README_FEEDS:
        try:
            status, raw = fetch(RAW_README % (repo, branch), timeout=60)
        except RuntimeError:
            continue
        if status != 200:
            continue
        text = raw.decode("utf-8", "replace")
        for m in _ROW.finditer(text):
            linked_name, url, bare_name, st, year, note = (
                (x or "").strip() for x in m.groups())
            name = linked_name or bare_name
            if not name or name.lower() in ("name", "company"):
                continue
            if "open" not in st.lower():
                continue  # closed or unknown — nothing actionable yet
            key = url or name
            if key in seen_urls:
                continue
            seen_urls.add(key)
            jobs.append({
                "uid": "program:%s:open" % key,
                "ats": "readme:" + repo.split("/")[0],
                "company": name,
                "title": "%s — %s (%s)" % (name, st, year or "all"),
                "url": url or ("https://github.com/%s" % repo),
                "locations": [],
                "posted_at": None,
            })
    return jobs
