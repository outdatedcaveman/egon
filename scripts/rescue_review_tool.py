"""Generate an interactive, OFFLINE triage tool so Bruno can mark which
classifications are right/wrong — in BOTH directions (drop a junk restore, or
rescue a real item from discard) — and re-categorise. Every row defaults to the
engine's verdict; he only touches the errors. Decisions persist in the browser
(localStorage) and export to rescue_overrides.json, which scripts/
rescue_apply_overrides.py folds into the restore set AND teaches the classifier.

No server, no daemon — pure file:// HTML (safe). Data is embedded.

  python scripts/rescue_review_tool.py
  -> state/panop/rescue_review_tool.html
"""
from __future__ import annotations
import sys, json, html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.rescue_audit import load_zotero_trash, canon, rescue_verdict

TAXO = ["articles", "books", "science_news", "content_longform", "references",
        "data_tools", "shopping", "study_work", "opportunities", "curios"]

CAT_HINT = {  # default category guess from evidence (he can change it)
    "body:citation_meta": "articles", "url:academic_host": "articles", "url:paper_path": "articles",
    "body:book_signals": "books", "body:product": "shopping", "url:scinews_host": "science_news",
    "drive_file": "references",
}


def hint(ev):
    for k, v in CAT_HINT.items():
        if ev.startswith(k) or k in ev:
            return v
    return ""


