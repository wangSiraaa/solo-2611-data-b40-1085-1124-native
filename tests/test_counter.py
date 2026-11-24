"""换卡柜（保证单 / 押换 / 即换 / 自适应开包 / 台账）单元测试。"""

import json
import unittest

from wrsource.counter import (CounterPlanner, CounterSpec, OrderRunner,
                              build_from_spec)
from wrsource.tape import (Belief, Card, FORWARD, REVERSE, Hypothesis,
                           Receipt, TapePattern, TapeRoom)

星 = Card("星", False)
船 = Card("船", False)
月 = Card("月", False)
亮船 = Card("船", True)
TAPE3 = TapePattern("星船月", (星, 船, 月))


def two_world_belief(pack, c1, c2):
    """造两个解释，使 ``pack`` 在两世界分别揭出 c1 / c2。"""
    h1 = Hypothesis(TAPE3, (TAPE3.cells.index(c1) - pack) % 3, FORWARD)
    h2 = Hypothesis(TAPE3, (TAPE3.cells.index(c2) - pack) % 3, FORWARD)
    return Belief((h1, h2))


class ExampleOneTest(unittest.TestCase):
    """题面例 1：两册各缺普船、柜台一张普星+一次换权、最多拆一包。"""

    def setUp(self):
        t2 = TapePattern("星月船", (星, 月, 船))
        rm = TapeRoom((TAPE3, t2),
                      (Receipt("r2", 2, 0, design="船"),))
        self.belief = Belief(rm.feasible_hypotheses(
            start_cells=[0], directions=[FORWARD]))
        self.counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "船", "foil": False}]},
                    {"album_id": "乙",
                     "slots": [{"design": "船", "foil": False}]}],
            holdings=[{"card_id": "S",
                       "card": {"design": "星", "foil": False}}],
            rights=1,
            swap_rules=[{"rule_id": "SW1",
                         "from": {"design": "星", "foil": False},
                         "to": {"design": "船", "foil": False}}],
            customers=[], packs=[5, 6], max_opens=1)
        self.spec = CounterSpec.from_dict(self.counter)

    def test_receipt_locks_pack_five_to_boat_and_guarantees_two(self):
        # 回条锁定第 5 包与第 2 包同相位 -> 必出普船。
        self.assertEqual(self.belief.pack_outcomes(5), ((船,),))
        slips, policy = CounterPlanner(self.spec, self.belief).plan_max()
        self.assertEqual(slips, ("甲", "乙"))
        self._completes_in_every_world(slips, policy)

    def test_retracting_receipt_drops_guarantee_to_one(self):
        rm = TapeRoom((TAPE3, TapePattern("星月船", (星, 月, 船))), ())
        belief = Belief(rm.feasible_hypotheses(start_cells=[0],
                                               directions=[FORWARD]))
        slips, _ = CounterPlanner(self.spec, belief).plan_max()
        self.assertEqual(slips, ("甲",))

    def test_removing_right_also_drops_to_one(self):
        spec = CounterSpec.from_dict({**self.counter, "rights": 0})
        slips, _ = CounterPlanner(spec, self.belief).plan_max()
        self.assertEqual(slips, ("甲",))

    def _completes_in_every_world(self, slips, policy):
        planner = CounterPlanner(self.spec, self.belief)
        for h in self.belief.hypotheses:
            runner = OrderRunner(planner, "ORD", slips)
            st = runner.run(policy, planner.initial_state(slips), h.key)
            self.assertTrue(all(not x for x in st.remaining))


