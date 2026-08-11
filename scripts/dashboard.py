"""Internship Sniper dashboard — local web UI over the seen-store.

Serves a single-page dashboard at http://localhost:8777 with:
  - pipeline stat tiles (tracked / new / alerted / applied / in-process / offers)
  - searchable, filterable, sortable table of every observed posting
  - per-row application status (interested → applied → OA → interview → offer)
    and notes, saved straight into the local SQLite store
  - CSV export of the current filtered view

Reads BOTH stores (local + cloud) merged by uid; status writes always land in
the local store (state/seen.sqlite), which is gitignored — your pipeline data
never leaves this machine.

Usage:  python3 scripts/dashboard.py          # serve on :8777
"""
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB = os.path.join(ROOT, "state", "seen.sqlite")
CLOUD_DB = os.path.join(ROOT, "cloud-state", "seen.sqlite")
PORT = 8777

STATUSES = ["", "interested", "applied", "oa", "interview", "offer", "rejected", "archived"]

COLS = ("uid, company, title, url, first_seen, notified, COALESCE(score,0), "
        "posted_at, COALESCE(locations,''), COALESCE(app_status,''), COALESCE(notes,'')")


def migrate():
    """Ensure tracking columns exist (same ALTERs sniper.store applies)."""
    for path in (LOCAL_DB, CLOUD_DB):
        if not os.path.exists(path):
            continue
        db = sqlite3.connect(path)
        for col, typ in (("score", "REAL"), ("posted_at", "REAL"), ("locations", "TEXT"),
                         ("app_status", "TEXT"), ("status_updated", "INTEGER"), ("notes", "TEXT")):
            try:
                db.execute("ALTER TABLE seen ADD COLUMN %s %s" % (col, typ))
            except sqlite3.OperationalError:
                pass
        db.commit()
        db.close()


def load_jobs():
    rows = {}
    for path in (CLOUD_DB, LOCAL_DB):  # local second → wins on conflict (holds statuses)
        if not os.path.exists(path):
            continue
        db = sqlite3.connect(path)
        try:
            for r in db.execute("SELECT %s FROM seen" % COLS):
                uid = r[0]
                prev = rows.get(uid)
                row = {
                    "uid": uid, "company": r[1], "title": r[2], "url": r[3],
                    "first_seen": r[4], "notified": r[5], "score": r[6],
                    "posted_at": r[7], "locations": r[8],
                    "status": r[9], "notes": r[10],
                }
                # never let a status-less copy clobber one that has pipeline data
                if prev and prev["status"] and not row["status"]:
                    row["status"], row["notes"] = prev["status"], prev["notes"]
                rows[uid] = row
        finally:
            db.close()
    return sorted(rows.values(), key=lambda r: (-(r["score"] or 0), -(r["first_seen"] or 0)))


def update_job(uid, status=None, notes=None):
    db = sqlite3.connect(LOCAL_DB)
    try:
        cur = db.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,))
        if cur.fetchone() is None:
            # row only exists in cloud store — copy it into local so the
            # status has somewhere private to live
            cdb = sqlite3.connect(CLOUD_DB)
            src = cdb.execute("SELECT %s FROM seen WHERE uid = ?" % COLS, (uid,)).fetchone()
            cdb.close()
            if src is None:
                return False
            db.execute(
                "INSERT OR IGNORE INTO seen (uid, company, title, url, first_seen,"
                " notified, score, posted_at, locations) VALUES (?,?,?,?,?,?,?,?,?)",
                src[:9])
        sets, vals = [], []
        if status is not None:
            if status not in STATUSES:
                return False
            sets += ["app_status = ?", "status_updated = ?"]
            vals += [status, int(time.time())]
        if notes is not None:
            sets.append("notes = ?")
            vals.append(notes[:2000])
        if sets:
            db.execute("UPDATE seen SET %s WHERE uid = ?" % ", ".join(sets), vals + [uid])
            db.commit()
        return True
    finally:
        db.close()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internship Sniper</title>
