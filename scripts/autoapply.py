"""Auto-apply engine for TRIVIAL applications only.

Scope: forms whose every required field is deterministic personal info
(name/email/phone/links/school/grad date/work-auth/resume file/consent).
Anything needing judgment — essays, "why us", custom prompts — is classified
MANUAL and left to the dossier flow. CAPTCHA-protected forms are always
MANUAL (never bypassed). Workday (accounts) is out of scope entirely.

Consent model — nothing is ever sent without explicit confirmation:
  scan    -> classify recent high-score postings, build out/apply-queue.json
             + human-readable out/apply-queue.md showing EVERY field value
             that would be submitted, per posting
  submit  -> requires --confirm; sends the queued applications one by one,
             verifies each response, marks app_status='applied' in the store
             ONLY on confirmed success, logs everything to out/applied.log

Profile: profile-autofill.json (gitignored) + resume file path inside it.
"""
import argparse
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB = os.path.join(ROOT, "state", "seen.sqlite")
PROFILE = os.path.join(ROOT, "profile-autofill.json")
QUEUE_JSON = os.path.join(ROOT, "out", "apply-queue.json")
QUEUE_MD = os.path.join(ROOT, "out", "apply-queue.md")
APPLIED_LOG = os.path.join(ROOT, "out", "applied.log")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------------------------------------------------------- profile

def load_profile():
    if not os.path.exists(PROFILE):
        return None
    with open(PROFILE) as f:
        p = json.load(f)
    if any("FILL_ME" in str(v) for v in p.values()):
        return None
    if not os.path.exists(os.path.expanduser(p.get("resume_path", ""))):
        return None
    return p


# ------------------------------------------------- greenhouse classification

# label-pattern -> (profile key | special rule)
GH_TEXT_MAP = [
    (r"first\s*name", "first_name"),
    (r"last\s*name", "last_name"),
    (r"full\s*name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone", "phone"),
    (r"linked\s*in", "linkedin"),
    (r"github|portfolio|website|personal\s*site", "github"),
    (r"school|university|college|institution", "school"),
    (r"(current\s*)?location|city", "location"),
]
# single-selects we can answer deterministically: label-pattern -> chooser
def _opt(options, *pats):
    """First option whose label matches any pattern (case-insens)."""
    for pat in pats:
        for o in options:
            if re.search(pat, str(o.get("label", "")), re.I):
                return o
    return None


def gh_choose(label, options, p):
    L = label.lower()
    if re.search(r"school|university|college|institution", L):
        return _opt(options, re.escape(p["school"]), r"other")
    if re.search(r"graduat", L):
        return _opt(options, re.escape(str(p["grad_year"])), r"other")
    # sponsorship check MUST precede authorization: sponsorship questions
    # frequently contain the word "authorization" ("...require employment
    # authorization sponsorship?") and would otherwise hit the wrong branch
    if re.search(r"sponsor|visa", L):
        return _opt(options, r"^yes" if p["needs_sponsorship"] else r"^no")
    if re.search(r"authoriz", L):
        return _opt(options, r"^yes" if p["us_work_authorized"] else r"^no")
    if re.search(r"discipline|major|field of study", L):
        return _opt(options, re.escape(p.get("major", "")), r"computer science")
    if re.search(r"country", L):
        return _opt(options, r"united states|usa")
    if re.search(r"\bstate\b", L):
        return _opt(options, re.escape(p.get("us_state", "")))
    if re.search(r"pronoun", L):
        return _opt(options, re.escape(p.get("pronouns", "")), r"decline|prefer not")
    if re.search(r"how did you hear|source", L):
        return _opt(options, r"job board|careers? (page|site)|company website|other")
    if re.search(r"18 years|age", L):
        return _opt(options, r"^yes")
    if re.search(r"relevant (internship )?experience|previously (worked|interned)", L):
        return _opt(options, r"^no", r"^yes") if not p.get("has_relevant_internship") \
            else _opt(options, r"^yes")
    if re.search(r"terms|conditions|disclosure|consent|acknowledg|privacy", L):
        return _opt(options, r"agree|acknowledge|accept|^yes|consent")
    if re.search(r"gender|race|ethnic|veteran|disab|hispanic", L):
        return _opt(options, r"decline|don.?t wish|prefer not|not.*answer")
    return None


