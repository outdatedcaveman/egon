# Add --remote-debugging-port=9222 to all reachable Chrome shortcuts.
# Idempotent — re-running is safe.
$flag = "--remote-debugging-port=9222"
$targets = @(
    "$env:USERPROFILE\Desktop\Google Chrome.lnk",
    "$env:PUBLIC\Desktop\Google Chrome.lnk",
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Google Chrome.lnk",
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Google Chrome.lnk",
    "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\Google Chrome.lnk"
)
$shell = New-Object -ComObject WScript.Shell
$results = @()
foreach ($p in $targets) {
    if (-not (Test-Path $p)) { continue }
    $sc = $shell.CreateShortcut($p)
    if ($sc.Arguments -match [Regex]::Escape($flag)) {
        $results += [pscustomobject]@{path=$p; status="already-set"}
    } else {
        $sc.Arguments = if ($sc.Arguments) { "$($sc.Arguments) $flag" } else { $flag }
        try {
            $sc.Save()
            $results += [pscustomobject]@{path=$p; status="modified"}
        } catch {
            $results += [pscustomobject]@{path=$p; status="error: $($_.Exception.Message.Split([Environment]::NewLine)[0])"}
        }
    }
}
$results | ConvertTo-Json -Compress
