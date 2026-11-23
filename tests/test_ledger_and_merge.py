"""单元测试：负利润反向表达、有理分数、原子回滚不跳号、前缀不可变、跨发票不配对。"""

import os
import tempfile
import unittest
from fractions import Fraction

from group_consol import (
    EntityLedger,
    ConsolidationService,
    ValidationError,
    ConflictError,
    PendingError,
    SnapshotConflictError,
)
from group_consol.verifier import verify_worksheet

SALE_DOC = dict(type="internal_sale", invoice_id="i1", seller="P", buyer="S",
                sku="X", q=4, T=80, C=40)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.ledger = EntityLedger(self.tmp.name)
        self.svc = ConsolidationService(self.ledger)

    def tearDown(self):
        self.ledger.close()
        os.unlink(self.tmp.name)

    def _seed_invoice(self, iid, q, t, c, r):
        self.ledger.post(
            f"sell-{iid}", "P",
            [["receivable", t, 0], ["revenue", 0, t], ["cost", c, 0], ["inventory", 0, c]],
            {**SALE_DOC, "invoice_id": iid, "side": "seller", "q": q, "T": t, "C": c},
        )
        self.ledger.post(
            f"buy-{iid}", "S", [["inventory", t, 0], ["payable", 0, t]],
            {**SALE_DOC, "invoice_id": iid, "side": "buyer", "q": q, "T": t, "C": c},
        )
        return {"P": self.ledger.head("P"), "S": self.ledger.head("S")}

    def test_rollback_keeps_sequence_continuous(self):
        self.ledger.post("j1", "P", [["cash", 10, 0], ["capital", 0, 10]])
        with self.assertRaises(ValidationError):
            self.ledger.post("bad", "P", [["cash", 5, 0], ["revenue", 0, 4]])
        ok = self.ledger.post("j2", "P", [["cash", 7, 0], ["capital", 0, 7]])
        self.assertEqual(ok["seq"], 2)  # 失败不跳号
        self.assertEqual(self.ledger.head("P"), 2)

    def test_replay_and_conflict(self):
        lines = [["cash", 10, 0], ["capital", 0, 10]]
        first = self.ledger.post("j", "P", lines)
        again = self.ledger.post("j", "P", lines)
        self.assertEqual(first["seq"], again["seq"])
        self.assertEqual(again["status"], "replayed")
        with self.assertRaises(ConflictError):
            self.ledger.post("j", "P", [["cash", 11, 0], ["capital", 0, 11]])

    def test_line_order_is_significant(self):
        a = [["cash", 10, 0], ["capital", 0, 10]]
        b = list(reversed(a))
        self.ledger.post("ord", "P", a)
        with self.assertRaises(ConflictError):
            self.ledger.post("ord", "P", b)

    def test_negative_profit_uses_opposite_direction(self):
        heads = self._seed_invoice("i1", 5, 30, 50, 0)
        self.ledger.register_stock("v", {"i1": 2})  # T<C：URP = (30-50)*2/5 = -8
        stmt = self.svc.publish("w", heads, "v")
        urp = next(e for e in stmt["eliminations"] if e["kind"] == "unrealized_profit")
        signed = {ln["account"]: ln["debit"] - ln["credit"] for ln in urp["lines"]}
        # 负利润：借 inventory 8、贷 cost 8（与正利润方向相反，不截零）
        self.assertEqual(signed, {"inventory": 8, "cost": -8})
        verify_worksheet(self.ledger.conn, "w")

    def test_rational_profit_kept_as_fraction(self):
        heads = self._seed_invoice("i1", 3, 90, 60, 1)  # (90-60)/3 = 10
        self.ledger.register_stock("v", {"i1": 1})
        stmt = self.svc.publish("w", heads, "v")
        detail = stmt["invoice_details"][0]
        self.assertEqual(detail["unrealized_profit"], 10)
        # 换一个真分数：q=6, T=90, C=60, r=1 -> 5
        heads2 = self._seed_invoice("i2", 6, 91, 60, 1)  # URP = 31/6
        self.ledger.register_stock("v2", {"i1": 1, "i2": 1})
        stmt2 = self.svc.publish("w2", heads2, "v2")
        d2 = next(d for d in stmt2["invoice_details"] if d["invoice_id"] == "i2")
        self.assertEqual(d2["unrealized_profit"], {"num": 31, "den": 6})
        self.assertEqual(Fraction(31, 6), Fraction(d2["unrealized_profit"]["num"],
                                                    d2["unrealized_profit"]["den"]))
        verify_worksheet(self.ledger.conn, "w2")

    def test_r_zero_still_declared_and_no_urp_entry(self):
        heads = self._seed_invoice("i1", 4, 80, 40, 0)
        # 用一张不含 i1 的非空声明触发 stock_missing（r=0 也不能省略）
        heads_other = self._seed_invoice("i9", 1, 10, 5, 0)
        self.ledger.register_stock("partial", {"i9": 0})
        with self.assertRaises(PendingError) as ctx:
            self.svc.publish("bad", heads_other, "partial")
        self.assertIn("stock_missing",
                      [i["issue_type"] for i in ctx.exception.details["issues"]])
        self.ledger.register_stock("v", {"i1": 0, "i9": 0})
        stmt = self.svc.publish("w", heads_other, "v")
        self.assertNotIn("unrealized_profit",
                         [e["kind"] for e in stmt["eliminations"]])
        detail = next(d for d in stmt["invoice_details"] if d["invoice_id"] == "i1")
        self.assertIn("unrealized_profit:zero_not_clamped", detail["skipped"])

    def test_same_amount_different_invoices_do_not_cross_match(self):
        self._seed_invoice("i6", 5, 50, 20, 2)
        self._seed_invoice("i7", 5, 50, 25, 1)
        # P 收 i6，S 付 i7：总额相等但逐发票不一致
        self.ledger.post("r6", "P", [["cash", 50, 0], ["receivable", 0, 50]],
                         {"type": "internal_payment", "side": "receipt", "invoice_id": "i6"})
        self.ledger.post("p7", "S", [["payable", 50, 0], ["cash", 0, 50]],
                         {"type": "internal_payment", "side": "settlement", "invoice_id": "i7"})
        self.ledger.register_stock("v", {"i6": 2, "i7": 1})
        with self.assertRaises(PendingError) as ctx:
            self.svc.publish("w", {"P": self.ledger.head("P"), "S": self.ledger.head("S")}, "v")
        mismatches = {i["details"]["invoice_id"]
                      for i in ctx.exception.details["issues"]
                      if i["issue_type"] == "payment_mismatch"}
        self.assertEqual(mismatches, {"i6", "i7"})

    def test_old_prefix_and_old_worksheet_immutable(self):
        heads1 = self._seed_invoice("i1", 4, 80, 40, 2)
        self.ledger.register_stock("v1", {"i1": 2})
        old = self.svc.publish("w", heads1, "v1")
        verify_worksheet(self.ledger.conn, "w")
        # 新发票、新前缀、新声明
        heads2 = self._seed_invoice("i2", 3, 90, 60, 1)
        self.ledger.register_stock("v2", {"i1": 2, "i2": 1})
        self.svc.publish("w2", heads2, "v2")
        # 旧底稿复读不变；旧前缀 GL 也不变
        self.assertEqual(self.svc.statement("w"), old)
        old_gl = self.ledger.gl("P", heads1["P"])
        self.assertEqual(old_gl["prefix"], heads1["P"])
        with self.assertRaises(SnapshotConflictError):
            self.svc.publish("w", heads2, "v2")

    def test_payment_cap_enforced_locally(self):
        self._seed_invoice("i1", 4, 80, 40, 2)
        with self.assertRaises(ValidationError):
            self.ledger.post("over", "P", [["cash", 81, 0], ["receivable", 0, 81]],
                             {"type": "internal_payment", "side": "receipt", "invoice_id": "i1"})
        self.ledger.post("ok", "P", [["cash", 80, 0], ["receivable", 0, 80]],
                         {"type": "internal_payment", "side": "receipt", "invoice_id": "i1"})
        with self.assertRaises(ValidationError):  # 再付即超限
            self.ledger.post("over2", "P", [["cash", 1, 0], ["receivable", 0, 1]],
                             {"type": "internal_payment", "side": "receipt", "invoice_id": "i1"})

    def test_one_sided_post_never_rejected_for_consolidation(self):
        # 只有卖方单边：单体入账成功，发布才待核
        self.ledger.post(
            "sell-only", "P",
            [["receivable", 80, 0], ["revenue", 0, 80], ["cost", 40, 0], ["inventory", 0, 40]],
            {**SALE_DOC, "side": "seller"},
        )
        self.ledger.register_stock("v", {"i1": 2})
        with self.assertRaises(PendingError):
            self.svc.publish("w", {"P": 1, "S": 0}, "v")


if __name__ == "__main__":
    unittest.main(verbosity=2)