def classify_greenhouse(board, job_id, p, eu=False):
    """Return (verdict, fills, reasons). verdict: trivial|manual|captcha|error"""
    api = "boards-api.eu.greenhouse.io" if eu else "boards-api.greenhouse.io"
    try:
        st, raw = http_get("https://%s/v1/boards/%s/jobs/%s?questions=true"
                           % (api, board, job_id))
    except Exception as e:
        return "error", [], [str(e)[:80]]
    if st != 200:
        return "error", [], ["schema http %d" % st]
    d = json.loads(raw)
    fills, reasons = [], []
    questions = list(d.get("questions") or [])
    for sec in (d.get("compliance") or []):
        questions += sec.get("questions") or []
    for q in questions:
        req = q.get("required")
        label = q.get("label", "")
        f = (q.get("fields") or [{}])[0]
        ftype, fname = f.get("type"), f.get("name")
        values = f.get("values") or []
        filled = None
        if ftype == "input_file":
            if re.search(r"resume|cv", label, re.I):
                filled = ("file", fname, os.path.expanduser(p["resume_path"]))
            elif not req:
                continue  # optional cover letter etc — skip
        elif ftype == "input_text":
            for pat, key in GH_TEXT_MAP:
                if re.search(pat, label, re.I) and p.get(key):
                    filled = ("text", fname, str(p[key]))
                    break
        elif ftype in ("multi_value_single_select", "multi_value_multi_select"):
            o = gh_choose(label, values, p)
            if o is not None:
                filled = ("select", fname, str(o.get("value")), str(o.get("label")))
        elif ftype == "textarea":
            pass  # never auto-answer prose
        if filled:
            fills.append({"label": label, "kind": filled[0], "name": filled[1],
                          "value": filled[2],
                          "display": filled[3] if len(filled) > 3 else filled[2]})
        elif req:
            reasons.append("unmapped required: %s (%s)" % (label[:50], ftype))
    if reasons:
        return "manual", fills, reasons
    # captcha check on the hosted form page
    try:
        st2, page = http_get("https://job-boards.greenhouse.io/%s/jobs/%s" % (board, job_id))
        if st2 == 200 and re.search(rb"recaptcha|hcaptcha|h-captcha", page, re.I):
            return "captcha", fills, ["form is captcha-protected"]
    except Exception:
        pass
    return "trivial", fills, []


# ---------------------------------------------------------------- scan

def parse_board(url):
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        return "greenhouse", m.group(1), m.group(2)
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
    if m:
        return "lever", m.group(1), m.group(2)
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
    if m:
        return "ashby", m.group(1), m.group(2)
    return None, None, None


