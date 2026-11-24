"""针织班版师/缝台单元与集成测试。

运行：python3.11 -m unittest knit_tests -v
覆盖：引擎谱系与收针、册版勘误与锁定、缝台吃势/线轴/改肩/交货、持久化重开。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library import IdempotencyConflict
from library.errors import Rejection

from knitroom import KnitRoom, engine, scenario
from knitroom.scenario import BODY_CHART, SLEEVE_CHART
from knitroom.sewing import BASTE, DECORATIVE, PERMANENT

EASE_INSIDE = ["AXL->SHD"]
EASE_OUTSIDE = ["SHD->SHD"]


class EngineTest(unittest.TestCase):
    def test_increase_two_named_children_traceable(self) -> None:
        sp = engine.knit(engine.build_spec(scenario.sleeve_chart("e", 1)))
        by_id = sp.loops
        self.assertEqual(by_id["M12A"].parents, ["S03"])
        self.assertEqual(by_id["M12B"].parents, ["S03"])
        self.assertEqual(
            sorted(by_id["S03"].children), ["M12A", "M12B"]
        )

    def test_decrease_two_parents_traceable(self) -> None:
        for end in ("left", "right"):
            sp = engine.knit(
                engine.build_spec(scenario.sleeve_reissue_chart("e", end))
            )
            child = "EL" if end == "left" else "ER"
            self.assertEqual(len(sp.loops[child].parents), 2)

    def test_region_arc_counts(self) -> None:
        one = engine.knit(engine.build_spec(scenario.sleeve_chart("e", 1)))
        two = engine.knit(engine.build_spec(scenario.sleeve_chart("e", 2)))
        body = engine.knit(engine.build_spec(scenario.body_chart("b")))
        self.assertEqual(one.count_region("AXL->SHD"), 3)
        self.assertEqual(two.count_region("AXL->SHD"), 4)
        self.assertEqual(body.count_region("AXL->SHD"), 2)

    def test_edge_decrease_kills_marker_by_errata_end(self) -> None:
        left = engine.knit(
            engine.build_spec(scenario.sleeve_reissue_chart("el", "left"))
        )
        right = engine.knit(
            engine.build_spec(scenario.sleeve_reissue_chart("er", "right"))
        )
        self.assertFalse(left.marker("AXL").alive)
        self.assertTrue(left.marker("SHD").alive)
        self.assertTrue(right.marker("AXL").alive)
        self.assertFalse(right.marker("SHD").alive)
        with self.assertRaises(Rejection) as ctx:
            right.arcs_between("AXL", "SHD")
        self.assertEqual(ctx.exception.reason, "marker_gone")

    def test_limits_enforced(self) -> None:
        payload = scenario.sleeve_chart("e", 1)
        payload["cast_on"] = [f"L{i}" for i in range(engine.MAX_STITCHES + 1)]
        with self.assertRaises(Rejection) as ctx:
            engine.build_spec(payload)
        self.assertEqual(ctx.exception.reason, "too_many_stitches")
        payload = scenario.sleeve_chart("e", 1)
        payload["rows"] = payload["rows"] * (engine.MAX_ROWS + 1)
        with self.assertRaises(Rejection):
            engine.build_spec(payload)

    def test_inner_decrease_does_not_kill_edge_marker(self) -> None:
        # 二并一发生在中间针位而非勘误指定边端：边针不收，标记仍活
        payload = scenario.sleeve_chart("e", 1)
        payload["edge_dec_end"] = "right"
        last = [nm for op in payload["rows"][-1]["ops"] for nm in (
            op.get("names") or [op["name"]]
        )]
        # 直接构造一行：中间二并一（非边端），边端保持平织
        payload["rows"].append({
            "row_no": 2,
            "ops": [
                {"action": "knit", "sources": [0], "name": "N1"},
                {"action": "dec", "sources": [1, 2], "name": "N2"},
                {"action": "knit", "sources": [3], "name": "N3"},
                {"action": "knit", "sources": [4], "name": "N4"},
            ],
        })
        sp = engine.knit(engine.build_spec(payload))
        self.assertTrue(sp.marker("AXL").alive)
        self.assertTrue(sp.marker("SHD").alive)
        # 二并一的两个父圈仍可追溯
        self.assertEqual(len(sp.loops["N2"].parents), 2)

    def test_original_book_id_preserved_on_edition(self) -> None:
        spec = engine.build_spec(scenario.sleeve_chart("e", 1))
        self.assertEqual(
            scenario.sleeve_chart("e", 1)["book_id"], "WB"
        )
        self.assertTrue(spec.chart_id)


class PatternServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.room = KnitRoom.open(":memory:")
        scenario.load_library_material(self.room.library)
        self.room.library.catalog.activate_revision("v2")
        scenario.fulfill_j5(self.room.library)
        self.pattern = self.room.pattern
        self.pattern.register_edition(scenario.sleeve_chart("e1", 1), "op-e1")
        self.pattern.register_edition(scenario.body_chart("b1"), "op-b1")

    def tearDown(self) -> None:
        self.room.close()

    def test_seam_record_reads_real_fulfilled_receipt(self) -> None:
        rec = self.pattern.create_seam_record(
            "S1", "H3", SLEEVE_CHART, "sleeve", "op-s1"
        )
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["copy_id"], "C3")
        self.assertEqual(rec["ticket_id"], "TC")
        self.assertEqual(rec["reader_id"], "RC")
        self.assertEqual(rec["revision"], "v1")  # 取件当时锁定的册版

    def test_unfulfilled_receipt_rejected(self) -> None:
        # 造一张仍在 held 的保留
        hold = self.room.library.ledger.reserve("op-h", "v2", "C1")
        rec = self.pattern.create_seam_record(
            "Sx", hold["hold_id"], SLEEVE_CHART, "sleeve", "op-sx"
        )
        self.assertEqual(rec["status"], "rejected")
        self.assertEqual(rec["error"]["reason"], "receipt_not_fulfilled")

    def test_edition_correction_does_not_rewrite_old_record(self) -> None:
        old = self.pattern.create_seam_record(
            "OLD", "H3", SLEEVE_CHART, "sleeve", "op-old"
        )
        self.assertEqual(old["edition"], "e1")
        self.pattern.register_edition(
            scenario.sleeve_reissue_chart("e2", "left"), "op-e2"
        )
        self.pattern.activate_edition(SLEEVE_CHART, "e2", "op-act")
        # 旧记录原样，新记录取更正版
        self.assertEqual(self.pattern.get_seam_record("OLD")["edition"], "e1")
        new = self.pattern.create_seam_record(
            "NEW", "H3", SLEEVE_CHART, "sleeve", "op-new"
        )
        self.assertEqual(new["edition"], "e2")

    def test_copy_mark_preserved_and_anchored(self) -> None:
        self.pattern.add_copy_mark(
            "MK1", "H3", "S03", "本册记号", "up", "op-mark"
        )
        rec = self.pattern.create_seam_record(
            "SM", "H3", SLEEVE_CHART, "sleeve", "op-sm"
        )
        markers = {m["marker_id"]: m for m in rec["markers"]}
        self.assertIn("MK1", markers)
        self.assertTrue(markers["MK1"]["alive"])

    def test_idempotent_same_payload_replays(self) -> None:
        a = self.pattern.create_seam_record(
            "S2", "H3", SLEEVE_CHART, "sleeve", "op-same"
        )
        b = self.pattern.create_seam_record(
            "S2", "H3", SLEEVE_CHART, "sleeve", "op-same"
        )
        self.assertEqual(a, b)
        # 同 op_id 异文（改 piece_kind）必须判冲突且不写库
        with self.assertRaises(IdempotencyConflict):
            self.pattern.create_seam_record(
                "S2", "H3", SLEEVE_CHART, "body", "op-same"
            )

    def test_ledgers_separately_queryable(self) -> None:
        self.pattern.create_seam_record("S3", "H3", SLEEVE_CHART, "sleeve", "op-s3")
        loops = self.pattern.edge_loop_ledger("S3")
        arcs = self.pattern.edge_arcs("S3")
        self.assertTrue(loops and arcs)
        self.assertTrue(all({"parents", "children"} <= set(l) for l in loops))


class SewingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.room = KnitRoom.open(":memory:")
        scenario.load_library_material(self.room.library)
        self.room.library.catalog.activate_revision("v2")
        scenario.fulfill_j5(self.room.library)
        p = self.room.pattern
        p.register_edition(scenario.sleeve_chart("e1", 1), "op-e1")
        p.register_edition(scenario.sleeve_chart("e2", 2), "op-e2")
        p.register_edition(scenario.body_chart("b1"), "op-b1")
        self.sleeve = "SLEEVE"
        self.sleeve2 = "SLEEVE2"
        self.body = "BODY"
        p.create_seam_record(self.sleeve, "H3", SLEEVE_CHART, "sleeve", "op-ss")
        p.create_seam_record(
            self.sleeve2, "H3", SLEEVE_CHART, "sleeve", "op-ss2", edition="e2"
        )
        p.create_seam_record(self.body, "H3", BODY_CHART, "body", "op-sb")
        self.s = self.room.sewing
        self.s.register_spool("BASTE", BASTE, 4, "op-sp-b")
        self.s.register_spool("PERM", PERMANENT, 16, "op-sp-p")
        self.s.register_spool("DECOR", DECORATIVE, 1, "op-sp-d")

    def tearDown(self) -> None:
        self.room.close()

    def test_baseline_one_ease_then_plain(self) -> None:
        plan = self.s.plan_needles(self.sleeve, self.body, EASE_INSIDE)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual([n["ease"] for n in plan["needles"]], [0, 1, 0, 0])
        ease_needle = next(n for n in plan["needles"] if n["ease"] == 1)
        self.assertEqual(len(ease_needle["sleeve_arcs"]), 2)
        self.assertEqual(len(ease_needle["body_arcs"]), 1)

    def test_double_increase_one_ease_insufficient(self) -> None:
        r = self.s.plan_needles(self.sleeve2, self.body, EASE_INSIDE)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "cannot_align")

    def test_no_ease_and_ease_outside_fail(self) -> None:
        for regions in ([], EASE_OUTSIDE):
            r = self.s.plan_needles(self.sleeve, self.body, regions)
            self.assertEqual(r["error"]["reason"], "cannot_align")

    def test_baste_thread_not_shareable_before_delivery(self) -> None:
        ok = self.s.baste_sleeve(
            "G", "SL", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-baste-1"
        )
        self.assertEqual(ok["status"], "ok")
        clash = self.s.baste_sleeve(
            "G", "SR", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-baste-2"
        )
        self.assertEqual(clash["error"]["reason"], "baste_thread_reserved_until_delivery")
        self.s.release_allocation("alloc-op-baste-1", "op-rel")
        after = self.s.baste_sleeve(
            "G", "SR", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-baste-3"
        )
        self.assertEqual(after["status"], "ok")

    def test_permanent_thread_releasable_rejected(self) -> None:
        self.s.place_permanent(
            "G", "SL", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-p1"
        )
        r = self.s.release_allocation("alloc-op-p1", "op-prel")
        self.assertEqual(r["error"]["reason"], "only_baste_thread_releasable")

    def test_permanent_loops_locked_not_reused(self) -> None:
        self.s.place_permanent(
            "G", "SL", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-p2",
            stop_after=2,
        )
        remaining = self.s.plan_needles(
            self.sleeve, self.body, EASE_INSIDE, sleeve_id="SL", seam_group="G"
        )
        used = {
            arc
            for nd in self.s.needle_log("G", "SL")
            for arc in nd["sleeve_arcs"]
        }
        for nd in remaining["needles"]:
            self.assertFalse(set(nd["sleeve_arcs"]) & used)

    def test_decorative_thread_one_sleeve(self) -> None:
        self.s.allocate_decorative("G", "SL", "DECOR", "op-d1")
        r = self.s.allocate_decorative("G", "SR", "DECOR", "op-d2")
        self.assertEqual(r["error"]["reason"], "decorative_thread_one_sleeve_only")

    def test_max_two_sleeves(self) -> None:
        self.s.baste_sleeve("G", "SL", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-x1")
        self.s.release_allocation("alloc-op-x1", "op-xrel")
        self.s.baste_sleeve("G", "SR", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-x2")
        third = self.s.baste_sleeve("G", "ST", self.sleeve, self.body, "BASTE", EASE_INSIDE, "op-x3")
        self.assertEqual(third["error"]["reason"], "too_many_sleeves")

    def test_shoulder_change_accept_and_two_rejections(self) -> None:
        # 左袖留活口：只缝定位段，肩标前吃势位仍在
        self.s.place_permanent(
            "G", "SL", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-sl",
            stop_after=1,
        )
        accept = self.s.evaluate_shoulder_change(
            "G", "SL", self.sleeve, EASE_INSIDE, "SHD"
        )
        self.assertEqual(accept["status"], "ok")
        # 右袖缝到底：定针挡住末弧
        self.s.place_permanent(
            "G", "SR", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-sr"
        )
        blocked = self.s.evaluate_shoulder_change(
            "G", "SR", self.sleeve, EASE_INSIDE, "SHD"
        )
        self.assertEqual(blocked["reason"], "permanent_needle_blocks_change")
        # 吃势已耗：左袖再缝到吃势针之后
        self.s.place_permanent(
            "G2", "SL", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-sl2",
            stop_after=2,
        )
        consumed = self.s.evaluate_shoulder_change(
            "G2", "SL", self.sleeve, EASE_INSIDE, "SHD"
        )
        self.assertEqual(consumed["reason"], "ease_already_consumed")

    def test_orientation_mismatch_rejected(self) -> None:
        # 一份定位标方向相反的衣身册版（标记不可翻面）
        self.room.pattern.register_edition(
            scenario.body_chart("b2-down", orientation="down"), "op-b2"
        )
        flipped = self.room.pattern.create_seam_record(
            "BODY-FLIP", "H3", BODY_CHART, "body", "op-bflip", edition="b2-down"
        )
        self.assertEqual(flipped["status"], "ok")
        r = self.s.plan_needles(self.sleeve, "BODY-FLIP", EASE_INSIDE)
        self.assertEqual(r["error"]["reason"], "marker_orientation_mismatch")

    def test_no_cross_marker_jump_in_plan(self) -> None:
        # 针序严格按弧序消费：每针的袖弧、身弧都是各自序列里的下一段，
        # 吃势针的两段袖弧也相邻，不跨标记、不回头。
        plan = self.s.plan_needles(self.sleeve, self.body, EASE_INSIDE)
        flat_s = [a for n in plan["needles"] for a in n["sleeve_arcs"]]
        flat_b = [a for n in plan["needles"] for a in n["body_arcs"]]
        self.assertEqual(flat_s, sorted(flat_s, key=lambda x: int(x[1:])))
        self.assertEqual(flat_b, sorted(flat_b, key=lambda x: int(x[1:])))
        for n in plan["needles"]:
            if n["ease"] == 1:
                self.assertEqual(len(n["sleeve_arcs"]), 2)
                self.assertTrue(
                    n["sleeve_arcs"][1] == f"A{int(n['sleeve_arcs'][0][1:]) + 1}"
                )

    def test_delivery_report_names_blockers(self) -> None:
        self.s.place_permanent(
            "G", "SL", self.sleeve, self.body, "PERM", EASE_INSIDE, "op-d1",
            stop_after=1,
        )
        report = self.s.delivery_report("G")
        self.assertFalse(report["pair_deliverable"])
        self.assertEqual(report["deliverable_sleeves"], ["SL"])
        self.assertTrue(report["sleeves"]["SL"]["blocking_for_pair"])


class PersistenceTest(unittest.TestCase):
    def test_reopen_recovers_pinned_edition_and_needles(self) -> None:
        dbp = tempfile.mktemp(suffix=".db")
        try:
            room = KnitRoom.open(dbp)
            scenario.load_library_material(room.library)
            room.library.catalog.activate_revision("v2")
            scenario.fulfill_j5(room.library)
            p, s = room.pattern, room.sewing
            p.register_edition(scenario.sleeve_chart("e1", 1), "op-e1")
            p.register_edition(scenario.body_chart("b1"), "op-b1")
            p.create_seam_record("OS", "H3", SLEEVE_CHART, "sleeve", "op-os")
            p.create_seam_record("OB", "H3", BODY_CHART, "body", "op-ob")
            s.register_spool("PERM", PERMANENT, 8, "op-sp")
            s.place_permanent(
                "GRP", "SL", "OS", "OB", "PERM", EASE_INSIDE, "op-n", stop_after=2
            )
            room.close()

            again = KnitRoom.reopen(dbp)
            self.assertEqual(again.pattern.get_seam_record("OS")["edition"], "e1")
            remaining = again.sewing.plan_needles(
                "OS", "OB", EASE_INSIDE, sleeve_id="SL", seam_group="GRP"
            )
            self.assertEqual(
                [n["sleeve_arcs"] for n in remaining["needles"]],
                [["A4"], ["A5"]],
            )
            again.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                path = Path(dbp + suffix)
                if path.exists():
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
