#!/usr/bin/env python3
"""Run the cheese QC/slicing canonical cases and weighted-source audit.

Run from the repository root:

    python3 demo/cheese_demo.py
    python3 -m wrsource.cheese examples
    python3 -m wrsource.cheese audit-demo --work demo/_work/cheese_audit

Exit code 0 means every prose example and audit invariant held.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wrsource.cheese.audit import build_weighted_audit
from wrsource.cheese.planner import plan_order
from wrsource.cheese.qc import explain_plane, quality_check
from wrsource.cheese.scenarios import (four_cell_case, rig_orientation_case,
                                       uncertain_half_case)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok - {message}")


def main() -> int:
    print("1. Original four-cell case: depth-four left probe -> sealed left half")
    case = four_cell_case()
    qc = quality_check(case)
    check(qc["possible_model_ids_by_block"]["B"] == ["only-cell-4"],
          "depth-four reading removes cells-1-and-4 model")
    plane = explain_plane(qc, "B", {"axis": "x", "at": 2})
    check(plane["necessarily_hole_free"], "midline cut face is guaranteed hole-free")
    sealed = plan_order(qc, mode="sealed", station_beats=1)
    check(sealed["status"] == "feasible", "one midline beat is enough")
    piece = sealed["plan"]["assignments"][0]["piece_id"]
    check(piece.endswith("left"), f"sealed label names left half ({piece})")

    print("2. Correct only first-hole depth to cell 1")
    qc1 = quality_check(four_cell_case(1))
    check(qc1["possible_model_ids_by_block"]["B"] == ["cells-1-and-4"],
          "depth-one reading leaves cells-1-and-4")
    corrected = plan_order(qc1, mode="sealed", station_beats=1)
    check(corrected["status"] == "no_feasible_box",
          "locked midline gives two holed halves and no two-cell strip")

    print("3. Restored depth-four record with midline-only fixture")
    qc4 = quality_check(four_cell_case(4))
    restored = plan_order(qc4, mode="sealed", cut_budget=0,
                          existing_cuts=[{"cut_id": "mid", "block_id": "B",
                                          "plane": {"axis": "x", "at": 2},
                                          "left_id": "B-left", "right_id": "B-right"}])
    check(restored["status"] == "feasible",
          "existing sawn B-left remains deliverable after reopening")

    print("4. Two readings pass separately but reject a shared rig orientation")
    rig_qc = quality_check(rig_orientation_case())
    conflict = rig_qc["rig_orientation_conflicts"][0]
    check(rig_qc["status"] == "conflict", "same-rig sample has no global model/world")
    check(conflict["each_reading_can_pass"], "each reading can pass after remounting")
    check(not conflict["common_orientation_exists"], "no one orientation serves both")

    print("5. Hole at cell 2 or 3: fixed label fails; table adapts")
    ucase = uncertain_half_case()
    uqc = quality_check(ucase)
    fixed = plan_order(uqc, mode="sealed", station_beats=1)
    check(fixed["status"] == "no_feasible_box",
          "neither fixedly written left nor right half is guaranteed")
    table = plan_order(uqc, mode="table", station_beats=2)
    check(table["status"] == "feasible",
          "two beats leave one cut plus the table beat")
    assigned = sorted(
        branch["plan"]["assignments"][0]["piece_id"]
        for branch in table["branches"]
    )
    check(any(x.endswith("left") for x in assigned) and
          any(x.endswith("right") for x in assigned),
          "observed cell 2 sends work right; observed cell 3 sends it left")
    no_beat = plan_order(uqc, mode="table", station_beats=1)
    check(no_beat["status"] != "feasible",
          "removing the table workstation beat invalidates the same promise")

    print("6. Real weighted sampler: correction, withdrawal, true replacement, reopen")
    work = os.path.join(ROOT, "demo", "_work", "cheese_audit")
    desk = build_weighted_audit(work)
    recheck = desk.state["last_recheck"]
    check(recheck["old_version_cut"]["supported"],
          "old source version still supports the signed midline cut")
    check(recheck["corrected_depth1_remainder"]["plan_status"] == "no_feasible_box",
          "corrected new version cannot serve the box from the sawn halves")
    check(recheck["new_version_remainder"]["plan_status"] == "feasible",
          "restored/replacement sample supports the retained remainder")
    check(desk.state["withdrawn_first_row_id"] == "probe-P0-left4",
          "withdrew the actual sample first name")
    check(desk.state["replacement_row_id"] == "probe-P2-left4-true-next",
          "refresh inserted the true next-ranked row")
    check(desk.state["replacement_matches_full_rebuild"],
          "withdrawal refresh is byte-identical to a true new-version rebuild")
    reopened = type(desk).reopen(work)
    check(len(reopened.state["cuts"]) == 1 and
          reopened.state["cuts"][0]["left_id"] == "B-left",
          "reopen preserves sawn identities and never merges the block")

    summary = {
        "answer": {
            "probe_depth4_surviving_model": "only cell 4",
            "hole_falls_on_cut": "the x=3 boundary exposes cell 4; the x=2 midline does not",
            "two_strip_box_answer": "the ordinary four-cell box is deliverable as sealed B-left under depth 4",
        },
        "examples": {
            "original": qc["possible_model_ids_by_block"],
            "corrected": qc1["possible_model_ids_by_block"],
            "rig_status": rig_qc["status"],
            "table_branches": len(table["branches"]),
        },
        "audit_root": work,
    }
    out = os.path.join(work, "cheese_demo_summary.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("\nAll cheese desk checks passed.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