def scan(args):
    p = load_profile()
    if p is None:
        print("profile-autofill.json is missing, incomplete, or resume_path is wrong.\n"
              "Fill it in first — nothing can be queued without it.")
        return 1
    db = sqlite3.connect(LOCAL_DB)
    rows = db.execute(
        "SELECT uid, company, title, url, COALESCE(score,0) FROM seen"
        " WHERE first_seen > ? AND COALESCE(score,0) >= ?"
        " AND COALESCE(app_status,'') = '' ORDER BY score DESC LIMIT ?",
        (time.time() - args.days * 86400, args.min_score, args.limit)).fetchall()
    db.close()
    queue, counts = [], {"trivial": 0, "manual": 0, "captcha": 0, "error": 0, "unsupported": 0}
    for uid, company, title, url, score in rows:
        ats, board, jid = parse_board(url)
        if ats != "greenhouse":     # lever/ashby classification: next iteration
            counts["unsupported"] += 1
            continue
        verdict, fills, reasons = classify_greenhouse(board, jid, p, eu=".eu." in url or "eu.greenhouse" in url)
        counts[verdict] += 1
        queue.append({"uid": uid, "company": company, "title": title, "url": url,
                      "score": score, "ats": ats, "board": board, "job_id": jid,
                      "verdict": verdict, "fills": fills, "reasons": reasons})
        print("  %-8s [%2d] %s — %s" % (verdict.upper(), score, company, title[:55]))
    with open(QUEUE_JSON, "w") as f:
        json.dump(queue, f, indent=1)
    with open(QUEUE_MD, "w") as f:
        f.write("# Auto-apply queue — generated %s\n\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("**Review every line. `submit --confirm` sends ONLY the TRIVIAL ones.**\n\n")
        for i, q in enumerate(x for x in queue if x["verdict"] == "trivial"):
            f.write("## %d. %s — %s  [score %d]\n%s\n\n" % (i + 1, q["company"], q["title"], q["score"], q["url"]))
            for fl in q["fills"]:
                f.write("- %s → `%s`\n" % (fl["label"], fl.get("display", fl["value"])[:80]))
            f.write("\n")
        manual = [x for x in queue if x["verdict"] != "trivial"]
        if manual:
            f.write("---\n## Not auto-appliable (use the dossier flow)\n\n")
            for q in manual:
                f.write("- **%s — %s** (%s): %s\n" % (q["company"], q["title"][:50],
                        q["verdict"], "; ".join(q["reasons"])[:100]))
    print("\nscan done: %s" % counts)
    print("review: %s" % QUEUE_MD)
    return 0


# ---------------------------------------------------------------- submit

def multipart(fields, files):
    b = uuid.uuid4().hex
    out = io.BytesIO()
    for name, val in fields:
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                   % (b, name, val)).encode())
    for name, path in files:
        fn = os.path.basename(path)
        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                   "Content-Type: %s\r\n\r\n" % (b, name, fn, ctype)).encode())
        with open(path, "rb") as f:
            out.write(f.read())
        out.write(b"\r\n")
    out.write(("--%s--\r\n" % b).encode())
    return out.getvalue(), "multipart/form-data; boundary=%s" % b


def submit_greenhouse(q):
    """POST the public hosted form. Returns (ok, detail)."""
    form_url = "https://job-boards.greenhouse.io/%s/jobs/%s" % (q["board"], q["job_id"])
    st, page = http_get(form_url)
    if st != 200:
        return False, "form page http %d" % st
    m = re.search(rb'name="authenticity_token"\s+value="([^"]+)"', page)
    if re.search(rb"recaptcha|hcaptcha", page, re.I):
        return False, "captcha appeared — manual"
    fields = [("authenticity_token", m.group(1).decode())] if m else []
    files = []
    for fl in q["fills"]:
        if fl["kind"] == "file":
            files.append((fl["name"], fl["value"]))
        else:
            fields.append((fl["name"], fl["value"]))
    body, ctype = multipart(fields, files)
    req = urllib.request.Request(form_url + "/application", data=body, method="POST",
                                 headers={"User-Agent": UA, "Content-Type": ctype,
                                          "Referer": form_url})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ok = r.status in (200, 201, 302)
            page2 = r.read()
    except urllib.error.HTTPError as e:
        return False, "submit http %d" % e.code
    confirmed = bool(re.search(rb"thank|received|confirmation|submitted", page2, re.I))
    return ok and confirmed, "http ok, confirmation text %s" % ("found" if confirmed else "NOT found")


def submit(args):
    if not args.confirm:
        print("refusing: submit requires --confirm after you've reviewed out/apply-queue.md")
        return 1
    with open(QUEUE_JSON) as f:
        queue = [q for q in json.load(f) if q["verdict"] == "trivial"]
    if args.only:
        want = set(args.only.split(","))
        queue = [q for q in queue if q["company"] in want or q["job_id"] in want]
    db = sqlite3.connect(LOCAL_DB)
    for q in queue:
        ok, detail = submit_greenhouse(q)
        line = "[%s] %s %s — %s | %s" % (time.strftime("%Y-%m-%d %H:%M"),
                "APPLIED" if ok else "FAILED", q["company"], q["title"][:50], detail)
        print(line)
        with open(APPLIED_LOG, "a") as f:
            f.write(line + "\n")
        if ok:
            db.execute("UPDATE seen SET app_status='applied', status_updated=? WHERE uid=?",
                       (int(time.time()), q["uid"]))
            db.commit()
        time.sleep(4)  # human-ish pacing between submissions
    db.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--min-score", type=int, default=5)
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=scan)
    t = sub.add_parser("submit")
    t.add_argument("--confirm", action="store_true")
    t.add_argument("--only", help="comma-separated company names or job ids")
    t.set_defaults(fn=submit)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
