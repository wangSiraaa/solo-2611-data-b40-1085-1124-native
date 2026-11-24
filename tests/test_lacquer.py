"""Unit and integration tests for the lacquer stacking-box workshop."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lacquer.errors import (
    BUDGET_EXHAUSTED,
    COMMITMENT_BLOCKED,
    ORIGINAL_TONGUE_REQUIRED,
    TONGUE_UNAVAILABLE,
    LacquerError,
)
from lacquer.model import (
    Base,
    Lid,
    VesselConfiguration,
    ring_from_features,
)
from lacquer.notebook import (
    CELL_COLLISION,
    CLOSURE_FAILURE,
    Journal,
    JournalBook,
    NO_USABLE_PAGES,
    Page,
)
from lacquer.orders import load_pieces, load_work_order
from lacquer.packing import (
    PackingProblem,
    configuration_signature,
    enumerate_worlds,
    find_robust_sheets,
    find_seatings_for_world,
)
from lacquer.planner import evaluate_all_worlds
from lacquer.sampling import reconstruct_from_export
from lacquer.witness import Witness
from lacquer.workbench import (
    GaugeQuestion,
    ResourceLedger,
    TrialBiteQuestion,
    WorkOrder,
    run_question,
)

MATERIALS = ROOT / "materials" / "lacquer"


def load_book(*, revised: bool = False) -> JournalBook:
    book = JournalBook.from_file(MATERIALS / "notebooks.json")
    if revised:
        pieces = json.loads((MATERIALS / "pieces.json").read_text(encoding="utf-8"))
        for change in pieces["revisions"]:
            book.revise_page(
                change["journal_id"],
                change["page_id"],
                step=change["step"],
                feature=change["feature"],
            )
    return book


def middle_configs(book: JournalBook) -> list[VesselConfiguration]:
    configs, reports = book.reconstruct_vessel(
        "M", bottom_journal="M.bottom", top_journal="M.top"
    )
    assert all(not report.conflicts for report in reports)
    return configs


def four_cell_problem(configs: list[VesselConfiguration]) -> PackingProblem:
    pieces = json.loads((MATERIALS / "pieces.json").read_text(encoding="utf-8"))
    _, bases, lids, stack_order = load_pieces(pieces)
    return PackingProblem(
        bases=bases,
        vessel_options={"M": configs},
        lids=lids,
        stack_order=stack_order,
        pinned_vessel_rotations={"M": 0},
    )


class NotebookReconstructionTests(unittest.TestCase):
    def test_two_worn_base_configurations_match_the_example(self):
        configs = middle_configs(load_book())
        self.assertEqual(
            [(c.origin, c.bottom.notches, c.top.lugs) for c in configs],
            [(0, (0,), (1,)), (2, (2,), (3,))],
        )

    def test_last_page_must_close_the_circle(self):
        book = load_book()
        report = book.reconstruct("X.top")
        self.assertEqual(report.conflicts, (CLOSURE_FAILURE,))
        self.assertEqual(report.rings, ())

    def test_cell_collision_is_contradictory(self):
        # Two pages landing on the same cell collide.
        book2 = JournalBook()
        book2.add_journal(
            Journal(
                "K",
                4,
                None,
                (
                    Page("a", 1, "lug"),    # cell 1
                    Page("b", 2, "notch"),  # cell 3
                    Page("c", 2, "lug"),    # cell 1 again
                    Page("d", 3, "notch"),  # cell 0, closes the circle
                ),
            )
        )
        self.assertIn(CELL_COLLISION, book2.reconstruct("K").conflicts)

        # A walk whose steps do not sum to one circle cannot close.
        book3 = JournalBook()
        book3.add_journal(
            Journal(
                "C",
                4,
                None,
                (Page("a", 1, "lug"), Page("b", 2, "notch")),
            )
        )
        self.assertIn(CLOSURE_FAILURE, book3.reconstruct("C").conflicts)

    def test_same_name_page_revision_and_replay(self):
        book = load_book(revised=True)
        revised = middle_configs(book)
        # 2+2 walk: top lugs move to cells 2/0.
        self.assertEqual(
            [(c.origin, c.top.lugs) for c in revised],
            [(0, (2,)), (2, (0,))],
        )
        # The old reading is still replayable under its original revision.
        old = book.reconstruct("M.top", revision_pins={"mt1": 1, "mt2": 1})
        self.assertEqual([ring.lugs for ring in old.rings], [(1,), (3,)])
        self.assertEqual(book.revision_of("M.top", "mt1"), 2)

    def test_top_and_bottom_share_the_worn_base_choice(self):
        book = JournalBook()
        book.add_journal(
            Journal("b", 4, "notch", (Page("p", 4, "notch"),), (0, 2))
        )
        book.add_journal(
            Journal("t", 4, None, (Page("q", 4, "lug"),), (0,))
        )
        with self.assertRaises(LacquerError):
            book.reconstruct_vessel(
                "V", bottom_journal="b", top_journal="t"
            )


class PackingTests(unittest.TestCase):
    def setUp(self):
        self.configs = middle_configs(load_book())
        self.problem = four_cell_problem(self.configs)

    def test_each_world_has_its_own_upright_seating(self):
        seatings = {
            configuration_signature(world): [
                (s["base"].item_id, s["lid"].item_id)
                for s in find_seatings_for_world(self.problem, world)
            ]
            for world in enumerate_worlds(self.problem)
        }
        self.assertEqual(seatings["M:o0"], [("B0", "L1")])
        self.assertEqual(seatings["M:o2"], [("B2", "L3")])

    def test_no_sheet_is_releasable_before_observation(self):
        self.assertEqual(find_robust_sheets(self.problem), [])

    def test_revised_board_cannot_be_carried_away_with_either_lid(self):
        revised = middle_configs(load_book(revised=True))
        for lid_id in ("L1", "L3"):
            problem = four_cell_problem(revised)
            problem.lids = [lid for lid in problem.lids if lid.item_id == lid_id]
            for world in enumerate_worlds(problem):
                self.assertEqual(find_seatings_for_world(problem, world), [])
            self.assertEqual(find_robust_sheets(problem), [])

    def test_adjacent_joints_share_one_vessel_rotation(self):
        def cfg(item, origin, bottom, top):
            return VesselConfiguration(
                item,
                ring_from_features(8, notches=bottom),
                ring_from_features(8, lugs=top),
                origin,
                "journal",
            )

        problem = PackingProblem(
            bases=[Base("B", ring_from_features(8, lugs=[0]))],
            vessel_options={
                "M1": [cfg("M1", 0, [0], [1]), cfg("M1", 4, [4], [5])],
                "M2": [cfg("M2", 0, [1], [2]), cfg("M2", 4, [5], [6])],
            },
            lids=[Lid("L", ring_from_features(8, notches=[2]))],
            stack_order=["M1", "M2"],
        )
        for world in enumerate_worlds(problem):
            for seating in find_seatings_for_world(problem, world):
                rotations = {seat.item_id: seat.rotation for seat in seating["seats"]}
                # Every vessel appears exactly once: one rotation, both joints.
                self.assertEqual(len(rotations), 2)

    def test_witness_rules_out_alignments(self):
        def cfg(item, origin, bottom, top):
            return VesselConfiguration(
                item,
                ring_from_features(8, notches=bottom),
                ring_from_features(8, lugs=top),
                origin,
                "journal",
            )

        witness = Witness("M1", "top", 1, "M2", "bottom", 1)
        problem = PackingProblem(
            bases=[Base("B", ring_from_features(8, lugs=[0]))],
            vessel_options={
                "M1": [cfg("M1", 0, [0], [1]), cfg("M1", 4, [4], [5])],
                "M2": [cfg("M2", 0, [1], [2]), cfg("M2", 4, [5], [6])],
            },
            lids=[Lid("L", ring_from_features(8, notches=[2]))],
            stack_order=["M1", "M2"],
            witnesses=[witness],
        )
        fit_origins = {
            world["M1"].origin
            for world in enumerate_worlds(problem)
            if find_seatings_for_world(problem, world)
        }
        self.assertEqual(fit_origins, {0})

    def test_six_piece_limit_is_enforced(self):
        ring = ring_from_features(4, notches=[0], lugs=[1])
        options = {
            f"M{i}": [VesselConfiguration(f"M{i}", ring, ring, 0, "j")]
            for i in range(6)
        }
        with self.assertRaises(LacquerError):
            PackingProblem(
                bases=[Base("B", ring_from_features(4, lugs=[0]))],
                vessel_options=options,
                lids=[Lid("L", ring_from_features(4, notches=[1]))],
                stack_order=[f"M{i}" for i in range(6)],
            )


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.configs = middle_configs(load_book())
        self.problem = four_cell_problem(self.configs)
        doc = json.loads((MATERIALS / "work_orders.json").read_text(encoding="utf-8"))
        self.orders = [load_work_order(entry) for entry in doc["orders"]]

    def test_two_beat_gauge_commits_both_worlds_without_tongue_use(self):
        results = evaluate_all_worlds(self.orders[0], self.problem)
        self.assertTrue(all(result["committable"] for result in results))
        self.assertTrue(all(result["beats_used"] == 2 for result in results))
        self.assertTrue(all(not result["consumed_tongues"] for result in results))
        sheets = {result["real_world"]: result["committed_sheet"] for result in results}
        self.assertEqual(
            (sheets["M:o0"]["base_id"], sheets["M:o0"]["lid_id"]),
            ("B0", "L1"),
        )
        self.assertEqual(
            (sheets["M:o2"]["base_id"], sheets["M:o2"]["lid_id"]),
            ("B2", "L3"),
        )

    def test_one_beat_bite_needs_the_authorized_named_spare(self):
        with_spare = evaluate_all_worlds(self.orders[1], self.problem)
        self.assertTrue(all(result["committable"] for result in with_spare))
        origin_zero = next(r for r in with_spare if r["real_world"] == "M:o0")
        self.assertEqual(origin_zero["consumed_tongues"], {"M": ["wax_spare"]})
        self.assertEqual(origin_zero["used_spares"], {"M": ["wax_spare"]})
        origin_two = next(r for r in with_spare if r["real_world"] == "M:o2")
        # A failed bite spends only the beat.
        self.assertEqual(origin_two["consumed_tongues"], {})

    def test_withdrawing_the_spare_blocks_signoff(self):
        order = self.orders[1]
        stripped = WorkOrder(
            order_id=order.order_id,
            beat_budget=order.beat_budget,
            required_original_tongues=order.required_original_tongues,
            allowed_spare_tongues={},
            original_tongues=order.original_tongues,
            offers=order.offers,
        )
        results = evaluate_all_worlds(stripped, self.problem)
        self.assertFalse(any(result["committable"] for result in results))

    def test_one_beat_without_spare_cannot_commit(self):
        results = evaluate_all_worlds(self.orders[2], self.problem)
        self.assertFalse(any(result["committable"] for result in results))

    def test_original_tongue_cannot_be_spent_or_impersonated(self):
        with self.assertRaises(LacquerError) as ctx:
            run_question(
                self.orders[0],
                ResourceLedger(),
                TrialBiteQuestion("S_bite", "M", "bottom", 0, "wax_orig"),
                self.configs[0],
            )
        self.assertEqual(ctx.exception.code, ORIGINAL_TONGUE_REQUIRED)

    def test_pressed_layer_cannot_be_reopened(self):
        order = WorkOrder(
            order_id="locked",
            beat_budget=4,
            required_original_tongues={},
            allowed_spare_tongues={},
            original_tongues={},
            offers=self.orders[0].offers,
            pressed_vessels=frozenset({"M"}),
        )
        with self.assertRaises(LacquerError) as ctx:
            run_question(
                order,
                ResourceLedger(),
                GaugeQuestion("S_gauge", "M", "bottom", 0),
                self.configs[0],
            )
        self.assertEqual(ctx.exception.code, COMMITMENT_BLOCKED)

    def test_gauge_dwells_two_beats_and_respects_budget(self):
        order = WorkOrder(
            order_id="tight",
            beat_budget=1,
            required_original_tongues={},
            allowed_spare_tongues={},
            original_tongues={},
            offers=self.orders[0].offers,
        )
        with self.assertRaises(LacquerError) as ctx:
            run_question(
                order,
                ResourceLedger(),
                GaugeQuestion("S_gauge", "M", "bottom", 0),
                self.configs[0],
            )
        self.assertEqual(ctx.exception.code, BUDGET_EXHAUSTED)

    def test_questions_must_be_offered_by_the_work_order(self):
        with self.assertRaises(LacquerError) as ctx:
            run_question(
                self.orders[0],
                ResourceLedger(),
                GaugeQuestion("unknown_station", "M", "bottom", 0),
                self.configs[0],
            )
        self.assertEqual(ctx.exception.code, "test_not_offered")

    def test_one_time_tongue_cannot_be_consumed_twice(self):
        order = self.orders[1]
        generous = WorkOrder(
            order_id=order.order_id,
            beat_budget=4,
            required_original_tongues=order.required_original_tongues,
            allowed_spare_tongues=order.allowed_spare_tongues,
            original_tongues=order.original_tongues,
            offers=order.offers,
        )
        ledger = ResourceLedger()
        question = TrialBiteQuestion("S_bite", "M", "bottom", 0, "wax_spare")
        run_question(generous, ledger, question, self.configs[0])
        with self.assertRaises(LacquerError) as ctx:
            run_question(generous, ledger, question, self.configs[0])
        self.assertEqual(ctx.exception.code, TONGUE_UNAVAILABLE)


class SamplingTests(unittest.TestCase):
    def test_only_usable_export_pages_are_sampled(self):
        book = load_book()
        with tempfile.TemporaryDirectory() as tmp:
            usable = Path(tmp) / "usable.jsonl"
            rows = []
            for journal_id in ("M.bottom", "M.top"):
                page_ids = [page.page_id for page in book.journal(journal_id).pages]
                rows.append(
                    {
                        "format": "citation-product/1",
                        "catalog_revision": 1,
                        "citation_id": f"SEG_{journal_id}",
                        "text": journal_id,
                        "kind": "PINNED",
                        "status": "usable",
                        "source": {"volume_id": "VLACQ", "page_ids": page_ids},
                        "pages": [
                            {
                                "volume_id": "VLACQ",
                                "page_id": page_id,
                                "section_id": "rim",
                                "label": page_id,
                                "active": True,
                            }
                            for page_id in page_ids
                        ],
                        "reason": None,
                    }
                )
            usable.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            sampled = reconstruct_from_export(
                book, "M.top", [usable], volume_id="VLACQ"
            )
            self.assertEqual(sampled.sampled_pages, ("mt1", "mt2"))
            self.assertEqual(len(sampled.rings), 2)
            unsampled = reconstruct_from_export(
                book, "X.top", [usable], volume_id="VLACQ"
            )
            self.assertEqual(unsampled.conflicts, (NO_USABLE_PAGES,))
            self.assertEqual(unsampled.rings, ())


if __name__ == "__main__":
    unittest.main()
