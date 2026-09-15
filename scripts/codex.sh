#!/usr/bin/env bash
# ChangeModel launcher for Codex CLI (Ubuntu/macOS).
#   CHANGE_MODEL_PROFILE: "changemodel" (все провайдеры через прокси) | "base" (Luna).
#   По умолчанию: changemodel. Если прокси не поднялся — фолбэк на base.
#   CODEX_PATH: опционально, путь к бинарю codex
set -uo pipefail
cd "$(dirname "$0")/.."

PROFILE="${CHANGE_MODEL_PROFILE:-changemodel}"

CODEX="${CODEX_PATH:-codex}"
if [ "$CODEX" = "codex" ] && ! command -v codex >/dev/null 2>&1; then
  echo "[ChangeModel] codex CLI not found in PATH. Set CODEX_PATH." >&2
  exit 1
fi

if [ "$PROFILE" = "base" ]; then
  python3 generator.py --profile base
  "$CODEX" "$@"
  exit $?
fi

if [ "$PROFILE" != "changemodel" ]; then
  echo "[ChangeModel] Unknown CHANGE_MODEL_PROFILE: $PROFILE (use 'changemodel' or 'base')." >&2
  exit 1
fi

# changemodel: все модели через прокси; при сбое прокси — фолбэк на base
if bash scripts/start-proxy.sh; then
  python3 generator.py --profile changemodel
else
  echo "[ChangeModel] Proxy unavailable; using Codex built-in model." >&2
  python3 generator.py --profile base
fi
"$CODEX" "$@"
exit $?