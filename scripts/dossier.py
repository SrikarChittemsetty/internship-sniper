"""Application dossier generator — the 'be ready in 10 minutes' half of speed.

For a job URL (or the latest alert), fetches the full posting and asks Claude
(via the `claude` CLI) to produce:
  - 5 tailored resume bullets emphasizing what THIS posting asks for
  - likely screening questions + drafted answers for you to review
  - a 3-sentence 'why us' paragraph
  - red flags / must-address gaps

You review and submit — nothing is auto-submitted.

Usage:
    python3 scripts/dossier.py <job_url>
    python3 scripts/dossier.py --latest          # most recent alert
Requires: `claude` CLI on PATH, and profile.md (your background) in repo root.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.http import fetch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROMPT = """You are helping me apply to an internship FAST. Below is my background and the
job posting (raw page text). Produce, in markdown:

1. **Fit score /10** with one-line justification.
2. **5 tailored resume bullets** — rewrite my strongest experience to mirror this
   posting's exact language and priorities. Truthful, no fabrication.
3. **Likely application questions** (from the posting or typical for this company)
   with drafted answers in my voice, <=120 words each.
4. **"Why {company}" paragraph** — 3 sentences, specific to their product.
5. **Gaps & red flags** — what they want that I lack, and how to frame it.

=== MY BACKGROUND ===
{profile}

=== JOB POSTING ===
{posting}
"""


def latest_alert_url():
    path = os.path.join(ROOT, "out", "alerts.log")
    if not os.path.exists(path):
        return None
    last = open(path).read().strip().splitlines()[-1]
    m = re.search(r"https?://\S+", last)
    return m.group(0) if m else None


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "--latest":
        url = sys.argv[1]
    else:
        url = latest_alert_url()
    if not url:
        print("no URL given and no alerts logged yet", file=sys.stderr)
        sys.exit(1)

    profile_path = os.path.join(ROOT, "profile.md")
    if not os.path.exists(profile_path):
        print("profile.md not found — write your background there first", file=sys.stderr)
        sys.exit(1)
    profile = open(profile_path).read()

    print("fetching %s ..." % url)
    status, raw = fetch(url)
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw.decode("utf-8", "replace"), flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)[:15000]

    prompt = PROMPT.format(profile=profile, posting="URL: %s\n\n%s" % (url, text),
                           company="the company")
    print("generating dossier with claude ...")
    res = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        sys.exit(1)

    slug = re.sub(r"[^a-z0-9]+", "-", url.lower())[-60:].strip("-")
    out = os.path.join(ROOT, "out", "dossier-%s.md" % slug)
    with open(out, "w") as f:
        f.write("# Dossier — %s\n\n" % url + res.stdout)
    print("wrote %s" % out)


if __name__ == "__main__":
    main()
