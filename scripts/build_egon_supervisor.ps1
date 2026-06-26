param(
    [switch]$RunOnce,
    [switch]$StopRunning
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "supervisor\EgonSupervisor.cs"
$outDir = Join-Path $root "bin"
$out = Join-Path $outDir "EgonSupervisor.exe"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if (!(Test-Path $csc)) {
    throw "C# compiler not found at $csc"
}
if (!(Test-Path $src)) {
    throw "Supervisor source not found at $src"
}

if ($StopRunning) {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -eq "EgonSupervisor.exe" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
& $csc /nologo /optimize+ /target:winexe /platform:x64 /out:$out /reference:System.Management.dll $src
if ($LASTEXITCODE -ne 0) {
    throw "C# compiler failed with exit code $LASTEXITCODE"
}

if (!(Test-Path $out)) {
    throw "Build did not produce $out"
}

Write-Host "Built $out"

if ($RunOnce) {
    & $out --root $root --once
}
