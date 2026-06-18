"""Rebuild the AI worklist for the FULL taxonomy: exclude only TRUE trash
(search-result pages, auth/login, app dashboards, banking, localhost). Keep
videos, podcasts, shopping, tools, repos, wikis, news — they are CATEGORIES
under Routster's comprehensive taxonomy, not noise."""
import json, re
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import requests
ROOT=Path(__file__).resolve().parents[1]
HIST=Path.home()/"Desktop"/"Takeout"/"Chrome"/"History.json"
TRACK={'utm_source','utm_medium','utm_campaign','utm_term','utm_content','fbclid','gclid','mc_cid','mc_eid','igshid','_ga','ref','ref_src','yclid','msclkid','spm','share','shared','from','source','_hsenc','_hsmi','gad_source'}
def canon(u):
    try:
        p=urlparse(u);net=(p.netloc or '').lower()
        if net.startswith('m.'):net='www.'+net[2:]
        path=(p.path or '').rstrip('/') or '/'
        qs=sorted((k,v) for k,v in parse_qsl(p.query) if k.lower() not in TRACK)
        return urlunparse(((p.scheme or 'https').lower(),net,path,'',urlencode(qs),''))
    except: return u
# TRUE trash only
TRASH_SUBSTR=("/search?","google.com/search","bing.com/search","duckduckgo.com/?q","/sso","/oauth","/login","/signin","/sign-in","/auth/","accounts.google","myaccount.google","mail.google.com","outlook.live","outlook.office","calendar.google","/checkout","/cart","localhost","127.0.0.1","chrome://","chrome-extension://","about:blank","web.whatsapp","messenger.com","speedtest","nubank","itau.com","bradesco","santander","/wp-admin","/wp-login","translate.google")
TRASH_HOST_EXACT={"google.com","www.google.com","bing.com","www.bing.com","duckduckgo.com","gmail.com","chatgpt.com","chat.openai.com","claude.ai","gemini.google.com"}
def is_trash(u):
    p=urlparse(u);host=(p.netloc or '').lower();path=(p.path or '').strip('/')
    if host in TRASH_HOST_EXACT and not path: return True
    low=u.lower()
    return any(s in low for s in TRASH_SUBSTR)
bh=json.loads(HIST.read_text(encoding='utf-8')).get('Browser History') or []
seen={}
for e in bh:
    u=e.get('url')
    if not u or not u.startswith('http'): continue
    t=(e.get('title') or '').strip()
    if u not in seen or (t and not seen[u]): seen[u]=t
pe=json.loads((ROOT/'panop_env.json').read_text(encoding='utf-8-sig'))
H={'Zotero-API-Key':pe['zotero_api_key'],'Zotero-API-Version':'3'}
base=f"https://api.zotero.org/users/{pe['zotero_user_id']}"
existing=set()
for ck in ['GKSJSJMJ','B3XGDC4J','BRZ3UUIR','24A43HSI']:
    start=0
    while True:
        r=requests.get(f'{base}/collections/{ck}/items/top?limit=100&start={start}',headers=H,timeout=40)
        if r.status_code!=200: break
        b=r.json()
        if not b: break
        for it in b:
            uu=it.get('data',{}).get('url')
            if uu: existing.add(canon(uu))
        if len(b)<100: break
        start+=len(b)
work=[];trash=0;already=0
for u,t in seen.items():
    if canon(u) in existing: already+=1; continue
    if is_trash(u): trash+=1; continue
    work.append({'url':u,'title':t})
json.dump(work, open('state/panop/routster_ai_worklist.json','w',encoding='utf-8'), ensure_ascii=False)
print(f"unique:{len(seen)} | already-saved:{already} | true-trash:{trash} | AI worklist:{len(work)}")
