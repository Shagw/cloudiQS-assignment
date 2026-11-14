"""Risk Monitor: a Strands agent that flags anomalous transactions and alerts the risk team."""

import argparse
import json
import sys


def main() -> None:
    from .agent import run_risk_monitor

    parser = argparse.ArgumentParser(description="Run the Risk Monitor agent on a JSON file of transactions.")
    parser.add_argument("file", help='JSON file shaped like {"transactions": [...]}, or "-" for stdin')
    args = parser.parse_args()

    raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
    report = run_risk_monitor(json.loads(raw))
    print(report.model_dump_json(indent=2))
