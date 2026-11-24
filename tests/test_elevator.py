"""桌面电梯演练测试：历史还原、确定性查询、交班信念态规划与回放。

覆盖题面给定的全部已确认窗口：

* 例 1 基础例（容量 2，可接乙）与两个反例（下客计数改 0、容量改 1）。
* 同载重、在途者去向不同时，停车卡只按读数分支、不偷看身份。
* 例 2：同一空位签给两站使后票落空；改下客计数改变可承诺组。
* 临时招呼多一拍错过交付；调宽窗口后可接组数变化。
* 与加权采样数据集对接：只认真正入样的计数行，且可重复。
"""

import json
import os
import shutil
import tempfile
import unittest

from wrsource.dataset import build_dataset
from wrsource.elevator import (HAIL, TICKET, Dispatch, Scenario,
                               certain_onboard, enumerate_histories,
                               handover_worlds, parse_dispatch,
                               parse_scenario, plan_handover,
                               possible_onboard, replay_all_worlds,
                               replay_plan, rows_from_dataset,
                               scenario_ingest_ops)
from wrsource.source import SourceService


def ex1(downs_at_1: int, capacity: int) -> Scenario:
    return parse_scenario({
        "name": "ex1", "floors": [0, 1, 2], "direction": "up",
        "capacity": capacity,
        "groups": [{"id": "甲", "size": 1, "origin": 0,
                    "destinations": [1, 2]}],
        "observations": [
            {"row_id": "c0", "trip_id": "t1", "interval": [0, 0],
             "type": "ups", "value": 1},
            {"row_id": "c1", "trip_id": "t1", "interval": [1, 1],
             "type": "downs", "value": downs_at_1},
            {"row_id": "c2", "trip_id": "t1", "interval": [2, 2],
             "type": "downs", "value": 1 - downs_at_1}],
    })


def ex1_dispatch(observed_alight: int) -> Dispatch:
    return parse_dispatch({
        "handover_floor": 1, "observed_alight": observed_alight,
        "max_beats": 10,
        "reservations": [{"id": "乙", "size": 2, "origin": 1,
                          "destinations": [2], "window": [0, 0]}]})