<style>
:root { --bg:#f7f7f8; --card:#fff; --ink:#1a1a1f; --ink2:#5c5c66; --ink3:#9a9aa5;
  --line:#e4e4e9; --accent:#4441d6; --accent-bg:#eeeefb;
  --good:#0d7a3f; --good-bg:#e7f5ec; --warn:#8a6100; --warn-bg:#fdf3d8;
  --bad:#b3261e; --bad-bg:#fbe9e7; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#131318; --card:#1d1d24; --ink:#ececf1; --ink2:#a5a5b1; --ink3:#6e6e7a;
    --line:#2c2c35; --accent:#8b88ff; --accent-bg:#26265a;
    --good:#5bd68f; --good-bg:#123524; --warn:#e8c35a; --warn-bg:#3a3110;
    --bad:#ff8a80; --bad-bg:#3d1512; } }
* { box-sizing:border-box; margin:0 }
body { font:14px/1.45 -apple-system,'Segoe UI',sans-serif; background:var(--bg); color:var(--ink); padding:20px }
h1 { font-size:18px; margin-bottom:2px } .sub { color:var(--ink3); font-size:12px; margin-bottom:16px }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-bottom:16px }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px }
.tile b { display:block; font-size:22px; font-variant-numeric:tabular-nums }
.tile span { color:var(--ink2); font-size:12px }
.filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; align-items:center }
.filters input,.filters select,.filters button { font:inherit; color:var(--ink); background:var(--card);
  border:1px solid var(--line); border-radius:8px; padding:7px 10px }
.filters input[type=search]{ flex:1; min-width:180px }
.filters button { cursor:pointer } .filters button:hover { border-color:var(--accent) }
label.chk { display:flex; gap:5px; align-items:center; color:var(--ink2); font-size:13px }
.tablewrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px }
table { border-collapse:collapse; width:100%; min-width:900px }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink3);
  padding:9px 10px; border-bottom:1px solid var(--line); cursor:pointer; user-select:none; white-space:nowrap }
th:hover { color:var(--accent) }
td { padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top }
tr:last-child td { border-bottom:none }
td.score { font-variant-numeric:tabular-nums; font-weight:600 }
.badge { display:inline-block; min-width:26px; text-align:center; border-radius:6px; padding:1px 6px }
.b-hi { background:var(--accent-bg); color:var(--accent) } .b-lo { background:var(--bg); color:var(--ink2) }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
.co { font-weight:600; white-space:nowrap } .loc,.meta { color:var(--ink2); font-size:12px }
.ping { color:var(--good); font-size:11px }
select.st { font:12px -apple-system,sans-serif; border-radius:7px; border:1px solid var(--line);
  padding:4px 6px; background:var(--card); color:var(--ink) }
select.st.s-applied,select.st.s-oa,select.st.s-interview { background:var(--warn-bg); color:var(--warn); border-color:transparent }
select.st.s-offer { background:var(--good-bg); color:var(--good); border-color:transparent }
select.st.s-rejected,select.st.s-archived { background:var(--bad-bg); color:var(--bad); border-color:transparent }
select.st.s-interested { background:var(--accent-bg); color:var(--accent); border-color:transparent }
input.nt { width:150px; font:12px -apple-system,sans-serif; border:1px solid transparent; border-radius:7px;
  padding:4px 6px; background:transparent; color:var(--ink2) }
input.nt:hover,input.nt:focus { border-color:var(--line); background:var(--card); outline:none }
.count { color:var(--ink3); font-size:12px; margin:10px 2px }
</style></head><body>
<h1>Internship Sniper</h1>
<div class="sub" id="sub">loading…</div>
<div class="tiles" id="tiles"></div>
<div class="filters">
  <input type="search" id="q" placeholder="Search company, title, location…">
  <select id="fstatus"><option value="">any status</option><option value="none">no status</option>
    <option>interested</option><option>applied</option><option>oa</option>
    <option>interview</option><option>offer</option><option>rejected</option><option>archived</option></select>
  <select id="fscore"><option value="0">score ≥ 0</option><option value="5">score ≥ 5</option>
    <option value="8">score ≥ 8</option><option value="11">score ≥ 11</option></select>
  <select id="fdays"><option value="">all time</option><option value="2">last 48 h</option>
    <option value="7">last 7 d</option><option value="14">last 14 d</option><option value="30">last 30 d</option></select>
  <label class="chk"><input type="checkbox" id="fping"> pinged only</label>
  <button id="csv">Export CSV</button>
</div>
<div class="count" id="count"></div>
<div class="tablewrap"><table>
<thead><tr>
  <th data-k="score">Score</th><th data-k="company">Company</th><th data-k="title">Title</th>
  <th data-k="locations">Location</th><th data-k="first_seen">Caught</th>
  <th data-k="status">Status</th><th>Notes</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<script>
