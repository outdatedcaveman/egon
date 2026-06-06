# Nightly wrapper for scripts/pass.py.
#
# Runs the FREE part of the pass — `_run_snapshots()` — which refreshes
# adapter snapshots (Chrome bookmarks, Zotero, Letterboxd, Instapaper, ...).
# Does NOT call `claude -p`; that path (the classification pass that writes
# `last_pass.json`) is expensive ($3-8/run) and stays opt-in via Egon's
# "⚡ Run pass now" button.
#
# Captures stdout+stderr to a date-stamped trace file so we can see why
# scheduled runs exit before pass.py's own logger attaches.

$ErrorActionPreference = "Continue"
$root  = Split-Path $PSScriptRoot -Parent
$py    = Join-Path $root ".venv\Scripts\python.exe"
$pass  = Join-Path $root "scripts\pass.py"
$log   = Join-Path $root "logs\sched-trace-$(Get-Date -Format 'yyyy-MM').log"

"=== sched start $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') ===" | Add-Content -Path $log -Encoding utf8

try {
    & $py $pass --kind snapshots *>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    "=== sched end · exit=$code · $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') ===" | Add-Content -Path $log -Encoding utf8
    exit $code
}
catch {
    "=== sched FATAL · $($_.Exception.Message) ===" | Add-Content -Path $log -Encoding utf8
    exit 99
}
