"""Filtering + scoring of postings against the user's profile (config.json)."""
import re


def _compile(patterns):
    return [re.compile(p, re.I) for p in (patterns or [])]


class Matcher:
    def __init__(self, cfg):
        f = cfg.get("filters", {})
        self.include = _compile(f.get("title_include") or [r"\bintern(ship)?\b", r"\bco[- ]?op\b"])
        self.exclude = _compile(f.get("title_exclude") or [])
        self.loc_include = _compile(f.get("location_include") or [])
        self.loc_exclude = _compile(f.get("location_exclude") or [])
        s = cfg.get("scoring", {})
        self.hot = [(re.compile(k, re.I), w) for k, w in (s.get("keywords") or {}).items()]
        self.hot_companies = {c.lower() for c in (s.get("hot_companies") or [])}
        self.threshold = s.get("notify_threshold", 0)

    def matches(self, job):
        """Hard filter: is this an internship posting we care about at all?"""
        title = job.get("title", "")
        if not any(p.search(title) for p in self.include):
            return False
        if any(p.search(title) for p in self.exclude):
            return False
        locs = " | ".join(job.get("locations") or [])
        if self.loc_exclude and any(p.search(locs) for p in self.loc_exclude):
            return False
        if self.loc_include and locs and not any(p.search(locs) for p in self.loc_include):
            return False
        return True

    def score(self, job):
        """Soft score: how well does it fit the profile? Higher = better."""
        text = "%s %s" % (job.get("title", ""), " ".join(job.get("locations") or []))
        sc = 0
        for pat, w in self.hot:
            if pat.search(text):
                sc += w
        if job.get("company", "").lower() in self.hot_companies:
            sc += 10
        return sc
