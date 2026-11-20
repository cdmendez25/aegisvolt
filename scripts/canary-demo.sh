#!/usr/bin/env bash
# Demostración del CANARY con rollback automático.
#
#   scripts/canary-demo.sh fail [env] [servicio]   -> despliega una revisión DEFECTUOSA
#   scripts/canary-demo.sh restore [env] [servicio] -> vuelve a la especificación sana
#
# "fail" agrega CANARY_SIMULATE_ERRORS=true al pod template => nueva revisión => Argo
# manda el 20% a esa revisión => el AnalysisRun mide ~50% de 5xx (umbral 5%) => FALLA
# => Argo ABORTA y deja el 100% del tráfico en la versión stable (rollback automático).
set -euo pipefail
ACTION="${1:?uso: canary-demo.sh fail|restore [env] [servicio]}"
ENV="${2:-dev}"
SVC="${3:-station-reservation-service}"
NS="aegisvolt-${ENV}"

case "$ACTION" in
  fail)
    kubectl -n "$NS" patch rollout "$SVC" --type=json -p '[
      {"op":"add","path":"/spec/template/spec/containers/0/env/-",
       "value":{"name":"CANARY_SIMULATE_ERRORS","value":"true"}}]'
    echo "Revisión defectuosa enviada. Observa el canary con:"
    echo "  kubectl argo rollouts get rollout $SVC -n $NS --watch"
    kubectl argo rollouts status "$SVC" -n "$NS" --timeout 300s || true
    echo "--- AnalysisRun:"
    kubectl -n "$NS" get analysisruns.argoproj.io --sort-by=.metadata.creationTimestamp | tail -2
    ;;
  restore)
    IDX=$(kubectl -n "$NS" get rollout "$SVC" -o json | python3 -c '
import json,sys
env=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env",[])
print(next((i for i,e in enumerate(env) if e["name"]=="CANARY_SIMULATE_ERRORS"), -1))')
    if [[ "$IDX" -ge 0 ]]; then
      kubectl -n "$NS" patch rollout "$SVC" --type=json \
        -p "[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/0/env/${IDX}\"}]"
    fi
    kubectl argo rollouts status "$SVC" -n "$NS" --timeout 300s
    ;;
  *) echo "acción inválida: $ACTION" >&2; exit 2 ;;
esac
