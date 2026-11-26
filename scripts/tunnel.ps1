. (Join-Path $PSScriptRoot 'common.ps1')

# Gives the local API a public HTTPS address (Cloudflare quick tunnel) so the Netlify-hosted pages can reach it,
# then writes that address into web\config.js. The address changes every time the tunnel starts,
# so redeploy the web folder to Netlify afterwards.

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) {
    $cf = @("$env:ProgramFiles\cloudflared\cloudflared.exe", "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe") |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $cf) { Fail 'cloudflared is not installed. Run: winget install --id Cloudflare.cloudflared -e' }

try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 5 } catch { $h = $null }
if (-not $h -or $h.database -ne 'connected') { Fail 'The API is not running. Double-click start.cmd first.' }

Step 'Starting the tunnel'
$pidFile = Join-Path $RunDir 'tunnel.pid'
if (Test-Path $pidFile) {
    $old = Get-Process -Id ([int](Get-Content $pidFile | Select-Object -First 1)) -ErrorAction SilentlyContinue
    if ($old) { taskkill /PID $old.Id /T /F | Out-Null; Info 'Stopped the previous tunnel.' }
    Remove-Item $pidFile -Force
}
$log = Join-Path $LogDir 'tunnel.err.log'
Remove-Item $log -Force -ErrorAction SilentlyContinue
$t = Start-Process -FilePath $cf -WindowStyle Hidden -PassThru `
    -ArgumentList 'tunnel', '--no-autoupdate', '--url', "http://127.0.0.1:$ApiPort" `
    -RedirectStandardError $log -RedirectStandardOutput (Join-Path $LogDir 'tunnel.out.log')
Set-Content -Path $pidFile -Value $t.Id

$url = $null
for ($i = 0; $i -lt 60 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    $m = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value }
}
if (-not $url) {
    $tail = (Get-Content $log -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
    Fail "The tunnel did not give an address. Last lines of ${log}:`n$tail"
}
Info "Tunnel address: $url"

$cfg = Join-Path $WebDir 'config.js'
$text = Get-Content $cfg -Raw -Encoding UTF8
$text = [regex]::Replace($text, "var HOSTED_API = '[^']*';", "var HOSTED_API = '$url';")
[IO.File]::WriteAllText($cfg, $text, (New-Object Text.UTF8Encoding $false))
Info "Wrote the address into $cfg"

Write-Host ''
Write-Host "Tunnel running. Now redeploy the web folder to Netlify so the site uses $url" -ForegroundColor Green
Write-Host 'The tunnel stops with stop.cmd. Each new tunnel has a new address and needs another redeploy.'
