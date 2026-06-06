param(
    [switch]$Wait
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\mind_service.py"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$python = Join-Path $root ".venv\Scripts\python.exe"

function Test-MindReady {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/mind/stats" -Method Get -TimeoutSec 2
        return ($r.status -eq "ok")
    } catch {
        return $false
    }
}

if (Test-MindReady) {
    Write-Host "Egon Mind is already running on http://127.0.0.1:8000"
    exit 0
}

$exe = if (Test-Path $pythonw) { $pythonw } elseif (Test-Path $python) { $python } else { "python" }
Start-Process -FilePath $exe -ArgumentList "`"$script`"" -WorkingDirectory $root -WindowStyle Hidden

if ($Wait) {
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-MindReady) {
            Write-Host "Egon Mind is running on http://127.0.0.1:8000"
            exit 0
        }
    }
    Write-Host "Egon Mind did not become ready within 15 seconds. Check logs\mind-service.log"
    exit 1
}

Write-Host "Egon Mind launch requested."
