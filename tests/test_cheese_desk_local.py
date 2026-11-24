from __future__ import annotations

import json
import os
import tempfile
import unittest

from wrsource.cheese.audit import CheeseDesk, build_weighted_audit
from wrsource.cheese.planner import plan_order, reconstruct_pieces
from wrsource.cheese.qc import explain_plane, quality_check
from wrsource.cheese.scenarios import (four_cell_case, rig_orientation_case,
                                       uncertain_half_case)


class FourCellQualityTests(unittest.TestCase):
    def test_depth_four_keeps_only_cell_four_model(self):
        qc = quality_check(four_cell_case())
        self.assertEqual(qc["status"], "ok")
        self.assertEqual(qc["possible_model_ids_by_block"]["B"], ["only-cell-4"])

    def test_midline_is_safe_but_x3_exposes_hole(self):
        qc = quality_check(four_cell_case())
        self.assertTrue(explain_plane(qc, "B", {"axis": "x", "at": 2})[
            "necessarily_hole_free"])
        x3 = explain_plane(qc, "B", {"axis": "x", "at": 3})
        self.assertFalse(x3["necessarily_hole_free"])
        self.assertEqual(x3["models"][0]["visible_hole_voxels"], [[3, 0, 0]])

    def test_sealed_left_half_delivery(self):
        qc = quality_check(four_cell_case())
        plan = plan_order(qc, mode="sealed", station_beats=1)
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["complete_box_count"], 1)
        assignment = plan["plan"]["assignments"][0]
        self.assertTrue(assignment["piece_id"].endswith("left"))
        self.assertFalse(plan["labels"][0]["substitution_allowed"])

    def test_corrected_depth_one_has_holed_halves_and_no_strip(self):
        qc = quality_check(four_cell_case(1))
        self.assertEqual(qc["possible_model_ids_by_block"]["B"],
                         ["cells-1-and-4"])
        plan = plan_order(qc, mode="sealed", station_beats=1)
        self.assertEqual(plan["status"], "no_feasible_box")
        self.assertEqual(plan["complete_box_count"], 0)

    def test_restored_record_still_delivers_existing_left_half(self):
        qc = quality_check(four_cell_case(4))
        cuts = [{
            "cut_id": "mid", "block_id": "B",
            "plane": {"axis": "x", "at": 2},
            "left_id": "B-left", "right_id": "B-right",
        }]
        plan = plan_order(qc, mode="sealed", existing_cuts=cuts, cut_budget=0)
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["plan"]["assignments"][0]["piece_id"], "B-left")

    def test_midline_only_cannot_extract_middle_two_cells(self):
        # Even cells 2-3 would be hole-free under holes {1,4}, but the clamps
        # do not permit the x=1 and x=3 cuts needed to isolate them.
        case = four_cell_case(1)
        qc = quality_check(case)
        plan = plan_order(qc, mode="sealed", station_beats=4,
                          fixtures=[{"id": "mid", "axis": "x", "at": 2}])
        self.assertEqual(plan["status"], "no_feasible_box")


class RigOrientationTests(unittest.TestCase):
    def test_each_reading_passes_but_shared_rig_fails(self):
        qc = quality_check(rig_orientation_case())
        self.assertEqual(qc["status"], "conflict")
        self.assertEqual(qc["possible_model_ids_by_block"]["B"], [])
        conflict = qc["rig_orientation_conflicts"][0]
        self.assertEqual(conflict["rig_id"], "RIG-1")
        self.assertTrue(conflict["each_reading_can_pass"])
        self.assertFalse(conflict["common_orientation_exists"])

    def test_same_readings_without_rig_constraint_are_independent(self):
        case = rig_orientation_case()
        for p in case["probes"]:
            p.pop("rig_id")
        # Standalone records use their supplied/local faces; the depth-one
        # reading alone conflicts with the only surviving depth-four world.
        qc = quality_check(case)
        self.assertEqual(qc["status"], "conflict")