class ReconstructTest(unittest.TestCase):
    def test_ex1_history_matches_count(self):
        # 1 层下客 1 => 甲只能在 1 层下。
        hist = enumerate_histories(ex1(1, 2))
        self.assertEqual([h.choice for h in hist], [{"甲": 1}])
        # 1 层下客 0 => 甲只能在 2 层下（终点空厢强制）。
        hist0 = enumerate_histories(ex1(0, 2))
        self.assertEqual([h.choice for h in hist0], [{"甲": 2}])

    def test_all_observation_kinds_filter_jointly(self):
        scn = parse_scenario({
            "name": "kinds", "floors": [0, 1, 2], "direction": "up",
            "capacity": 3,
            "groups": [
                {"id": "甲", "size": 1, "origin": 0,
                 "destinations": [1, 2]},
                {"id": "乙", "size": 2, "origin": 0,
                 "destinations": [1, 2]}],
            "observations": [
                {"row_id": "u", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 3},
                {"row_id": "g", "trip_id": "t", "interval": [1, 2],
                 "type": "gross", "value": 3},
                {"row_id": "n", "trip_id": "t", "interval": [0, 2],
                 "type": "net", "value": 0}],
        })
        # 四种计数共同约束：所有组合里只有“3 人全部下完”的两类去向合法，
        # 且 1 层下客人数只能是 0、1、2、3 中的可行值。
        rides = [tuple(sorted(h.choice.items())) for h in
                 enumerate_histories(scn)]
        self.assertIn((("乙", 1), ("甲", 1)), rides)
        self.assertIn((("乙", 2), ("甲", 2)), rides)
        for h in enumerate_histories(scn):
            self.assertEqual(h.loads[0], 3)
            self.assertEqual(h.loads[2], 0)
            self.assertLessEqual(max(h.loads.values()), 3)

    def test_capacity_violation_rejected(self):
        # 两人组 + 容量 1，任何搭乘都超载 => 只剩“都不上”，但 ups=1 又要求
        # 有人上，因此没有相容历史。
        scn = parse_scenario({
            "name": "cap", "floors": [0, 1], "direction": "up",
            "capacity": 1,
            "groups": [{"id": "甲", "size": 2, "origin": 0,
                        "destinations": [1]}],
            "observations": [
                {"row_id": "u", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 2}],
        })
        self.assertEqual(enumerate_histories(scn), [])

    def test_named_observation_pins_identity(self):
        scn = parse_scenario({
            "name": "named", "floors": [0, 1, 2], "direction": "up",
            "capacity": 3,
            "groups": [
                {"id": "甲", "size": 1, "origin": 0,
                 "destinations": [1, 2]},
                {"id": "乙", "size": 1, "origin": 0,
                 "destinations": [1, 2]},
                {"id": "丙", "size": 1, "origin": 0,
                 "destinations": [2]}],
            "observations": [
                {"row_id": "n0", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 3, "named_floor": 0,
                 "named_in": ["甲", "乙", "丙"], "named_out": []},
                {"row_id": "g", "trip_id": "t", "interval": [1, 2],
                 "type": "gross", "value": 3}],
        })
        hist = enumerate_histories(scn)
        # 丙只允许 2 层下；甲、乙各两选 => 4 条历史。
        self.assertEqual(len(hist), 4)
        self.assertTrue(all(h.choice["丙"] == 2 for h in hist))
        self.assertEqual(certain_onboard(hist, 1), ("丙",))
        self.assertEqual(possible_onboard(hist, 1), ("丙", "乙", "甲"))
        # 实名名单与实际整进整出不符 => 零历史：乙候梯层为 1，0 层不可能登厢。
        bad = parse_scenario({
            "name": "bad", "floors": [0, 1, 2], "direction": "up",
            "capacity": 2,
            "groups": [
                {"id": "甲", "size": 1, "origin": 0,
                 "destinations": [1, 2]},
                {"id": "乙", "size": 1, "origin": 1,
                 "destinations": [2]}],
            "observations": [
                {"row_id": "n", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 2, "named_floor": 0,
                 "named_in": ["甲", "乙"], "named_out": []}],
        })
        self.assertEqual(enumerate_histories(bad), [])

    def test_mixed_trip_rejected(self):
        with self.assertRaises(ValueError):
            parse_scenario({
                "name": "mix", "floors": [0, 1], "direction": "up",
                "capacity": 2,
                "groups": [{"id": "甲", "size": 1, "origin": 0,
                            "destinations": [1]}],
                "observations": [
                    {"row_id": "a", "trip_id": "t1", "interval": [0, 0],
                     "type": "ups", "value": 1},
                    {"row_id": "b", "trip_id": "t2", "interval": [1, 1],
                     "type": "downs", "value": 1}],
            })

    def test_deterministic_ordering(self):
        scn = ex1(1, 2)
        self.assertEqual(enumerate_histories(scn),
                         enumerate_histories(scn))

    def test_sampled_subset_can_leave_more_histories(self):
        # 只入样部分计数行时，约束更弱，相容历史只可能变多。
        scn = parse_scenario({
            "name": "sub", "floors": [0, 1, 2], "direction": "up",
            "capacity": 2,
            "groups": [{"id": "甲", "size": 1, "origin": 0,
                        "destinations": [1, 2]}],
            "observations": [
                {"row_id": "c0", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 1},
                {"row_id": "c1", "trip_id": "t", "interval": [1, 1],
                 "type": "downs", "value": 1},
                {"row_id": "c2", "trip_id": "t", "interval": [2, 2],
                 "type": "downs", "value": 0}],
        })
        full = enumerate_histories(scn)
        only_ups = enumerate_histories(scn, [scn.rows[0]])
        self.assertEqual(len(full), 1)
        self.assertEqual(len(only_ups), 2)  # 甲在 1 或 2 下都相容


