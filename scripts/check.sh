#!/usr/bin/env bash
# Send a file of transactions to the Risk Monitor API and print a readable summary.
#
#   scripts/check.sh <file.json|file.csv> [-o report.json]
#
# The API address defaults to the local deployment; point it elsewhere with RISK_API_URL:
#   RISK_API_URL=https://risk-monitor.example.com scripts/check.sh data.csv
set -euo pipefail

usage() { echo "usage: $0 <file.json|file.csv> [-o report.json]" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
INPUT="$1"; shift
OUTPUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) [[ $# -ge 2 ]] || usage; OUTPUT="$2"; shift 2 ;;
    *) usage ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
API="${RISK_API_URL:-http://localhost:8080}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 "$HERE/prepare_batches.py" "$INPUT" "$WORK"

if ! curl -fsS --max-time 5 "$API/ping" >/dev/null 2>&1; then
  echo "✗ can't reach the API at $API — is it running? (docker compose up -d)" >&2
  exit 1
fi

for batch in "$WORK"/batch_*.json; do
  name="$(basename "$batch" .json)"
  echo "→ sending $name to $API/invocations ..."
  curl -sS --max-time 300 -X POST "$API/invocations" \
    -H 'Content-Type: application/json' \
    --data-binary @"$batch" > "$WORK/$name.report"
done

python3 - "$WORK" "$OUTPUT" <<'EOF'
import json, sys
from pathlib import Path

work, output = Path(sys.argv[1]), sys.argv[2]
reports, failed = [], False
for path in sorted(work.glob("batch_*.report")):
    try:
        r = json.loads(path.read_text())
    except ValueError:
        print(f"\n✗ {path.stem}: the API returned something that isn't JSON:\n{path.read_text()[:500]}")
        failed = True
        continue
    if not isinstance(r, dict):
        print(f"\n✗ {path.stem}: unexpected response from the API:\n{str(r)[:500]}")
        failed = True
        continue
    if "error" in r:
        print(f"\n✗ {path.stem}: the API rejected the data ({r['error']}):")
        for d in r.get("details", []):
            loc = ".".join(str(x) for x in d.get("loc", []))
            print(f"    {loc}: {d.get('msg')}")
        failed = True
        continue
    reports.append(r)

if reports:
    alerts = [a for r in reports for a in r["alerts"]]
    reviewed = sum(r["transactions_reviewed"] for r in reports)
    undelivered = sum(not a["webhook_delivered"] for a in alerts)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    print(f"\n══ Reviewed {reviewed} transactions — flagged {len(alerts)} ══")
    for a in sorted(alerts, key=lambda a: order.get(a["risk_level"], 9)):
        print(f"  {a['risk_level'].upper():8} {a['transaction_id']:12} customer {a['customer_id']}")
        for reason in a["reasons"]:
            print(f"           • {reason}")
    if undelivered:
        print(f"\n  ⚠ {undelivered} alert(s) could not be delivered to the risk team webhook")
    for i, r in enumerate(reports, start=1):
        label = f"Summary (batch {i})" if len(reports) > 1 else "Summary"
        print(f"\n── {label} ──\n{r['summary']}")
    if output:
        Path(output).write_text(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2))
        print(f"\nfull report saved to {output}")

sys.exit(1 if failed else 0)
EOF
