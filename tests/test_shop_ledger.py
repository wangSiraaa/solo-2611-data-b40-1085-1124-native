import json
import os
import shutil
import tempfile
import unittest

from wrsource.ledger import (EVENT_CONTRADICTION, EVENT_CORRECTION,
                             LedgerError, OrderLedger, Pickup)
from wrsource.receipts import make_receipt_body


def record(row_id, buyer, visible, picked):
    body = make_receipt_body(buyer, visible, picked)
    import hashlib
    return {"row_id": row_id, "group": buyer, "rank": 1, "weight": 10,
            "hash_hex": "00", "a": "1", "log_screen": 0.0,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "body_bytes": len(body.encode()), "source_version": 1,
            "seed_hex": "00" * 32, "body": body}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = OrderLedger(os.path.join(self.dir, "ledger.sqlite"))
        self.ledger.init()
        self.records = [
            record("t1", "小林", ["甲", "乙"], "甲"),
            record("t2", "小齐", ["乙"], "乙"),
        ]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_open_and_reopen_order(self):
        oid = self.ledger.open_order(
            "plain", "ds/v1", 1, "ab" * 32, {"g": 1}, self.records,
            note="day one")
        self.ledger.record_pickup(
            oid, Pickup(0, "小林", "甲", "public", "case-1#p01"))
        self.ledger.record_pickup(
            oid, Pickup(1, "小齐", "乙", "public", "case-1#p00"))
        data = self.ledger.reopen_order(oid)
        self.assertEqual(data["mode"], "plain")
        self.assertEqual([t["row_id"] for t in data["tickets"]],
                         ["t1", "t2"])
        self.assertEqual([p["buyer"] for p in data["pickups"]],
                         ["小林", "小齐"])
        self.assertEqual(data["pickups"][0]["pack_id"], "case-1#p01")
        self.assertEqual(data["contradictions"], [])

    def test_unknown_order(self):
        with self.assertRaises(LedgerError):
            self.ledger.reopen_order(999)

    def test_correction_without_contradiction_is_logged(self):
        oid = self.ledger.open_order(
            "plain", "ds", 1, "ab" * 32, {"g": 1}, self.records)
        old = self.ledger.tickets(oid)[0].as_dict()
        new = dict(old, picked="乙")
        seq = self.ledger.record_correction(oid, "小林", old, new)
        data = self.ledger.reopen_order(oid)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn(EVENT_CORRECTION, kinds)
        self.assertNotIn(EVENT_CONTRADICTION, kinds)
        self.assertEqual(seq, 0)

    def test_contradiction_never_rewrites_pickup(self):
        oid = self.ledger.open_order(
            "plain", "ds", 1, "ab" * 32, {"g": 1}, self.records)
        pickup = Pickup(0, "小林", "乙", "public", "case-1#p00")
        self.ledger.record_pickup(oid, pickup)
        old = self.ledger.tickets(oid)[0].as_dict()
        new = dict(old, picked="甲")
        self.ledger.record_correction(
            oid, "小林", old, new, contradicts_pack=pickup.as_dict())
        data = self.ledger.reopen_order(oid)
        self.assertEqual(len(data["contradictions"]), 1)
        # The immutable pickup still shows the actual handed-out 乙 pack.
        self.assertEqual(self.ledger.pickups_for(oid, "小林"),
                         [pickup.as_dict()])
        contradiction = data["contradictions"][0]
        self.assertEqual(contradiction["payload"]["handed_out"]["pack_id"],
                         "case-1#p00")
        self.assertEqual(contradiction["payload"]["new_ticket"]["picked"],
                         "甲")

    def test_append_only_event_ordering(self):
        oid = self.ledger.open_order(
            "plain", "ds", 1, "ab" * 32, {"g": 1}, self.records)
        for i in range(3):
            self.ledger.record_note(oid, f"n{i}")
        data = self.ledger.reopen_order(oid)
        self.assertEqual([e["seq"] for e in data["events"]], [0, 1, 2])

    def test_persistence_across_reopen(self):
        oid = self.ledger.open_order(
            "plain", "ds", 1, "ab" * 32, {"g": 1}, self.records)
        self.ledger.record_pickup(
            oid, Pickup(0, "小林", "甲", "public", "p1"))
        # A brand new ledger instance on the same file reads the same day.
        second = OrderLedger(self.ledger.path)
        data = second.reopen_order(oid)
        self.assertEqual(data["pickups"][0]["pack_id"], "p1")
        self.assertEqual(second.list_orders()[0]["order_id"], oid)


if __name__ == "__main__":
    unittest.main()
