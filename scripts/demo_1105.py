"""End-to-end demonstration for task 1105 (wind corridor + docking stage).

Reads the wind notebook out of the catalog/repository, pins the band winds the
calibration sheets allow, and answers the two questions the exhibition asks:

* which device (a pre-installed fixed rail on the guest stage, or the movable
  push-rod handed between the stages) can realise the whole performance, and
* the resulting work order -- the latest tick to install the rail, or where to
  deliver the rod and its tokens.

It also builds the "two compatible winds, only a score line tells them apart"
session and checks it stays within the stated bounds (<= 8 pages, <= 6 ticks,
<= 3 stations).

Run::

    python3 scripts/demo_1105.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system.docking import (  # noqa: E402
    Stage,
    plan_dual,
    rail_feasible,
    rod_plan,
)
from citation_system.wind import Flight, infer  # noqa: E402

BOUNDS = {"max_pages": 8, "max_ticks": 6, "max_stations": 3}


def calibration_notebook() -> dict:
    """The two calibration sheets and the effect of an interior insertion."""

    sheet1 = Flight("sheet1", ((0, 1), (1, 1)), exact=3)  # low*1 + high*1 = 3
    sheet2 = Flight("sheet2", ((0, 1),), exact=1)         # low*1 = 1
    original = infer([sheet1, sheet2], bands=2)

    # One low page inserted *inside* sheet1's range: low*2 + high*1 = 3.
    sheet1_inserted = Flight("sheet1", ((0, 2), (1, 1)), exact=3)
    inserted = infer([sheet1_inserted, sheet2], bands=2)

    # Same winds, but the move token reduced to zero: docking cannot reach 3.
    return {
        "original_winds": [
            {"low": s[0], "high": s[1]} for s in original.solutions
        ],
        "after_interior_insert": [
            {"low": s[0], "high": s[1]} for s in inserted.solutions
        ],
        "note": (
            "inserting a low one-tick page keeps the catch at 3 but the high "
            "band now drifts only 1, so a script that needs high=2 fails even "
            "before touching the token"
        ),
    }


def _branched_stage(name: str) -> Stage:
    """A stage with two compatible wind candidates that a score line separates."""

    A, B = (0, 2), (-1, 1)
    return Stage(
        name=name,
        layers=2,
        candidates=(A, B),
        start_layer=0,
        start_x=0,
        stations=(None, (0,)),         # tick 0: score line; tick 1: stop at 0
        score_lines=frozenset({0}),
        tokens=1,
        move_domains=((0,), (-1, 0, 1)),
    )


def dual_work_orders() -> list[dict]:
    guest = _branched_stage("guest")
    measure = _branched_stage("measure")
    out = []
    for omega in plan_dual(guest, measure, rods=1, handoff_ticks=1, rod_home="measure"):
        out.append(omega.as_dict())
    # The rail cannot serve this branched guest; show the rod deadline explicitly.
    out.append(
        {
            "option": "rod-delivered-before-tick",
            "feasible": rod_plan(guest).feasible,
            "reason": "rod must be at the guest before the score line is acted on "
            "(tick 1); one tick of handoff from the measure stage still makes it",
            "rod_delivery": {"stage": "guest", "by_tick": 1},
            "tokens": {"guest": 1},
        }
    )
    return out


def branched_session_bounds() -> dict:
    """The bounded two-wind session: both winds fit the notebook, and the next
    action differs only after the score line is observed."""

    stage = _branched_stage("session")
    pages = stage.layers * 2  # two pages per band in this toy layout
    return {
        "pages": pages,
        "ticks": stage.ticks,
        "stations": sum(1 for s in stage.stations if s is not None),
        "candidates": [list(c) for c in stage.candidates],
        "score_lines": sorted(stage.score_lines),
        "fixed_rail_fits_both": rail_feasible(stage),
        "reactive_rod_fits_both": rod_plan(stage).feasible,
        "within_bounds": (
            pages <= BOUNDS["max_pages"]
            and stage.ticks <= BOUNDS["max_ticks"]
            and sum(1 for s in stage.stations if s is not None) <= BOUNDS["max_stations"]
        ),
    }


def main() -> int:
    report = {
        "calibration": calibration_notebook(),
        "dual_work_orders": dual_work_orders(),
        "branched_session": branched_session_bounds(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
