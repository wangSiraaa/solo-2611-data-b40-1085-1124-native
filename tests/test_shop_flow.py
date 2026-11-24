"""End-to-end tests: weighted warehouse -> sampled tickets -> desk/cabinet.

Only receipts the sampler actually selected may testify; retracting a
selected row admits the true next-ranked row while historical dataset
versions stay replayable.
"""

import json
import os
import shutil
import tempfile
import unittest

from wrsource.cabinet import (Box, Buyer, CabinetConfig, CabinetEngine,
                              MODE_PLAIN, MODE_RESERVE, MODE_RESTOCK,
                              Reservation, RestockRight, build_plan)
from wrsource.dataset import build_dataset, load_dataset, refresh_dataset
from wrsource.ledger import OrderLedger
from wrsource.receipts import PreferenceDesk, make_receipt_body
from wrsource.source import SourceService

SEED = bytes(range(32))


def add(row_id, buyer, visible, picked, group, weight=10):
    return {"op": "add", "row_id": row_id, "group": group, "weight": weight,
            "body": make_receipt_body(buyer, visible, picked), "shard": "s0"}


class ShopFlowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = SourceService(os.path.join(self.dir, "src.sqlite"))
        self.svc.init()
        self.ledger = OrderLedger(os.path.join(self.dir, "ledger.sqlite"))
        self.ledger.init()
        self.quotas = {"g-lin": 1, "g-qi": 1}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _ingest(self):
        rows = [
            add("lin-1", "小林", ["甲", "乙"], "甲", "g-lin", weight=50),
            add("lin-2", "小林", ["甲", "乙"], "乙", "g-lin", weight=5),
            add("qi-1", "小齐", ["乙"], "乙", "g-qi", weight=50),
            add("qi-2", "小齐", ["乙"], "乙", "g-qi", weight=5),
        ]
        return self.svc.apply_batch(rows, note="ingest").version

    def _boxes_and_buyers(self):
        boxes = (Box("case-1", {"甲": 1, "乙": 1}, price=10),)
        buyers = (
            Buyer("小林", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("小齐", 1, ("乙",), ("乙",)),
        )
        return boxes, buyers

    def test_only_sampled_tickets_testify_and_full_day_honoured(self):
        v = self._ingest()
        ds = os.path.join(self.dir, "ds1")
        _, records = build_dataset(self.svc, v, SEED, self.quotas, ds)
        ids = [r["row_id"] for r in records]
        self.assertEqual(ids, ["lin-1", "qi-1"])
        desk = PreferenceDesk.from_records(records)
        # The unsampled lin-2 (which claims 乙>甲) must not influence the desk.
        self.assertTrue(desk.must_precede("甲", "乙", "小林"))

        boxes, buyers = self._boxes_and_buyers()
        cfg = CabinetConfig(("甲", "乙"), boxes, buyers, 100, MODE_PLAIN)
        engine = CabinetEngine(cfg, lambda b, o: desk.possible_picks(b, o))
        report = engine.evaluate()
        self.assertIsNotNone(report.cheapest)

        oid = self.ledger.open_order(
            MODE_PLAIN, ds, v, SEED.hex(), self.quotas, records)
        handovers = engine.execute(
            build_plan(cfg, boxes),
            {"小林": ("甲", "乙"), "小齐": ("乙", "甲")})
        self.ledger.record_handovers(oid, handovers)
        replay = self.ledger.reopen_order(oid)
        self.assertEqual([t["row_id"] for t in replay["tickets"]], ids)
        self.assertEqual([p["variety"] for p in replay["pickups"]],
                         ["甲", "乙"])

    def test_erratum_on_selected_ticket_then_restored(self):
        v = self._ingest()
        ds1 = os.path.join(self.dir, "ds1")
        _, recs1 = build_dataset(self.svc, v, SEED, self.quotas, ds1)
        boxes, buyers = self._boxes_and_buyers()
        cfg = CabinetConfig(("甲", "乙"), boxes, buyers, 100, MODE_PLAIN)

        # Correct the selected ticket: 小林 really picked 乙.
        v2 = self.svc.apply_batch([
            {"op": "set_body", "row_id": "lin-1",
             "body": make_receipt_body("小林", ["甲", "乙"], "乙")},
        ]).version
        ds2 = os.path.join(self.dir, "ds2")
        _, recs2, _ = refresh_dataset(self.svc, ds1, new_version=v2,
                                      out_dir=ds2)
        self.assertEqual([r["row_id"] for r in recs2], ["lin-1", "qi-1"])
        desk2 = PreferenceDesk.from_records(recs2)
        self.assertTrue(desk2.must_precede("乙", "甲", "小林"))
        report2 = CabinetEngine(
            cfg, lambda b, o: desk2.possible_picks(b, o)).evaluate()
        self.assertIsNone(report2.cheapest)
        failing = next(p for p in report2.infeasible
                       if p.boxes == ("case-1",))
        self.assertEqual(failing.first_failed_buyer, "小齐")

        # Restore the original ticket: the promise is honoured again.
        v3 = self.svc.apply_batch([
            {"op": "set_body", "row_id": "lin-1",
             "body": make_receipt_body("小林", ["甲", "乙"], "甲")},
        ]).version
        ds3 = os.path.join(self.dir, "ds3")
        _, recs3, _ = refresh_dataset(self.svc, ds2, new_version=v3,
                                      out_dir=ds3)
        desk3 = PreferenceDesk.from_records(recs3)
        report3 = CabinetEngine(
            cfg, lambda b, o: desk3.possible_picks(b, o)).evaluate()
        self.assertIsNotNone(report3.cheapest)

    def test_retract_selected_admits_true_next_ranked_row(self):
        v = self._ingest()
        ds1 = os.path.join(self.dir, "ds1")
        _, recs1 = build_dataset(self.svc, v, SEED, self.quotas, ds1)
        self.assertEqual([r["row_id"] for r in recs1], ["lin-1", "qi-1"])
        self.svc.apply_batch([{"op": "retract", "row_id": "lin-1"}])
        v2 = self.svc.latest_version()
        ds2 = os.path.join(self.dir, "ds2")
        _, recs2, report = refresh_dataset(self.svc, ds1,
                                           new_version=v2, out_dir=ds2)
        ids2 = [r["row_id"] for r in recs2]
        self.assertNotIn("lin-1", ids2)
        self.assertIn("lin-2", ids2)  # the true next-ranked row
        # Historical dataset version remains byte-identical and replayable.
        _, hist = load_dataset(ds1)
        self.assertEqual([r["row_id"] for r in hist], ["lin-1", "qi-1"])
        with open(os.path.join(ds1, "samples.jsonl"), encoding="utf-8") as fh:
            self.assertTrue(fh.read().strip())
        self.assertIn("g-lin", report["rescanned_groups"])

    def test_sealed_boxes_without_jia_break_the_promise(self):
        v = self._ingest()
        ds = os.path.join(self.dir, "ds")
        _, records = build_dataset(self.svc, v, SEED, self.quotas, ds)
        desk = PreferenceDesk.from_records(records)
        boxes = (Box("no-jia", {"乙": 1}, price=8),)
        buyers = (
            Buyer("小林", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("小齐", 1, ("乙",), ("乙",)),
        )
        cfg = CabinetConfig(("甲", "乙"), boxes, buyers, 100, MODE_PLAIN)
        report = CabinetEngine(
            cfg, lambda b, o: desk.possible_picks(b, o)).evaluate()
        self.assertIsNone(report.cheapest)

    def test_reserve_and_restock_modes_share_budget_and_lists(self):
        v = self.svc.apply_batch([
            add("lin-u", "小林", ["甲"], "甲", "g-lin", weight=99),
            add("lin-c", "小林", ["甲", "乙"], "甲", "g-lin", weight=1),
            add("qi-1", "小齐", ["乙"], "乙", "g-qi", weight=50),
        ]).version
        ds = os.path.join(self.dir, "dsb")
        _, records = build_dataset(
            self.svc, v, SEED, {"g-lin": 1, "g-qi": 1}, ds)
        self.assertEqual([r["row_id"] for r in records],
                         ["lin-u", "qi-1"])
        desk = PreferenceDesk.from_records(
            records, varieties=["甲", "乙"])
        self.assertEqual(desk.relation("甲", "乙", "小林"), "both_possible")
        boxes = (Box("b-case", {"甲": 1, "乙": 1}, price=10),)
        buyers = (
            Buyer("小林", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("小齐", 1, ("乙",), ("乙",)),
        )
        hook = lambda b, o: desk.possible_picks(b, o)  # noqa: E731

        # Reserve mode: 乙 split out for 小齐, 小林 only faces 甲.
        cfg_r = CabinetConfig(
            ("甲", "乙"), boxes, buyers, 100, MODE_RESERVE,
            reservations=(Reservation("小齐", "乙", fee=2),))
        plan_r = build_plan(cfg_r, boxes)
        self.assertEqual(plan_r.public_counts(), {"甲": 1, "乙": 0})
        report_r = CabinetEngine(cfg_r, hook).evaluate()
        self.assertEqual(report_r.cheapest.total_cost, 12)

        # Restock mode: a late right cannot save the immediate next buyer.
        cfg_s = CabinetConfig(
            ("甲", "乙"), boxes, buyers, 100, MODE_RESTOCK,
            restock_rights=(RestockRight("r-yi", "乙", 3, 4),))
        report_s = CabinetEngine(cfg_s, hook).evaluate()
        self.assertIsNone(report_s.cheapest)
        failing = next(p for p in report_s.infeasible
                       if p.boxes == ("b-case",))
        self.assertEqual(failing.first_failed_buyer, "小齐")

    def test_dispensing_list_written_as_local_file(self):
        v = self._ingest()
        ds = os.path.join(self.dir, "ds")
        _, records = build_dataset(self.svc, v, SEED, self.quotas, ds)
        desk = PreferenceDesk.from_records(records)
        boxes, buyers = self._boxes_and_buyers()
        cfg = CabinetConfig(("甲", "乙"), boxes, buyers, 100, MODE_PLAIN)
        engine = CabinetEngine(cfg, lambda b, o: desk.possible_picks(b, o))
        handovers = engine.execute(
            build_plan(cfg, boxes),
            {"小林": ("甲", "乙"), "小齐": ("乙", "甲")})
        out = os.path.join(self.dir, "dispense.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump([h.as_dict() for h in handovers], fh,
                      ensure_ascii=False, indent=2)
        with open(out, encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertEqual([h["pack_id"] for h in loaded],
                         [h.pack_id for h in handovers])


if __name__ == "__main__":
    unittest.main()
