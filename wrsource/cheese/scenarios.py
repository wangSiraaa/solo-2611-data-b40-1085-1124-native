"""Canonical small cases from the cheese-desk specification."""

from __future__ import annotations

import copy
from typing import Any, Dict

from .qc import quality_check


def four_cell_case(corrected_depth: int | None = None) -> Dict[str, Any]:
    """Original four-cell ordinary sealed-label order.

    ``corrected_depth=1`` reproduces the body-correction branch.  ``None`` and
    ``4`` both mean the restored/original depth-four record.
    """
    depth = 4 if corrected_depth is None else corrected_depth
    return {
        "name": "four-cell-sealed-order",
        "station_beats": 1,
        "blocks": [{
            "block_id": "B",
            "size": [4, 1, 1],
            "models": [
                {
                    "model_id": "only-cell-4",
                    "holes": [4],
                    "wax_faces": "all",
                },
                {
                    "model_id": "cells-1-and-4",
                    "holes": [1, 4],
                    "wax_faces": "all",
                },
            ],
            "fixtures": [
                {"id": "mid", "axis": "x", "at": 2, "description": "only locked midline clamp"}
            ],
        }],
        "probes": [{
            "probe_id": "P-left-first-hole",
            "block_id": "B",
            "entry_face": "left",
            "grid_point": [0, 0],
            "direction": "right",
            "first_hole_depth": depth,
        }],
        "boxes": [{
            "box_id": "BOX-TWO-CELL-NO-HOLE",
            "strips": [{
                "strip_id": "two-cell-strip",
                "shape": [2, 1, 1],
                "max_holes": 0,
            }],
        }],
    }


def rig_orientation_case() -> Dict[str, Any]:
    """Two readings pass individually but cannot share one cradle orientation."""
    case = four_cell_case()
    case["name"] = "shared-rig-orientation-conflict"
    case["probes"] = [
        {
            "probe_id": "P-requires-as-is",
            "block_id": "B",
            "rig_id": "RIG-1",
            "entry_face": "left",
            "first_hole_depth": 4,
        },
        {
            "probe_id": "P-requires-flip",
            "block_id": "B",
            "rig_id": "RIG-1",
            "entry_face": "left",
            "first_hole_depth": 1,
        },
    ]
    return case


def uncertain_half_case() -> Dict[str, Any]:
    """Hole at cell 2 or 3: fixed left/right labels fail, table delivery works."""
    return {
        "name": "uncertain-middle-half",
        "blocks": [{
            "block_id": "U",
            "size": [4, 1, 1],
            "models": [
                {"model_id": "hole-cell-2", "holes": [2],
                 "colored_wax": {"left": "exhibit", "right": "exhibit"}},
                {"model_id": "hole-cell-3", "holes": [3],
                 "colored_wax": {"left": "exhibit", "right": "exhibit"}},
            ],
            "fixtures": [{"id": "mid", "axis": "x", "at": 2}],
        }],
        "probes": [],
        "boxes": [{
            "box_id": "BOX-SAFE-HALF-WITH-DISPLAY-FACE",
            "strips": [{
                "strip_id": "safe-half",
                "shape": [2, 1, 1],
                "max_holes": 0,
                "display_face": "any_outer",
                "wax_color": "exhibit",
            }],
        }],
    }


def run_named_examples() -> Dict[str, Any]:
    """Run all prose examples and return a JSON-safe assertion summary."""
    out: Dict[str, Any] = {}

    original = four_cell_case()
    qc = quality_check(original)
    out["original_qc"] = {
        "status": qc["status"],
        "possible": qc["possible_model_ids_by_block"],
    }
    from .planner import plan_order
    sealed = plan_order(qc, mode="sealed", station_beats=1)
    out["original_sealed"] = {
        "status": sealed["status"],
        "boxes": sealed["promised_box_ids"],
        "piece": sealed["plan"]["assignments"][0]["piece_id"] if sealed["plan"]["assignments"] else None,
    }

    corrected = four_cell_case(1)
    qc1 = quality_check(corrected)
    corrected_plan = plan_order(qc1, mode="sealed", station_beats=1)
    out["corrected_depth_1"] = {
        "possible": qc1["possible_model_ids_by_block"],
        "status": corrected_plan["status"],
        "boxes": corrected_plan["promised_box_ids"],
    }

    rig = rig_orientation_case()
    qcr = quality_check(rig)
    out["rig_conflict"] = {
        "status": qcr["status"],
        "possible": qcr["possible_model_ids_by_block"],
        "rig_conflicts": [c["rig_id"] for c in qcr["rig_orientation_conflicts"]],
        "each_reading_can_pass": qcr["rig_orientation_conflicts"][0]["each_reading_can_pass"]
        if qcr["rig_orientation_conflicts"] else None,
    }

    uncertain = uncertain_half_case()
    qcu = quality_check(uncertain)
    fixed_left = plan_order(qcu, mode="sealed", station_beats=1,
                            target_box_ids=["BOX-SAFE-HALF-WITH-DISPLAY-FACE"])
    table2 = plan_order(qcu, mode="table", station_beats=2)
    table1 = plan_order(qcu, mode="table", station_beats=1)
    out["uncertain_half"] = {
        "fixed_status": fixed_left["status"],
        "table_with_beat_status": table2["status"],
        "table_without_beat_status": table1["status"],
        "branches": [
            {
                "models": b["model_ids"],
                "piece": b["plan"]["assignments"][0]["piece_id"]
                if b["plan"]["assignments"] else None,
            }
            for b in table2.get("branches", [])
        ],
    }
    return out
