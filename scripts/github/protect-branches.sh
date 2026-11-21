#!/usr/bin/env bash
set -euo pipefail
REPO="${1:?uso: protect-branches.sh <owner>/<repo>}"

read -r -d '' RULESET <<'JSON' || true
{
  "name": "aegisvolt-ambientes-protegidos",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {
    "ref_name": { "include": ["refs/heads/dev", "refs/heads/test", "refs/heads/prod"], "exclude": [] }
  },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["merge"]
      } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "promotion-guard" },
          { "context": "lint-build" },
          { "context": "unit-tests" },
          { "context": "integration-tests" },
          { "context": "e2e-tests" },
          { "context": "terraform-plan" }
        ]
      } }
  ]
}
JSON

EXISTING=$(gh api "repos/${REPO}/rulesets" --jq '.[] | select(.name=="aegisvolt-ambientes-protegidos") | .id' || true)
if [[ -n "$EXISTING" ]]; then
  gh api -X PUT "repos/${REPO}/rulesets/${EXISTING}" --input - <<<"$RULESET" >/dev/null
  echo "Ruleset actualizado (id $EXISTING)"
else
  gh api -X POST "repos/${REPO}/rulesets" --input - <<<"$RULESET" --jq '"Ruleset creado (id \(.id))"'
fi
gh api "repos/${REPO}/rulesets" --jq '.[] | "\(.id)  \(.name)  \(.enforcement)"'
