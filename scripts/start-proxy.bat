@echo off
setlocal
cd /d "%~dp0\.."

REM Starts the ChangeModel proxy (idempotent).
REM PROXY_PORT: опционально, порт прокси (по умолчанию 4096).

if not defined PROXY_PORT set PROXY_PORT=4096

where python >nul 2>nul
if errorlevel 1 (
  echo [ChangeModel] Python not found in PATH.
  exit /b 1
)

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PROXY_PORT%/healthz' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {} exit 1" >nul 2>nul
if %errorlevel% equ 0 (
  echo [ChangeModel] Proxy already running.
  exit /b 0
)

start "ChangeModel Proxy" /min cmd /c "cd /d "%~dp0\.." && python proxy\run_proxy.py"
echo [ChangeModel] Starting proxy on http://127.0.0.1:%PROXY_PORT% ...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 15;$i++){ try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:%PROXY_PORT%/healthz' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Milliseconds 500 }; if($ok){exit 0}else{exit 1}"
if errorlevel 1 (
  echo [ChangeModel] Proxy failed to start. Will fall back to Codex built-in models.
  exit /b 1
)
echo [ChangeModel] Proxy is up.
exit /b 0