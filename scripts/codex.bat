@echo off
setlocal
cd /d "%~dp0\.."

REM ChangeModel launcher for Codex CLI.
REM   CHANGE_MODEL_PROFILE: "changemodel" (все провайдеры через прокси) | "base" (Luna).
REM   По умолчанию: changemodel (все модели из providers.json через прокси).
REM   Если прокси не поднялся — фолбэк на base (встроенные модели Codex).
REM   CODEX_PATH: опционально, путь к codex.exe.

if not defined CHANGE_MODEL_PROFILE set CHANGE_MODEL_PROFILE=changemodel

set CODEX=codex
where codex >nul 2>nul
if errorlevel 1 (
  for /f "delims=" %%f in ('dir /b /s "%LOCALAPPDATA%\OpenAI\Codex\bin\codex.exe" 2^>nul') do set "CODEX=%%f"
)
if "%CODEX%"=="codex" (
  if defined CODEX_PATH if exist "%CODEX_PATH%" set "CODEX=%CODEX_PATH%"
)
if not exist "%CODEX%" (
  echo [ChangeModel] codex CLI not found. Set CODEX_PATH to codex.exe.
  exit /b 1
)

if "%CHANGE_MODEL_PROFILE%"=="base" (
  python generator.py --profile base
  "%CODEX%" %*
  exit /b %errorlevel%
)

REM changemodel: все модели через прокси
call "%~dp0start-proxy.bat"
if errorlevel 1 (
  echo [ChangeModel] Proxy unavailable; using Codex built-in model.
  python generator.py --profile base
) else (
  python generator.py --profile changemodel
)
"%CODEX%" %*
exit /b %errorlevel%