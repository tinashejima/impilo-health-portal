. (Join-Path $PSScriptRoot 'common.ps1')

foreach ($name in @('tunnel', 'web', 'api')) {
    $pidFile = Join-Path $RunDir "$name.pid"
    if (Test-Path $pidFile) {
        $procId = [int](Get-Content $pidFile | Select-Object -First 1)
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        # /T also stops the child python.exe that a venv launcher starts
        if ($p) { taskkill /PID $procId /T /F | Out-Null; Info "Stopped $name (process $procId)." }
        Remove-Item $pidFile -Force
    }
}

foreach ($pm in @(@($ApiPort, 'uvicorn'), @($WebPort, 'http.server'))) {
    $o = Get-PortOwner $pm[0]
    if ($o -and $o.CommandLine -like "*$($pm[1])*") { taskkill /PID $o.Id /T /F | Out-Null; Info "Stopped the process on port $($pm[0])." }
}

$db = Find-MariaDbBin
if ($db -and (Test-DbUp $db)) {
    & $db.Admin "--defaults-extra-file=$RootCnf" 'shutdown'
    Info 'Database stopped.'
}
Remove-Item (Join-Path $RunDir 'mariadb.pid') -Force -ErrorAction SilentlyContinue
Write-Host 'All stopped.' -ForegroundColor Green
