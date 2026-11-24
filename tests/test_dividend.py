"""红利子系统单元测试：可分余额、亏损冲回、稳定选择、保底承诺、权益锁定与兑领。"""

import os
import tempfile
import unittest
from fractions import Fraction

from group_consol import (
    DividendWorkspace,
    ShareBook,
    DividendError,
    CustodyError,
    BookError,
    fresh,
)


def F(x):
    return Fraction(x)


class DividendBalanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.ws = fresh(self.tmp.name)

    def tearDown(self):
        self.ws.close()
        os.unlink(self.tmp.name)

    def _seed(self):
        ws = self.ws
        ws.main_post(
            "P-i1",
            [["receivable", 16, 0], ["revenue", 0, 16], ["cost", 8, 0], ["inventory", 0, 8]],
            {"type": "internal_sale", "side": "seller", "invoice_id": "i1",
             "q": 4, "T": 16, "C": 8})
        ws.main_post("S-ext2", [["cash", 10, 0], ["revenue", 0, 10],
                                ["cost", 8, 0], ["inventory", 0, 8]])
        ws.main_post("S-other", [["cash", 6, 0], ["revenue", 0, 6]])
        ws.declare_stock("s1", {"i1": 2})

    def test_distributable_from_consolidated_base(self):
        self._seed()
        b = self.ws.balance("s1")
        # 对外收入 16 - 对外成本 8 + 内部已实现 4 = 12；余 2 件的内部毛利 4 留库
        self.assertEqual(b["distributable"], 12)
        self.assertEqual(b["internal_gross_profit"], 8)
        self.assertEqual(b["internal_realized_profit"], 4)
        self.assertEqual(b["inventory_kept_profit"]["total"], 4)
        item = b["inventory_kept_profit"]["items"][0]
        self.assertEqual((item["q"], item["T"], item["C"], item["r"]), (4, 16, 8, 2))

    def test_expense_reduces_then_floor_at_zero(self):
        self._seed()
        self.ws.main_post("exp3", [["cost", 3, 0], ["cash", 0, 3]])
        self.assertEqual(self.ws.balance("s1")["distributable"], 9)
        self.ws.main_post("exp-big", [["cost", 100, 0], ["cash", 0, 100]])
        b = self.ws.balance("s1")
        self.assertEqual(b["distributable"], 0)  # 最低取零
        self.assertLess(b["raw_before_floor"], 0)

    def test_declaration_is_deducted_and_immutable(self):
        self._seed()
        self.ws.main.declare("D0", F(4), "prior")
        self.assertEqual(self.ws.balance("s1")["distributable"], 8)
        self.assertEqual(self.ws.balance("s1")["net_declared"], 4)
        # 撤销后恢复
        self.ws.main.revoke("D0")
        self.assertEqual(self.ws.balance("s1")["distributable"], 12)

    def test_loss_recovery_partial_and_no_double_count(self):
        memo = self.ws.house.memo
        memo.register_loss("L1", F(5))
        memo.recover_loss("R1", "L1", F(2))
        self.assertEqual(memo.effective_loss(), 3)
        # 新版替换旧版：只算一次
        memo.recover_loss("R1b", "L1", F(4), supersedes="R1")
        self.assertEqual(memo.effective_loss(), 1)
        with self.assertRaises(BookError) as ctx:
            memo.recover_loss("R1", "L1", F(2))  # 旧版再用
        self.assertEqual(ctx.exception.code, "stale_reversal")
        with self.assertRaises(BookError):
            memo.recover_loss("Rx", "L1", F(99))  # 超额

    def test_overwrite_voucher_not_double_counted(self):
        memo = self.ws.house.memo
        memo.register_loss("L1", F(10))
        memo.recover_loss("R1", "L1", F(5))
        memo.recover_loss("R2", "L1", F(5))
        self.assertEqual(memo.effective_loss(), 0)
        # 用新版替换 R1 时，旧 R1 不再计入：R1(5)->6 只计一次，合计 6+5=11 超额
        with self.assertRaises(BookError) as ctx:
            memo.recover_loss("R1x", "L1", F(6), supersedes="R1")
        self.assertEqual(ctx.exception.code, "over_recovery")

    def test_balance_from_real_group_worksheet(self):
        from group_consol import EntityLedger, ConsolidationService
        from group_consol.verifier import verify_worksheet
        gdb = tempfile.mktemp(suffix="-group.db")
        led = EntityLedger(gdb)
        try:
            svc = ConsolidationService(led)
            sale = {"type": "internal_sale", "invoice_id": "i1", "seller": "P",
                    "buyer": "S", "sku": "X", "q": 4, "T": 16, "C": 8}
            led.post("Popen", "P", [["inventory", 8, 0], ["capital", 0, 8]])
            led.post("sale", "P",
                     [["receivable", 16, 0], ["revenue", 0, 16], ["cost", 8, 0],
                      ["inventory", 0, 8]], {**sale, "side": "seller"})
            led.post("buy", "S", [["inventory", 16, 0], ["payable", 0, 16]],
                     {**sale, "side": "buyer"})
            led.post("Sext", "S", [["cash", 10, 0], ["revenue", 0, 10],
                                   ["cost", 8, 0], ["inventory", 0, 8]])
            led.post("Sother", "S", [["cash", 6, 0], ["revenue", 0, 6]])
            led.register_stock("gv", {"i1": 2})
            stmt = svc.publish("g1", {"P": led.head("P"), "S": led.head("S")}, "gv")
            verify_worksheet(led.conn, "g1")
            b = self.ws.balance_from_worksheet(stmt)
            self.assertEqual(b["distributable"], 12)
            self.assertEqual(b["traceable_income"], 12)
        finally:
            led.close()
            os.unlink(gdb)