class ExampleTwoEscrowTest(unittest.TestCase):
    """题面例 2：押换柜制可签两册；仅即换只能保一册。"""

    def setUp(self):
        self.belief = two_world_belief(5, 星, 船)
        self.counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "船", "foil": False}]},
                    {"album_id": "乙",
                     "slots": [{"design": "星", "foil": False}]}],
            holdings=[{"card_id": "S",
                       "card": {"design": "星", "foil": False}}],
            rights=1,
            customers=[{"customer_id": "换卡户",
                        "card": {"card_id": "C",
                                 "card": {"design": "船", "foil": False}},
                        "wants": {"design": "星"},
                        "departs_before_pack": 5}],
            packs=[5], max_opens=1)

    def test_escrow_guarantees_both_albums_under_either_outcome(self):
        spec = CounterSpec.from_dict(self.counter)
        planner = CounterPlanner(spec, self.belief)
        slips, policy = planner.plan_max()
        self.assertEqual(slips, ("甲", "乙"))
        # 办理树必须是“押换 -> 开包 -> 两个观察分支”。
        self.assertEqual(policy.kind, "action")
        self.assertEqual(policy.detail["event"]["event"], "escrow_placed")
        chance = policy.next
        self.assertEqual(chance.kind, "chance")
        self.assertEqual(len(chance.branches), 2)
        for h in self.belief.hypotheses:
            runner = OrderRunner(planner, "ORD-2", slips)
            st = runner.run(policy, planner.initial_state(slips), h.key)
            self.assertTrue(all(not x for x in st.remaining))

    def test_instant_only_guarantees_one(self):
        spec = CounterSpec.from_dict(
            {**self.counter, "escrow_allowed": False})
        slips, policy = CounterPlanner(spec, self.belief).plan_max()
        self.assertEqual(len(slips), 1)
        self.assertEqual(policy.kind, "action")
        self.assertEqual(policy.detail["event"]["event"], "instant_swap")

    def test_blocking_escrow_card_falls_back_to_one(self):
        spec = CounterSpec.from_dict(
            {**self.counter, "escrow_blocked_cards": ["S"]})
        self.assertEqual(
            CounterPlanner(spec, self.belief).plan_max()[0], ("甲",))

    def test_early_departure_breaks_escrow_timing(self):
        # 户在签单时（任何包拆开前）已经离柜：即换/押换都办不成，
        # 只剩开包本身，星/船两种包况下最多保住一册。
        counter = dict(self.counter)
        counter["customers"] = [
            {**self.counter["customers"][0],
             "departs_before_pack": 0}]
        spec = CounterSpec.from_dict(counter)
        slips, policy = CounterPlanner(spec, self.belief).plan_max()
        # 没有任何交易可做，两种包况下只能保住同一册（具体哪册由规划器
        # 选定的通用策略决定），绝不可能两册同签。
        self.assertEqual(len(slips), 1)
        text = json.dumps(policy.to_dict(), ensure_ascii=False)
        self.assertNotIn("instant_swap", text)
        self.assertNotIn("escrow_placed", text)


class AdaptiveOpeningTest(unittest.TestCase):
    """补例：开包后才该决定下一包；固定路线严格更差。"""

    def setUp(self):
        h_f = Hypothesis(TAPE3, 0, FORWARD)
        h_r = Hypothesis(TAPE3, 0, REVERSE)
        self.belief = Belief((h_f, h_r))
        # 包 4：正向普月、反向普船；包 5：正向普船、反向普月。
        self.counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "月", "foil": False}]},
                    {"album_id": "乙",
                     "slots": [{"design": "船", "foil": False}]}],
            holdings=[], rights=0, customers=[],
            packs=[4, 5], max_opens=2)

    def test_adaptive_policy_branches_on_observation(self):
        spec = CounterSpec.from_dict(self.counter)
        planner = CounterPlanner(spec, self.belief)
        slips, policy = planner.plan_max()
        self.assertEqual(slips, ("甲", "乙"))
        self.assertEqual(policy.kind, "chance")
        self.assertEqual(policy.detail["event"]["pack_id"], 4)
        for label, branch in policy.branches:
            # 第一个包的观察直接决定第二包怎么用，且都能办结。
            self.assertIn(branch.kind, ("action", "chance"))

    def test_single_open_cannot_guarantee_two(self):
        # 只开一包：无论开 4 还是 5，另一册要的图案都可能拿不到 -> 0 本。
        spec = CounterSpec.from_dict({**self.counter, "max_opens": 1})
        slips, _ = CounterPlanner(spec, self.belief).plan_max()
        self.assertEqual(slips, ())

    def test_holdings_can_cover_one_with_single_open(self):
        # 柜台先持有一张普月时，单开包 5 必能拿到普船，可保一册。
        counter = dict(self.counter, max_opens=1, holdings=[
            {"card_id": "H", "card": {"design": "月", "foil": False}}])
        spec = CounterSpec.from_dict(counter)
        slips, _ = CounterPlanner(spec, self.belief).plan_max()
        self.assertEqual(slips, ("甲",))


