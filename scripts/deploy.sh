#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ENV="${1:?uso: deploy.sh <dev|test|prod> <TAG>}"
TAG="${2:?falta TAG}"
NS="aegisvolt-${ENV}"
[[ "$ENV" =~ ^(dev|test|prod)$ ]] || { echo "Ambiente inválido: $ENV" >&2; exit 2; }

ROLLOUTS=(station-reservation-service billing-wallet-service energy-dispatch-service graphql-gateway)
IMAGES=("${ROLLOUTS[@]/#/aegisvolt/}")
IMAGES=("${IMAGES[@]/%/:${TAG}}" "aegisvolt/frontend:${TAG}" "aegisvolt/canary-analysis:1.0")

echo "==> Cargando imágenes en kind (${TAG})"
kind load docker-image --name aegisvolt "${IMAGES[@]}" >/dev/null

echo "==> Aplicando overlay ${ENV} en ${NS}"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
kubectl kustomize "k8s/overlays/${ENV}" \
  | sed -E "s#(image: aegisvolt/[a-z-]+):latest#\1:${TAG}#" \
  | kubectl apply -f -

kubectl -n "$NS" rollout status deploy/frontend --timeout=180s

echo "==> Esperando estrategia canary de los Rollouts"
FAILED=0
for r in "${ROLLOUTS[@]}"; do
  if kubectl argo rollouts status "$r" -n "$NS" --timeout 420s; then
    echo "   [OK] $r -> $(kubectl argo rollouts get rollout "$r" -n "$NS" -o json 2>/dev/null | \
      python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",{}).get("stableRS","?"))' 2>/dev/null || echo stable)"
  else
    echo "   [ROLLBACK] $r: el análisis canary falló; Argo restauró la versión stable" >&2
    FAILED=1
  fi
done

kubectl -n "$NS" get rollouts.argoproj.io
exit $FAILED