class StabilityTests(unittest.TestCase):
    def test_single_pref_convert_is_unique_stable(self):
        classes = {"common": {"kind": "common"},
                   "B": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0}}
        holdings = {"甲": {"common": 1}, "乙": {"B": 1}}
        sb = ShareBook(classes, holdings)
        ev = sb.evaluate_all(F(12))
        stable = [o["choice"] for o in ev["stable_options"]]
        self.assertEqual(stable, [{"B": "convert"}])
        self.assertEqual(ev["stable_options"][0]["amounts"], {"甲": F(4), "乙": F(8)})

    def test_fixed_9_makes_cash_stable(self):
        classes = {"common": {"kind": "common"},
                   "B": {"kind": "preference", "shares": 1, "fixed": 9, "ratio": 2, "order": 0}}
        holdings = {"甲": {"common": 1}, "乙": {"B": 1}}
        ev = ShareBook(classes, holdings).evaluate_all(F(12))
        self.assertEqual([o["choice"] for o in ev["stable_options"]], [{"B": "cash"}])
        self.assertEqual(ev["stable_options"][0]["amounts"], {"甲": F(3), "乙": F(9)})

    def test_no_stable_option_lists_deviations(self):
        classes = {"common": {"kind": "common"},
                   "A": {"kind": "preference", "shares": 1, "fixed": 1, "ratio": 1, "order": 0},
                   "B": {"kind": "preference", "shares": 1, "fixed": 6, "ratio": 2, "order": 1},
                   "C": {"kind": "preference", "shares": 1, "fixed": 1, "ratio": 2, "order": 2}}
        holdings = {"甲": {"common": 1}, "a": {"A": 1}, "b": {"B": 1}, "c": {"C": 1}}
        ev = ShareBook(classes, holdings).evaluate_all(F(6))
        self.assertTrue(ev["all_unstable"])
        self.assertEqual(len(ev["rejected_deviations"]), 8)
        for d in ev["rejected_deviations"]:
            self.assertGreater(d["deviation"]["gain"], 0)

    def test_shortfall_later_class_absorbs(self):
        classes = {"common": {"kind": "common"},
                   "A": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 0},
                   "B": {"kind": "preference", "shares": 1, "fixed": 5, "ratio": 2, "order": 1}}
        holdings = {"甲": {"common": 1}, "乙": {"A": 1}, "丙": {"B": 1}}
        ev = ShareBook(classes, holdings).evaluate_all(F(7))
        amt = ev["stable_options"][0]["amounts"]
        self.assertEqual(amt["乙"], 5)   # 先取全额
        self.assertEqual(amt["丙"], 2)   # 后类承受短缺