class LedgerTest(unittest.TestCase):
    def test_card_and_right_traces_cover_full_lifecycle(self):
        belief = two_world_belief(5, 星, 船)
        counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "船", "foil": False}]},
                    {"album_id": "乙",
                     "slots": [{"design": "星", "foil": False}]}],
            holdings=[{"card_id": "S",
                       "card": {"design": "星", "foil": False}}],
            rights=1,
            customers=[{"customer_id": "换卡户",
                        "card": {"card_id": "C",
                                 "card": {"design": "船", "foil": False}},
                        "wants": {"design": "星"},
                        "departs_before_pack": 5}],
            packs=[5], max_opens=1)
        spec = CounterSpec.from_dict(counter)
        planner = CounterPlanner(spec, belief)
        slips, policy = planner.plan_max()
        # 选“开船”世界：押换落空，S 与 R0 都应在台账里看到释放与去向。
        boat_world = next(h for h in belief.hypotheses
                          if h.pack_content(5, 1) == (船,))
        runner = OrderRunner(planner, "ORD-X", slips)
        runner.run(policy, planner.initial_state(slips), boat_world.key)
        ledger = runner.ledger
        self.assertEqual([e["album_id"] for e in ledger.commitment()],
                         ["甲", "乙"])
        s_trace = ledger.card_trace("S")
        self.assertTrue(any(e["event"] == "escrow_placed" for e in s_trace))
        self.assertTrue(any(e["event"] == "escrow_failed" for e in s_trace))
        self.assertTrue(any(e["event"] == "mount" and
                            e.get("album_id") == "乙" for e in s_trace))
        right = ledger.right_trace("R0")
        self.assertTrue(any(e["event"] == "escrow_placed" for e in right))
        self.assertTrue(any(e["event"] == "escrow_failed" for e in right))
        # 原卡号重查：包卡 P5#0 装到了甲。
        p5 = ledger.card_trace("P5#0")
        self.assertTrue(any(e["event"] == "mount" and
                            e.get("album_id") == "甲" for e in p5))

    def test_listed_swap_consumes_right_irreversibly(self):
        belief = Belief((Hypothesis(TAPE3, 0, FORWARD),))
        counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "船", "foil": False}]}],
            holdings=[{"card_id": "S",
                       "card": {"design": "星", "foil": False}}],
            rights=1,
            swap_rules=[{"rule_id": "SW1",
                         "from": {"design": "星", "foil": False},
                         "to": {"design": "船", "foil": False}}],
            customers=[], packs=[], max_opens=0)
        spec = CounterSpec.from_dict(counter)
        planner = CounterPlanner(spec, belief)
        slips, policy = planner.plan_max()
        self.assertEqual(slips, ("甲",))
        runner = OrderRunner(planner, "ORD-L", slips)
        st = runner.run(policy, planner.initial_state(slips),
                        belief.hypotheses[0].key)
        self.assertEqual(st.rights, ())       # 换权已用掉，不退还
        self.assertTrue(any(e["event"] == "listed_swap"
                            for e in runner.ledger.events))


class FoilAndSpecTest(unittest.TestCase):
    def test_foil_requirement_is_distinguished(self):
        # 亮船不能拿来填普船空位。
        belief = Belief((Hypothesis(TAPE3, 0, FORWARD),))
        counter = dict(
            albums=[{"album_id": "甲",
                     "slots": [{"design": "船", "foil": True}]}],
            holdings=[{"card_id": "FB",
                       "card": {"design": "船", "foil": True}}],
            rights=0, customers=[], packs=[], max_opens=0)
        spec = CounterSpec.from_dict(counter)
        slips, _ = CounterPlanner(spec, belief).plan_max()
        self.assertEqual(slips, ("甲",))
        # 换成普通船就签不下来。
        counter2 = dict(counter, holdings=[
            {"card_id": "PB", "card": {"design": "船", "foil": False}}])
        self.assertEqual(
            CounterPlanner(CounterSpec.from_dict(counter2), belief)
            .plan_max()[0], ())

    def test_build_from_spec_shared_tape_and_counter_vocabulary(self):
        spec = {
            "pack_size": 1,
            "patterns": [TAPE3.as_dict(),
                          TapePattern("星月船", (星, 月, 船)).as_dict()],
            "receipts": [Receipt("r2", 2, 0, design="船").as_dict()],
            "constraints": {"start_cells": [0], "directions": ["forward"]},
            "counter": {
                "albums": [{"album_id": "甲",
                            "slots": [{"design": "船",
                                       "foil": False}]}],
                "holdings": [], "rights": 0, "customers": [],
                "packs": [5], "max_opens": 1}}
        room, cs, belief, planner = build_from_spec(spec)
        self.assertEqual(belief.pack_outcomes(5), ((船,),))
        slips, _ = planner.plan_max()
        self.assertEqual(slips, ("甲",))

    def test_opened_pack_cannot_be_handed_to_counter(self):
        spec = {
            "pack_size": 1, "patterns": [TAPE3.as_dict()],
            "receipts": [Receipt("r2", 2, 0, design="船").as_dict()],
            "counter": {"albums": [], "holdings": [], "rights": 0,
                        "customers": [], "packs": [2], "max_opens": 1}}
        with self.assertRaises(Exception):
            build_from_spec(spec)

    def test_impossible_belief_rejected(self):
        room = TapeRoom((TAPE3,), (
            Receipt("a", 0, 0, design="船"),
            Receipt("b", 0, 0, design="月")))
        with self.assertRaises(Exception):
            CounterPlanner(CounterSpec.from_dict(
                {"albums": [], "holdings": [], "rights": 0,
                 "customers": [], "packs": [1], "max_opens": 1}),
                room.build_belief())


if __name__ == "__main__":
    unittest.main()
