"""Command-line interface for AutoLearn."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import AutoLearn, AutoLearnError, initialize


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autolearn", description="Measure changes and keep only proven improvements.")
    p.add_argument("--root", default=".", help="project root (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create configuration and durable state")
    init.add_argument("--goal", required=True, help="the AUTOLEARN goal")
    for name in ("audit_only", "plan_only", "do_it", "run_forever", "report"):
        sub.add_parser(name)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "init":
            result = initialize(root, args.goal)
        else:
            runner = AutoLearn(root)
            result = getattr(runner, args.command)()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (AutoLearnError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
