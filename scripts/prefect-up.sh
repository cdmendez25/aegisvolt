#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

docker build -q -t aegisvolt/prefect-flows:1.0 observability/prefect >/dev/null

if ! docker ps -a --format '{{.Names}}' | grep -qx prefect-server; then
  docker run -d --name prefect-server --network kind -p 4200:4200 \
    -e PREFECT_UI_API_URL=http://localhost:4200/api -e PREFECT_SERVER_API_HOST=0.0.0.0 \
    -v aegisvolt-prefect-data:/root/.prefect --restart unless-stopped \
    prefecthq/prefect:3-latest prefect server start --host 0.0.0.0 >/dev/null
fi
docker start prefect-server >/dev/null
timeout 120 bash -c 'until curl -sf http://127.0.0.1:4200/api/health >/dev/null; do sleep 2; done'

docker rm -f prefect-observability-flow >/dev/null 2>&1 || true
docker run -d --name prefect-observability-flow --network kind --restart unless-stopped \
  -e PREFECT_API_URL=http://prefect-server:4200/api \
  -e AWS_ENDPOINT_URL=http://floci:4566 \
  -e GATEWAY_URL_DEV=http://aegisvolt-control-plane:30081 \
  -e GATEWAY_URL_TEST=http://aegisvolt-control-plane:30082 \
  -e GATEWAY_URL_PROD=http://aegisvolt-control-plane:30083 \
  aegisvolt/prefect-flows:1.0 python flows.py serve >/dev/null
echo "Prefect listo: http://localhost:4200  (flow 'aegisvolt-observability', deployment 'cada-5-minutos')"