class HandoverExample1Test(unittest.TestCase):
    def test_base_can_pick_up_two_person_group(self):
        scn, dsp = ex1(1, 2), ex1_dispatch(1)
        hs = enumerate_histories(scn)
        for proto in (TICKET, HAIL):
            plan = plan_handover(scn, dsp, hs, proto)
            self.assertEqual(plan["score"], 1)
            if proto == TICKET:
                self.assertEqual(plan["tickets"], ["乙"])
            for rep in replay_all_worlds(scn, dsp, plan):
                self.assertEqual(rep["status"], "closed")
                self.assertEqual(rep["delivered"], ["乙"])
                self.assertIsNone(rep["missed_window"])
                # 乙上车后称重必须是 2，到站下完为 0。
                self.assertEqual([x["weighed_load"] for x in rep["log"]],
                                 [2, 0])

    def test_zero_alight_keeps_rider_cannot_pick_up_two(self):
        scn, dsp = ex1(0, 2), ex1_dispatch(0)
        hs = enumerate_histories(scn)
        self.assertEqual(certain_onboard(hs, 1), ("甲",))
        for proto in (TICKET, HAIL):
            plan = plan_handover(scn, dsp, hs, proto)
            self.assertEqual(plan["score"], 0)
            if proto == TICKET:
                self.assertEqual(plan["tickets"], [])
            rep = replay_all_worlds(scn, dsp, plan)[0]
            self.assertIn("missed:window:乙", rep["missed_window"])

    def test_capacity_one_cannot_pick_up_two(self):
        scn, dsp = ex1(1, 1), ex1_dispatch(1)
        hs = enumerate_histories(scn)
        for proto in (TICKET, HAIL):
            plan = plan_handover(scn, dsp, hs, proto)
            self.assertEqual(plan["score"], 0)
            rep = replay_all_worlds(scn, dsp, plan)[0]
            self.assertIn("missed:window:乙", rep["missed_window"])

    def test_handover_reading_without_world_is_error(self):
        scn = ex1(0, 2)
        hs = enumerate_histories(scn)
        with self.assertRaises(ValueError):
            handover_worlds(scn, hs, 1, observed_alight=1)


