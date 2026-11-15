#!/usr/bin/env bash
# Post-deploy check: the running service is healthy, is the expected version, and works end to end.
#   scripts/smoke_test.sh [expected_git_sha]
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8080}"
EXPECTED_SHA="${1:-}"
json() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "1/4 health";   curl -fsS "$BASE/ping" >/dev/null && echo "    ok"

echo "2/4 version";  VERSION=$(curl -fsS "$BASE/version"); echo "    $VERSION"
if [[ -n "$EXPECTED_SHA" && "$(echo "$VERSION" | json "['git_sha']")" != "$EXPECTED_SHA" ]]; then
  echo "    ✗ expected git_sha $EXPECTED_SHA"; exit 1
fi

echo "3/4 rejects bad input"
curl -fsS -X POST "$BASE/invocations" -H 'Content-Type: application/json' -d '{"transactions":[]}' \
  | json "['error']" | grep -qx invalid_input && echo "    ok"

echo "4/4 end-to-end run on sample transactions (calls the LLM)"
REPORT=$(curl -fsS --max-time 240 -X POST "$BASE/invocations" -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/../examples/transactions.json")
echo "$REPORT" | python3 -c '
import json, sys
r = json.load(sys.stdin)
undelivered = sum(not a["webhook_delivered"] for a in r["alerts"])
reviewed, flagged = r["transactions_reviewed"], r["flagged_count"]
print("    reviewed=%s flagged=%s undelivered_webhooks=%s" % (reviewed, flagged, undelivered))
ok = reviewed == 12 and flagged >= 1 and undelivered == 0
sys.exit(0 if ok else "    ✗ unexpected report")
'
echo "✓ smoke test passed"
