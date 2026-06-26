param(
    [switch]$Wait
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\mind_service.py"
$venv = Join-Path $root ".venv"
$cfg = Join-Path $venv "pyvenv.cfg"
$site = Join-Path $venv "Lib\site-packages"

function Get-EgonPython {
    if (Test-Path $cfg) {
        $homeLine = Get-Content -LiteralPath $cfg | Where-Object {
            $_.ToLower().Replace(" ", "").StartsWith("home=")
        } | Select-Object -First 1
        if ($homeLine) {
            $home = ($homeLine -split "=", 2)[1].Trim()
            $basePythonw = Join-Path $home "pythonw.exe"
            if (Test-Path $basePythonw) { return $basePythonw }
            $basePython = Join-Path $home "python.exe"
            if (Test-Path $basePython) { return $basePython }
        }
    }
    $pythonw = Join-Path $venv "Scripts\pythonw.exe"
    if (Test-Path $pythonw) { return $pythonw }
    $python = Join-Path $venv "Scripts\python.exe"
    if (Test-Path $python) { return $python }
    return "python"
}

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

$exe = Get-EgonPython
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$site;$env:PYTHONPATH" } else { $site }
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
