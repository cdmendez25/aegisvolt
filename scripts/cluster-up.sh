#!/usr/bin/env bash
# Crea (idempotente) el clúster kind "aegisvolt" con:
#   - ingress-nginx  (entrada HTTP por host: dev|test|prod.aegisvolt.localhost)
#   - Argo Rollouts  (estrategia Canary + AnalysisTemplates)
#   - namespaces aegisvolt-dev|test|prod
#   - Floci conectado a la red docker "kind" (los pods lo ven como http://floci:4566)
set -euo pipefail
cd "$(dirname "$0")/.."

INGRESS_VERSION="controller-v1.15.1"
ROLLOUTS_VERSION="v1.10.2"
BIN="${HOME}/.local/bin"

if ! kind get clusters 2>/dev/null | grep -qx aegisvolt; then
  kind create cluster --config k8s/cluster/kind-config.yaml --wait 120s
fi
kubectl config use-context kind-aegisvolt >/dev/null

# Floci dentro de la red de kind
./scripts/floci-up.sh

echo "==> ingress-nginx ${INGRESS_VERSION}"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_VERSION}/deploy/static/provider/kind/deploy.yaml" >/dev/null
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s

echo "==> Argo Rollouts ${ROLLOUTS_VERSION}"
kubectl create namespace argo-rollouts --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply --server-side --force-conflicts -n argo-rollouts -f "https://github.com/argoproj/argo-rollouts/releases/download/${ROLLOUTS_VERSION}/install.yaml" >/dev/null
kubectl -n argo-rollouts rollout status deploy/argo-rollouts --timeout=180s

if ! command -v kubectl-argo-rollouts >/dev/null; then
  echo "==> plugin kubectl argo rollouts"
  curl -fsSLo "${BIN}/kubectl-argo-rollouts" \
    "https://github.com/argoproj/argo-rollouts/releases/download/${ROLLOUTS_VERSION}/kubectl-argo-rollouts-linux-amd64"
  chmod +x "${BIN}/kubectl-argo-rollouts"
fi

for env in dev test prod; do
  kubectl create namespace "aegisvolt-${env}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace "aegisvolt-${env}" aegisvolt/env="${env}" --overwrite >/dev/null
done
echo "Clúster listo: $(kubectl get nodes -o name)"
