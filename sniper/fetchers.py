"""Fetchers for public ATS job-board APIs.

Every fetcher takes a target dict {"ats": ..., "slug": ..., "company": ...}
and returns a list of normalized job dicts:

    {
      "uid":       "greenhouse:stripe:12345",   # globally unique, stable
      "ats":       "greenhouse",
      "company":   "Stripe",
      "title":     "Software Engineering Intern",
      "url":       "https://...",               # direct application link
      "locations": ["San Francisco, CA", "Remote"],
      "posted_at": "2026-08-09T12:00:00Z" or None,
    }

All endpoints below are public and unauthenticated — they are the same JSON
the company's own careers page loads, so polling them is both fast and safe.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .http import fetch_json


# ---------------------------------------------------------------- greenhouse
def fetch_greenhouse(t):
    data = fetch_json(
        "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % t["slug"]
    )
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        jobs.append({
            "uid": "greenhouse:%s:%s" % (t["slug"], j["id"]),
            "ats": "greenhouse",
            "company": t.get("company") or t["slug"],
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "locations": [j.get("location", {}).get("name", "")],
            "posted_at": j.get("first_published") or j.get("updated_at"),
        })
    return jobs


# --------------------------------------------------------------------- lever
def fetch_lever(t):
    data = fetch_json("https://api.lever.co/v0/postings/%s?mode=json" % t["slug"])
    if data is None or not isinstance(data, list):
        return None
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""]
        locs += (j.get("additionalLocations") or [])
        ts = j.get("createdAt")
        jobs.append({
            "uid": "lever:%s:%s" % (t["slug"], j.get("id")),
            "ats": "lever",
            "company": t.get("company") or t["slug"],
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "locations": [l for l in locs if l],
            "posted_at": ts,  # epoch ms
        })
    return jobs


# --------------------------------------------------------------------- ashby
def fetch_ashby(t):
    data = fetch_json(
        "https://api.ashbyhq.com/posting-api/job-board/%s" % t["slug"]
    )
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        for extra in (j.get("secondaryLocations") or []):
            locs.append(extra.get("location") or "")
        jobs.append({
            "uid": "ashby:%s:%s" % (t["slug"], j.get("id")),
            "ats": "ashby",
            "company": t.get("company") or t["slug"],
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "locations": [l for l in locs if l],
            "posted_at": j.get("publishedAt"),
        })
    return jobs


# ------------------------------------------------------------ smartrecruiters
def fetch_smartrecruiters(t):
    # paginated; 100/page
    jobs, offset = [], 0
    while True:
        data = fetch_json(
            "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&offset=%d"
            % (t["slug"], offset)
        )
        if not data or "content" not in data:
            return jobs if offset else None
        for j in data["content"]:
            loc = j.get("location") or {}
            city = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            jobs.append({
                "uid": "smartrecruiters:%s:%s" % (t["slug"], j.get("id")),
                "ats": "smartrecruiters",
                "company": t.get("company") or t["slug"],
                "title": j.get("name", ""),
                "url": "https://jobs.smartrecruiters.com/%s/%s" % (t["slug"], j.get("id")),
                "locations": [city] if city else [],
                "posted_at": j.get("releasedDate"),
            })
        offset += 100
        if offset >= int(data.get("totalFound", 0)):
            break
    return jobs


# ------------------------------------------------------------------ workable
def fetch_workable(t):
    data = fetch_json(
        "https://apply.workable.com/api/v1/widget/accounts/%s?details=false" % t["slug"]
    )
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        jobs.append({
            "uid": "workable:%s:%s" % (t["slug"], j.get("shortcode")),
            "ats": "workable",
            "company": t.get("company") or data.get("name") or t["slug"],
            "title": j.get("title", ""),
            "url": "https://apply.workable.com/%s/j/%s/" % (t["slug"], j.get("shortcode")),
            "locations": [", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)],
            "posted_at": j.get("published_on") or j.get("created_at"),
        })
    return jobs


# ------------------------------------------------------------------- workday
def fetch_workday(t):
    """Workday CXS API. Target needs: slug (tenant), wd_host (e.g. nvidia.wd5),
    wd_site (e.g. NVIDIAExternalCareerSite). Paginated POST, 20/page."""
    host = t.get("wd_host")
    site = t.get("wd_site")
    if not host or not site:
        return None
    base = "https://%s.myworkdayjobs.com" % host
    url = "%s/wday/cxs/%s/%s/jobs" % (base, t["slug"], site)
    jobs, offset = [], 0
    search = t.get("wd_search", "intern")
    while offset < 200:  # cap: with a search term this covers all intern reqs
        data = fetch_json(url, method="POST", body={
            "appliedFacets": {}, "limit": 20, "offset": offset,
            "searchText": search,
        })
        if not data or "jobPostings" not in data:
            return jobs if offset else None
        batch = data["jobPostings"]
        if not batch:
            break
        for j in batch:
            path = j.get("externalPath", "")
            jobs.append({
                "uid": "workday:%s:%s" % (t["slug"], path.rsplit("/", 1)[-1] or j.get("title")),
                "ats": "workday",
                "company": t.get("company") or t["slug"],
                "title": j.get("title", ""),
                "url": "%s/en-US/%s%s" % (base, site, path),
                "locations": [j.get("locationsText", "")],
                "posted_at": j.get("postedOn"),  # human string like "Posted Today"
            })
        offset += 20
        if offset >= int(data.get("total", 0)):
            break
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "workday": fetch_workday,
}


def fetch_all(targets, max_workers=24):
    """Fetch every target concurrently.

    Returns (jobs, failures) where failures is a list of (target, reason).
    A fetcher returning None means the board was unreachable/invalid — the
    caller must NOT treat its jobs as 'gone' (no delta processing for it).
    """
    jobs, failures = [], []

    def one(t):
        fn = FETCHERS.get(t["ats"])
        if fn is None:
            return t, None, "unknown ats %r" % t["ats"]
        try:
            res = fn(t)
        except Exception as e:
            return t, None, "error: %s" % e
        if res is None:
            return t, None, "unreachable or bad payload"
        return t, res, None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(one, t) for t in targets]
        for f in as_completed(futs):
            t, res, err = f.result()
            if err:
                failures.append((t, err))
            else:
                jobs.extend(res)
    return jobs, failures
