"""Mobile Connect — phone surface for the Connection Engine (strategy #4).

Bruno 2026-06-12: the phone can't run the desktop overlay, and a native
share-target needs an installed app + HTTPS. This is the pragmatic v1 that
works today: the mind service hosts a tiny TOKEN-GUARDED mobile web app on the
LAN. On the phone: select text anywhere → copy → open the Egon bookmark →
paste → Connect (or 🧠 Synthesize). Results look like the desktop widget.

Security model (privacy first, per Bruno's rules):
  • The full mind API stays bound to 127.0.0.1 — NOTHING else is exposed.
  • This is a SEPARATE FastAPI app on 0.0.0.0:8765 with exactly three routes
    (page, connect, synthesize), every one requiring the secret token from
    egon-config.json {"connect_mobile": {"token": …}} (auto-generated on
    first start). Wrong/missing token → 403, no information leaked.
  • LAN-only by nature (no port forwarding; home WiFi).

Phone bookmark:  http://<pc-lan-ip>:8765/m?k=<token>
(The exact URL is printed into state/mobile_connect_url.txt on start.)
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

# Module level on purpose: with `from __future__ import annotations`, FastAPI
# resolves parameter type hints by NAME against module globals. When Request
# was imported inside build_app(), the hint couldn't resolve and FastAPI fell
# back to treating `req` as a required query field → every call 422'd.
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "egon-config.json"
URL_FILE = ROOT / "state" / "mobile_connect_url.txt"
MOBILE_PORT = 8765


def get_token() -> str:
    """Read (or create once) the mobile token in egon-config.json."""
    cfg = {}
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        pass
    tok = ((cfg.get("connect_mobile") or {}).get("token") or "").strip()
    if not tok:
        tok = secrets.token_urlsafe(18)
        cfg.setdefault("connect_mobile", {})["token"] = tok
        try:
            CFG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass
    return tok


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.0.8"


_PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Egon Connect</title><style>
 body{background:#0B1F28;color:#F0E9D5;font:15px/1.45 -apple-system,Roboto,sans-serif;
      margin:0;padding:14px}
 h1{color:#D4A24C;font-size:19px;margin:2px 0 10px}
 textarea{width:100%;box-sizing:border-box;height:110px;background:#102F3C;color:#F0E9D5;
      border:1px solid #1F4858;border-radius:8px;padding:10px;font-size:15px}
 .row{display:flex;gap:8px;margin:10px 0}
 button{flex:1;border:none;border-radius:8px;padding:12px;font-weight:700;font-size:15px}
 #go{background:#D4A24C;color:#0E2630}#syn{background:#7BC5C7;color:#0E2630}
 #paste{background:#16404F;color:#F0E9D5}
 #insight{display:none;background:#143038;border:1px solid #7BC5C7;border-radius:8px;
      padding:10px;margin:8px 0;font-size:14px;white-space:pre-wrap}
 .hit{background:#0E2630;border:1px solid #1F4858;border-radius:8px;padding:9px;margin:7px 0}
 .hit b{font-size:14px}.meta{color:#9CA3AF;font-size:12px}.why{color:#D4A24C}
 a{color:#7BC5C7;text-decoration:none}#st{color:#9CA3AF;font-size:13px;margin:4px 0}
</style></head><body>
<h1>✨ Egon Connect</h1>
<textarea id="t" placeholder="Paste what you're reading or writing…"></textarea>
<div class="row"><button id="paste">📋 Paste</button>
<button id="go">Connect</button><button id="syn">🧠 Synthesize</button></div>
<div id="st"></div><div id="insight"></div><div id="res"></div>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
const E={instapaper:'📰',zotero:'📚',paperpile:'📄',kindle:'📖',letterboxd:'🎬',
 youtube_music:'🎵',pocketcasts:'🎧',chrome_bookmarks:'🔖',chrome_tabs:'🗂️',
 notion_workspace:'🟦',tvtime:'📺','mind-memory':'🧠'};
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.getElementById('paste').onclick=async()=>{try{
 document.getElementById('t').value=await navigator.clipboard.readText();}catch(e){
 document.getElementById('st').textContent='clipboard blocked — long-press and paste manually';}};
async function call(path){
 const t=document.getElementById('t').value.trim();
 if(t.length<3){document.getElementById('st').textContent='paste some text first';return;}
 document.getElementById('st').textContent=path.includes('synth')?'🧠 thinking… (can take ~40s)':'connecting…';
 try{
  const r=await fetch(path+'?k='+encodeURIComponent(K),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  if(r.status===403){document.getElementById('st').textContent='wrong token in URL';return;}
  render(await r.json());
 }catch(e){document.getElementById('st').textContent='Egon not reachable — same WiFi as the PC?';}}
function render(d){
 const syn=d.synthesis||{};const ins=document.getElementById('insight');
 if(syn.status==='ok'){ins.style.display='block';ins.textContent='🧠 '+syn.insight;}
 const conns=d.connections||[];
 document.getElementById('st').textContent=(d.mode||'')+' · '+conns.length+' connections';
 document.getElementById('res').innerHTML=conns.map(c=>
  '<div class="hit"><b>'+(E[c.source]||'•')+' '+esc((c.title||'').slice(0,90))+'</b>'+
  (c.url?' <a href="'+esc(c.url)+'" target="_blank">open ↗</a>':'')+
  '<div class="meta">'+esc(c.source)+(c.why&&c.why.length?' <span class="why">↳ '+
  esc(c.why.slice(0,4).join(', '))+'</span>':'')+'</div></div>').join('');}
document.getElementById('go').onclick=()=>call('/m/connect');
document.getElementById('syn').onclick=()=>call('/m/synthesize');
// Android app share-target: app opens /m?k=…&shared=<text> → prefill + auto-connect.
const SH=new URLSearchParams(location.search).get('shared');
if(SH&&SH.trim().length>2){document.getElementById('t').value=SH;call('/m/connect');}
</script></body></html>"""


def build_app():
    """The tiny LAN-facing FastAPI app. Three routes, all token-guarded."""
    token = get_token()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def _authed(req: Request) -> bool:
        return secrets.compare_digest(req.query_params.get("k", ""), token)

    @app.get("/m")
    def page(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return HTMLResponse(_PAGE)

    @app.post("/m/connect")
    async def m_connect(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        from lib.connection_engine import connect
        return connect(str(body.get("text") or ""), limit=14)

    @app.post("/m/synthesize")
    async def m_synth(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        text = str(body.get("text") or "")
        from lib.connection_engine import connect
        res = connect(text, limit=12)
        if res.get("status") == "ok":
            from lib.synthesis import synthesize
            res["synthesis"] = synthesize(text, res.get("connections") or [])
        return res

    return app


def write_url_file() -> str:
    url = f"http://{_lan_ip()}:{MOBILE_PORT}/m?k={get_token()}"
    try:
        URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        URL_FILE.write_text(
            "Egon Connect — phone bookmark (keep private; contains your token):\n"
            + url + "\n", encoding="utf-8")
    except Exception:
        pass
    return url
