"""Program-page watcher, v2.

Early-ID programs (Microsoft Explore, Google STEP, NVIDIA Ignite...) live on
marketing pages, not ATS boards. v1 alerted on any content-hash change, which
false-positived on pages with rotating testimonials/carousels (NVIDIA fired
3x in one day on cosmetic churn).

v2: store the normalized TEXT, not just a hash. On change, diff old vs new
and alert only when the ADDED text contains hiring-relevant words — and the
alert carries the added snippet, so you can see what appeared without
clicking. Cosmetic churn updates the stored text silently. A 12h per-page
cooldown caps worst-case noise.
"""
import difflib
import re
import json
import os
import time

from .http import fetch

# a change only matters if what APPEARED speaks the language of an opening
ACTION_WORDS = {
    "apply", "application", "applications", "applying", "open", "opens",
    "opened", "deadline", "deadlines", "register", "registration", "submit",
    "live", "hiring", "accepting", "recruiting", "session", "event",
}
COOLDOWN = 12 * 3600
MAX_STORED = 80000


def _norm_words(raw):
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.findall(r"[a-z]{2,}", text.lower())


def _added_words(old_words, new_words):
    sm = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    added = []
    for op, _, _, j1, j2 in sm.get_opcodes():
        if op in ("insert", "replace"):
            added.extend(new_words[j1:j2])
    return added


def check_pages(root, store, out_dir):
    path = os.path.join(root, "pages.json")
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        pages = json.load(f)
    alerted = 0
    now = time.time()
    for p in pages:
        url, name = p["url"], p["name"]
        try:
            status, raw = fetch(url, retries=1)
        except RuntimeError:
            continue
        if status != 200 or not raw:
            continue
        words = _norm_words(raw)
        if not words:
            continue
        text_key, alert_key = "pagetext:" + url, "pagealert:" + url
        old_text = store.get_meta(text_key)
        store.set_meta(text_key, " ".join(words)[:MAX_STORED])
        if old_text is None:
            continue  # first observation — baseline silently
        added = _added_words(old_text.split(), words)
        hits = sorted(set(added) & ACTION_WORDS)
        if not hits:
            continue  # cosmetic churn — updated silently
        last_alert = store.get_meta(alert_key)
        if last_alert and now - float(last_alert) < COOLDOWN:
            continue
        store.set_meta(alert_key, str(now))
        alerted += 1
        snippet = " ".join(added[:30])[:180]
        # heuristic signal — NEVER pushed to the phone. Phone pings are
        # reserved for verified postings; page signals go to a log the
        # morning brief points at.
        with open(os.path.join(out_dir, "watch.md"), "a") as f:
            f.write("- **%s** [%s] page text now mentions *%s*: “%s…” — [check](%s)\n" % (
                time.strftime("%Y-%m-%d %H:%M"), name, "/".join(hits[:4]), snippet, url))
    store.commit()
    return alerted
