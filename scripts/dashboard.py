"""Internship Sniper dashboard — local web UI over the seen-store.

Serves a single-page dashboard at http://localhost:8777 with three views:
  - Inbox     — postings from the last 48 h scoring 5+ (the triage list)
  - Pipeline  — every posting you gave a status, grouped by stage
  - All       — the full searchable / filterable / sortable table

Plus pipeline stat tokens, per-row application status (interested → applied →
OA → interview → offer) and notes saved straight into the local SQLite store,
and CSV export of the current filtered view.

Reads BOTH stores (local + cloud) merged by uid; status writes always land in
the local store (state/seen.sqlite), which is gitignored — your pipeline data
never leaves this machine.

Usage:  python3 scripts/dashboard.py [--port 8777]
"""
import argparse
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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internship Sniper</title>
<style>
:root{
  --bg:#f6f6f4; --card:#ffffff; --ink:#1b1b1f; --ink2:#5f5f68; --ink3:#8b8b94;
  --line:#e7e7e3; --accent:#3a5fc0; --accent-bg:#edf1fb;
  --good:#1a7f4e; --good-bg:#e9f5ee; --warn:#8a5a00; --warn-bg:#faf1dd;
  --bad:#b3423a; --bad-bg:#faeceb;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#141418; --card:#1e1e24; --ink:#ececef; --ink2:#a8a8b2; --ink3:#82828d;
    --line:#2b2b33; --accent:#96abf5; --accent-bg:#262c47;
    --good:#6cc996; --good-bg:#1b2f24; --warn:#d9b463; --warn-bg:#332b16;
    --bad:#e39790; --bad-bg:#372021;
  }
}
*{box-sizing:border-box;margin:0}
html{-webkit-text-size-adjust:100%}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--ink);max-width:1040px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:20px;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--ink3);font-size:13px;margin-top:2px}
.stats{display:flex;flex-wrap:wrap;gap:4px 26px;margin:18px 0 24px}
.stat b{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums;margin-right:6px}
.stat span{color:var(--ink2);font-size:13px}
.tabs{display:inline-flex;gap:2px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:3px;margin-bottom:16px}
.tabs button{font:inherit;font-size:14px;border:0;background:none;color:var(--ink2);
  padding:6px 14px;border-radius:8px;cursor:pointer;white-space:nowrap}
.tabs button:hover{color:var(--ink)}
.tabs button.on{background:var(--accent-bg);color:var(--accent);font-weight:600}
.tabs button i{font-style:normal;font-variant-numeric:tabular-nums;opacity:.75;
  margin-left:6px;font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
.bar input,.bar select,.bar button{font:inherit;font-size:14px;color:var(--ink);
  background:var(--card);border:1px solid var(--line);border-radius:9px;padding:7px 11px}
.bar input[type=search]{flex:1;min-width:170px}
.bar input:focus,.bar select:focus{outline:none;border-color:var(--accent)}
.bar button{cursor:pointer;color:var(--ink2)}
.bar button:hover{border-color:var(--accent);color:var(--accent)}
label.chk{display:flex;gap:6px;align-items:center;color:var(--ink2);font-size:13px}
body:not([data-tab=all]) .allonly{display:none}
.count{color:var(--ink3);font-size:12.5px;margin:0 2px 10px}
.cards{display:flex;flex-direction:column;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 16px}
.crow{display:flex;gap:12px;align-items:flex-start}
.cmain{flex:1;min-width:0}
.co{font-weight:600;font-size:13.5px}
.tag{font-size:11.5px;color:var(--good);font-weight:500;margin-left:8px}
a.jt{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--ink);font-weight:500;text-decoration:none;margin:1px 0}
a.jt:hover{color:var(--accent);text-decoration:underline}
.meta{color:var(--ink2);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cact{display:flex;gap:8px;align-items:center;flex-shrink:0}
.badge{flex-shrink:0;min-width:32px;text-align:center;border-radius:999px;padding:2px 8px;
  font-size:12px;font-variant-numeric:tabular-nums;color:var(--ink2);
  background:var(--bg);border:1px solid var(--line);margin-top:2px}
.badge.hi{background:var(--accent-bg);color:var(--accent);border-color:transparent}
select.st{font:inherit;font-size:12.5px;border-radius:8px;border:1px solid var(--line);
  padding:4px 7px;background:var(--card);color:var(--ink2)}
select.st.s-interested{background:var(--accent-bg);color:var(--accent);border-color:transparent}
select.st.s-applied,select.st.s-oa,select.st.s-interview{background:var(--warn-bg);color:var(--warn);border-color:transparent}
select.st.s-offer{background:var(--good-bg);color:var(--good);border-color:transparent}
select.st.s-rejected,select.st.s-archived{background:var(--bad-bg);color:var(--bad);border-color:transparent}
button.ntb{font:inherit;font-size:12.5px;border:1px solid var(--line);border-radius:8px;
  padding:4px 9px;background:none;color:var(--ink3);cursor:pointer;max-width:180px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button.ntb:hover{color:var(--accent);border-color:var(--accent)}
button.ntb.has{color:var(--ink2)}
input.nt{width:100%;font:inherit;font-size:13px;border:1px solid var(--line);border-radius:8px;
  padding:6px 9px;background:var(--bg);color:var(--ink);margin-top:10px}
input.nt:focus{outline:none;border-color:var(--accent)}
td input.nt{margin-top:0;min-width:170px;background:var(--card)}
.ghead{margin:22px 2px 9px;font-size:12px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink3);font-weight:600}
#view > .ghead:first-child{margin-top:0}
.empty{padding:56px 20px;text-align:center;color:var(--ink2);background:var(--card);
  border:1px dashed var(--line);border-radius:12px}
.empty b{display:block;font-size:16px;font-weight:600;color:var(--ink);margin-bottom:4px}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:860px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3);
  padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--accent)}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13.5px}
