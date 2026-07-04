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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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
 .actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
 button{flex:1;border:none;border-radius:11px;padding:12px 10px;font-weight:700;font-size:14.5px;
  cursor:pointer;transition:transform .08s ease,filter .15s ease;color:#08171c}
 button:active{transform:scale(.96)}
 #go{background:linear-gradient(135deg,var(--gold),#d6a548);box-shadow:0 4px 16px rgba(230,182,92,.3)}
 #syn{background:linear-gradient(135deg,var(--teal),#5fb6b8);box-shadow:0 4px 16px rgba(127,203,205,.25)}
 #paste,#cap{flex:1 1 70px;padding:12px 12px;background:var(--surface2);color:var(--text);
  border:1px solid var(--line)}
 #go,#syn{flex:1 1 70px}
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
 .capture{width:100%;border:none;border-radius:13px;padding:14px;margin-bottom:12px;
  font-weight:800;font-size:15.5px;color:#08171c;
  background:linear-gradient(135deg,var(--gold),#d6a548);box-shadow:0 4px 16px rgba(230,182,92,.3)}
 .share{font-size:12px;color:var(--muted)}
 .drainbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:18px;
  padding:11px 13px;background:var(--surface);border:1px solid var(--line);border-radius:12px;
  font-size:12.5px;color:var(--muted)}
 .drainbar b{color:var(--text)}
 .drainbar button{flex:0 0 auto;padding:8px 12px;border-radius:9px;border:1px solid var(--line);
  background:var(--surface2);color:var(--text);font-weight:700;font-size:12.5px}
 /* tab bar */
 .tabs{display:flex;gap:6px;margin:2px 0 14px;background:var(--surface);border:1px solid var(--line);
  border-radius:13px;padding:5px}
 .tabs button{flex:1;background:transparent;color:var(--muted);border:none;border-radius:9px;
  padding:10px 6px;font-weight:700;font-size:13.5px}
 .tabs button.on{background:var(--surface2);color:var(--text)}
 /* chat */
 #chatlog{display:flex;flex-direction:column;gap:9px;min-height:42vh;padding:2px 0 12px}
 .msg{max-width:88%;padding:10px 12px;border-radius:13px;font-size:14.5px;line-height:1.5}
 .msg .txt{white-space:pre-wrap}
 .msg.u{align-self:flex-end;background:rgba(127,203,205,.14);border:1px solid rgba(127,203,205,.28)}
 .msg.a{align-self:flex-start;background:var(--surface);border:1px solid var(--line)}
 .msg.err .txt{color:#f2a0a0}
 .msg .who{font-size:10.5px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;
  margin-bottom:3px;color:var(--gold)}
 .msg.u .who{color:var(--teal)}
 .prov{display:flex;gap:8px;align-items:center;margin-bottom:8px;font-size:12px;color:var(--muted)}
 .prov select{background:var(--surface2);color:var(--text);border:1px solid var(--line);
  border-radius:8px;padding:6px 8px;font-size:12.5px}
 .chatbar{position:sticky;bottom:0;display:flex;gap:8px;padding:8px 0 2px;
  background:linear-gradient(180deg,rgba(8,21,25,0),var(--bg0) 45%)}
 .chatbar textarea{height:54px;flex:1;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;color:var(--text);padding:9px;font-size:15px;resize:none;outline:none}
 .chatbar button{flex:0 0 70px;color:#08171c;background:linear-gradient(135deg,var(--gold),#d6a548)}
 /* oversee */
 .ocard{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:12px;margin:9px 0}
 .ocard .ttl{font-weight:700;font-size:14px;margin-bottom:4px}
 .ocard .meta{font-size:12.5px;color:var(--muted);line-height:1.45}
 .prop{background:var(--surface);border:1px solid rgba(230,182,92,.25);border-radius:12px;padding:11px;margin:8px 0}
 .prop .btns{display:flex;gap:8px;margin-top:10px}
 .prop .btns button{flex:1;padding:10px;color:#08171c;font-weight:700;border-radius:9px;border:none}
 .prop .appr{background:linear-gradient(135deg,var(--gold),#d6a548)}
 .prop .veto{background:var(--surface2);color:var(--text);border:1px solid var(--line)}
</style></head><body>
<header>
 <div class="mark">✨</div>
 <div class="brand">Egon Connect<small>what in your world connects to this?</small></div>
</header>
<div class="tabs">
 <button data-t="connect" class="on">Connect</button>
 <button data-t="oversee">Oversee</button>
</div>
<div id="view-connect">
<div class="composer">
 <textarea id="t" placeholder="Type, paste, or capture — then Search…"></textarea>
 <div class="actions">
  <button id="cap" style="display:none">📸 Capture</button>
  <button id="paste">Paste</button>
  <button id="go">Search</button>
  <button id="syn">Synthesize</button>
 </div>
</div>
<div id="st"></div><div id="insight"></div><div id="res"></div>
<div id="drainbar" class="drainbar" style="display:none"></div>
</div>
<div id="view-oversee" style="display:none">
 <!-- ONE surface (Bruno 2026-07-02): talk AND command here. Orders are
      auto-detected and dispatched to the orchestrator; the reply describes
      what was queued. The dashboard below shows agents + proposals live. -->
 <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <span style="color:var(--gold);font-size:11px;font-weight:800;letter-spacing:.5px">💬</span>
  <select id="csess" style="flex:1;background:var(--surface2);color:var(--text);
   border:1px solid var(--line);border-radius:9px;padding:9px 10px;font-size:13.5px"></select>
  <button id="cnew" style="flex:0 0 auto;padding:9px 13px;border-radius:9px;font-size:13px;
   font-weight:700;background:var(--surface2);color:var(--gold);border:1px solid var(--line)">＋ New</button>
 </div>
 <div class="prov">Model
  <select id="cprov"><option value="gemini">gemini</option>
   <option value="claude">claude</option><option value="openai">openai</option></select>
  <span id="cnote"></span></div>
 <div id="chatlog"></div>
 <div id="cchips" style="display:flex;flex-wrap:wrap;gap:6px;margin:4px 0"></div>
 <div class="chatbar">
  <button id="cattach" style="flex:0 0 46px;background:var(--surface2);color:var(--text);
   border:1px solid var(--line);font-size:17px">📎</button>
  <textarea id="cin" placeholder="Talk to Egon or give an order — orders are dispatched to your agents…"></textarea>
  <button id="csend">Send</button>
 </div>
 <input id="cfile" type="file" multiple style="display:none"
  accept="image/*,audio/*,video/*,.pdf,.docx,.txt,.md,.json,.csv">
 <div class="sec mind" style="margin-top:20px">Agents &amp; proposals</div>
 <div id="orchbody"><div class="empty">loading…</div></div>
 <div style="text-align:center;margin:18px 0 6px">
  <a id="apkdl" style="color:var(--muted);font-size:12px" href="#">⬇ update Egon app</a>
 </div>
</div>
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
 let html='<div class="links">';
 if(c.url){
  const useApp=ANDROID&&c.app_url;
  const primary=useApp?c.app_url:c.url;
  const label=useApp?('Open in '+esc(c.app_label||'app')):'open ↗';
  html+='<a class="open'+(useApp?' app':'')+'" href="'+esc(primary)+'">'+label+'</a>';
  if(useApp) html+='<a class="web" href="'+esc(c.url)+'" target="_blank">web ↗</a>';
 }
 html+='<a class="share" href="javascript:void 0" data-u="'+esc(c.url||'')+'" data-t="'+esc(c.title||'')+'">share</a>';
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

// Native bridge (injected by the Android app's WebView as `Android`): lets the
// page read the screen and toggle USB debugging. Absent on iOS/desktop.
const BR=(typeof Android!=='undefined')?Android:null;

// CAPTURE: read the screen behind the panel via the accessibility service, fill
// the box, and search. Distinct from Search (which uses whatever's in the box).
if(BR&&BR.captureScreen){
 $('cap').style.display='block';
 $('cap').onclick=async()=>{
  // Ask the panel to re-read the app CURRENTLY behind it (drops panel focus
  // briefly so a11y can see it), then read the refreshed text. Works even if the
  // content underneath changed since the panel opened. requestCapture is absent
  // on older builds — the open-time snapshot still serves as a fallback.
  setStatus('capturing…');
  try{ if(BR.requestCapture) BR.requestCapture(); }catch(e){}
  await new Promise(r=>setTimeout(r, BR.requestCapture?320:0));
  let txt='';try{txt=BR.captureScreen()||'';}catch(e){}
  if(txt.trim().length<3){setStatus('nothing readable on screen — type or paste instead');return;}
  $('t').value=txt;call('/m/connect');
 };
}
// SHARE each result (native share sheet).
$('res').addEventListener('click',ev=>{
 const a=ev.target.closest('.share');if(!a)return;ev.preventDefault();
 const data={title:a.dataset.t||'Egon',text:a.dataset.t||'',url:a.dataset.u||''};
 if(navigator.share){navigator.share(data).catch(()=>{});}
 else if(a.dataset.u){window.open(a.dataset.u,'_blank');}
});
// USB-debug toggle (tab-sync). The app holds WRITE_SECURE_SETTINGS so it flips
// both ways with no deadlock: ON to let egon sync/close tabs, OFF for banking.
function renderDrain(){
 if(!BR||!BR.setUsbDebug)return;
 let on=false;try{on=BR.usbDebugOn();}catch(e){}
 $('drainbar').style.display='flex';
 $('drainbar').innerHTML='<span>Tab-sync debugging: <b>'+(on?'ON':'OFF — banking safe')+'</b></span>'+
  '<button id="draintog">'+(on?'Turn OFF':'Turn ON')+'</button>';
 $('draintog').onclick=()=>{try{BR.setUsbDebug(!on);}catch(e){}setTimeout(renderDrain,900);};
}
renderDrain();

// Android app share-target: app opens /m?k=…&shared=<text> → prefill + auto-connect.
const SH=new URLSearchParams(location.search).get('shared');
if(SH&&SH.trim().length>2){$('t').value=SH;call('/m/connect');}

// ── Tabs ────────────────────────────────────────────────────────────────────
function tab(name){
 for(const v of ['connect','oversee'])
  $('view-'+v).style.display=(v===name)?'block':'none';
 document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===name));
 if(name==='oversee'){loadOrch();checkProviders();setTimeout(()=>$('cin').focus(),50);}
}
document.querySelector('.tabs').addEventListener('click',ev=>{
 const b=ev.target.closest('button[data-t]');if(b)tab(b.dataset.t);});
$('apkdl').href='/m/apk?k='+encodeURIComponent(K);

// ── Chat (streaming SSE) ─────────────────────────────────────────────────────
// Persistence: the conversation (text only — attachments elided) survives
// reloads via localStorage. Attachments: 📎 → /m/attach converts each file to a
// message part (images/audio/video base64 through; documents text-extracted).
let CHIST=[];let PROV_OK=false;let CATT=[];let CHID='';
// Separate conversations, shared with the desktop (server = source of truth;
// localStorage per-session = offline cache).
function cacheKey(){return 'egon_chat_'+(CHID||'default');}
function saveChat(){
 const el=CHIST.map(m=>({role:m.role,content:(typeof m.content==='string')?m.content:
  m.content.map(p=>p.data?{...p,data:'',elided:true}:p)}));
 try{localStorage.setItem(cacheKey(),JSON.stringify(el));}catch(e){}
 if(CHID) fetch('/m/chats/'+encodeURIComponent(CHID)+'?k='+encodeURIComponent(K),
  {method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({history:el})}).catch(()=>{});
}
function showChat(h){
 CHIST=Array.isArray(h)?h:[];$('chatlog').innerHTML='';renderHist();
 try{localStorage.setItem(cacheKey(),JSON.stringify(CHIST));}catch(e){}
}
async function openChat(id){
 CHID=id;
 try{
  const r=await fetch('/m/chats/'+encodeURIComponent(id)+'?k='+encodeURIComponent(K));
  const h=(await r.json()).history;
  let local=[];try{local=JSON.parse(localStorage.getItem(cacheKey())||'[]')||[];}catch(e){}
  // never lose messages: longer thread wins; push local up if it's ahead
  if(Array.isArray(h)&&h.length>=local.length){showChat(h);}
  else{showChat(local);saveChat();}
 }catch(e){
  let local=[];try{local=JSON.parse(localStorage.getItem(cacheKey())||'[]')||[];}catch(e2){}
  showChat(local);
 }
}
async function loadSessions(sel){
 try{
  const r=await fetch('/m/chats?k='+encodeURIComponent(K));
  const d=await r.json();
  const s=$('csess');s.innerHTML='';
  for(const x of (d.sessions||[])){
   const o=document.createElement('option');o.value=x.id;
   o.textContent=(x.title||x.id).slice(0,34);s.appendChild(o);
  }
  const pick=sel||d.current||(d.sessions[0]&&d.sessions[0].id);
  if(pick){s.value=pick;await openChat(pick);}
 }catch(e){}
}
$('csess').addEventListener('change',()=>openChat($('csess').value));
$('cnew').onclick=async()=>{
 try{
  const r=await fetch('/m/chats/new?k='+encodeURIComponent(K),{method:'POST'});
  const d=await r.json();
  await loadSessions(d.id);
  $('cin').focus();
 }catch(e){}
};
loadSessions();
function scrollChat(){window.scrollTo(0,document.body.scrollHeight);}
function addMsg(role,text){
 const d=document.createElement('div');d.className='msg '+role;
 d.innerHTML='<div class="who">'+(role==='u'?'You':'Egon')+'</div><div class="txt"></div>';
 d.querySelector('.txt').textContent=text;
 $('chatlog').appendChild(d);scrollChat();return d;
}
function renderHist(){
 for(const m of CHIST){
  let t=(typeof m.content==='string')?m.content:
   m.content.filter(p=>p.type==='text').map(p=>p.text).join(' ')+
   m.content.filter(p=>p.type!=='text').map(p=>' 📎'+(p.name||p.type)).join('');
  addMsg(m.role==='user'?'u':'a',t);
 }}
renderHist();
const ICO={image:'🖼',audio:'🎵',video:'🎬',document:'📄'};
function renderChips(){
 $('cchips').innerHTML=CATT.map((p,i)=>'<span data-i="'+i+'" style="background:var(--surface2);'+
  'border:1px solid var(--line);border-radius:9px;padding:4px 9px;font-size:12px">'+
  (ICO[p.type]||'📄')+' '+esc((p.name||'').slice(0,24))+' ✕</span>').join('');}
$('cchips').addEventListener('click',ev=>{
 const s=ev.target.closest('span[data-i]');if(!s)return;
 CATT.splice(parseInt(s.dataset.i,10),1);renderChips();});
$('cattach').onclick=()=>$('cfile').click();
$('cfile').addEventListener('change',async ev=>{
 for(const f of ev.target.files||[]){
  if(f.size>20*1024*1024){addMsg('a','⚠ '+f.name+' too large (>20MB)');continue;}
  const b64=await new Promise(res=>{const r=new FileReader();
   r.onload=()=>res(String(r.result).split(',')[1]||'');r.readAsDataURL(f);});
  try{
   const r=await fetch('/m/attach?k='+encodeURIComponent(K),{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:f.name,mime:f.type,data:b64})});
   const p=await r.json();
   if(p.error){addMsg('a','⚠ '+f.name+': '+p.error);continue;}
   CATT.push(p);renderChips();
  }catch(e){addMsg('a','⚠ upload failed — same WiFi as the PC?');}
 }
 ev.target.value='';
});
async function checkProviders(){
 if(PROV_OK)return;
 try{const r=await fetch('/m/providers?k='+encodeURIComponent(K));const a=await r.json();
  const sel=$('cprov');let first=null;
  [...sel.options].forEach(o=>{const ok=a[o.value];o.textContent=o.value+(ok?'':' (no key)');
   if(ok&&first===null)first=o.value;});
  if(first){sel.value=first;PROV_OK=true;}
  $('cnote').textContent=first?'':'no keys configured';
 }catch(e){}
}
async function sendChat(){
 const inp=$('cin');const t=inp.value.trim();
 if(!t&&!CATT.length)return;
 if($('csend').disabled)return;
 const atts=CATT.slice();CATT=[];renderChips();
 const content=atts.length?((t?[{type:'text',text:t}]:[]).concat(atts)):t;
 inp.value='';
 addMsg('u',t+(atts.length?(' '+atts.map(p=>' 📎'+(p.name||p.type)).join('')):''));
 CHIST.push({role:'user',content:content});saveChat();
 if(CHIST.length===1&&t){const o=$('csess').selectedOptions[0];if(o)o.textContent=t.slice(0,34);}
 const a=addMsg('a','');const tx=a.querySelector('.txt');tx.textContent='…';
 $('csend').disabled=true;
 let acc='';let first=true;
 try{
  const r=await fetch('/m/chat?k='+encodeURIComponent(K),{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({messages:CHIST,provider:$('cprov').value})});
  if(r.status===403){tx.textContent='⚠ wrong token';a.classList.add('err');$('csend').disabled=false;return;}
  const rd=r.body.getReader();const dec=new TextDecoder();let buf='';
  while(true){const{value,done}=await rd.read();if(done)break;
   buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf('\\n\\n'))>=0){
    const line=buf.slice(0,i).trim();buf=buf.slice(i+2);
    if(!line.startsWith('data:'))continue;
    const p=line.slice(5).trim();if(p==='[DONE]')continue;
    try{const o=JSON.parse(p);
     if(o.error){tx.textContent='⚠ '+o.error;a.classList.add('err');}
     else if(o.t){if(first){tx.textContent='';first=false;}acc+=o.t;tx.textContent=acc;scrollChat();}
    }catch(e){}
   }
  }
  if(acc){CHIST.push({role:'assistant',content:acc});saveChat();}
  setTimeout(loadOrch,900);   // an order may have queued tasks — refresh the deck
 }catch(e){tx.textContent='⚠ Egon not reachable — same WiFi as the PC?';a.classList.add('err');}
 $('csend').disabled=false;
}
$('csend').onclick=sendChat;
$('cin').addEventListener('keydown',e=>{
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}});