class PromiseTests(unittest.TestCase):
    def setUp(self):
        self.classes = {"common": {"kind": "common"},
                        "B": {"kind": "preference", "shares": 1, "fixed": 5,
                              "ratio": 2, "order": 0}}
        self.holdings = {"甲": {"common": 1}, "乙": {"B": 1}}

    def test_promise_within_stable_min(self):
        ev = ShareBook(self.classes, self.holdings).evaluate_all(F(9))
        sb = ShareBook(self.classes, self.holdings)
        ev["_book"] = sb
        # 费用 3 后 -> 稳定方案甲=3
        self.assertEqual(ev["ranges"]["甲"]["min"], 3)
        p = DividendWorkspace.promise(ev, "甲", F(3))
        self.assertTrue(p["funded"])
        with self.assertRaises(DividendError):
            DividendWorkspace.promise(ev, "甲", F(4))


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.ws = fresh(self.tmp.name)

    def tearDown(self):
        self.ws.close()
        os.unlink(self.tmp.name)

    def _claim(self, owner, amount, channel="facade"):
        rec = self.ws.claims.open_claims({owner: F(amount)}, {owner: channel}, 1)
        return rec[0]["claim_id"]

    def test_transfer_split_and_receipt_gating(self):
        jia = self._claim("甲", 4)
        t = self.ws.transfer("TR", jia, "甲", "丙", F(1), at_step=2, at="facade")
        self.assertEqual(t["source_remaining"], 3)
        bing = t["new_claim_id"]
        with self.assertRaises(CustodyError) as ctx:
            self.ws.pay_claim(bing, "p1", 3)
        self.assertEqual(ctx.exception.code, "receipt_not_registered")
        self.assertIn("go_to", ctx.exception.details)
        self.ws.pay_claim(jia, "p2", 4)
        self.ws.register_receipt(t["receipt_id"], 5)
        self.ws.pay_claim(bing, "p3", 6)
        self.assertEqual(self.ws.claims.paid_total("甲"), 3)
        self.assertEqual(self.ws.claims.paid_total("丙"), 1)

    def test_contested_transfers_second_rejected(self):
        k = self._claim("K", 4)
        self.ws.transfer("A1", k, "K", "X", F(3), at_step=2, at="facade")
        with self.assertRaises(CustodyError) as ctx:
            self.ws.transfer("A2", k, "K", "Y", F(3), at_step=3, at="facade")
        self.assertEqual(ctx.exception.code, "insufficient_balance")
        self.assertEqual(self.ws.claims.claim_state(k)["remaining"], 1)

    def test_paid_claim_cannot_be_reassigned(self):
        k = self._claim("K", 2)
        self.ws.pay_claim(k, "p", 2)
        with self.assertRaises(CustodyError):
            self.ws.transfer("A3", k, "K", "Z", F(1), at_step=3, at="facade")

    def test_circuit_ticket_split_requires_genuine_unsplit(self):
        p = self._claim("P", 2, channel="circuit")
        tk = self.ws.issue_ticket(p, at_step=1)
        with self.assertRaises(CustodyError) as ctx:
            self.ws.pay_ticket(tk["ticket_id"], "p0", 2)  # 未交票
        self.assertEqual(ctx.exception.code, "ticket_not_delivered")
        sp = self.ws.split_ticket(tk["ticket_id"], "P", [("Q", F(2))], at_step=3)
        child = sp["children"][0]
        self.assertEqual(child["old_no"], tk["ticket_id"])  # 只带拆前旧票号
        with self.assertRaises(CustodyError):
            self.ws.deliver_ticket(tk["ticket_id"], "P", 4)  # 旧票已拆
        self.assertTrue(self.ws.check_ticket(child["ticket_id"])["genuine"])
        self.ws.deliver_ticket(child["ticket_id"], "Q", 5)
        self.ws.pay_ticket(child["ticket_id"], "pq", 6)
        with self.assertRaises(CustodyError):
            self.ws.pay_ticket(child["ticket_id"], "pq2", 7)  # 不得重复付
        with self.assertRaises(CustodyError):
            self.ws.deliver_ticket(child["ticket_id"], "Q", 8)  # 已领不得退还

    def test_buyer_cannot_claim_seller_dividend(self):
        # 宣告归甲；甲卖股给乙，乙无 claim，不能领
        jia = self._claim("甲", 4)
        self.ws.transfer("TR", jia, "甲", "丙", F(1), at_step=2, at="facade")
        holders = {c["owner"] for c in self.ws.claims.list_claims()}
        self.assertIn("甲", holders)
        self.assertIn("丙", holders)
        self.assertNotIn("乙", holders)
        # 乙无红利权：按持有人查不到
        with self.assertRaises(DividendError):
            self.ws.claim_by_holder("no-lot", "乙")


if __name__ == "__main__":
    unittest.main(verbosity=2)
