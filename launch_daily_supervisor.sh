#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PAPER_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$PAPER_PYTHON_BIN"
elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/venv/bin/python"
else
  PYTHON_BIN="python3"
fi

for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    "$PYTHON_BIN" -m paper_trade.daily_supervisor "$@"
    exit $?
  fi
done

UI_HOST="${PAPER_SUPERVISOR_UI_HOST:-127.0.0.1}"
UI_PORT="${PAPER_SUPERVISOR_UI_PORT:-8790}"
UPSTOX_WEBHOOK="${PAPER_UPSTOX_WEBHOOK:-0}"
UPSTOX_WEBHOOK_HOST="${PAPER_UPSTOX_WEBHOOK_HOST:-127.0.0.1}"
UPSTOX_WEBHOOK_PORT="${PAPER_UPSTOX_WEBHOOK_PORT:-8791}"
UPSTOX_WEBHOOK_PATH="${PAPER_UPSTOX_WEBHOOK_PATH:-/upstox/token}"
SYMBOL_MODE="${PAPER_SYMBOL_MODE:-split}"
REFRESH_SECONDS="${PAPER_REFRESH_SECONDS:-3.0}"
RATE_LIMIT_COOLDOWN="${PAPER_RATE_LIMIT_COOLDOWN:-300}"
REPORT_DIR="paper_trade/reports"

mkdir -p "$REPORT_DIR" "paper_trade/supervisor_logs"

"$PYTHON_BIN" -m paper_trade.supervisor_ui --host "$UI_HOST" --port "$UI_PORT" &
UI_PID="$!"
WEBHOOK_PID=""

if [[ "$UPSTOX_WEBHOOK" == "1" || "$UPSTOX_WEBHOOK" == "true" || "$UPSTOX_WEBHOOK" == "yes" ]]; then
  "$PYTHON_BIN" -m paper_trade.upstox_auth webhook \
    --host "$UPSTOX_WEBHOOK_HOST" \
    --port "$UPSTOX_WEBHOOK_PORT" \
    --path "$UPSTOX_WEBHOOK_PATH" \
    --token-file paper_trade/.upstox_token &
  WEBHOOK_PID="$!"
fi

cleanup() {
  kill "$UI_PID" >/dev/null 2>&1 || true
  if [[ -n "$WEBHOOK_PID" ]]; then
    kill "$WEBHOOK_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "Supervisor dashboard: http://${UI_HOST}:${UI_PORT}"
if [[ -n "$WEBHOOK_PID" ]]; then
  echo "Upstox token webhook: http://${UPSTOX_WEBHOOK_HOST}:${UPSTOX_WEBHOOK_PORT}${UPSTOX_WEBHOOK_PATH}"
fi
echo "Reports folder: ${ROOT_DIR}/${REPORT_DIR}"

set +e
"$PYTHON_BIN" -m paper_trade.daily_supervisor \
  --token-file paper_trade/.upstox_token \
  --symbols all \
  --session-minutes 30 \
  --symbol-mode "$SYMBOL_MODE" \
  --refresh "$REFRESH_SECONDS" \
  --rate-limit-cooldown "$RATE_LIMIT_COOLDOWN" \
  --agents adaptive_edge_maker,raider_core,apex_maker \
  "$@"
SUPERVISOR_CODE="$?"
set -e

if [[ "$SUPERVISOR_CODE" -ne 0 ]]; then
  echo "Daily supervisor exited with code ${SUPERVISOR_CODE}. Dashboard is staying online for inspection."
else
  echo "Daily supervisor completed. Dashboard is staying online for inspection."
fi

wait "$UI_PID"
