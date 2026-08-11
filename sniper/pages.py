"""Program-page watcher.

Early-ID programs (Microsoft Explore, Google STEP, DE Shaw Discovery, Jane
Street events...) mostly live on marketing pages, not ATS boards — so they're
invisible to API polling. This module watches a curated list of those pages
(pages.json) and alerts when a page's text meaningfully changes — which is
how "applications are now open" announcements actually appear.

Method: fetch page, reduce to lowercase letters only (kills dates, counters,
csrf tokens and most trivial churn), sha256, compare to last hash in the meta
table. First observation is stored silently.

Limitation: JS-rendered SPAs serve a static shell — changes there may be
invisible. Pages in pages.json were picked to be mostly server-rendered.
"""
import hashlib
import json
import os
import re

from .http import fetch


def _text_hash(raw):
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z]", "", text.lower())
    return hashlib.sha256(text.encode()).hexdigest()


def check_pages(root, store, notifier):
    path = os.path.join(root, "pages.json")
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        pages = json.load(f)
    changed = 0
    for p in pages:
        url, name = p["url"], p["name"]
        try:
            status, raw = fetch(url, retries=1)
        except RuntimeError:
            continue
        if status != 200 or not raw:
            continue
        h = _text_hash(raw)
        key = "page:" + url
        old = store.get_meta(key)
        store.set_meta(key, h)
        if old is None or old == h:
            continue
        changed += 1
        notifier.alert({
            "company": name,
            "title": "program page CHANGED — check for open applications",
            "url": url,
            "locations": [],
        }, score=10)
    store.commit()
    return changed