// ── Oversee (orchestrator + Hermes) ─────────────────────────────────────────
async function loadOrch(){
 $('orchbody').innerHTML='<div class="empty">loading…</div>';
 try{
  const r=await fetch('/m/orch?k='+encodeURIComponent(K));
  if(r.status===403){$('orchbody').innerHTML='<div class="empty">wrong token</div>';return;}
  renderOrch(await r.json());
 }catch(e){$('orchbody').innerHTML='<div class="empty">core not reachable</div>';}
}
function renderOrch(d){
 if(d.error){$('orchbody').innerHTML='<div class="empty">'+esc(d.error)+'</div>';return;}
 const m=d.mission||{};const s=m.summary||{};const agents=m.agents||{};
 const au=(d.autonomy&&(d.autonomy.autonomy||d.autonomy))||{};
 let h='<div class="ocard"><div class="ttl">Mission · autonomy '+(au.enabled?'ON':'OFF')+'</div>'+
  '<div class="meta">active '+(s.active_work||0)+' · paused '+(s.paused||0)+
  ' · clarification '+(s.needs_clarification||0)+' · leases '+(s.open_leases||0)+'</div></div>';
 const names=Object.keys(agents);
 if(names.length){h+='<div class="sec mind">Agents</div>';}
 for(const name of names){const info=agents[name]||{};
  const st=((info.state||{}).status)||'idle';
  const ct=((info.current_task||{}).sub_task_desc)||'no active task';
  const le=(info.latest_event||{}).content||'';
  h+='<div class="ocard"><div class="ttl">'+esc(name)+' · '+esc(st)+'</div>'+
   '<div class="meta">'+esc(ct.slice(0,150))+
   (le?('<br>latest: '+esc(le.slice(0,120))):'')+'</div></div>';
 }
 // Sessions across all AIs, newest first (canonical project attached).
 const sess=d.sessions||[];
 if(sess.length){h+='<div class="sec mind">Sessions</div>';
  for(const s of sess.slice(0,8)){
   const when=s.started_at?new Date(s.started_at*1000).toLocaleString([],{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
   h+='<div class="ocard"><div class="ttl">'+esc(s.agent||'?')+' · '+esc(s.project||'')+
    ' <span style="color:var(--muted);font-weight:400;font-size:11px">'+esc(when)+'</span></div>'+
    '<div class="meta">'+esc((s.goal||s.external_id||'').slice(0,120))+'</div></div>';
  }
 }
 // Visibility: what the agents actually DID — outcomes, not just status.
 const done=(m.tasks||[]).filter(t=>['completed','failed'].includes(t.status)).slice(0,6);
 if(done.length){h+='<div class="sec mind">Recent results</div>';
  for(const t of done){const mark=t.status==='completed'?'✓':'✗';
   const ev=((t.latest_event||{}).content||'').slice(0,110);
   h+='<div class="ocard"><div class="ttl">'+mark+' #'+t.id+' · '+esc(t.agent_name||'')+'</div>'+
    '<div class="meta">'+esc((t.sub_task_desc||'').slice(0,130))+
    (ev?('<br><span style="color:var(--teal)">'+esc(ev)+'</span>'):'')+'</div></div>';
  }
 }
 const props=d.proposals||[];
 if(props.length){h+='<div class="sec arch">Hermes proposals — your call</div>';
  for(const p of props.slice(0,12)){const tier=p.masterlaw_tier||'ok';const tid=p.task_id;
   const mark={block:'⛔ BLOCK',confirm:'⚠ ASK',ok:'✓ OK'}[tier]||'·';
   h+='<div class="prop"><div>'+mark+' #'+tid+' → '+esc(p.agent||'')+'<br>'+
    esc((p.why||'').slice(0,140))+'</div><div class="btns">';
   if(tier!=='block')h+='<button class="appr" data-act="requeue" data-tid="'+tid+'">Approve</button>';
   h+='<button class="veto" data-act="cancel" data-tid="'+tid+'">Veto</button></div></div>';
  }
 }else{h+='<div class="ocard"><div class="meta">Nothing needs your call right now.</div></div>';}
 $('orchbody').innerHTML=h;
}
$('orchbody').addEventListener('click',ev=>{
 const b=ev.target.closest('button[data-act]');if(!b)return;
 b.disabled=true;b.textContent='…';
 fetch('/m/orch/act?k='+encodeURIComponent(K),{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({task_id:parseInt(b.dataset.tid,10),action:b.dataset.act})})
  .then(()=>setTimeout(loadOrch,700)).catch(()=>setTimeout(loadOrch,700));
});
</script></body></html>"""


def build_app():
    """The tiny LAN-facing FastAPI app. Three routes, all token-guarded."""
    token = get_token()
    # Speed: use the locally-cached embedding model (skip the slow HF Hub online
    # check that dominated the ~57s cold start), and pre-warm the model +
    # turbovec index in the background so the FIRST phone search is already fast
    # (~0.4s) instead of paying the cold start. Bruno 2026-06-23.
    import os as _os
    _os.environ.setdefault("HF_HUB_OFFLINE", "1")
    _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    def _warm():
        try:
            from lib.connection_engine import connect
            connect("warmup", limit=1)
        except Exception:
            pass
    import threading as _th
    _th.Thread(target=_warm, name="connect-warmup", daemon=True).start()
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
        import asyncio
        from lib.connection_engine import connect
        # Full semantic search over the WHOLE index (Drive, Letterboxd, YouTube,
        # Zotero, …) via turbovec (~1s warm). Run it in a WORKER THREAD: the
        # search is blocking, and running it inline froze the event loop so
        # /api/v1/mind/stats stopped answering — egon_core's health probe then
        # timed out and RESTARTED mind_service mid-search, so the warm model/
        # turbo cache never survived and every query was cold (14-57s flapping).
        # Off-loading keeps the loop responsive → no restart → cache persists.
        # turbovec is thread-safe (verified). Bruno 2026-06-24.
        # semantic_search=True, lexical_search=False: the turbovec index already
        # spans every source in ~1s; the lexical archive scan added ~30s.
        return await asyncio.to_thread(
            connect, str(body.get("text") or ""), 14, True, False)

    @app.post("/m/synthesize")
    async def m_synth(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        text = str(body.get("text") or "")
        import asyncio
        def _work():
            from lib.connection_engine import connect
            res = connect(text, limit=12)
            if res.get("status") == "ok":
                from lib.synthesis import synthesize
                res["synthesis"] = synthesize(text, res.get("connections") or [])
            return res
        # Worker thread: keep the event loop free during the blocking search +
        # synthesis (see m_connect note on the flapping). Bruno 2026-06-24.
        return await asyncio.to_thread(_work)

    # ── Chat: a real streaming conversation on the phone, same cloud backend as
    # the desktop Mission Control chat. One-directional (types in → model out,
    # vault injected as data; never dispatches agents). SSE stream so the reply
    # appears token-by-token. Bruno 2026-07-01. ──────────────────────────────
    @app.get("/m/providers")
    def m_providers(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            from lib import egon_chat
            return JSONResponse(egon_chat.available_providers())
        except Exception as e:
            return JSONResponse({"error": str(e)[:120]}, status_code=500)

    @app.post("/m/chat")
    async def m_chat(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        messages = body.get("messages") or []
        provider = str(body.get("provider") or "gemini")

        def gen():
            # Sync generator → Starlette iterates it in a threadpool, keeping the
            # event loop free (same discipline as the search routes above).
            try:
                from lib import egon_chat
                got = False
                # Consolidated surface: orders are auto-detected and dispatched
                # to the orchestrator; the reply describes what was queued.
                for piece in egon_chat.stream_chat_with_dispatch(
                    messages, provider=provider
                ):
                    if piece:
                        got = True
                        yield "data: " + json.dumps({"t": piece}) + "\n\n"
                if not got:
                    yield "data: " + json.dumps({"error": "empty reply"}) + "\n\n"
            except Exception as e:
                msg = str(e)
                if "429" in msg:
                    msg = "rate-limited (429) — pick another provider"
                yield "data: " + json.dumps({"error": msg[:200]}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ── Attachments from the phone: images/audio/video pass straight through as
    # base64 parts; documents (pdf/docx/txt) are saved to a temp file and run
    # through egon_chat.attach_from_path for text extraction. Bruno 2026-07-02.
    @app.post("/m/attach")
    async def m_attach(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        name = str(body.get("name") or "file")
        mime = str(body.get("mime") or "application/octet-stream")
        data = str(body.get("data") or "")          # base64, no data: prefix
        if not data:
            return JSONResponse({"error": "no data"}, status_code=400)
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        from lib import egon_chat as ec
        if ext in ec._IMAGE_EXT:
            return JSONResponse({"type": "image", "mime": mime, "data": data, "name": name})
        if ext in ec._AUDIO_EXT or ext in ec._VIDEO_EXT:
            kind = "audio" if ext in ec._AUDIO_EXT else "video"
            return JSONResponse({"type": kind, "mime": mime, "data": data, "name": name})
        # document: extract text server-side
        import asyncio, base64 as b64, tempfile, os as _os
        def _work():
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=ext or ".bin")
                with _os.fdopen(fd, "wb") as f:
                    f.write(b64.b64decode(data))
                part = ec.attach_from_path(tmp)
                return part or {"error": "could not read file"}
            except Exception as e:
                return {"error": str(e)[:120]}
            finally:
                try:
                    if tmp:
                        _os.remove(tmp)
                except Exception:
                    pass
        return JSONResponse(await asyncio.to_thread(_work))

    # ── Oversight: view the orchestrator/Hermes from the phone and act on
    # proposals (approve/veto). Proxies the local mind API (127.0.0.1:8000) so
    # the phone never talks to it directly. Bruno 2026-07-01. ─────────────────
    _MIND = "http://127.0.0.1:8000/api/v1/mind"

    @app.get("/m/orch")
    async def m_orch(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        import httpx
        out: dict = {"mission": {}, "proposals": []}
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r1 = await c.get(_MIND + "/orchestrator/mission-control?limit_events=20")
                out["mission"] = r1.json()
                r2 = await c.get(_MIND + "/orchestrator/hermes")
                out["proposals"] = (r2.json() or {}).get("proposals") or []
                try:
                    r4 = await c.get(_MIND + "/sessions?limit=8")
                    out["sessions"] = (r4.json() or {}).get("sessions") or []
                except Exception:
                    out["sessions"] = []
                try:
                    r3 = await c.get(_MIND + "/orchestrator/autonomy/status")
                    out["autonomy"] = r3.json()
                except Exception:
                    pass
        except Exception as e:
            out["error"] = str(e)[:140]
        return JSONResponse(out)

    @app.get("/m/apk")
    def m_apk(req: Request):
        """Serve the latest EgonConnect.apk so the phone can self-update with
        one tap (adb path needs wifi-debug on, which Bruno toggles off for
        banking). Token-guarded like everything else."""
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from fastapi.responses import FileResponse
        apk = ROOT / "state" / "EgonConnect.apk"
        if not apk.exists():
            return JSONResponse({"error": "apk not built"}, status_code=404)
        return FileResponse(str(apk), media_type="application/vnd.android.package-archive",
                            filename="EgonConnect.apk")

    # ── ONE conversation everywhere (Bruno 2026-07-04: "the app shows a
    # previous conversation, not the one I had today"). Desktop and phone were
    # keeping separate histories (chat_history.json vs localStorage). Now BOTH
    # read/write the same server-side file, so the thread follows you across
    # devices. Attachment payloads are elided as usual. ──────────────────────
    # Separate conversations (Bruno 2026-07-04) — one store for PC + phone via
    # lib/chat_store. /m/history stays as the CURRENT-session alias.
    from lib import chat_store

    @app.get("/m/chats")
    def m_chats(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"sessions": chat_store.list_sessions(),
                             "current": chat_store.current_id()})

    @app.post("/m/chats/new")
    def m_chats_new(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"id": chat_store.new_session()})

    @app.get("/m/chats/{sid}")
    def m_chats_get(sid: str, req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        chat_store.set_current(sid)
        return JSONResponse({"id": sid, "history": chat_store.load(sid)})

    @app.post("/m/chats/{sid}")
    async def m_chats_set(sid: str, req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        hist = body.get("history")
        if not isinstance(hist, list):
            return JSONResponse({"error": "history must be a list"}, status_code=400)
        chat_store.save(sid, hist)
        chat_store.set_current(sid)
        return JSONResponse({"status": "ok", "id": sid, "messages": len(hist)})

    @app.get("/m/history")
    def m_history_get(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        sid = chat_store.current_id()
        return JSONResponse({"id": sid, "history": chat_store.load(sid)})

    @app.post("/m/history")
    async def m_history_set(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        hist = body.get("history")
        if not isinstance(hist, list):
            return JSONResponse({"error": "history must be a list"}, status_code=400)
        sid = body.get("id") or chat_store.current_id()
        chat_store.save(sid, hist)
        return JSONResponse({"status": "ok", "id": sid, "messages": len(hist)})

    @app.get("/m/remote_url")
    def m_remote_url(req: Request):
        """Current public tunnel URL. The Android app calls this on every
        successful load and CACHES the result — so when the phone leaves the
        LAN, it already knows today's remote address. Self-updating; no rebuild
        when the quick-tunnel URL rotates. Bruno 2026-07-04."""
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            p = ROOT / "state" / "mobile_connect_url_remote.txt"
            url = p.read_text(encoding="utf-8").strip() if p.exists() else ""
            return JSONResponse({"remote_url": url})
        except Exception as e:
            return JSONResponse({"error": str(e)[:80]}, status_code=500)

    @app.post("/m/orch/dispatch")
    async def m_orch_dispatch(req: Request):
        """Bruno-initiated dispatch from the phone (2026-07-02: he sent an order
        via the phone expecting the orchestrator to pick it up — but Chat is
        deliberately one-directional and never dispatches). This is the explicit
        bridge: the Oversee tab's command box posts here, we proxy to the same
        /orchestrator/dispatch the desktop COMMAND panel uses. User-initiated,
        masterlaw-screened downstream — not model self-dispatch."""
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        prompt = str(body.get("prompt") or "").strip()
        if len(prompt) < 5:
            return JSONResponse({"error": "prompt too short"}, status_code=400)
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(_MIND + "/orchestrator/dispatch",
                                 json={"prompt": prompt})
                return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"error": str(e)[:140]}, status_code=502)

    @app.post("/m/orch/act")
    async def m_orch_act(req: Request):
        if not _authed(req):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await req.json()
        tid = body.get("task_id")
        action = str(body.get("action") or "")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.post(
                    f"{_MIND}/orchestrator/tasks/{tid}/control",
                    json={"action": action})
                return JSONResponse(r.json())
        except Exception as e:
            return JSONResponse({"error": str(e)[:120]}, status_code=502)

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
