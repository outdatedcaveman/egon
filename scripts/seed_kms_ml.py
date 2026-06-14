"""Bootstrap the native ML with this cleanup's labels (AI source)."""
import json
from pathlib import Path
from lib import kms_ml
BK = Path("state/panop/backups")
suspects = json.loads((BK/"suspects.json").read_text(encoding="utf-8"))
v = {}
for f in ("verdicts.json","verdicts2.json"):
    v.update({k:val for k,val in json.loads((BK/f).read_text(encoding="utf-8")).items() if not k.startswith("_")})
n=0
for idx,cat in v.items():
    it=suspects[int(idx)]
    if kms_ml.record({"url":it.get("url"),"title":it.get("title")}, cat, source="ai"): n+=1
print("seeded", n, "labels")
print("stats:", kms_ml.stats())
print("agreement:", kms_ml.agreement())
