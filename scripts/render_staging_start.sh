#!/bin/sh
set -eu

mkdir -p "${ORDER_DOCUMENT_DIR:-/tmp/controls_exchange/order_documents}"

# Render may create the staging service without prompting for sync:false
# Blueprint variables. Secure/replace the local-development bootstrap admin
# before the public web process starts.
python scripts/staging_admin_bootstrap.py

python scripts/feed_worker.py &
FEED_PID=$!
python scripts/webhook_worker.py &
WEBHOOK_PID=$!
WEB_PID=""

cleanup() {
  [ -z "${WEB_PID:-}" ] || kill "$WEB_PID" 2>/dev/null || true
  kill "$FEED_PID" "$WEBHOOK_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

uvicorn web_entrypoint:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips='*' &
WEB_PID=$!
wait "$WEB_PID"
