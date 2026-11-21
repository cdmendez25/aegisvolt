#!/usr/bin/env bash
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-floci}"
ENV="${1:?uso: restore-tfstate.sh <env> [version-id] [--apply]}"
VERSION="${2:-}"
APPLY="${3:-}"
BUCKET=aegisvolt-tfstate
KEY="env:/${ENV}/aegisvolt/terraform.tfstate"
TMP="$(mktemp -d)"
trap 'rm -r "$TMP"' EXIT

if [[ -z "$VERSION" ]]; then
  aws s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" \
    --query "Versions[?Key=='${KEY}'].[VersionId,LastModified,IsLatest,Size]" --output table
  exit 0
fi

aws s3api get-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" "$TMP/old.tfstate" >/dev/null
aws s3api get-object --bucket "$BUCKET" --key "$KEY" "$TMP/current.tfstate" >/dev/null
summary() {
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print("serial=%s lineage=%s recursos=%d" % (s["serial"], s["lineage"][:8], len(s["resources"])))' "$1"
}
echo "Actual   : $(summary "$TMP/current.tfstate")"
echo "Versión  : $(summary "$TMP/old.tfstate")   ($VERSION)"

if [[ "$APPLY" != "--apply" ]]; then
  echo "Dry-run. Para restaurar:  $0 $ENV $VERSION --apply"
  exit 0
fi

python3 - "$TMP/old.tfstate" "$TMP/current.tfstate" <<'PY'
import json, sys
old, cur = (json.load(open(p)) for p in sys.argv[1:3])
old["serial"] = cur["serial"] + 1
json.dump(old, open(sys.argv[1], "w"), indent=2)
PY
aws s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$TMP/old.tfstate" \
  --metadata "restored-from-version=${VERSION}" --query VersionId --output text
echo "Restaurado. Verifica con:  terraform -chdir=infra/live workspace select $ENV && terraform -chdir=infra/live plan"