class IdentityBlindTest(unittest.TestCase):
    """同载重、去向不同：停车口令只依赖读数，不依赖匿名身份。"""

    def setUp(self):
        self.scn = parse_scenario({
            "name": "blind", "floors": [0, 1, 2, 3], "direction": "up",
            "capacity": 2,
            "groups": [
                {"id": "甲", "size": 1, "origin": 0,
                 "destinations": [2, 3]},
                {"id": "乙", "size": 1, "origin": 0,
                 "destinations": [2, 3]}],
            "observations": [
                {"row_id": "o0", "trip_id": "t", "interval": [0, 1],
                 "type": "ups", "value": 2},
                {"row_id": "o1", "trip_id": "t", "interval": [2, 3],
                 "type": "downs", "value": 2},
                {"row_id": "o2", "trip_id": "t", "interval": [0, 3],
                 "type": "net", "value": 0}],
        })
        self.hs = enumerate_histories(self.scn)
        self.dsp = parse_dispatch({
            "handover_floor": 1, "observed_alight": 0,
            "max_beats": 10, "reservations": []})

    def test_anonymous_worlds_erase_identity(self):
        worlds = handover_worlds(self.scn, self.hs, 1, 0)
        # 4 条身份历史折叠成 3 个匿名构型（按目的层人数区分，不含名字）。
        self.assertEqual(sorted(worlds),
                         [((2, 1), (2, 1)),
                          ((2, 1), (3, 1)),
                          ((3, 1), (3, 1))])

    def test_policy_branches_only_on_readings(self):
        plan = plan_handover(self.scn, self.dsp, self.hs, TICKET)
        # 根节点（1 层交班开门，无人下）口令对三个世界完全相同，统一前进 2 层。
        self.assertEqual(plan["floor"], 1)
        self.assertEqual(list(plan["branches"]), ["alight=0;load=2"])
        floor2 = plan["branches"]["alight=0;load=2"]
        # 到 2 层开门才按下客读数分裂；分支键只含读数，不含任何身份。
        root_keys = set(floor2["branches"])
        self.assertTrue(all(k.startswith("alight=") and ";load=" in k
                            for k in root_keys))
        self.assertEqual(root_keys,
                         {"alight=0;load=2", "alight=1;load=1",
                          "alight=2;load=0"})
        # 每个世界都能沿同一棵树关门清空。
        for rep in replay_all_worlds(self.scn, self.dsp, plan):
            self.assertEqual(rep["status"], "closed")
            self.assertEqual(rep["log"][-1]["weighed_load"], 0)

    def test_replay_unknown_reading_signals_peeking(self):
        plan = plan_handover(self.scn, self.dsp, self.hs, TICKET)
        # 手工篡改一个分支键，回放时应因“策略没准备该读数”报错，
        # 证明策略树确实是按读数而非身份驱动的。
        old_branch = plan["branches"].pop("alight=0;load=2")
        plan["branches"]["alight=9;load=9"] = old_branch
        with self.assertRaises(RuntimeError):
            replay_plan(self.scn, self.dsp, plan,
                        world=((3, 1), (3, 1)))


class DoubleBookingTest(unittest.TestCase):
    def _scn(self, downs_at_2: int) -> Scenario:
        return parse_scenario({
            "name": "ex2", "floors": [0, 1, 2, 3], "direction": "up",
            "capacity": 2,
            "groups": [{"id": "甲", "size": 1, "origin": 0,
                        "destinations": [2, 3]}],
            "observations": [
                {"row_id": "u0", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 1},
                {"row_id": "d2", "trip_id": "t", "interval": [2, 2],
                 "type": "downs", "value": downs_at_2},
                {"row_id": "d3", "trip_id": "t", "interval": [3, 3],
                 "type": "downs", "value": 1 - downs_at_2}],
        })

    def _dsp(self) -> Dispatch:
        return parse_dispatch({
            "handover_floor": 1, "observed_alight": 0, "max_beats": 10,
            "reservations": [
                {"id": "丙", "size": 1, "origin": 1,
                 "destinations": [3], "window": [0, 0]},
                {"id": "丁", "size": 2, "origin": 2,
                 "destinations": [3], "window": [2, 2]}]})

    def test_same_seat_signed_twice_makes_later_ticket_fail(self):
        # 甲在 3 层下：1 层时厢内已有 1 人，丙上车占满；2 层丁的 2 人票
        # 永远兑现不了 => 最多保证 1 组，且票不能同时开给丙和丁。
        scn, dsp = self._scn(0), self._dsp()
        hs = enumerate_histories(scn)
        plan = plan_handover(scn, dsp, hs, TICKET)
        self.assertEqual(plan["score"], 1)
        self.assertNotEqual(set(plan["tickets"]), {"丙", "丁"})
        rep = replay_all_worlds(scn, dsp, plan)[0]
        self.assertIn("丁", rep["missed_window"])

    def test_changing_alight_count_changes_promisable_group(self):
        # 把 2 层下客计数改成 1：甲在 2 层下空，丁的 2 人整组可在 2 层上，
        # 可承诺组从丙换成丁。
        scn, dsp = self._scn(1), self._dsp()
        hs = enumerate_histories(scn)
        plan = plan_handover(scn, dsp, hs, TICKET)
        self.assertEqual(plan["tickets"], ["丁"])
        self.assertEqual(plan["score"], 1)
        rep = replay_all_worlds(scn, dsp, plan)[0]
        self.assertEqual(rep["delivered"], ["丁"])
        self.assertIn("丙", rep["missed_window"])


