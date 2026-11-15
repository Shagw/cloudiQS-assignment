"""Risk Monitor: a Strands agent that flags anomalous transactions and alerts the risk team."""

import argparse
import json
import sys


def main() -> None:
    from .agent import run_risk_monitor

    parser = argparse.ArgumentParser(description="Run the Risk Monitor agent on a JSON file of transactions.")
    parser.add_argument("file", help='JSON file shaped like {"transactions": [...]}, or "-" for stdin')
    parser.add_argument("--trace", action="store_true", help="print each step of the run (LLM calls, tool calls) to the console")
    parser.add_argument("--step", action="store_true", help="like --trace, but pause before each step until you press Enter")
    args = parser.parse_args()
    if args.step and args.file == "-":
        parser.error("--step needs the keyboard, so it can't be combined with reading transactions from stdin")

    raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
    hooks = []
    if args.trace or args.step:
        from .trace import ConsoleTracer

        hooks.append(ConsoleTracer(step=args.step))
    report = run_risk_monitor(json.loads(raw), hooks=hooks)
    print(report.model_dump_json(indent=2))
