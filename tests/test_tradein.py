"""以旧换新·打字机拆件回收 单元测试。

覆盖题目要求：
- 验收台：发票对齐/真实外销、小票现金+折价逐项守恒、两台具名旧机、未交钥匙不可拆、
  结算前整单退机、先挂应收后结清、交付+入账才成存货、逐机可回收与已兑现折价、
  两卖场双重认领拒绝。
- 分解图/工场：阶段顺序、相邻杆损伤、字篮整体不可再散交、可复装、工位/格班时、
  具名机型+键位/完整字篮/整机订单、最大兑现数 + 平手最少工时的字典序。
- 改单/预约：在途测试（只松螺帽可改；固定杆已剪断 -> 拒绝且旧报价失效）、
  预约占位/释放、窗口保护、签回路径、其余订单可交复核、两种做法的代价对比。
- 甲机三视图：原头两单、账头退回现金到账前零单、压条检验改互损最多一单。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradein.diagram import Basket, CellLockError, Diagram, Piece
from tradein.workshop import Order, ScheduleError, Workshop
from tradein.acceptance import (
    AcceptanceDesk,
    ClaimError,
    InventoryGateError,
)
from tradein.convert import (
    ConversionError,
    ConversionWindow,
    POLICY_ALWAYS,
    POLICY_DEMAND,
    Quote,
    Reservation,
    ReservationReuseError,
    SlotPool,
    Station,
    Ticket,
    compare_policies,
    convert_via_signed_back,
    issue_reservation,
    release_reservation,
    station_guard,
    try_convert_to_whole,
)
from tradein.audit import audit_jia_machine, jia_diagram


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def simple_machine(model="M", keys=("a", "b"), *, label=None, cells=1,
                   damage=None, basket=None, nut_reversible=True):
    damage = damage or {}
    pieces = {
        "N": Piece("N", "nut", cells=1, reversible=nut_reversible, reassemble_cells=1),
        "R": Piece("R", "retainer", cells=1),
    }
    for k in keys:
        pieces[k] = Piece(k, "bar", key=k, cells=cells, damage=tuple(damage.get(k, ())))
    bk = Basket("BK", tuple(keys), cells=2) if basket else None
    return Diagram(model=model, pieces=pieces, basket=bk, label=label or model)


class AcceptanceDeskTests(unittest.TestCase):
    def setUp(self):
        self.desk = AcceptanceDesk()
        self.desk.align_invoice("i1", "P", "S", q=4, T=80, C=40)
        self.sale = self.desk.open_external_sale("s1", outlet="北店", price=60, invoice_id="i1")

    def _two_machines(self):
        m1 = self.desk.register_machine("北店", "TP-1", "bodyA", diagram=simple_machine(label="A"))
        m2 = self.desk.register_machine("北店", "TP-1", "bodyB", diagram=simple_machine(label="B"))
        return m1, m2

    def test_receivable_then_settle(self):
        self.assertEqual(self.sale.status, "receivable")
        self.desk.settle_external_sale("s1", 30)
        self.assertEqual(self.sale.status, "receivable")
        self.desk.settle_external_sale("s1", 30)
        self.assertEqual(self.sale.status, "settled")
        with self.assertRaises(InventoryGateError):
            self.desk.settle_external_sale("s1", 1)

    def test_ticket_must_reconcile_cash_plus_tradein(self):
        self._two_machines()
        with self.assertRaises(ValueError):
            self.desk.record_ticket("t1", "北店", "s1", cash=30,
                                    trade_values={"bodyA": 10, "bodyB": 10},  # 50 != 60
                                    old_bodies=["bodyA", "bodyB"])
        t = self.desk.record_ticket("t1", "北店", "s1", cash=35,
                                    trade_values={"bodyA": 15, "bodyB": 10},
                                    old_bodies=["bodyA", "bodyB"])
        self.assertTrue(t.reconciled())
        self.assertEqual(t.total(), 60)

    def test_old_values_must_match_bodies_one_to_one(self):
        self._two_machines()
        with self.assertRaises(ValueError):
            self.desk.record_ticket("t1", "北店", "s1", cash=40,
                                    trade_values={"bodyA": 20}, old_bodies=["bodyA", "bodyB"])

    def test_cross_outlet_duplicate_body_rejected(self):
        self.desk.open_external_sale("s2", outlet="南店", price=60, invoice_id="i1")
        self.desk.register_machine("北店", "TP-1", "bodyA")
        with self.assertRaises(ClaimError):
            self.desk.register_machine("南店", "TP-1", "bodyA")

    def test_uncashed_key_stored_and_not_dismantlable(self):
        m, _ = self._two_machines()
        self.assertTrue(m.stored)
        self.assertFalse(m.can_be_dismantled())
        self.desk.hand_over_key("bodyA")
        self.assertTrue(m.can_be_dismantled())

    def test_cancel_ticket_returns_whole_order(self):
        self._two_machines()
        t = self.desk.record_ticket("t1", "北店", "s1", cash=35,
                                    trade_values={"bodyA": 15, "bodyB": 10},
                                    old_bodies=["bodyA", "bodyB"])
        self.desk.cancel_ticket("t1")
        self.assertTrue(t.cancelled)
        self.assertEqual(self.sale.status, "cancelled")
        self.assertEqual(self.desk.inventory, [])

    def test_inventory_gate_requires_delivery_and_cash(self):
        self._two_machines()
        self.desk.hand_over_key("bodyA")
        self.desk.hand_over_key("bodyB")
        t = self.desk.record_ticket("t1", "北店", "s1", cash=35,
                                    trade_values={"bodyA": 15, "bodyB": 10},
                                    old_bodies=["bodyA", "bodyB"])
        with self.assertRaises(InventoryGateError):
            self.desk.admit_to_inventory("t1")
        self.desk.deliver_new("t1")
        with self.assertRaises(InventoryGateError):
            self.desk.admit_to_inventory("t1")
        self.desk.book_cash("t1")
        added = self.desk.admit_to_inventory("t1")
        self.assertEqual(sorted(added), ["bodyA", "bodyB"])

    def test_per_machine_recoverable_and_realized_value(self):
        self._two_machines()
        self.desk.hand_over_key("bodyA")  # bodyB 钥匙未交
        self.desk.record_ticket("t1", "北店", "s1", cash=35,
                                trade_values={"bodyA": 15, "bodyB": 10},
                                old_bodies=["bodyA", "bodyB"])
        self.desk.deliver_new("t1")
        self.desk.book_cash("t1")
        report = {r["body_id"]: r for r in self.desk.recoverable_bodies()}
        self.assertTrue(report["bodyA"]["recoverable"])
        self.assertEqual(report["bodyA"]["realized_value"], 15)
        self.assertFalse(report["bodyB"]["recoverable"])  # 无钥匙、柜方无权拆
        self.assertEqual(report["bodyB"]["realized_value"], 10)

    def test_cancelled_ticket_yields_no_realized_value(self):
        self._two_machines()
        self.desk.hand_over_key("bodyA")
        self.desk.deliver_new("t1") if False else None
        t = self.desk.record_ticket("t1", "北店", "s1", cash=60,
                                    trade_values={"bodyA": 0, "bodyB": 0},
                                    old_bodies=["bodyA", "bodyB"])
        self.desk.cancel_ticket("t1")
        report = {r["body_id"]: r for r in self.desk.recoverable_bodies()}
        self.assertEqual(report["bodyA"]["realized_value"], 0)
        self.assertFalse(report["bodyA"]["bookable"])


class DiagramTests(unittest.TestCase):
    def test_phase_order_enforced(self):
        d = simple_machine()
        st = d.initial_state()
        with self.assertRaises(CellLockError):
            d.apply_remove(st, "a")  # 螺帽/压条未拆
        st = d.apply_remove(st, "N")
        st = d.apply_remove(st, "R")
        st = d.apply_remove(st, "a")
        self.assertIn("a", st.removed)

    def test_directional_damage_y_then_x(self):
        d = Diagram(model="JIA", pieces={
            "X": Piece("X", "bar", key="X", cells=1, damage=("Y",)),
            "Y": Piece("Y", "bar", key="Y", cells=1),
        })
        # 先取 Y 不伤 X
        s = d.apply_remove(d.initial_state(), "Y")
        s = d.apply_remove(s, "X")
        keys = {i.key for i in s.produced if i.kind == "bar"}
        self.assertEqual(keys, {"X", "Y"})
        # 先取 X 损 Y
        s2 = d.apply_remove(d.initial_state(), "X")
        self.assertIn("Y", s2.damaged)
        s2 = d.apply_remove(s2, "Y")
        keys2 = {i.key for i in s2.produced if i.kind == "bar"}
        self.assertEqual(keys2, {"X"})  # Y 已损，没到手

    def test_basket_keeps_members_but_no_loose_bars(self):
        d = simple_machine(basket=True)
        st = d.apply_remove(d.initial_state(), "N")
        st = d.apply_remove(st, "R")
        st = d.apply_take_basket(st)
        items = st.produced
        self.assertTrue(any(i.kind == "basket" for i in items))
        self.assertFalse(any(i.kind == "bar" for i in items))
        with self.assertRaises(CellLockError):
            d.apply_remove(st, "a")  # 字篮已整体拆走

    def test_whole_delivery_requires_reversible_only(self):
        d = simple_machine(nut_reversible=True)
        st = d.apply_remove(d.initial_state(), "N")  # 可复装
        ws = d.apply_deliver_whole(st)
        self.assertTrue(ws.whole_delivered)
        # 花了 1 格 + 复装 1 格
        self.assertEqual(ws.spent, 2)

        d2 = Diagram(model="M", pieces={
            "R": Piece("R", "retainer", cells=1, reversible=False),
            "a": Piece("a", "bar", key="a", cells=1),
        })
        st2 = d2.apply_remove(d2.initial_state(), "R")
        with self.assertRaises(CellLockError):
            d2.apply_deliver_whole(st2)  # 不可复装 -> 不能追认整机


class WorkshopTests(unittest.TestCase):
    def test_maximize_fulfilled_then_min_time(self):
        # 便宜机 1 格、昂贵机 3 格都能供同一根杆 -> 取便宜
        cheap = Diagram(model="C", pieces={"a": Piece("a", "bar", key="a", cells=1)}, label="cheap")
        exp = Diagram(model="C", pieces={"a": Piece("a", "bar", key="a", cells=3)}, label="exp")
        res = Workshop([cheap, exp], [Order("o", "C", "bar", "a")], budget=8).solve()
        self.assertEqual(res.fulfilled_count, 1)
        self.assertEqual(res.total_cells, 1)
        self.assertEqual(res.plans[0].label, "cheap")

    def test_incompatible_key_not_substitutable(self):
        d = simple_machine()
        res = Workshop([d], [Order("o", "M", "bar", "z")], budget=8).solve()
        self.assertEqual(res.fulfilled_count, 0)

    def test_whole_vs_bar_conflict_picks_one(self):
        d = Diagram(model="M", pieces={"a": Piece("a", "bar", key="a", cells=1)}, label="M")
        orders = [Order("barA", "M", "bar", "a"), Order("whole", "M", "whole_machine")]
        res = Workshop([d], orders, budget=8).solve()
        self.assertEqual(res.fulfilled_count, 1)  # 同机不能既交整机又交散杆
        self.assertEqual(len(res.assignments), 1)

    def test_basket_order_matches_whole_basket(self):
        d = simple_machine(basket=True)
        res = Workshop([d], [Order("bk", "M", "basket")], budget=8).solve()
        self.assertEqual(res.fulfilled_count, 1)
        self.assertEqual(res.assignments[0].item.kind, "basket")

    def test_budget_excludes_costly_plan(self):
        d = Diagram(model="M", pieces={"a": Piece("a", "bar", key="a", cells=5)}, label="M")
        res = Workshop([d], [Order("o", "M", "bar", "a")], budget=3).solve()
        self.assertEqual(res.fulfilled_count, 0)
        self.assertEqual(res.total_cells, 0)

    def test_cross_machine_two_orders(self):
        m1 = simple_machine(label="A")
        m2 = simple_machine(label="B")
        orders = [Order("o1", "M", "bar", "a"), Order("o2", "M", "bar", "b")]
        res = Workshop([m1, m2], orders, budget=8).solve()
        self.assertEqual(res.fulfilled_count, 2)
        # 两台各取一根即可，仍须先拆 N/R + 目标杆 -> 至少若干格
        self.assertLessEqual(res.total_cells, 8)

    def test_limits(self):
        d = simple_machine()
        with self.assertRaises(ValueError):
            Workshop([d, d, d, d], [], 8)  # >3 机
        with self.assertRaises(ValueError):
            Workshop([d], [Order(f"o{i}", "M", "whole_machine") for i in range(7)], 8)


class ConvertTests(unittest.TestCase):
    def _jia_station(self, *, irreversible_retainer=False):
        pieces = {
            "N": Piece("N", "nut", cells=1, reversible=True, reassemble_cells=1),
        }
        if irreversible_retainer:
            pieces["R"] = Piece("R", "retainer", cells=1, reversible=False)
        pieces["X"] = Piece("X", "bar", key="X", cells=1, damage=("Y",))
        pieces["Y"] = Piece("Y", "bar", key="Y", cells=1)
        d = Diagram(model="JIA", pieces=pieces, label="JIA")
        return Station("b1", d)

    def test_inflight_nut_only_is_convertible(self):
        st = self._jia_station()
        st.loosen_nut("N")
        q = Quote("Q")
        dec = try_convert_to_whole(st, Ticket("T1", "JIA", "X"),
                                   q, [Ticket("T2", "JIA", "Y")], [], 8)
        self.assertTrue(dec.granted)
        self.assertTrue(q.valid)
        self.assertEqual(dec.reassembly_cells, 1)  # 复装占格

    def test_inflight_cut_retainer_rejects_and_invalidates_quote(self):
        st = self._jia_station(irreversible_retainer=True)
        st.loosen_nut("N")          # 可复装：松螺帽
        st.pry_retainer("R")        # 不可逆：固定杆已剪断
        q = Quote("Q")
        dec = try_convert_to_whole(st, Ticket("T3", "JIA", "X"),
                                   q, [Ticket("T4", "JIA", "Y")], [], 8)
        self.assertFalse(dec.granted)
        self.assertFalse(q.valid)  # 旧“整机可交”报价必须失效
        self.assertIn("不可逆", dec.reason)

    def test_shipped_bar_prevents_whole(self):
        st = self._jia_station()
        st.remove("N")
        st.remove("X")  # 取走一根，Y 已损，杆已发
        q = Quote("Q")
        dec = try_convert_to_whole(st, Ticket("T", "JIA", "X"), q,
                                   [], [], 8)
        self.assertFalse(dec.granted)
        self.assertFalse(q.valid)

    def test_reservation_occupies_scarce_slot_and_release(self):
        pool = SlotPool(1)
        win = ConversionWindow(open=True)
        res = issue_reservation(pool, Reservation("R1", "T1", "b1", "JIA"), win)
        self.assertEqual(pool.free(), 0)
        with self.assertRaises(ReservationReuseError):
            issue_reservation(pool, Reservation("R2", "T2", "b2", "JIA"), win)
        release_reservation(pool, res)
        self.assertEqual(pool.free(), 1)

    def test_window_guard_blocks_irreversible(self):
        pool = SlotPool(1)
        win = ConversionWindow(open=True)
        st = self._jia_station(irreversible_retainer=True)
        res = issue_reservation(pool, Reservation("R1", "T1", "b1", "JIA"), win)
        with self.assertRaises(ConversionError):
            station_guard(win, res, st, "R")  # 窗开+预约占位 -> 禁止不可逆
        # 窗关后可执行
        win.close()
        station_guard(win, res, st, "R")
        # 或收到释放后可执行
        win2 = ConversionWindow(open=True)
        win2.give_release()
        station_guard(win2, res, self._jia_station(irreversible_retainer=True), "R")

    def test_signed_back_path_requires_sign(self):
        st = self._jia_station()
        st.loosen_nut("N")
        q = Quote("Q")
        with self.assertRaises(ConversionError):
            convert_via_signed_back(st, Ticket("T", "JIA", "X"), q, [], [], 8)
        st.sign_back()
        dec = convert_via_signed_back(st, Ticket("T", "JIA", "X"), q, [], [], 8)
        self.assertTrue(dec.granted)

    def test_remaining_orders_feasibility_checked(self):
        # 预算极低：改整机后其余订单无法交 -> 拒绝
        st = self._jia_station()
        dec = try_convert_to_whole(
            st, Ticket("T1", "JIA", "X"), Quote("Q"),
            [Ticket("T2", "JIA", "Y")], [], budget=0,
        )
        self.assertFalse(dec.granted)
        self.assertFalse(dec.remaining_orders_ok)

    def test_policy_comparison_exposes_tradeoff(self):
        always, demand = compare_policies(
            reservation_window_end=10, third_machine_deadline=6, customer_convert_deadline=8,
        )
        self.assertEqual(always.policy, POLICY_ALWAYS)
        self.assertTrue(always.conversion_guaranteed)
        self.assertFalse(always.third_machine_met)
        self.assertEqual(demand.policy, POLICY_DEMAND)
        self.assertTrue(demand.third_machine_met)
        # 截止 <= 窗结束的约束
        with self.assertRaises(ValueError):
            compare_policies(reservation_window_end=5, third_machine_deadline=6,
                             customer_convert_deadline=4)


class JiaAuditTests(unittest.TestCase):
    def test_three_views(self):
        audit = audit_jia_machine(already_spent=2, budget=8)
        self.assertEqual(audit.original_two["fulfilled_count"], 2)
        self.assertEqual(audit.rolled_back_two["fulfilled_count"], 0)
        self.assertLessEqual(audit.pressure_bar_at_most_one["fulfilled_count"], 1)

    def test_mutual_damage_limits_to_one(self):
        d = jia_diagram(mutual_damage=True)
        res = Workshop([d], [Order("X", "JIA", "bar", "X"), Order("Y", "JIA", "bar", "Y")],
                       budget=8).solve()
        self.assertLessEqual(res.fulfilled_count, 1)


if __name__ == "__main__":
    unittest.main()