class HailTimingTest(unittest.TestCase):
    def setUp(self):
        # 交班时厢已空（甲在 1 层下完），容量 2，两个 1 人预约同在 2 层。
        self.scn = parse_scenario({
            "name": "hail", "floors": [0, 1, 2, 3], "direction": "up",
            "capacity": 2,
            "groups": [{"id": "甲", "size": 1, "origin": 0,
                        "destinations": [1]}],
            "observations": [
                {"row_id": "u0", "trip_id": "t", "interval": [0, 0],
                 "type": "ups", "value": 1},
                {"row_id": "d1", "trip_id": "t", "interval": [1, 1],
                 "type": "downs", "value": 1}],
        })
        self.hs = enumerate_histories(self.scn)

    def _dsp(self, r_window, r_deadline) -> Dispatch:
        return parse_dispatch({
            "handover_floor": 1, "observed_alight": 1, "max_beats": 10,
            "reservations": [
                {"id": "丁", "size": 1, "origin": 2,
                 "destinations": [3], "window": [2, 2]},
                {"id": "戊", "size": 1, "origin": 2,
                 "destinations": [3], "window": r_window,
                 "deliver_by": r_deadline}]})

    def test_hail_extra_beat_misses_delivery_even_with_free_seats(self):
        dsp = self._dsp([2, 2], 4)
        plan_t = plan_handover(self.scn, dsp, self.hs, TICKET)
        plan_h = plan_handover(self.scn, dsp, self.hs, HAIL)
        # 发票规程不耗招呼拍，两组都能在 beat4 到 3 层；但戊的交付时限就是
        # beat4，而招呼把 3 层开门推到 beat5 => 规程 B 保证数降为 1。
        self.assertEqual(plan_t["score"], 2)
        self.assertEqual(plan_h["score"], 1)
        rep = replay_all_worlds(self.scn, dsp, plan_h)[0]
        hail_beats = [x["beat"] for x in rep["log"]
                      if x["hail_extra_beat"]]
        self.assertTrue(hail_beats)
        self.assertIn("戊", rep["missed_window"])

    def test_widening_window_changes_servable_groups(self):
        narrow = self._dsp([2, 2], 4)
        wide = self._dsp([2, 4], 6)
        plan_n = plan_handover(self.scn, narrow, self.hs, HAIL)
        plan_w = plan_handover(self.scn, wide, self.hs, HAIL)
        self.assertEqual(plan_n["score"], 1)
        self.assertEqual(plan_w["score"], 2)
        rep = replay_all_worlds(self.scn, wide, plan_w)[0]
        self.assertEqual(sorted(rep["delivered"]), ["丁", "戊"])
        self.assertIsNone(rep["missed_window"])


