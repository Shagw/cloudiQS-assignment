"""Turn a .json or .csv file of transactions into API request bodies of at most 500 transactions.

    python3 scripts/prepare_batches.py <input.json|input.csv> <output_dir>

Writes <output_dir>/batch_001.json, batch_002.json, ... Each customer's transactions stay in the
same batch, so the agent always sees a customer's full history. Standard library only, so it
runs on the macOS system python3.

CSV columns (header row required): transaction_id, customer_id, amount, merchant, timestamp[, currency]
"""

import csv
import json
import sys
from collections import OrderedDict
from pathlib import Path

MAX_BATCH = 500
REQUIRED = ["transaction_id", "customer_id", "amount", "merchant", "timestamp"]


def load(path: Path) -> list:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:  # utf-8-sig drops Excel's BOM
            reader = csv.DictReader(f)
            headers = [h.strip().lower() for h in (reader.fieldnames or [])]
            missing = [c for c in REQUIRED if c not in headers]
            if missing:
                sys.exit(f"CSV is missing column(s): {', '.join(missing)}. Found: {', '.join(headers) or 'none'}")
            rows = []
            for line_no, row in enumerate(reader, start=2):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                if not any(row.values()):
                    continue  # skip blank lines
                try:
                    row["amount"] = float(row["amount"].replace(",", "").lstrip("$"))
                except ValueError:
                    sys.exit(f"line {line_no}: amount {row['amount']!r} is not a number")
                if not row.get("currency"):
                    row.pop("currency", None)
                rows.append(row)
            return rows

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["transactions"] if isinstance(data, dict) else data
    if not isinstance(rows, list):
        sys.exit('JSON must be {"transactions": [...]} or a plain list of transactions')
    return rows


def batches(rows: list) -> list:
    by_customer = OrderedDict()
    for row in rows:
        by_customer.setdefault(str(row.get("customer_id")), []).append(row)

    out, current = [], []
    for txns in by_customer.values():
        if current and len(current) + len(txns) > MAX_BATCH:
            out.append(current)
            current = []
        # A single customer with more than MAX_BATCH transactions has to be split.
        while len(txns) > MAX_BATCH:
            out.append(txns[:MAX_BATCH])
            txns = txns[MAX_BATCH:]
        current.extend(txns)
    if current:
        out.append(current)
    return out


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dest = Path(sys.argv[1]), Path(sys.argv[2])
    if not src.is_file():
        sys.exit(f"file not found: {src}")
    if src.suffix.lower() not in (".json", ".csv"):
        sys.exit("input must be a .json or .csv file")

    rows = load(src)
    if not rows:
        sys.exit("no transactions found in the file")
    groups = batches(rows)
    for i, group in enumerate(groups, start=1):
        (dest / f"batch_{i:03d}.json").write_text(json.dumps({"transactions": group}))
    customers = len({str(r.get("customer_id")) for r in rows})
    print(f"loaded {len(rows)} transactions from {customers} customers -> {len(groups)} batch(es)")


if __name__ == "__main__":
    main()