tr:last-child td{border-bottom:none}
td .jt{max-width:340px}
td.num{font-variant-numeric:tabular-nums}
@media(max-width:600px){
  body{padding:18px 12px 48px}
  .crow{flex-wrap:wrap}
  .cact{width:100%;margin-top:8px}
}
</style></head><body data-tab="inbox">
<header>
  <h1>Internship Sniper</h1>
  <div class="sub" id="sub">loading…</div>
</header>
<div class="stats" id="stats"></div>
<div class="tabs" role="tablist">
  <button type="button" class="on" data-tab="inbox">Inbox<i id="n-inbox"></i></button>
  <button type="button" data-tab="pipeline">Pipeline<i id="n-pipeline"></i></button>
  <button type="button" data-tab="all">All<i id="n-all"></i></button>
</div>
<div class="bar">
  <input type="search" id="q" placeholder="Search company, title, location…">
  <select id="fstatus" class="allonly"><option value="">any status</option><option value="none">no status</option>
    <option>interested</option><option>applied</option><option>oa</option>
    <option>interview</option><option>offer</option><option>rejected</option><option>archived</option></select>
  <select id="fscore" class="allonly"><option value="0">score ≥ 0</option><option value="5">score ≥ 5</option>
    <option value="8">score ≥ 8</option><option value="11">score ≥ 11</option></select>
  <select id="fdays" class="allonly"><option value="">all time</option><option value="2">last 48 h</option>
    <option value="7">last 7 d</option><option value="14">last 14 d</option><option value="30">last 30 d</option></select>
  <label class="chk allonly"><input type="checkbox" id="fping"> pinged only</label>
  <button type="button" id="csv" class="allonly">Export CSV</button>
</div>
<div class="count" id="count"></div>
<div id="view"></div>
<script>
let ALL=[], TAB="inbox", sortK="score", sortDir=-1;
const OPEN=new Set(), CAP=500;
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const rel=ts=>{const h=(Date.now()/1000-ts)/3600;
  return h<1?Math.max(1,Math.round(h*60))+" min":h<48?Math.round(h)+" h":Math.round(h/24)+" d";};
const ago=ts=>ts?rel(ts)+" ago":"";
const STS=["","interested","applied","oa","interview","offer","rejected","archived"];
const LBL={offer:"Offer",interview:"Interview",oa:"Online assessment",applied:"Applied",
  interested:"Interested",rejected:"Rejected",archived:"Archived"};
