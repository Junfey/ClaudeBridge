# ClaudeBridge launcher — runs bridge + Cloudflare Quick Tunnel side-by-side.
# Use: powershell -ExecutionPolicy Bypass -File C:\Creation\ClaudeBridge\start.ps1

$ErrorActionPreference = "Stop"
$py = "C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe"
$root = "C:\Creation\ClaudeBridge"

Write-Host "== ClaudeBridge ==" -ForegroundColor Cyan
Write-Host "Bridge on http://127.0.0.1:8765"
Write-Host "Public URL will appear from cloudflared below." -ForegroundColor Yellow
Write-Host ""

# Start bridge in this window's background
$bridge = Start-Process -FilePath $py `
    -ArgumentList @("-m", "uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8765", "--log-level", "warning") `
    -WorkingDirectory $root `
    -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 2

# Verify it's up
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8765/api/windows" -TimeoutSec 5 -UseBasicParsing | Out-Null
    Write-Host "[ok] bridge alive (pid=$($bridge.Id))" -ForegroundColor Green
} catch {
    Write-Host "[fail] bridge didn't come up — aborting" -ForegroundColor Red
    exit 1
}

# Run cloudflared in the foreground so its public URL stays visible
Write-Host ""
Write-Host "Starting Cloudflare Quick Tunnel (URL changes each launch)..." -ForegroundColor Yellow
Write-Host "Stop with Ctrl+C — bridge will be killed too." -ForegroundColor DarkGray
Write-Host ""

try {
    & "$root\cloudflared.exe" tunnel --url http://127.0.0.1:8765
} finally {
    Write-Host ""
    Write-Host "Stopping bridge (pid=$($bridge.Id))..." -ForegroundColor Yellow
    try { Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue } catch {}
}