class InspectionTableTests(unittest.TestCase):
    def test_fixed_left_or_right_cannot_guarantee(self):
        qc = quality_check(uncertain_half_case())
        plan = plan_order(qc, mode="sealed", station_beats=1)
        self.assertEqual(plan["status"], "no_feasible_box")

    def test_table_observation_routes_to_safe_half(self):
        qc = quality_check(uncertain_half_case())
        plan = plan_order(qc, mode="table", station_beats=2)
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(len(plan["branches"]), 2)
        by_models = {tuple(b["model_ids"]): b for b in plan["branches"]}
        h2 = by_models[("hole-cell-2",)]
        h3 = by_models[("hole-cell-3",)]
        self.assertTrue(h2["plan"]["assignments"][0]["piece_id"].endswith("right"))
        self.assertTrue(h3["plan"]["assignments"][0]["piece_id"].endswith("left"))

    def test_removing_table_beat_breaks_promise(self):
        qc = quality_check(uncertain_half_case())
        plan = plan_order(qc, mode="table", station_beats=1)
        self.assertNotEqual(plan["status"], "feasible")


class ExistingCutIdentityTests(unittest.TestCase):
    def test_sawn_halves_are_not_merged_back(self):
        case = four_cell_case()
        blocks = case["blocks"]
        cuts = [{"cut_id": "c1", "block_id": "B",
                 "plane": {"axis": "x", "at": 2},
                 "left_id": "L", "right_id": "R"}]
        pieces, history = reconstruct_pieces(blocks, cuts)
        self.assertEqual(set(pieces), {"L", "R"})
        self.assertEqual(pieces["L"]["source_block_id"], "B")
        with self.assertRaises(Exception):
            reconstruct_pieces(blocks, [
                {"cut_id": "c1", "block_id": "B",
                 "plane": {"axis": "x", "at": 2},
                 "left_id": "L", "right_id": "R"},
                {"cut_id": "c2", "piece_id": "B",
                 "plane": {"axis": "x", "at": 1}},
            ])

    def test_one_strip_cannot_serve_two_boxes(self):
        qc = quality_check({"blocks": [{
            "block_id": "S", "size": [2, 1, 1],
            "models": [{"id": "solid", "holes": []}],
        }]})
        boxes = [
            {"box_id": "one", "strips": [{"shape": [2, 1, 1]}]},
            {"box_id": "two", "strips": [{"shape": [2, 1, 1]}]},
        ]
        plan = plan_order(qc, boxes=boxes, mode="sealed", cut_budget=0)
        self.assertEqual(plan["complete_box_count"], 1)


class WeightedAuditTests(unittest.TestCase):
    def test_correction_withdrawal_replacement_and_reopen(self):
        root = tempfile.mkdtemp(prefix="cheese-audit-")
        desk = build_weighted_audit(os.path.join(root, "desk"))
        result = desk.state["last_recheck"]
        self.assertTrue(result["old_version_cut"]["supported"])
        self.assertEqual(
            result["corrected_depth1_remainder"]["plan_status"],
            "no_feasible_box")
        self.assertEqual(
            result["new_version_remainder"]["plan_status"], "feasible")
        self.assertEqual(desk.state["withdrawn_first_row_id"], "probe-P0-left4")
        self.assertTrue(desk.state["replacement_row_id"])
        self.assertTrue(desk.state["replacement_matches_full_rebuild"])
        # Old immutable dataset still says depth four.
        manifest, records = __import__("wrsource.dataset", fromlist=["load_dataset"]).load_dataset(
            desk.state["datasets"]["v1"])
        old_probe = next(r for r in records if r["row_id"] == "probe-P0-left4")
        self.assertEqual(json.loads(old_probe["body"])["first_hole_depth"], 4)

        reopened = CheeseDesk.reopen(desk.root)
        again = reopened.recheck()
        self.assertTrue(again["old_version_cut"]["supported"])
        self.assertEqual(again["new_version_remainder"]["piece"], "B-left")


if __name__ == "__main__":
    unittest.main()
