import json, subprocess, time, os
ADB=os.path.expanduser("~/AppData/Local/Android/Sdk/platform-tools/adb.exe")
stuck=json.load(open("state/panop/stuck_links.json",encoding="utf-8"))
def adb(*a,t=20):
    try: return subprocess.run([ADB,*a],capture_output=True,timeout=t)
    except Exception: return None
adb("connect","192.168.0.9:5555")
adb("shell","svc","power","stayon","true")
opened=0
for i,u in enumerate(stuck):
    r=adb("shell","am","start","-a","android.intent.action.VIEW","-d",u)
    if r is not None and r.returncode==0: opened+=1
    if i%25==0:
        adb("shell","input","keyevent","KEYCODE_WAKEUP")
        print(f"  opened {opened}/{len(stuck)}",flush=True)
    time.sleep(0.35)
print(f"DONE opened {opened}/{len(stuck)} stuck links on phone",flush=True)
