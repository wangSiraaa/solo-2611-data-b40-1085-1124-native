import unittest

from wrsource.cabinet import (Box, Buyer, CabinetConfig, CabinetEngine,
                              CabinetError, CabinetFailure, MODE_PLAIN,
                              MODE_RESERVE, MODE_RESTOCK, Reservation,
                              RestockRight, build_plan)
from wrsource.receipts import PreferenceDesk, Ticket, make_receipt_body


def ticket(rid, buyer, visible, picked):
    return Ticket(rid, buyer, tuple(visible), picked, 1, "h")


def two_buyer_boxes():
    return (Box("case-1", {"甲": 1, "乙": 1}, price=10),)


def two_buyers():
    return (
        Buyer("小林", 0, acceptable=("甲", "乙"), visible=("甲", "乙")),
        Buyer("小齐", 1, acceptable=("乙",), visible=("乙",)),
    )


def hook(desk):
    return lambda buyer, options: desk.possible_picks(buyer, options)


class PlainCabinetTest(unittest.TestCase):
    def test_original_ticket_fully_honoured(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=100, mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNotNone(report.cheapest)
        self.assertEqual(report.cheapest.boxes, ("case-1",))
        self.assertEqual(report.cheapest.total_cost, 10)

    def test_correcting_selected_ticket_to_yi_breaks_qi(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "乙"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=100, mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNone(report.cheapest)
        failing = next(p for p in report.infeasible
                       if p.boxes == ("case-1",))
        self.assertEqual(failing.first_failed_buyer, "小齐")
        picked = [e.picked for e in failing.witness if e.picked]
        self.assertEqual(picked, ["乙"])
        self.assertEqual(failing.witness[-1].options, [])

    def test_removing_jia_from_boxes_also_fails(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        boxes = (Box("no-jia", {"乙": 1}, price=8),)
        cfg = CabinetConfig(("甲", "乙"), boxes, two_buyers(), budget=100,
                            mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNone(report.cheapest)
        failing = next(p for p in report.infeasible
                       if p.boxes == ("no-jia",))
        # 小林 still accepts 乙 when 甲 is absent; 小齐 then misses out.
        self.assertEqual(failing.first_failed_buyer, "小齐")

    def test_unknown_preference_is_adversarial(self):
        # 小林's ticket only saw 甲, so either tea could be his favourite.
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ], varieties=["甲", "乙"])
        self.assertEqual(desk.possible_picks("小林", ["甲", "乙"]),
                         ["甲", "乙"])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=100, mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNone(report.cheapest)
        failing = next(p for p in report.infeasible
                       if p.boxes == ("case-1",))
        self.assertEqual(failing.first_failed_buyer, "小齐")

    def test_cheapest_plan_picks_cheaper_boxes(self):
        boxes = (Box("cheap", {"甲": 1, "乙": 1}, price=6),
                 Box("pricey", {"甲": 1, "乙": 1}, price=20))
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), boxes, two_buyers(), budget=100,
                            mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertEqual(report.cheapest.boxes, ("cheap",))
        self.assertEqual(report.cheapest.total_cost, 6)

    def test_budget_filters_plans(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=5, mode=MODE_PLAIN)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        # The only real box is over budget; the empty subset cannot serve.
        self.assertIsNone(report.cheapest)
        self.assertTrue(all(p.boxes == () for p in report.infeasible))

    def test_shelf_only_shrinks_through_handovers(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=100, mode=MODE_PLAIN)
        engine = CabinetEngine(cfg, hook(desk))
        plan = build_plan(cfg, two_buyer_boxes())
        handovers = engine.execute(
            plan, {"小林": ("甲", "乙"), "小齐": ("乙", "甲")})
        self.assertEqual([h.variety for h in handovers], ["甲", "乙"])
        self.assertTrue(all(h.pack_id.startswith("case-1#")
                            for h in handovers))

    def test_concrete_execution_failure_raises(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲", "乙"], "乙"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ])
        cfg = CabinetConfig(("甲", "乙"), two_buyer_boxes(), two_buyers(),
                            budget=100, mode=MODE_PLAIN)
        engine = CabinetEngine(cfg, hook(desk))
        plan = build_plan(cfg, two_buyer_boxes())
        with self.assertRaises(CabinetFailure) as ctx:
            engine.execute(plan, {"小林": ("乙", "甲"),
                                  "小齐": ("乙", "甲")})
        self.assertEqual(ctx.exception.buyer, "小齐")
        self.assertEqual(len(ctx.exception.handovers), 1)


class ReserveModeTest(unittest.TestCase):
    def test_reservation_removes_pack_from_public_shelf(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ], varieties=["甲", "乙"])
        cfg = CabinetConfig(
            ("甲", "乙"), two_buyer_boxes(), two_buyers(), budget=100,
            mode=MODE_RESERVE,
            reservations=(Reservation("小齐", "乙", fee=2),))
        plan = build_plan(cfg, two_buyer_boxes())
        self.assertEqual(plan.public_counts(), {"甲": 1, "乙": 0})
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNotNone(report.cheapest)
        self.assertEqual(report.cheapest.total_cost, 12)
        self.assertEqual(report.cheapest.reservation_fee, 2)
        handovers = CabinetEngine(cfg, hook(desk)).execute(
            plan, {"小林": ("甲", "乙"), "小齐": ("乙", "甲")})
        self.assertEqual([h.source for h in handovers],
                         ["public", "reserved"])
        self.assertEqual([h.variety for h in handovers], ["甲", "乙"])

    def test_reservation_requires_stock(self):
        boxes = (Box("only-jia", {"甲": 1}, price=5),)
        cfg = CabinetConfig(
            ("甲", "乙"), boxes, two_buyers(), budget=100,
            mode=MODE_RESERVE,
            reservations=(Reservation("小齐", "乙", fee=2),))
        report = CabinetEngine(cfg).evaluate()
        self.assertIsNone(report.cheapest)

    def test_reservation_rejected_in_plain_mode(self):
        with self.assertRaises(CabinetError):
            CabinetConfig(
                ("甲", "乙"), two_buyer_boxes(), two_buyers(), budget=100,
                mode=MODE_PLAIN,
                reservations=(Reservation("小齐", "乙", fee=2),))


class RestockModeTest(unittest.TestCase):
    def _cfg(self, boxes=None, rights=None, budget=100):
        return CabinetConfig(
            ("甲", "乙"),
            boxes or two_buyer_boxes(), two_buyers(), budget=budget,
            mode=MODE_RESTOCK,
            restock_rights=rights or (
                RestockRight("r-yi", "乙", order_fee=3, execute_fee=4),))

    def test_restock_cannot_save_immediate_next_buyer(self):
        desk = PreferenceDesk([
            ticket("t1", "小林", ["甲"], "甲"),
            ticket("t2", "小齐", ["乙"], "乙"),
        ], varieties=["甲", "乙"])
        cfg = self._cfg()
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        self.assertIsNone(report.cheapest)
        failing = next(p for p in report.infeasible
                       if p.boxes == ("case-1",))
        self.assertEqual(failing.first_failed_buyer, "小齐")
        # No restock pack is ever handed out in time.
        self.assertTrue(all(e.source != "restock" for e in failing.witness))

    def test_restock_can_help_buyer_two_steps_later(self):
        boxes = (Box("one-jia", {"甲": 2}, price=10),)
        buyers = (
            Buyer("b0", 0, ("甲",), ("甲",)),
            Buyer("b1", 1, ("甲",), ("甲",)),
            Buyer("b2", 2, ("乙",), ("乙",)),
        )
        desk = PreferenceDesk([
            ticket("t0", "b0", ["甲"], "甲"),
            ticket("t1", "b1", ["甲"], "甲"),
            ticket("t2", "b2", ["乙"], "乙"),
        ], varieties=["甲", "乙"])
        cfg = CabinetConfig(
            ("甲", "乙"), boxes, buyers, budget=100, mode=MODE_RESTOCK,
            restock_rights=(RestockRight("r-yi", "乙", order_fee=2,
                                         execute_fee=3),))
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        cheapest = report.cheapest
        self.assertIsNotNone(cheapest)
        # Order fee 2 + one guaranteed execution 3 on top of the box.
        self.assertEqual(cheapest.restock_order_fee, 2)
        self.assertEqual(cheapest.restock_execute_fee, 3)
        self.assertEqual(cheapest.total_cost, 15)
        # A concrete run delivers a restock-sourced 乙 at step 2.
        engine = CabinetEngine(cfg, hook(desk))
        plan = build_plan(cfg, boxes, cfg.restock_rights)
        handovers = engine.execute(
            plan, {"b0": ("甲", "乙"), "b1": ("甲", "乙"),
                   "b2": ("乙", "甲")})
        self.assertEqual([h.variety for h in handovers],
                         ["甲", "甲", "乙"])
        self.assertEqual(handovers[-1].source, "restock")
        self.assertEqual(handovers[-1].pack_id, "restock:r-yi")

    def test_guaranteed_execution_fee_covers_worst_branch(self):
        # b0's preference is unknown so two picks stay compatible; whichever
        # tea b0 takes, the 乙-only buyer at step 2 still needs the right.
        boxes = (Box("two-jia", {"甲": 2}, price=10),)
        buyers = (
            Buyer("b0", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("b1", 1, ("甲",), ("甲",)),
            Buyer("b2", 2, ("乙",), ("乙",)),
        )
        desk = PreferenceDesk(
            [ticket("t1", "b1", ["甲"], "甲"),
             ticket("t2", "b2", ["乙"], "乙")],
            varieties=["甲", "乙"])
        self.assertEqual(desk.possible_picks("b0", ["甲", "乙"]),
                         ["甲", "乙"])
        cfg = CabinetConfig(
            ("甲", "乙"), boxes, buyers, budget=1000, mode=MODE_RESTOCK,
            restock_rights=(RestockRight("r-yi", "乙", order_fee=2,
                                         execute_fee=5),))
        cheapest = CabinetEngine(cfg, hook(desk)).evaluate().cheapest
        self.assertIsNotNone(cheapest)
        self.assertEqual(cheapest.restock_execute_fee, 5)
        self.assertEqual(cheapest.total_cost, 17)
        # And the same plan is rejected when the budget cannot cover the
        # guaranteed execution cost.
        cfg_tight = CabinetConfig(
            ("甲", "乙"), boxes, buyers, budget=16, mode=MODE_RESTOCK,
            restock_rights=(RestockRight("r-yi", "乙", order_fee=2,
                                         execute_fee=5),))
        self.assertIsNone(
            CabinetEngine(cfg_tight, hook(desk)).evaluate().cheapest)

    def test_rights_rejected_in_plain_mode(self):
        with self.assertRaises(CabinetError):
            CabinetConfig(
                ("甲", "乙"), two_buyer_boxes(), two_buyers(), budget=100,
                mode=MODE_PLAIN,
                restock_rights=(RestockRight("r", "乙", 1, 1),))

    def test_delay_below_two_rejected(self):
        with self.assertRaises(CabinetError):
            CabinetConfig(
                ("甲", "乙"), two_buyer_boxes(), two_buyers(), budget=100,
                mode=MODE_RESTOCK, restock_arrival_delay=1,
                restock_rights=(RestockRight("r", "乙", 1, 1),))


class RobustnessTest(unittest.TestCase):
    def test_unknown_pick_is_adversarial_over_real_branches(self):
        # With no preference evidence, b0 might take either tea.  One pack of
        # each therefore cannot promise the 乙-only buyer b1 their gift;
        # once a receipt proves b0 prefers 甲, the same boxes become robust.
        buyers = (
            Buyer("b0", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("b1", 1, ("乙",), ("乙",)),
        )
        boxes = (Box("balanced", {"甲": 1, "乙": 1}, price=10),)
        unknown = PreferenceDesk([], varieties=["甲", "乙"])
        bad = CabinetEngine(
            CabinetConfig(("甲", "乙"), boxes, buyers, 100),
            hook(unknown)).evaluate()
        self.assertIsNone(bad.cheapest)
        failing = next(p for p in bad.infeasible
                       if p.boxes == ("balanced",))
        self.assertEqual(failing.first_failed_buyer, "b1")

        known = PreferenceDesk([
            ticket("a", "b0", ["甲", "乙"], "甲"),
            ticket("b", "b1", ["乙"], "乙"),
        ])
        ok = CabinetEngine(
            CabinetConfig(("甲", "乙"), boxes, buyers, 100),
            hook(known)).evaluate()
        self.assertIsNotNone(ok.cheapest)

    def test_jia_only_boxes_cannot_serve_yi_only_buyer(self):
        buyers = (
            Buyer("b0", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("b1", 1, ("乙",), ("乙",)),
        )
        desk = PreferenceDesk([
            ticket("a", "b0", ["甲", "乙"], "甲"),
            ticket("b", "b1", ["乙"], "乙"),
        ])
        boxes = (Box("jia-only", {"甲": 1}, price=6),)
        report = CabinetEngine(
            CabinetConfig(("甲", "乙"), boxes, buyers, 100),
            hook(desk)).evaluate()
        self.assertIsNone(report.cheapest)
        failing = next(p for p in report.infeasible
                       if p.boxes == ("jia-only",))
        self.assertEqual(failing.first_failed_buyer, "b1")

    def test_fixed_orders_make_two_packs_of_one_tea_enough(self):
        # Sanity counterpart: constrained buyers always take 甲, so two 甲
        # packs robustly serve both of them.
        buyers = (
            Buyer("b0", 0, ("甲", "乙"), ("甲", "乙")),
            Buyer("b1", 1, ("甲", "乙"), ("甲", "乙")),
        )
        desk = PreferenceDesk([
            ticket("a", "b0", ["甲", "乙"], "甲"),
            ticket("b", "b1", ["甲", "乙"], "甲"),
        ])
        boxes = (Box("two-jia", {"甲": 2}, price=10),)
        report = CabinetEngine(
            CabinetConfig(("甲", "乙"), boxes, buyers, 100),
            hook(desk)).evaluate()
        self.assertIsNotNone(report.cheapest)

    def test_five_buyers_five_varieties_six_boxes_bound(self):
        # Stays inside the task's size bound and terminates quickly.
        varieties = tuple("v%d" % i for i in range(5))
        boxes = tuple(Box(f"c{i}", {varieties[i % 5]: 1,
                                    varieties[(i + 1) % 5]: 1}, price=3)
                      for i in range(6))
        buyers = tuple(Buyer(f"b{i}", i, varieties, varieties)
                       for i in range(5))
        desk = PreferenceDesk([], varieties=list(varieties))
        cfg = CabinetConfig(varieties, boxes, buyers, budget=1000)
        report = CabinetEngine(cfg, hook(desk)).evaluate()
        # 64 subsets evaluated, every result internally consistent.
        self.assertEqual(len(report.feasible) + len(report.infeasible), 64)
        for p in report.feasible:
            self.assertIsNone(p.first_failed_buyer)
        for p in report.infeasible:
            self.assertIsNotNone(p.first_failed_buyer)


if __name__ == "__main__":
    unittest.main()
