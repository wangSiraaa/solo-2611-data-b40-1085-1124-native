"""Command line interface for the cheese quality/cutting desk."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict

from .planner import plan_order
from .qc import quality_check
from .sample_io import quality_check_dataset
from .scenarios import run_named_examples


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wrsource.cheese",
                                     description="Cheese-model QC and slice planner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("examples", help="run all canonical prose examples")

    qc = sub.add_parser("qc", help="quality-check a JSON case")
    qc.add_argument("--case", required=True)
    qc.add_argument("--out")

    qcd = sub.add_parser("qc-dataset", help="read a real wrsource dataset directory")
    qcd.add_argument("--dataset", required=True)
    qcd.add_argument("--db")
    qcd.add_argument("--verify", action="store_true")

    plan = sub.add_parser("plan", help="plan boxes from QC/case JSON")
    plan.add_argument("--case", required=True)
    plan.add_argument("--mode", "--delivery", dest="mode",
                      choices=["sealed", "table", "auto"], default="sealed")
    plan.add_argument("--beats", type=int)
    plan.add_argument("--cut-budget", type=int)
    plan.add_argument("--out")

    audit = sub.add_parser("audit-demo", help="build/run weighted source revision audit")
    audit.add_argument("--work", default=os.path.join("demo", "_work", "cheese_audit"))

    args = parser.parse_args(argv)
    if args.cmd == "examples":
        _print(run_named_examples())
        return 0
    if args.cmd == "qc":
        report = quality_check(_load_json(args.case))
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
        return 0
    if args.cmd == "qc-dataset":
        report = quality_check_dataset(args.dataset, args.db, verify=args.verify)
        _print(report)
        return 0
    if args.cmd == "plan":
        data = _load_json(args.case)
        qc_report = quality_check(data)
        result = plan_order(qc_report, mode=args.mode, station_beats=args.beats,
                            cut_budget=args.cut_budget)
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
        return 0
    if args.cmd == "audit-demo":
        from .audit import build_weighted_audit
        desk = build_weighted_audit(args.work)
        _print(desk.state["last_recheck"])
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
