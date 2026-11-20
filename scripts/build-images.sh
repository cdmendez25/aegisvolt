#!/usr/bin/env bash
# Construye las imágenes Docker de los 4 microservicios + frontend.
# Uso: scripts/build-images.sh [TAG]   (TAG por defecto: sha corto de git o "local")
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"

declare -A SERVICES=(
  [station-reservation-service]=station_reservation
  [billing-wallet-service]=billing_wallet
  [energy-dispatch-service]=energy_dispatch
  [graphql-gateway]=graphql_gateway
)

uv export --no-dev --no-hashes --format requirements-txt -q > services/requirements.txt

for name in "${!SERVICES[@]}"; do
  echo "==> aegisvolt/${name}:${TAG}"
  docker build -q -f services/Dockerfile \
    --build-arg SERVICE="${SERVICES[$name]}" --build-arg APP_VERSION="${TAG}" \
    -t "aegisvolt/${name}:${TAG}" .
done

# Imagen del analizador canary (usa la librería común; versionada aparte)
if ! docker image inspect aegisvolt/canary-analysis:1.0 >/dev/null 2>&1 || [[ "${REBUILD_ANALYSIS:-}" == 1 ]]; then
  echo "==> aegisvolt/canary-analysis:1.0"
  docker build -q -f services/Dockerfile --build-arg SERVICE=aegis_common -t aegisvolt/canary-analysis:1.0 .
fi

if [[ -f frontend/package.json ]]; then
  echo "==> aegisvolt/frontend:${TAG}"
  docker build -q -t "aegisvolt/frontend:${TAG}" frontend
fi
echo "TAG=${TAG}"
