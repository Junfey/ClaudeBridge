# ClaudeBridge - one-command launcher.
# Ensures VS Code runs with the CDP debug port, then starts the bridge and tunnel.
# Non-destructive when already set up: if port 9222 is open, VS Code is left alone.
#
# Run:  powershell -ExecutionPolicy Bypass -File C:\Creation\ClaudeBridge\start-all.ps1

$ErrorActionPreference = "Continue"
$root    = "C:\Creation\ClaudeBridge"
$py      = "C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe"
$code    = "C:\Users\Admin\AppData\Local\Programs\Microsoft VS Code\Code.exe"
$port    = 8765
$dbgPort = 9222
# Tunnel: localtunnel. On this network it's the only reliable HTTPS option —
# cloudflared quick-tunnel subdomains don't resolve (DNS-restricted ISP) and the
# path to Cloudflare hangs; serveo shows a browser-warning interstitial and drops.
# localtunnel's own reminder page appears once per browser (enter the shown IP).

function Test-Port($p) {
  # CDP debug port (9222) exposes /json/version.
  try { Invoke-WebRequest "http://127.0.0.1:$p/json/version" -TimeoutSec 3 -UseBasicParsing | Out-Null; return $true }
  catch { return $false }
}
function Test-Bridge($p) {
  # The bridge answers 200 on "/" (HTML shell, no auth).
  try { $r = Invoke-WebRequest "http://127.0.0.1:$p/" -TimeoutSec 3 -UseBasicParsing; return ($r.StatusCode -eq 200) }
  catch { return $false }
}

Write-Host "== ClaudeBridge launcher ==" -ForegroundColor Cyan

# 1. Ensure VS Code has the CDP debug port.
if (Test-Port $dbgPort) {
  Write-Host "[ok] VS Code debug port $dbgPort already open - leaving VS Code as is." -ForegroundColor Green
} else {
  Write-Host "[..] Debug port closed. Restarting VS Code with debug port (save your files first)." -ForegroundColor Yellow
  Stop-Process -Name Code -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
  Start-Process -FilePath $code -ArgumentList "--remote-debugging-port=$dbgPort"
  Write-Host "     waiting for debug port..."
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Port $dbgPort) { break }
  }
  if (Test-Port $dbgPort) { Write-Host "[ok] VS Code with debug port is up." -ForegroundColor Green }
  else { Write-Host "[!!] Debug port never opened - check VS Code manually." -ForegroundColor Red }
}

# 2. Start the bridge (kill any stale uvicorn first).
Write-Host "[..] Starting bridge on port $port" -ForegroundColor Yellow
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like "*uvicorn*bridge.main*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Start-Process -FilePath $py `
  -ArgumentList @("-m","uvicorn","bridge.main:app","--host","0.0.0.0","--port","$port","--log-level","warning") `
  -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 4
if (Test-Bridge $port) { Write-Host "[ok] Bridge is live." -ForegroundColor Green }
else { Write-Host "[!!] Bridge did not start." -ForegroundColor Red; exit 1 }

# 3. Start the tunnel, capture its URL, and KEEP IT ALIVE (auto-restart on drop).
$logFile = Join-Path $env:TEMP "claudebridge-tunnel.log"
$shownUrl = ""

function Start-Tunnel {
  if (Test-Path $logFile) { Remove-Item $logFile -Force -ErrorAction SilentlyContinue }
  # localtunnel: the only provider that works reliably on this DNS-restricted
  # network (wildcard *.loca.lt resolves; cloudflared subdomains NXDOMAIN and
  # serveo shows its own browser-warning interstitial). Random subdomain each run.
  $cmdLine = "npx --yes localtunnel --port $port > `"$logFile`" 2>&1"
  return (Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $cmdLine) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru)
}
function Get-TunnelUrl {
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $logFile) {
      $m = Select-String -Path $logFile -Pattern "https://[a-z0-9-]+\.loca\.lt" -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($m) { return $m.Matches[0].Value }
    }
  }
  return ""
}

Write-Host "[..] Starting tunnel (localtunnel) with auto-restart..." -ForegroundColor Yellow
# Kill stale tunnel clients so a second launch can't fight over the subdomain.
Get-Process node -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$tunnel = Start-Tunnel
$shownUrl = Get-TunnelUrl
if (-not $shownUrl) { Write-Host "[!!] Tunnel produced no URL. See $logFile" -ForegroundColor Red; exit 1 }
& $py (Join-Path $root "tools\show_url.py") $shownUrl
Write-Host "Keep this window open (minimize is fine). It auto-recovers if the tunnel drops." -ForegroundColor DarkGray
Write-Host ""

# Watchdog: if the tunnel process dies OR the bridge stops answering, restart.
while ($true) {
  Start-Sleep -Seconds 8
  $tunnelDead = $tunnel.HasExited
  $bridgeDead = -not (Test-Bridge $port)
  if ($bridgeDead) {
    Write-Host "[..] Bridge stopped — restarting." -ForegroundColor Yellow
    Start-Process -FilePath $py `
      -ArgumentList @("-m","uvicorn","bridge.main:app","--host","0.0.0.0","--port","$port","--log-level","warning") `
      -WorkingDirectory $root -WindowStyle Hidden
    Start-Sleep -Seconds 3
  }
  if ($tunnelDead) {
    Write-Host "[..] Tunnel dropped — reconnecting." -ForegroundColor Yellow
    try { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue } catch {}
    $tunnel = Start-Tunnel
    $newUrl = Get-TunnelUrl
    if ($newUrl -and $newUrl -ne $shownUrl) { $shownUrl = $newUrl; & $py (Join-Path $root "tools\show_url.py") $shownUrl }
    elseif ($newUrl) { Write-Host "[ok] Tunnel back at $shownUrl" -ForegroundColor Green }
  }
}
