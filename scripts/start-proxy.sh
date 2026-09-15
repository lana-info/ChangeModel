#!/usr/bin/env bash
# ChangeModel proxy launcher (Ubuntu/macOS).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ChangeModel] python3 not found in PATH." >&2
  exit 1
fi
if [ ! -f proxy/run_proxy.py ]; then
  echo "[ChangeModel] proxy/run_proxy.py not found." >&2
  exit 1
fi

# If proxy already running, do nothing.
if curl -sf --max-time 2 http://127.0.0.1:4096/healthz >/dev/null 2>&1; then
  echo "[ChangeModel] Proxy already running."
  exit 0
fi

nohup python3 proxy/run_proxy.py >/tmp/change-model-proxy.log 2>&1 &
echo "[ChangeModel] Starting proxy on http://127.0.0.1:4096 ..."

for _ in $(seq 1 15); do
  if curl -sf --max-time 1 http://127.0.0.1:4096/healthz >/dev/null 2>&1; then
    echo "[ChangeModel] Proxy is up."
    exit 0
  fi
  sleep 0.5
done

echo "[ChangeModel] Proxy failed to start. Will fall back to Codex built-in models." >&2
exit 1