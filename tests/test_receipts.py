import unittest

from wrsource.receipts import (PreferenceCycleError,
                               PreferenceDesk, ReceiptError, Ticket,
                               make_receipt_body, parse_receipt,
                               tickets_from_records)


def rec(row_id, buyer, visible, picked, version=1):
    body = make_receipt_body(buyer, visible, picked)
    import hashlib
    return {"row_id": row_id, "group": buyer, "rank": 1, "weight": 10,
            "hash_hex": "00", "a": "1", "log_screen": 0.0,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "body_bytes": len(body.encode()), "source_version": version,
            "seed_hex": "00" * 32, "body": body}


class ReceiptParseTest(unittest.TestCase):
    def test_round_trip(self):
        body = make_receipt_body("b", ["x", "y"], "x")
        parsed = parse_receipt(body)
        self.assertEqual(parsed["buyer"], "b")
        self.assertEqual(parsed["visible"], ["x", "y"])
        self.assertEqual(parsed["picked"], "x")

    def test_errors(self):
        with self.assertRaises(ReceiptError):
            parse_receipt({"buyer": "b", "visible": ["x"], "picked": "y"})
        with self.assertRaises(ReceiptError):
            parse_receipt({"buyer": "b", "visible": [], "picked": "x"})
        with self.assertRaises(ReceiptError):
            parse_receipt({"buyer": "", "visible": ["x"], "picked": "x"})
        with self.assertRaises(ReceiptError):
            parse_receipt("not json")
        with self.assertRaises(ReceiptError):
            parse_receipt({"buyer": "b", "visible": ["x", "x"],
                           "picked": "x"})

    def test_ticket_from_record_only_uses_sampled_rows(self):
        tickets = tickets_from_records([rec("r1", "b", ["x", "y"], "x")])
        self.assertEqual(len(tickets), 1)
        self.assertIsInstance(tickets[0], Ticket)
        self.assertEqual(tickets[0].visible, ("x", "y"))


class PreferenceDeskTest(unittest.TestCase):
    def setUp(self):
        records = [
            rec("t1", "lin", ["甲", "乙"], "甲"),
            rec("t2", "lin", ["乙", "丙"], "乙"),
            rec("t3", "qi", ["乙"], "乙"),
        ]
        self.desk = PreferenceDesk.from_records(
            records, varieties=["甲", "乙", "丙"])

    def test_edges_and_provenance(self):
        edges = { (x, y): prov for x, y, prov in self.desk.edges("lin") }
        self.assertIn(("甲", "乙"), edges)
        self.assertIn(("乙", "丙"), edges)
        self.assertEqual(edges[("甲", "乙")], ["t1"])

    def test_must_and_can_precede(self):
        self.assertTrue(self.desk.must_precede("甲", "丙", "lin"))
        self.assertFalse(self.desk.must_precede("丙", "甲", "lin"))
        # The transitive chain 甲>乙>丙 forbids 丙 before 甲 in every order.
        self.assertFalse(self.desk.can_precede("丙", "甲", "lin"))
        self.assertTrue(self.desk.can_precede("甲", "丙", "lin"))
        # 甲 must beat 乙 from the direct receipt edge.
        self.assertEqual(self.desk.relation("甲", "乙", "lin"), "x_first")
        self.assertEqual(self.desk.relation("乙", "甲", "lin"), "y_first")

    def test_both_possible_when_unconstrained(self):
        desk = PreferenceDesk.from_records(
            [rec("u", "lin", ["甲"], "甲")],
            varieties=["甲", "乙"])
        self.assertEqual(desk.relation("甲", "乙", "lin"), "both_possible")
        self.assertEqual(desk.possible_picks("lin", ["甲", "乙"]),
                         ["甲", "乙"])

    def test_possible_picks_respects_constraints(self):
        self.assertEqual(
            self.desk.possible_picks("lin", ["甲", "乙", "丙"]), ["甲"])
        # Without 甲, 乙 is the top compatible choice.
        self.assertEqual(
            self.desk.possible_picks("lin", ["乙", "丙"]), ["乙"])

    def test_witness_and_compatible_orders(self):
        orders = self.desk.compatible_orders("lin", ["甲", "乙", "丙"])
        self.assertIn(("甲", "乙", "丙"), orders)
        self.assertNotIn(("乙", "甲", "丙"), orders)
        witness = self.desk.witness_order("lin", varieties=["甲", "乙", "丙"])
        self.assertEqual(witness, ("甲", "乙", "丙"))

    def test_merged_scope_and_per_buyer_separation(self):
        merged = {(x, y) for x, y, _ in self.desk.edges(None)}
        self.assertIn(("甲", "乙"), merged)
        self.assertIn(("乙", "丙"), merged)
        # 小齐's own graph has no 甲 edge at all.
        qi_edges = {(x, y) for x, y, _ in self.desk.edges("qi")}
        self.assertNotIn(("甲", "乙"), qi_edges)
        self.assertFalse(self.desk.must_precede("甲", "乙", "qi"))

    def test_merged_ring_single_tickets_fine(self):
        records = [
            rec("r1", "b1", ["x", "y"], "x"),
            rec("r2", "b2", ["y", "z"], "y"),
            rec("r3", "b3", ["z", "x"], "z"),
        ]
        desk = PreferenceDesk.from_records(records, varieties=["x", "y", "z"])
        for buyer in desk.buyers:
            self.assertIsNone(desk.find_cycle(buyer))
        cycle = desk.find_cycle()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        evidence = desk.cycle_evidence()
        self.assertEqual({e["preferred"] for e in evidence},
                         {"x", "y", "z"})
        with self.assertRaises(PreferenceCycleError):
            desk.relation("x", "y")
        self.assertFalse(desk.satisfiable("b1", [("y", "x")]))

    def test_per_buyer_contradiction(self):
        records = [
            rec("a", "b", ["x", "y"], "x"),
            rec("b", "b", ["x", "y"], "y"),
        ]
        desk = PreferenceDesk.from_records(records, varieties=["x", "y"])
        self.assertIn("b", desk.per_buyer_cycles())
        with self.assertRaises(PreferenceCycleError):
            desk.witness_order("b", varieties=["x", "y"])


if __name__ == "__main__":
    unittest.main()