def main():
    pe = json.loads((ROOT / "panop_env.json").read_text(encoding="utf-8-sig"))
    cands = json.loads((ROOT / "state" / "panop" / "rescue_restore_candidates.json").read_text(encoding="utf-8"))
    restore_canon = {canon(c["url"]) for c in cands}

    # re-derive final DISCARD (same as rescue_report)
    items = load_zotero_trash(pe)
    for fn in ("history_harddelete.json", "history_junk_to_purge.json"):
        p = ROOT / "state" / "panop" / fn
        if p.exists():
            for j in json.loads(p.read_text(encoding="utf-8")):
                items.append({"src": "zotero_trash" if False else fn, "key": None,
                              "title": j.get("title", ""), "url": j.get("url", "")})
    seen, discard = set(), []
    for it in items:
        c = canon(it["url"])
        if not c or c in seen:
            continue
        seen.add(c)
        v, src, rt = rescue_verdict(it["title"], it["url"], allow_fetch=False)
        if v == "DISCARD" and c not in restore_canon:
            discard.append({**it, "evidence": src, "title": it["title"]})

    data = []
    for c in cands:
        data.append({"act": c["verdict"], "src": "library" if c["src"] == "zotero_trash" else "history",
                     "ev": c["evidence"], "t": (c.get("rtitle") or c.get("title") or "")[:140],
                     "u": c["url"], "k": c.get("key") or "", "cat": hint(c["evidence"])})
    for d in discard:
        data.append({"act": "DISCARD", "src": "discard", "ev": d["evidence"],
                     "t": (d.get("title") or "")[:140], "u": d["url"], "k": "", "cat": ""})

    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    opts = "".join(f"<option value='{t}'>{t}</option>" for t in TAXO)

    htmldoc = """<!doctype html>
<html lang=en><head><meta charset="utf-8"><title>Rescue triage — mark right/wrong</title>
<style>
*{box-sizing:border-box}
body{font:13px/1.45 system-ui,Segoe UI,Arial;margin:0;color:#1f2328;background:#fff}
header{position:sticky;top:0;background:#fff;border-bottom:2px solid #d0d7de;padding:12px 18px;z-index:5}
h1{margin:0 0 8px;font-size:18px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.bar input[type=search]{padding:6px 10px;border:1px solid #d0d7de;border-radius:6px;min-width:260px}
button,.fbtn{padding:6px 10px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;cursor:pointer;font:inherit}
button.on{background:#0969da;color:#fff;border-color:#0969da}
.counts{margin-left:auto;font-weight:600}
#export{background:#1a7f37;color:#fff;border-color:#1a7f37;font-weight:600}
table{border-collapse:collapse;width:100%}
td,th{padding:5px 8px;border-bottom:1px solid #eaecef;vertical-align:top;text-align:left}
th{background:#f6f8fa;position:sticky;top:96px}
tr.changed{background:#fff8c5}
.seg{display:inline-flex;border:1px solid #d0d7de;border-radius:6px;overflow:hidden}
.seg span{padding:3px 9px;cursor:pointer;user-select:none;font-weight:600}
.seg .keep.act{background:#1a7f37;color:#fff}
.seg .drop.act{background:#cf222e;color:#fff}
code{font-size:11px;color:#57606a}
a{color:#0969da;text-decoration:none}a:hover{text-decoration:underline}
select{font:inherit;padding:2px;border:1px solid #d0d7de;border-radius:5px}
.src{font-size:11px;padding:1px 6px;border-radius:10px;background:#eaecef}
.src.discard{background:#ffd8d8}.src.library{background:#d2f8d2}.src.history{background:#dde7ff}
</style></head><body>
<header>
<h1>Rescue triage &mdash; mark what's right / wrong, then Export</h1>
<div class=bar>
<input type=search id=q placeholder="filter title / url / evidence&hellip;">
<button class=f data-f=all>All</button>
<button class=f data-f=library>Library</button>
<button class=f data-f=history>History</button>
<button class=f data-f=discard>Discard</button>
<button class=f data-f=changed>Changed only</button>
<span style="border-left:1px solid #d0d7de;padding-left:8px">bulk visible:</span>
<button id=bk>&rarr; Keep</button><button id=bd>&rarr; Discard</button>
<button id=reset>Reset all</button>
<button id=export>Export corrections &darr;</button>
<span class=counts id=counts></span>
</div>
<div style="font-size:12px;color:#57606a;margin-top:6px">
Each row shows the engine's call. Click <b style="color:#1a7f37">Keep</b>/<b style="color:#cf222e">Discard</b> to override; optionally set a category to move it. Only changes are exported. Your work is saved in this browser automatically.</div>
</header>
<table><thead><tr><th>verdict</th><th>move&nbsp;to</th><th>from</th><th>evidence</th><th>title</th><th>url</th></tr></thead>
<tbody id=tb></tbody></table>
<script>
const DATA=__PAYLOAD__;
const CATOPTS=`__OPTS__`;
const LS='rescue_overrides_v1';
let ov=JSON.parse(localStorage.getItem(LS)||'{}');   // url -> {verdict, cat}
const def=d=>d.act==='DISCARD'?'discard':'keep';
function save(){localStorage.setItem(LS,JSON.stringify(ov));render();}
let filt='all',q='';
function changed(d){const o=ov[d.u];if(!o)return false;return o.verdict!==def(d)||(o.cat||'')!==(d.cat||'');}
function visible(d){
  if(filt==='library'&&d.src!=='library')return false;
  if(filt==='history'&&d.src!=='history')return false;
  if(filt==='discard'&&d.src!=='discard')return false;
  if(filt==='changed'&&!changed(d))return false;
  if(q){const s=(d.t+' '+d.u+' '+d.ev).toLowerCase();if(!s.includes(q))return false;}
  return true;
}
function render(){
  const tb=document.getElementById('tb');const rows=DATA.filter(visible).slice(0,4000);
  tb.innerHTML=rows.map((d,ix)=>{
    const i=DATA.indexOf(d);const o=ov[d.u]||{};const v=o.verdict||def(d);const cat=o.cat||d.cat||'';
    return `<tr class="${changed(d)?'changed':''}">
      <td><span class=seg data-i=${i}>
        <span class="keep ${v==='keep'?'act':''}">Keep</span>
        <span class="drop ${v==='discard'?'act':''}">Discard</span></span></td>
      <td><select data-cat=${i}><option value=''>&mdash;</option>${CATOPTS.replace("value='"+cat+"'","value='"+cat+"' selected")}</select></td>
      <td><span class="src ${d.src}">${d.src}</span></td>
      <td><code>${d.ev}</code></td>
      <td>${d.t.replace(/</g,'&lt;')||'<i>&mdash;</i>'}</td>
      <td><a href="${d.u}" target=_blank rel=noopener>${d.u.slice(0,72).replace(/</g,'&lt;')}</a></td></tr>`;
  }).join('');
  const ch=DATA.filter(changed);
  const keepN=DATA.filter(d=>(ov[d.u]?.verdict||def(d))==='keep').length;
  document.getElementById('counts').textContent=
    `${DATA.length} items · ${keepN} keep / ${DATA.length-keepN} discard · ${ch.length} changed · showing ${rows.length}`;
}
document.getElementById('tb').addEventListener('click',e=>{
  const seg=e.target.closest('.seg');if(!seg)return;
  const i=+seg.dataset.i,d=DATA[i];const v=e.target.classList.contains('drop')?'discard':'keep';
  ov[d.u]=Object.assign({},ov[d.u],{verdict:v});
  if(v===def(d)&&!(ov[d.u].cat))delete ov[d.u];save();
});
document.getElementById('tb').addEventListener('change',e=>{
  const sel=e.target.closest('[data-cat]');if(!sel)return;
  const i=+sel.dataset.cat,d=DATA[i];ov[d.u]=Object.assign({},ov[d.u],{cat:sel.value});
  if(!sel.value&&(ov[d.u].verdict||def(d))===def(d))delete ov[d.u];save();
});
document.querySelectorAll('.f').forEach(b=>b.onclick=()=>{filt=b.dataset.f;
  document.querySelectorAll('.f').forEach(x=>x.classList.remove('on'));b.classList.add('on');render();});
document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase().trim();render();};
document.getElementById('bk').onclick=()=>{DATA.filter(visible).forEach(d=>{ov[d.u]=Object.assign({},ov[d.u],{verdict:'keep'});if(def(d)==='keep'&&!ov[d.u].cat)delete ov[d.u];});save();};
document.getElementById('bd').onclick=()=>{DATA.filter(visible).forEach(d=>{ov[d.u]=Object.assign({},ov[d.u],{verdict:'discard'});if(def(d)==='discard'&&!ov[d.u].cat)delete ov[d.u];});save();};
document.getElementById('reset').onclick=()=>{if(confirm('Clear all your corrections?')){ov={};save();}};
document.getElementById('export').onclick=()=>{
  const out=DATA.filter(changed).map(d=>{const o=ov[d.u];return{url:d.u,key:d.k,source:d.src,
    engine:def(d),verdict:o.verdict||def(d),move_to:o.cat||'',title:d.t};});
  const blob=new Blob([JSON.stringify(out,null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='rescue_overrides.json';a.click();
  alert(out.length+' corrections exported to rescue_overrides.json (check your Downloads).');
};
document.querySelector('.f[data-f=all]').classList.add('on');render();
</script></body></html>"""
    htmldoc = htmldoc.replace("__PAYLOAD__", payload).replace("__OPTS__", opts)
    out = ROOT / "state" / "panop" / "rescue_review_tool.html"
    out.write_text(htmldoc, encoding="utf-8")
    print(f"items embedded: {len(data)} (restore {len(cands)} + discard {len(discard)})")
    print(f"triage tool: {out}")


if __name__ == "__main__":
    main()