let ALL=[], sortK="score", sortDir=-1;
const $=id=>document.getElementById(id);
const esc=s=>String(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const rel=ts=>{ if(!ts) return ""; const h=(Date.now()/1000-ts)/3600;
  return h<1?Math.round(h*60)+"m":h<48?Math.round(h)+"h":Math.round(h/24)+"d"; };
const STS=["","interested","applied","oa","interview","offer","rejected","archived"];
function filtered(){
  const q=$("q").value.toLowerCase(), st=$("fstatus").value, sc=+$("fscore").value,
        days=$("fdays").value, ping=$("fping").checked, cut=days?Date.now()/1000-days*86400:0;
  return ALL.filter(r=>{
    if(r.score<sc) return false;
    if(cut&&r.first_seen<cut) return false;
    if(ping&&!r.notified) return false;
    if(st==="none"&&r.status) return false;
    if(st&&st!=="none"&&r.status!==st) return false;
    if(q&&!(r.company+" "+r.title+" "+r.locations).toLowerCase().includes(q)) return false;
    return true;
  }).sort((a,b)=>{ const x=a[sortK]??"", y=b[sortK]??"";
    return (x<y?-1:x>y?1:0)*sortDir; });
}
function tiles(){
  const n=s=>ALL.filter(r=>r.status===s).length;
  const t=[["tracked",ALL.length],["new 48h",ALL.filter(r=>r.first_seen>Date.now()/1000-172800).length],
    ["pinged",ALL.filter(r=>r.notified).length],["applied",n("applied")],
    ["in process",n("oa")+n("interview")],["offers",n("offer")]];
  $("tiles").innerHTML=t.map(([l,v])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
}
function render(){
  const rows=filtered();
  $("count").textContent=rows.length+" postings shown";
  $("rows").innerHTML=rows.slice(0,800).map(r=>`<tr>
    <td class="score"><span class="badge ${r.score>=5?"b-hi":"b-lo"}">${r.score}</span></td>
    <td><div class="co">${esc(r.company)}</div>${r.notified?'<div class="ping">✓ pinged</div>':""}</td>
    <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a></td>
    <td class="loc">${esc(r.locations)}</td>
    <td class="meta">${rel(r.first_seen)} ago</td>
    <td><select class="st s-${r.status||"none"}" data-uid="${esc(r.uid)}">
      ${STS.map(s=>`<option value="${s}" ${s===r.status?"selected":""}>${s||"—"}</option>`).join("")}
    </select></td>
    <td><input class="nt" data-uid="${esc(r.uid)}" value="${esc(r.notes)}" placeholder="notes…"></td>
  </tr>`).join("");
  if(rows.length>800) $("count").textContent+=" (showing first 800 — narrow the filters)";
  tiles();
}
async function save(uid,patch){
  await fetch("/api/update",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(Object.assign({uid},patch))});
  const r=ALL.find(x=>x.uid===uid);
  if(r) Object.assign(r, patch.status!==undefined?{status:patch.status}:{}, patch.notes!==undefined?{notes:patch.notes}:{});
}
document.addEventListener("change",e=>{
  if(e.target.matches("select.st")){ save(e.target.dataset.uid,{status:e.target.value}).then(render); }
});
let deb;
document.addEventListener("input",e=>{
  if(e.target.matches("input.nt")){ clearTimeout(deb);
    const uid=e.target.dataset.uid,v=e.target.value;
    deb=setTimeout(()=>save(uid,{notes:v}),600); }
  if(e.target.matches("#q")) render();
});
["fstatus","fscore","fdays","fping"].forEach(id=>$(id).addEventListener("change",render));
document.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("click",()=>{
  const k=th.dataset.k; sortDir=(sortK===k)?-sortDir:(k==="company"||k==="title"?1:-1); sortK=k; render();
}));
$("csv").addEventListener("click",()=>{
  const rows=filtered();
  const csv=[["score","company","title","url","locations","caught","pinged","status","notes"].join(",")]
    .concat(rows.map(r=>[r.score,r.company,r.title,r.url,r.locations,
      new Date(r.first_seen*1000).toISOString(),r.notified?"yes":"no",r.status,r.notes]
      .map(v=>'"'+String(v??"").replace(/"/g,'""')+'"').join(","))).join("\n");
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download="sniper-export.csv"; a.click();
});
fetch("/api/jobs").then(r=>r.json()).then(d=>{ ALL=d.jobs;
  $("sub").textContent=d.jobs.length+" postings tracked · data as of "+new Date().toLocaleString();
  render(); });
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE, "text/html")
        elif self.path.startswith("/api/jobs"):
            self._send(200, json.dumps({"jobs": load_jobs()}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/update":
            return self._send(404, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            ok = update_job(body["uid"], body.get("status"), body.get("notes"))
            self._send(200 if ok else 400, json.dumps({"ok": ok}))
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}))


def main():
    migrate()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("dashboard: http://localhost:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
