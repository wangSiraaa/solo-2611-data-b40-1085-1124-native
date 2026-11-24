"""Run every worked scenario in the exhibit brief, exactly.

    python3 -m inklab.demo          # human readable
    python3 -m inklab.demo --json   # structured (Fractions as "n/d")
"""

from __future__ import annotations

import argparse
import json

from .scenarios import all_scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    data = all_scenarios()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    _print(data)
    return 0


def _print(data: dict) -> None:
    print("inklab — exact worked scenarios\n")
    for name, body in data.items():
        print(f"== {name} ==")
        for key, value in body.items():
            if key == "plans":
                print(f"  {key}:")
                for plan_name, summary in value.items():
                    print(f"    - {plan_name}: guaranteed={summary['guaranteed']} "
                          f"max_test_ink={summary['max_test_ink']}")
                    for w, o in summary["worlds"].items():
                        print(f"        {w}: completed={o['completed']} "
                              f"pen={o['chosen_pen']} test_ink={o['test_ink']} "
                              f"failure={o['failure']}")
            elif key == "records" and isinstance(value, list):
                print(f"  {key}: {len(value)} weighing originals")
                for r in value:
                    print(f"    {r['pen']} b{r['beat_index']:02d} {r['chamber']}="
                          f"{r['value']} bits={r['bits']}")
            else:
                print(f"  {key}: {value}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
