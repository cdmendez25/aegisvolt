#!/usr/bin/env bash
# Corre los 4 microservicios como procesos locales contra Floci (ambiente ENV, default dev).
# Útil para desarrollo rápido sin Kubernetes.  Ctrl+C detiene todo.
set -euo pipefail
cd "$(dirname "$0")/.."
export ENV="${ENV:-dev}" AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localhost:4566}" \
       AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_REGION=us-east-1 \
       CLOUDWATCH_ENABLED="${CLOUDWATCH_ENABLED:-true}" PEAK_HOURS="${PEAK_HOURS:-0-24}" \
       FLAG_ADMIN_ENABLED="${FLAG_ADMIN_ENABLED:-true}" PYTHONPATH=services \
       STATION_SERVICE_URL=http://localhost:8101 BILLING_SERVICE_URL=http://localhost:8102 \
       DISPATCH_SERVICE_URL=http://localhost:8103
trap 'kill 0' EXIT
uv run uvicorn station_reservation.app:app --port 8101 --log-level warning &
uv run uvicorn billing_wallet.app:app      --port 8102 --log-level warning &
uv run uvicorn energy_dispatch.app:app     --port 8103 --log-level warning &
uv run uvicorn graphql_gateway.app:app     --port 8100 --log-level warning &
wait