class DirectionTest(unittest.TestCase):
    def test_downward_visit_order_and_handover(self):
        scn = parse_scenario({
            "name": "down", "floors": [0, 1, 2], "direction": "down",
            "capacity": 2,
            "groups": [{"id": "甲", "size": 1, "origin": 2,
                        "destinations": [0, 1]}],
            "observations": [
                {"row_id": "u", "trip_id": "t", "interval": [2, 2],
                 "type": "ups", "value": 1},
                {"row_id": "d", "trip_id": "t", "interval": [0, 1],
                 "type": "downs", "value": 1}],
        })
        self.assertEqual(scn.visit_order, (2, 1, 0))
        hs = enumerate_histories(scn)
        self.assertEqual({h.choice["甲"] for h in hs}, {0, 1})
        dsp = parse_dispatch({
            "handover_floor": 1, "observed_alight": 0, "max_beats": 10,
            "reservations": [{"id": "乙", "size": 1, "origin": 1,
                              "destinations": [0], "window": [0, 0]}]})
        for proto in (TICKET, HAIL):
            plan = plan_handover(scn, dsp, hs, proto)
            self.assertEqual(plan["score"], 1)
            rep = replay_all_worlds(scn, dsp, plan)[0]
            floors = [x["floor"] for x in rep["log"]]
            self.assertEqual(floors, [1, 0])       # 下行访问序
            self.assertEqual(rep["delivered"], ["乙"])

    def test_destination_behind_origin_rejected(self):
        # 上行时目的层不能在候梯层之前（访问序约束）。
        scn = parse_scenario({
            "name": "behind", "floors": [0, 1, 2], "direction": "up",
            "capacity": 2,
            "groups": [{"id": "甲", "size": 1, "origin": 1,
                        "destinations": [0, 2]}],
            "observations": [
                {"row_id": "u", "trip_id": "t", "interval": [1, 1],
                 "type": "ups", "value": 1},
                {"row_id": "d", "trip_id": "t", "interval": [2, 2],
                 "type": "downs", "value": 1}],
        })
        # 甲只能选 2 层下；选 0 层会被访问序约束拒绝。
        self.assertEqual([h.choice for h in enumerate_histories(scn)],
                         [{"甲": 2}])


class DatasetIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.scn = parse_scenario({
            "name": "ds", "floors": [0, 1, 2, 3], "direction": "up",
            "capacity": 2,
            "groups": [
                {"id": "甲", "size": 1, "origin": 0,
                 "destinations": [2, 3]},
                {"id": "乙", "size": 1, "origin": 0,
                 "destinations": [2, 3]}],
            "observations": [
                {"row_id": "o0", "trip_id": "t", "interval": [0, 1],
                 "type": "ups", "value": 2, "weight": 10},
                {"row_id": "o1", "trip_id": "t", "interval": [2, 3],
                 "type": "downs", "value": 2, "weight": 90},
                {"row_id": "o2", "trip_id": "t", "interval": [0, 3],
                 "type": "net", "value": 0, "weight": 50},
                {"row_id": "o3", "trip_id": "t", "interval": [0, 3],
                 "type": "gross", "value": 4, "weight": 80}],
        })
        self.seed = bytes(range(32))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reconstruction_uses_only_sampled_rows_and_is_reproducible(self):
        svc = SourceService(os.path.join(self.dir, "s.db"))
        svc.init()
        version = svc.apply_batch(scenario_ingest_ops(self.scn)).version
        out1 = os.path.join(self.dir, "ds1")
        m1, r1 = build_dataset(svc, version, self.seed,
                               {"elevator-counts": 2}, out1)
        out2 = os.path.join(self.dir, "ds2")
        m2, r2 = build_dataset(svc, version, self.seed,
                               {"elevator-counts": 2}, out2)
        ids1 = [x["row_id"] for x in r1]
        self.assertEqual(ids1, [x["row_id"] for x in r2])  # 可重复开班
        rows = rows_from_dataset(self.scn, m1, r1)
        self.assertEqual({r.row_id for r in rows}, set(ids1))
        # 账房只拿入样行也能还原，且历史与直接用这些行枚举一致。
        hs = enumerate_histories(self.scn, rows)
        self.assertTrue(hs)
        self.assertEqual(hs, enumerate_histories(self.scn, rows))

    def test_foreign_row_rejected(self):
        svc = SourceService(os.path.join(self.dir, "s.db"))
        svc.init()
        version = svc.apply_batch(scenario_ingest_ops(self.scn)).version
        _, records = build_dataset(svc, version, self.seed,
                                   {"elevator-counts": 1},
                                   os.path.join(self.dir, "ds"))
        records = [dict(records[0], row_id="not-a-scenario-row")]
        with self.assertRaises(ValueError):
            rows_from_dataset(self.scn, {}, records)


if __name__ == "__main__":
    unittest.main()
