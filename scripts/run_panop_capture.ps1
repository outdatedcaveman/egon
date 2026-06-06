# Nightly wrapper for the Panop capture orchestrator.
#
# Invokes `egon.lib.adapters.panop_capture.run_capture()` which:
#   1. discovers the phone over wireless ADB (mDNS first, static IP fallback)
#   2. wakes screen + foregrounds Chrome
#   3. delegates the actual sweep to the vendored Panop server's run_adb_sweep()
#   4. logs structured JSONL to egon/logs/panop-YYYY-MM.log
#
# Captures stdout+stderr to a trace file alongside, so Task Scheduler failures
# are debuggable.

$ErrorActionPreference = "Continue"
$root  = Split-Path $PSScriptRoot -Parent
$py    = Join-Path $root ".venv\Scripts\python.exe"
$log   = Join-Path $root "logs\panop-sched-$(Get-Date -Format 'yyyy-MM').log"

"=== panop sched start $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') ===" |
    Add-Content -Path $log -Encoding utf8

try {
    Push-Location $root
    & $py -m lib.adapters.panop_capture *>&1 |
        Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    Pop-Location
    "=== panop sched end · exit=$code · $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') ===" |
        Add-Content -Path $log -Encoding utf8
    exit $code
}
catch {
    "=== panop sched FATAL · $($_.Exception.Message) ===" | Add-Content -Path $log -Encoding utf8
    exit 99
}
