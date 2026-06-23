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
    """Read (or create once) the mobile token in egon-config.json.

    The stored value may be DPAPI-encrypted at rest (``__dpapi__:`` prefix, added
    for secrets hygiene). We MUST decrypt it before returning, otherwise the
    server authenticates against the opaque blob while the phone app sends the
    original plaintext token baked in at build time -> every request 403s. New
    tokens are stored encrypted but returned plaintext. Bruno 2026-06-23."""
    cfg = {}
    try:
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        pass
    tok = ((cfg.get("connect_mobile") or {}).get("token") or "").strip()
    if tok:
        try:
            from lib.secrets import decrypt_val
            return decrypt_val(tok)
        except Exception:
            return tok
    # First run — generate, persist (encrypted if DPAPI is available), return plaintext.
    tok = secrets.token_urlsafe(18)
    stored = tok
    try:
        from lib.secrets import encrypt_val
        stored = encrypt_val(tok)
    except Exception:
        pass
    cfg.setdefault("connect_mobile", {})["token"] = stored
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
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0A1A22">
<title>Egon Connect</title><style>
 :root{
  --bg0:#081519; --bg1:#0E2730; --surface:rgba(255,255,255,.045);
  --surface2:rgba(255,255,255,.07); --line:rgba(123,197,201,.16);
  --gold:#E6B65C; --teal:#7FCBCD; --text:#F2ECDB; --muted:#90A6AD;
 }
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 body{margin:0;color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
  background:
    radial-gradient(120% 70% at 50% -10%, #134150 0%, rgba(19,65,80,0) 55%),
    linear-gradient(180deg,var(--bg1) 0%,var(--bg0) 60%) fixed;
  min-height:100vh;padding:env(safe-area-inset-top) 16px calc(env(safe-area-inset-bottom) + 24px)}
 header{display:flex;align-items:center;gap:10px;padding:18px 2px 14px}
 .mark{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;font-size:18px;
  background:linear-gradient(135deg,var(--gold),#caa044);color:#0A1A22;
  box-shadow:0 4px 14px rgba(230,182,92,.35)}
 .brand{font-size:18px;font-weight:800;letter-spacing:.2px}
 .brand small{display:block;font-size:11.5px;font-weight:500;color:var(--muted);letter-spacing:.1px;margin-top:1px}
 .composer{background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:12px;backdrop-filter:blur(8px);box-shadow:0 8px 30px rgba(0,0,0,.25)}
 textarea{width:100%;height:108px;resize:none;background:transparent;color:var(--text);
  border:0;outline:none;font-size:15.5px;line-height:1.5;padding:2px}
 textarea::placeholder{color:var(--muted)}
 .actions{display:flex;gap:8px;margin-top:10px}
 button{flex:1;border:none;border-radius:11px;padding:12px 10px;font-weight:700;font-size:14.5px;
  cursor:pointer;transition:transform .08s ease,filter .15s ease;color:#08171c}
 button:active{transform:scale(.96)}
 #go{background:linear-gradient(135deg,var(--gold),#d6a548);box-shadow:0 4px 16px rgba(230,182,92,.3)}
 #syn{background:linear-gradient(135deg,var(--teal),#5fb6b8);box-shadow:0 4px 16px rgba(127,203,205,.25)}
 #paste{flex:0 0 auto;padding:12px 16px;background:var(--surface2);color:var(--text);
  border:1px solid var(--line)}
 #st{color:var(--muted);font-size:13px;margin:14px 2px 2px;min-height:16px;display:flex;align-items:center;gap:7px}
 .dot{width:7px;height:7px;border-radius:50%;background:var(--teal);animation:pulse 1s infinite ease-in-out}
 @keyframes pulse{0%,100%{opacity:.3;transform:scale(.8)}50%{opacity:1;transform:scale(1.1)}}
 #insight{display:none;background:linear-gradient(180deg,rgba(127,203,205,.12),rgba(127,203,205,.05));
  border:1px solid rgba(127,203,205,.32);border-radius:14px;padding:14px 14px 14px 16px;margin:12px 0;
  font-size:14.5px;line-height:1.55;white-space:pre-wrap;position:relative}
 #insight::before{content:"🧠 synthesis";display:block;font-size:11px;font-weight:700;letter-spacing:.6px;
  text-transform:uppercase;color:var(--teal);margin-bottom:6px}
 .sec{display:flex;align-items:center;gap:8px;margin:18px 2px 8px;font-size:12px;font-weight:700;
  letter-spacing:.7px;text-transform:uppercase}
 .sec.arch{color:var(--gold)} .sec.mind{color:var(--teal)}
 .sec::after{content:"";flex:1;height:1px;background:var(--line)}
 .hit{display:flex;gap:11px;background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:12px;margin:9px 0;transition:transform .08s ease,background .15s ease}
 .hit:active{transform:scale(.99);background:var(--surface2)}
 .chip{flex:0 0 auto;width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
  font-size:17px;background:var(--surface2)}
 .hit .body{flex:1;min-width:0}
 .hit .ttl{font-size:14.5px;font-weight:650;line-height:1.35;margin-bottom:3px}
 .hit .src{font-size:11.5px;color:var(--muted)}
 .pills{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
 .pill{font-size:11px;color:var(--gold);background:rgba(230,182,92,.12);
  border:1px solid rgba(230,182,92,.22);border-radius:999px;padding:2px 8px;white-space:nowrap}
 .links{display:flex;align-items:center;gap:12px;margin-top:9px}
 .open{display:inline-block;background:rgba(127,203,205,.13);border:1px solid rgba(127,203,205,.3);
  border-radius:9px;padding:6px 12px;color:var(--teal);font-weight:700;font-size:13px}
 .open.app{background:linear-gradient(135deg,rgba(127,203,205,.22),rgba(127,203,205,.1))}
 .web{font-size:12px;color:var(--muted)}
 .empty{color:var(--muted);text-align:center;padding:30px 10px;font-size:14px}
 a{color:var(--teal);text-decoration:none}
</style></head><body>
<header>
 <div class="mark">✨</div>
 <div class="brand">Egon Connect<small>what in your world connects to this?</small></div>
</header>
<div class="composer">
 <textarea id="t" placeholder="Paste what you're reading or writing…"></textarea>
 <div class="actions">
  <button id="paste">Paste</button>
  <button id="go">Connect</button>
  <button id="syn">Synthesize</button>
 </div>
</div>
<div id="st"></div><div id="insight"></div><div id="res"></div>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
const E={instapaper:'📰',zotero:'📚',paperpile:'📄',kindle:'📖',letterboxd:'🎬',
 youtube_music:'🎵',pocketcasts:'🎧',chrome_bookmarks:'🔖',chrome_tabs:'🗂️',
 notion_workspace:'🟦',tvtime:'📺','mind-memory':'🧠'};
const $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const setStatus=(t,busy)=>{$('st').innerHTML=(busy?'<span class="dot"></span>':'')+esc(t);};
const ANDROID=/Android/i.test(navigator.userAgent);
// Open a hit in its native phone app when we have an Android deep link
// (Drive file → Drive, Notion note → Notion, …); otherwise open the web URL.
// The intent:// link carries a browser fallback, so an uninstalled app still
// lands on the web page. Off Android (iOS/desktop) we always use the web URL.
function links(c){
 if(!c.url) return '';
 const useApp=ANDROID&&c.app_url;
 const primary=useApp?c.app_url:c.url;
 const label=useApp?('Open in '+esc(c.app_label||'app')):'open ↗';
 let html='<div class="links"><a class="open'+(useApp?' app':'')+'" href="'+esc(primary)+'">'+label+'</a>';
 if(useApp) html+='<a class="web" href="'+esc(c.url)+'" target="_blank">web ↗</a>';
 return html+'</div>';
}
$('paste').onclick=async()=>{try{
 $('t').value=await navigator.clipboard.readText();$('t').focus();}catch(e){
 setStatus('clipboard blocked — long-press the box and paste');}};
async function call(path){
 const t=$('t').value.trim();
 if(t.length<3){setStatus('paste some text first');return;}
 $('insight').style.display='none';
 setStatus(path.includes('synth')?'thinking… (can take ~40s)':'connecting…',true);
 try{
  const r=await fetch(path+'?k='+encodeURIComponent(K),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  if(r.status===403){setStatus('wrong token in the link');return;}
  render(await r.json());
 }catch(e){setStatus('Egon not reachable — on the same WiFi as the PC?');}}
function card(c){
 const why=(c.why&&c.why.length)?'<div class="pills">'+c.why.slice(0,5).map(w=>
   '<span class="pill">'+esc(w)+'</span>').join('')+'</div>':'';
 return '<div class="hit"><div class="chip">'+(E[c.source]||'•')+'</div>'+
  '<div class="body"><div class="ttl">'+esc((c.title||'').slice(0,110))+'</div>'+
  '<div class="src">'+esc(c.source||'')+'</div>'+why+links(c)+'</div></div>';}
function render(d){
 const syn=d.synthesis||{};const ins=$('insight');
 if(syn.status==='ok'){ins.style.display='block';ins.textContent=syn.insight;}
 const conns=d.connections||[];
 setStatus((conns.length||'no')+' connection'+(conns.length===1?'':'s')+(d.mode?' · '+d.mode:''));
 const arch=conns.filter(c=>c.source!=='mind-memory');
 const mind=conns.filter(c=>c.source==='mind-memory');
 let html='';
 if(arch.length){html+='<div class="sec arch">From your archives</div>'+arch.map(card).join('');}
 if(mind.length){html+='<div class="sec mind">From your mind</div>'+mind.map(card).join('');}
 if(!conns.length){html='<div class="empty">No connections yet — try more distinctive words.</div>';}
 $('res').innerHTML=html;}
$('go').onclick=()=>call('/m/connect');
$('syn').onclick=()=>call('/m/synthesize');
// Android app share-target: app opens /m?k=…&shared=<text> → prefill + auto-connect.
const SH=new URLSearchParams(location.search).get('shared');
if(SH&&SH.trim().length>2){$('t').value=SH;call('/m/connect');}
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
        # Fast lexical-only path so the phone answers in <1s instead of timing
        # out on the ~50s brute-force semantic pass. Restore semantic_search=True
        # once turbovec makes the embedding query sub-second. Bruno 2026-06-23.
        return connect(str(body.get("text") or ""), limit=14, semantic_search=False)

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
