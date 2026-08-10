"""Notification fan-out.

Channels (all optional, enabled via config.json "notify"):
  - ntfy:    push notification to your phone via ntfy.sh — install the ntfy
             app, subscribe to your (secret) topic, zero accounts needed.
             This is the fastest phone-buzz channel; alerts are actionable
             (tap opens the application URL directly).
  - discord: webhook URL.
  - macos:   local macOS notification banner (only fires when run locally).
  - stdout:  always on; also appended to out/alerts.log.
"""
import json
import os
import subprocess
import time

from .http import fetch


def _fmt(job):
    locs = ", ".join(job.get("locations") or [])[:120]
    return "%s — %s%s" % (job["company"], job["title"], (" (%s)" % locs) if locs else "")


class Notifier:
    def __init__(self, cfg, out_dir):
        n = cfg.get("notify", {})
        self.ntfy_topic = n.get("ntfy_topic") or os.environ.get("SNIPER_NTFY_TOPIC")
        self.discord = n.get("discord_webhook") or os.environ.get("SNIPER_DISCORD_WEBHOOK")
        self.use_macos = n.get("macos", True) and os.uname().sysname == "Darwin" \
            and not os.environ.get("GITHUB_ACTIONS")
        self.log_path = os.path.join(out_dir, "alerts.log")
        os.makedirs(out_dir, exist_ok=True)

    # ------------------------------------------------------------- channels
    def _ntfy(self, job):
        if not self.ntfy_topic:
            return
        try:
            fetch(
                "https://ntfy.sh/%s" % self.ntfy_topic,
                method="POST",
                body=_fmt(job).encode(),
                headers={
                    "Title": "New internship: %s" % job["company"],
                    "Priority": "high",
                    "Tags": "rotating_light",
                    "Click": job.get("url") or "",
                    "Content-Type": "text/plain",
                },
                retries=1,
            )
        except Exception:
            pass

    def _discord(self, job):
        if not self.discord:
            return
        try:
            fetch(self.discord, method="POST", body={
                "content": ":rotating_light: **%s**\n%s" % (_fmt(job), job.get("url", "")),
            }, retries=1)
        except Exception:
            pass

    def _macos(self, job):
        if not self.use_macos:
            return
        try:
            msg = _fmt(job).replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 'display notification "%s" with title "Internship Sniper" sound name "Glass"' % msg],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

    # --------------------------------------------------------------- public
    def alert(self, job, score):
        line = "[%s] score=%-3d %s  %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), score, _fmt(job), job.get("url", ""))
        print("ALERT " + line)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")
        self._ntfy(job)
        self._discord(job)
        self._macos(job)

    def digest(self, jobs, path):
        """Write the low-score remainder to a reviewable digest file."""
        if not jobs:
            return
        with open(path, "a") as f:
            f.write("\n## %s — %d new postings\n\n" % (time.strftime("%Y-%m-%d %H:%M"), len(jobs)))
            for score, j in sorted(jobs, key=lambda x: -x[0]):
                f.write("- [%d] %s — %s\n" % (score, _fmt(j), j.get("url", "")))

    def summary(self, text):
        print(text)