const PIPE=["offer","interview","oa","applied","interested","rejected","archived"];
const hay=r=>(r.company+" "+r.title+" "+r.locations).toLowerCase();
const q=()=>$("q").value.trim().toLowerCase();

function inboxList(){
  const cut=Date.now()/1000-172800, s=q();
  return ALL.filter(r=>r.first_seen>=cut&&r.score>=5
      &&r.status!=="rejected"&&r.status!=="archived"
      &&(!s||hay(r).includes(s)))
    .sort((a,b)=>(b.score-a.score)||(b.first_seen-a.first_seen));
}
function pipelineList(){
  const s=q();
  return ALL.filter(r=>r.status&&(!s||hay(r).includes(s)));
}
function allList(){
  const s=q(), st=$("fstatus").value, sc=+$("fscore").value,
    days=$("fdays").value, ping=$("fping").checked,
    cut=days?Date.now()/1000-days*86400:0;
  return ALL.filter(r=>{
    if(r.score<sc) return false;
    if(cut&&r.first_seen<cut) return false;
    if(ping&&!r.notified) return false;
    if(st==="none"&&r.status) return false;
    if(st&&st!=="none"&&r.status!==st) return false;
    if(s&&!hay(r).includes(s)) return false;
    return true;
  }).sort((a,b)=>{const x=a[sortK]??"", y=b[sortK]??"";
    return (x<y?-1:x>y?1:0)*sortDir;});
}
function stSel(r){
  return `<select class="st s-${r.status||"none"}" data-uid="${esc(r.uid)}" aria-label="application status">`+
    STS.map(s=>`<option value="${s}" ${s===r.status?"selected":""}>${s||"no status"}</option>`).join("")+
    `</select>`;
}
function ntCtl(r){
  if(OPEN.has(r.uid))
    return `<input class="nt" data-uid="${esc(r.uid)}" value="${esc(r.notes)}" placeholder="Notes — saved as you type">`;
  return `<button type="button" class="ntb${r.notes?" has":""}" data-uid="${esc(r.uid)}" title="${esc(r.notes)}">${r.notes?esc(r.notes):"Add note"}</button>`;
}
function card(r){
  return `<div class="card"><div class="crow">
    <span class="badge${r.score>=8?" hi":""}">${r.score}</span>
    <div class="cmain">
      <div class="co">${esc(r.company)}${r.notified?'<span class="tag">pinged</span>':""}</div>
      <a class="jt" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
      <div class="meta">${esc(r.locations)}${r.locations&&r.first_seen?" · ":""}${ago(r.first_seen)}</div>
    </div>
    <div class="cact">${stSel(r)}${OPEN.has(r.uid)?"":ntCtl(r)}</div>
  </div>${OPEN.has(r.uid)?ntCtl(r):""}</div>`;
}
function stats(){
  const n=s=>ALL.filter(r=>r.status===s).length;
  const t=[[ALL.length,"tracked"],
    [ALL.filter(r=>r.first_seen>Date.now()/1000-172800).length,"new in 48 h"],
    [ALL.filter(r=>r.notified).length,"pinged"],
    [n("applied"),"applied"],[n("oa")+n("interview"),"in process"],[n("offer"),"offers"]];
  $("stats").innerHTML=t.map(([v,l])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
}
function render(){
  document.body.dataset.tab=TAB;
  document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===TAB));
  const inbox=inboxList(), pipe=pipelineList(), all=allList();
  $("n-inbox").textContent=inbox.length;
  $("n-pipeline").textContent=pipe.length;
  $("n-all").textContent=all.length;
  let html="", count="";
  if(TAB==="inbox"){
    if(!inbox.length){
      html=`<div class="empty"><b>Nothing new — you're caught up.</b>Postings from the last 48 hours scoring 5+ land here.</div>`;
    }else{
      html=`<div class="cards">${inbox.slice(0,CAP).map(card).join("")}</div>`;
      count=inbox.length>CAP?`Showing ${CAP} of ${inbox.length} — refine with search`
        :`${inbox.length} posting${inbox.length===1?"":"s"} from the last 48 h, score 5+`;
    }
  }else if(TAB==="pipeline"){
    if(!pipe.length){
      html=`<div class="empty"><b>No applications tracked yet.</b>Set a status on any posting and it will show up here.</div>`;
    }else{
      html=PIPE.filter(s=>pipe.some(r=>r.status===s)).map(s=>{
        const g=pipe.filter(r=>r.status===s).sort((a,b)=>(b.first_seen||0)-(a.first_seen||0));
        return `<div class="ghead">${LBL[s]} · ${g.length}</div><div class="cards">${g.slice(0,CAP).map(card).join("")}</div>`;
      }).join("");
      count=`${pipe.length} in your pipeline`;
    }
  }else{
    const shown=all.slice(0,CAP);
    count=all.length>CAP?`Showing ${CAP} of ${all.length} — refine with search or filters`
      :`${all.length} posting${all.length===1?"":"s"}`;
    html=`<div class="tablewrap"><table><thead><tr>
      <th data-k="score">Score</th><th data-k="company">Company</th><th data-k="title">Title</th>
      <th data-k="locations">Location</th><th data-k="first_seen">Caught</th>
      <th data-k="status">Status</th><th>Notes</th></tr></thead><tbody>`+
      shown.map(r=>`<tr>
        <td class="num"><span class="badge${r.score>=8?" hi":""}">${r.score}</span></td>
        <td><span class="co">${esc(r.company)}</span>${r.notified?'<span class="tag">pinged</span>':""}</td>
        <td><a class="jt" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a></td>
        <td class="meta">${esc(r.locations)}</td>
        <td class="meta num">${ago(r.first_seen)}</td>
        <td>${stSel(r)}</td>
        <td>${ntCtl(r)}</td>
      </tr>`).join("")+`</tbody></table></div>`;
  }
  $("count").textContent=count;
  $("view").innerHTML=html;
  stats();
}
async function save(uid,patch){
  await fetch("/api/update",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(Object.assign({uid},patch))});
  const r=ALL.find(x=>x.uid===uid);
  if(r) Object.assign(r, patch.status!==undefined?{status:patch.status}:{}, patch.notes!==undefined?{notes:patch.notes}:{});
}
document.addEventListener("change",e=>{
  if(e.target.matches("select.st")) save(e.target.dataset.uid,{status:e.target.value}).then(render);
  if(e.target.matches("#fstatus,#fscore,#fdays,#fping")) render();
});
let deb;
document.addEventListener("input",e=>{
  if(e.target.matches("input.nt")){ clearTimeout(deb);
    const uid=e.target.dataset.uid, v=e.target.value;
    deb=setTimeout(()=>save(uid,{notes:v}),600); }
  if(e.target.matches("#q")) render();
});
document.addEventListener("click",e=>{
  const tb=e.target.closest(".tabs button");
  if(tb){ TAB=tb.dataset.tab; render(); return; }
  const nb=e.target.closest("button.ntb");
  if(nb){ const uid=nb.dataset.uid; OPEN.add(uid); render();
    const inp=document.querySelector(`input.nt[data-uid="${CSS.escape(uid)}"]`);
    if(inp){ inp.focus(); inp.setSelectionRange(inp.value.length,inp.value.length); }
    return; }
  const th=e.target.closest("th[data-k]");
  if(th){ const k=th.dataset.k;
    sortDir=(sortK===k)?-sortDir:(k==="company"||k==="title"?1:-1); sortK=k; render(); }
});
$("csv").addEventListener("click",()=>{
  const rows=allList();
  const csv=[["score","company","title","url","locations","caught","pinged","status","notes"].join(",")]
    .concat(rows.map(r=>[r.score,r.company,r.title,r.url,r.locations,
      new Date(r.first_seen*1000).toISOString(),r.notified?"yes":"no",r.status,r.notes]
      .map(v=>'"'+String(v??"").replace(/"/g,'""')+'"').join(","))).join("\n");
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download="sniper-export.csv"; a.click();
});
fetch("/api/jobs").then(r=>r.json()).then(d=>{ ALL=d.jobs;
  $("sub").textContent=ALL.length+" postings tracked · updated "+new Date().toLocaleString();
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
    ap = argparse.ArgumentParser(description="Internship Sniper dashboard")
    ap.add_argument("--port", type=int, default=PORT,
                    help="port to serve on (default %d)" % PORT)
    args = ap.parse_args()
    migrate()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("dashboard: http://localhost:%d" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
