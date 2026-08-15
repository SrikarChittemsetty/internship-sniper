"""Auto-apply engine for TRIVIAL applications only.

Scope: forms whose every required field is deterministic personal info
(name/email/phone/links/school/grad date/work-auth/resume file/consent).
Anything needing judgment — essays, "why us", custom prompts — is classified
MANUAL and left to the dossier flow. CAPTCHA-protected forms are always
MANUAL (never bypassed). Workday (accounts) is out of scope entirely.

ATS coverage: Greenhouse and Lever classify + submit; Ashby is classify-only
(its write API is an internal GraphQL mutation behind reCAPTCHA Enterprise,
so queued Ashby entries are review sheets to apply from by hand).

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


def unesc(s):
    """Minimal HTML-entity unescape (enough for Lever attribute values)."""
    for a, b in (("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


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
    if re.search(r"how (did )?you (hear|heard|learn)|source", L):
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


# ----------------------------------------------------- lever classification

# extra label patterns that only show up on lever/ashby custom questions;
# tried after GH_TEXT_MAP (which already covers name/email/phone/links/school)
EXTRA_TEXT_MAP = [
    (r"^name$", "full_name"),
    (r"\bmajor\b|field of study", "major"),
    (r"graduat", "grad_year"),
    (r"^state\b", "us_state"),
]
# a lone "I understand / I agree"-style checkbox is a consent ack we may tick
CONSENT_OPT = r"i (understand|agree|acknowledge|consent)|^(agree|acknowledge|accept)\b"
CAPTCHA_PAT = r"hcaptcha\.com/1|data-sitekey|g-recaptcha|cf-turnstile"

# lever standard-field input name -> profile key
LV_NAME_MAP = {
    "name": "full_name", "email": "email", "phone": "phone",
    "location": "location", "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github", "urls[Portfolio]": "github",
    "urls[Other]": "github",
}


def text_fill(label, p):
    for pat, key in GH_TEXT_MAP + EXTRA_TEXT_MAP:
        if re.search(pat, label, re.I) and p.get(key):
            return str(p[key])
    return None


def classify_lever(company, job_id, p):
    """Return (verdict, fills, reasons) for a Lever hosted apply form.

    Source of truth is the apply page HTML at jobs.lever.co/{co}/{id}/apply:
    the v0 postings API 404s for many accounts and never includes custom
    questions. Standard fields are <li class="application-question"> blocks;
    each custom-question card additionally ships its complete definition
    (labels, required flags, options) as JSON in a hidden
    cards[<id>][baseTemplate] input, which we parse instead of card HTML.
    """
    url = "https://jobs.lever.co/%s/%s/apply" % (company, job_id)
    try:
        st, page = http_get(url)
    except urllib.error.HTTPError as e:
        return "error", [], ["apply page http %d (posting closed?)" % e.code]
    except Exception as e:
        return "error", [], [str(e)[:80]]
    html = page.decode("utf-8", "replace")
    if st != 200 or 'id="application-form"' not in html:
        return "error", [], ["no application form on page (http %d)" % st]
    form = html.split('id="application-form"', 1)[1].split("</form>", 1)[0]
    fills, reasons = [], []

    def record(filled, req, label, ftype):
        if filled:
            fills.append({"label": label, "kind": filled[0], "name": filled[1],
                          "value": filled[2],
                          "display": filled[3] if len(filled) > 3 else filled[2]})
        elif req:
            reasons.append("unmapped required: %s (%s)" % (label[:50], ftype))

    # standard blocks (name/email/phone/links/EEO); cards handled below
    for m in re.finditer(r'<li[^>]*class="[^"]*application-(?:question|additional)'
                         r'[^"]*"[^>]*>(.*?)</li>', form, re.S):
        block = m.group(1)
        if "cards[" in block:
            continue
        lab = re.search(r'application-label[^>]*>(.*?)</div>', block, re.S)
        label = re.sub(r"<[^>]+>", " ", lab.group(1)) if lab else ""
        label = " ".join(label.replace("✱", " ").split())  # drop ✱ marker
        names = [n for n in re.findall(r'name="([^"]+)"', block)
                 if n != "selectedLocation"]        # autocomplete companion
        if not names:
            continue                                # e.g. apply-with-LinkedIn row
        name, req, filled = names[0], "required" in block, None
        if 'type="file"' in block:
            if re.search(r"resume|cv", label, re.I):
                filled = ("file", name, os.path.expanduser(p["resume_path"]))
            ftype = "file"
        elif "<select" in block:
            opts = [{"label": t.strip(), "value": v} for v, t in
                    re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>',
                               block) if v and t.strip()]
            o = gh_choose(label, opts, p)
            if o is not None:
                filled = ("select", name, str(o.get("value")), str(o.get("label")))
            ftype = "select"
        elif "<textarea" in block:
            ftype = "textarea"                      # never auto-answer prose
        else:
            key = LV_NAME_MAP.get(name)
            val = str(p[key]) if key and p.get(key) else text_fill(label, p)
            if val:
                filled = ("text", name, val)
            ftype = "text"
        record(filled, req, label, ftype)

    # custom question cards, from their embedded baseTemplate JSON
    for tag in re.findall(r"<input[^>]*baseTemplate[^>]*>", form):
        cid = re.search(r"cards\[([0-9a-f-]{36})\]", tag)
        val = re.search(r'value="([^"]*)"', tag)
        if not cid or not val:
            continue
        try:
            card = json.loads(unesc(val.group(1)))
        except ValueError:
            reasons.append("unparseable card %s" % cid.group(1)[:8])
            continue
        for i, fd in enumerate(card.get("fields") or []):
            fname = "cards[%s][field%d]" % (cid.group(1), i)
            label, ftype = fd.get("text", ""), fd.get("type", "?")
            req, filled = bool(fd.get("required")), None
            opts = [{"label": o.get("text", ""), "value": o.get("text", "")}
                    for o in fd.get("options") or []]
            if ftype in ("dropdown", "multiple-choice"):
                o = gh_choose(label, opts, p)
                if o is not None:
                    filled = ("select", fname, str(o.get("value")), str(o.get("label")))
            elif ftype == "multiple-select":
                if len(opts) == 1 and re.search(CONSENT_OPT, opts[0]["label"], re.I):
                    filled = ("select", fname, opts[0]["value"], opts[0]["label"])
            elif ftype == "university":             # typeahead, free text accepted
                filled = ("text", fname, p["school"])
            elif ftype == "text":
                v = text_fill(label, p)
                if v:
                    filled = ("text", fname, v)
            # textarea / file cards: never auto-answer
            record(filled, req, label, ftype)

    if reasons:
        return "manual", fills, reasons
    # NB: as of 2026-08 every live jobs.lever.co apply page ships the same
    # platform-wide invisible hCaptcha sitekey, so fully-mappable postings
    # land here rather than in trivial. Kept anyway in case that changes.
    if re.search(CAPTCHA_PAT, html, re.I):
        return "captcha", fills, ["form is captcha-protected"]
    return "trivial", fills, []


# ----------------------------------------------------- ashby classification

ASHBY_GQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
# query shape read out of the public SPA bundle (FormRenderParts /
# FormFieldEntryParts fragments); `field` is a JSON scalar carrying
# path/title/type/selectableValues, so no deeper selection is needed
ASHBY_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, "
    "$jobPostingId: String!) { jobPosting(organizationHostedJobsPageName: "
    "$organizationHostedJobsPageName, jobPostingId: $jobPostingId) { id title "
    "isListed applicationForm { sections { isHidden fieldEntries "
    "{ field isRequired isHidden } } } } }")


def classify_ashby(org, job_id, p):
    """Return (verdict, fills, reasons) for an Ashby application form.

    Classify-only: Ashby's submit mutation goes through their write API and
    is gated by an invisible reCAPTCHA Enterprise token, so trivial verdicts
    here mean "you could fill this by hand in one minute", not "we submit".
    """
    body = json.dumps({"operationName": "ApiJobPosting",
                       "variables": {"organizationHostedJobsPageName": org,
                                     "jobPostingId": job_id},
                       "query": ASHBY_QUERY}).encode()
    req = urllib.request.Request(ASHBY_GQL, data=body, method="POST",
                                 headers={"User-Agent": UA,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
    except Exception as e:
        return "error", [], [str(e)[:80]]
    jp = (d.get("data") or {}).get("jobPosting")
    if not jp:
        return "error", [], ["posting not found (closed or unlisted?)"]
    fills, reasons = [], []
    for sec in (jp.get("applicationForm") or {}).get("sections") or []:
        if sec.get("isHidden"):
            continue
        for fe in sec.get("fieldEntries") or []:
            if fe.get("isHidden"):
                continue
            f = fe.get("field") or {}
            req_, filled = bool(fe.get("isRequired")), None
            label, ftype, path = f.get("title", ""), f.get("type", "?"), f.get("path", "")
            opts = [{"label": o.get("label", ""), "value": o.get("value", "")}
                    for o in f.get("selectableValues") or []]
            if ftype == "File":
                if re.search(r"resume|cv", label, re.I):
                    filled = ("file", path, os.path.expanduser(p["resume_path"]))
            elif ftype in ("String", "Email", "Phone", "Url", "Location"):
                v = text_fill(label, p)
                if v:
                    filled = ("text", path, v)
            elif ftype == "ValueSelect":
                o = gh_choose(label, opts, p)
                if o is not None:
                    filled = ("select", path, str(o.get("value")), str(o.get("label")))
            elif ftype == "MultiValueSelect":
                if len(opts) == 1 and re.search(CONSENT_OPT, opts[0]["label"], re.I):
                    filled = ("select", path, opts[0]["value"], opts[0]["label"])
            elif ftype == "Boolean":
                o = gh_choose(label, [{"label": "Yes", "value": "true"},
                                      {"label": "No", "value": "false"}], p)
                if o is not None:
                    filled = ("select", path, str(o.get("value")), str(o.get("label")))
            # LongText (essays): never auto-answer
            if filled:
                fills.append({"label": label, "kind": filled[0], "name": filled[1],
                              "value": filled[2],
                              "display": filled[3] if len(filled) > 3 else filled[2]})
            elif req_:
                reasons.append("unmapped required: %s (%s)" % (label[:50], ftype))
    if reasons:
        return "manual", fills, reasons
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
        if ats == "greenhouse":
            verdict, fills, reasons = classify_greenhouse(
                board, jid, p, eu=".eu." in url or "eu.greenhouse" in url)
        elif ats == "lever":
            verdict, fills, reasons = classify_lever(board, jid, p)
        elif ats == "ashby":
            verdict, fills, reasons = classify_ashby(board, jid, p)
        else:                       # workday etc — out of scope
            counts["unsupported"] += 1
            continue
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
            if q["ats"] == "ashby":
                f.write("**Ashby: classify-only — submit is not implemented "
                        "(reCAPTCHA-gated write API). Apply by hand with the "
                        "values below.**\n\n")
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


def submit_lever(q):
    """POST the hosted Lever form back to its own /apply URL. Returns (ok, detail).

    The form is a plain multipart <form method="POST"> with no action (posts to
    itself); hidden inputs (accountId, per-card baseTemplate JSON, …) must be
    passed through. In practice every live Lever page currently ships an
    invisible platform-wide hCaptcha, in which case we refuse here — kept for
    parity with submit_greenhouse and for any board where that ever differs.
    """
    form_url = "https://jobs.lever.co/%s/%s/apply" % (q["board"], q["job_id"])
    try:
        st, page = http_get(form_url)
    except Exception as e:
        return False, "form page fetch failed: %s" % str(e)[:60]
    if st != 200:
        return False, "form page http %d" % st
    html = page.decode("utf-8", "replace")
    if re.search(CAPTCHA_PAT, html, re.I):
        return False, "captcha present — manual"
    if 'id="application-form"' not in html:
        return False, "application form disappeared"
    form = html.split('id="application-form"', 1)[1].split("</form>", 1)[0]
    fields, filled_names = [], set(f["name"] for f in q["fills"])
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', form):
        nm = re.search(r'name="([^"]+)"', tag)
        vv = re.search(r'value="([^"]*)"', tag)
        if nm and nm.group(1) not in filled_names \
                and nm.group(1) != "h-captcha-response":
            fields.append((nm.group(1), unesc(vv.group(1)) if vv else ""))
    files = []
    for fl in q["fills"]:
        if fl["kind"] == "file":
            files.append((fl["name"], fl["value"]))
        else:
            fields.append((fl["name"], fl["value"]))
    body, ctype = multipart(fields, files)
    req = urllib.request.Request(form_url, data=body, method="POST",
                                 headers={"User-Agent": UA, "Content-Type": ctype,
                                          "Referer": form_url})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ok = r.status in (200, 201, 302)
            final, page2 = r.geturl(), r.read()
    except urllib.error.HTTPError as e:
        return False, "submit http %d" % e.code
    confirmed = final.rstrip("/").endswith("/thanks") or \
        bool(re.search(rb"application submitted|thank", page2, re.I))
    return ok and confirmed, "http ok, confirmation %s" % ("found" if confirmed else "NOT found")


SUBMITTERS = {"greenhouse": submit_greenhouse, "lever": submit_lever}
# ashby deliberately absent: write endpoint is an internal GraphQL mutation
# behind reCAPTCHA Enterprise — never guessed at, classify-only


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
        fn = SUBMITTERS.get(q.get("ats", "greenhouse"))
        if fn is None:
            print("SKIPPED %s — %s: no submitter for ats=%s (apply manually)"
                  % (q["company"], q["title"][:50], q.get("ats")))
            continue
        ok, detail = fn(q)
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
