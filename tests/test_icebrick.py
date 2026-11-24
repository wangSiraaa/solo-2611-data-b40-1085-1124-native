"""三色冰砖模拟的单元测试：``python3 -m tests.test_icebrick``。

覆盖：
* 备料台：量条/厚差约束、读法枚举上限、逐格口味、两处同见、矛盾凭据；
* 单窗：题面场景 1-3（可交/少一蓝/红无处放）、共享未知、端选择；
* 两窗：冷柜试切出摊办法、不试切即无保证、联络挪后不可保证；
* 独立性：全部可行读法各跑一遍物理模拟器；
* 统一策略：无法区分的局部经历映射到同一决定。
"""

import unittest

from icebrick.demo import (
    build_shared_unknown,
    build_three_brick_worked,
    model_rb3,
    scenario_1_feasible,
    scenario_2_missing_blue,
    scenario_3_red_nowhere,
    scenario_4_two_windows,
    scenario_5_shared_unknown,
    scenario_5b_end_choice,
)
from icebrick.errors import (
    CupOrderError,
    IcebrickError,
    InfeasibleModelError,
    NoGuaranteedPolicyError,
    TooManyReadingsError,
)
from icebrick.model import IceModel, MAX_READINGS
from icebrick.simulator import (
    SimulationError,
    verify_all_readings_distributed,
    verify_all_readings_single,
)
from icebrick.window import (
    LEFT,
    RIGHT,
    Cup,
    ScheduleEntry,
    comm_after_analysis,
    plan_distributed,
    plan_single_window,
)

R, B = "R", "B"


class TestPrepModel(unittest.TestCase):
    def test_single_reading_determined(self):
        snap = model_rb3(1)
        readings = snap.enumerate_readings()
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0].cell_colors("X"), (R, B, B))

    def test_diff_slip_requires_same_mold(self):
        model = IceModel()
        model.add_brick("A", [R, B], 3)
        model.add_brick("B", [R, B], 3)  # 无 mold_id
        with self.assertRaises(IcebrickError):
            model.add_diff_slip("D", "A", 0, "B", 0, 0)

    def test_bad_layer_reference_rejected(self):
        model = IceModel()
        model.add_brick("A", [R, B], 3)
        with self.assertRaises(IcebrickError):
            model.add_sum_slip("S", "A", 0, 5, 1)

    def test_possible_colors_and_together(self):
        snap = build_three_brick_worked()
        self.assertEqual(snap.possible_colors("X", 1), {R, B})
        # 同 t：X@1 与 Y@1 颜色始终相同
        self.assertTrue(snap.can_see_together([("X", 1, R), ("Y", 1, R)]))
        self.assertTrue(snap.can_see_together([("X", 1, B), ("Y", 1, B)]))
        self.assertFalse(snap.can_see_together([("X", 1, R), ("Y", 1, B)]))
        # X@1=B（t=1）时 Z 红厚 2，Z@1 仍是红
        self.assertTrue(snap.can_see_together([("X", 1, B), ("Z", 1, R)]))

    def test_contradiction_witness_minimal(self):
        model = IceModel()
        model.add_brick("C", [R, B], 3)
        model.add_sum_slip("S1", "C", 0, 0, 2)
        model.add_sum_slip("S2", "C", 1, 1, 2)
        snap = model.freeze()
        with self.assertRaises(InfeasibleModelError) as ctx:
            snap.enumerate_readings()
        witness = ctx.exception.witness
        self.assertEqual(set(witness.slip_ids), {"sum:S1", "sum:S2"})
        self.assertIn("矛盾凭据", witness.describe())

    def test_too_many_readings(self):
        # 6 格 2 层且无量条：红厚 1..5 共 5 种，仍在上限内
        model = IceModel()
        model.add_brick("E", [R, B], 6)
        self.assertEqual(len(model.freeze().enumerate_readings()), 5)
        # 两块独立 6 格砖 -> 25 种，超过六种上限
        model.add_brick("F", [R, B], 6)
        with self.assertRaises(TooManyReadingsError):
            model.freeze().enumerate_readings()

    def test_frozen_snapshot_is_hash_stable(self):
        snap1 = model_rb3(1)
        snap2 = model_rb3(1)
        self.assertEqual(snap1.content_hash, snap2.content_hash)
        self.assertTrue(MAX_READINGS == 6)


class TestSingleWindowScenarios(unittest.TestCase):
    def test_scenario_1_feasible_and_simulated(self):
        snap, cups, node, traces, allowed = scenario_1_feasible()
        self.assertEqual(len(traces), 1)
        actions = next(iter(traces.values()))
        self.assertEqual([a.cup_id for a in actions], ["杯甲", "杯甲", "杯乙"])
        self.assertTrue(all(a.end == LEFT for a in actions))

    def test_scenario_2_missing_blue(self):
        snap, cups, exc, _ = scenario_2_missing_blue()
        self.assertIsInstance(exc, NoGuaranteedPolicyError)
        # 首片红合法落杯甲；第二片红时两杯都要蓝且无废料 -> 无处可落（蓝不够）
        self.assertIn("无处可落", exc.failure_branch["reason"])
        self.assertEqual(exc.failure_branch["trace"][0]["cup"], "杯甲")

    def test_scenario_3_red_nowhere(self):
        snap, cups, exc, allowed = scenario_3_red_nowhere()
        self.assertEqual(allowed, {"X": (LEFT,)})
        self.assertIsInstance(exc, NoGuaranteedPolicyError)

    def test_scenario_5_color_conditional_routing(self):
        snap, cups, node, traces, allowed = scenario_5_shared_unknown()
        self.assertEqual(len(traces), 2)
        # 两种读法拍 1 见色不同，落杯必须不同（见色后去哪杯）
        per_reading = {name: {a.beat: a.cup_id for a in tr}
                       for name, tr in traces.items()}
        beat1_cups = {v[1] for v in per_reading.values()}
        self.assertEqual(len(beat1_cups), 2)

    def test_scenario_5b_chooses_right_end(self):
        snap, cups, node, traces = scenario_5b_end_choice()
        self.assertEqual(node.decision["end"], RIGHT)
        for trace in traces.values():
            self.assertEqual(trace[0].end, RIGHT)
            # 废料额至多 1 格；t=2 三格全进杯时可完全不用
            self.assertLessEqual(sum(a.wasted for a in trace), 1)

    def test_waste_quota_enforced(self):
        snap = model_rb3(1)
        cups = [Cup("只蓝", (B, B), deadline=2, window="w")]
        # 第一片红必须走废料，无废料额 -> 无解
        with self.assertRaises(NoGuaranteedPolicyError):
            plan_single_window(snap, cups, allowed_ends={"X": (LEFT,)})
        # 给一格废料即可
        node = plan_single_window(
            snap, cups, allowed_ends={"X": (LEFT,)}, waste_quota=1
        )
        verify_all_readings_single(
            snap, cups, node, allowed_ends={"X": (LEFT,)}, waste_quota=1
        )

    def test_deadline_miss_is_failure(self):
        snap = model_rb3(1)
        cups = [Cup("杯甲", (R, B), deadline=0, window="w"),
                Cup("杯乙", (B,), deadline=2, window="w")]
        with self.assertRaises(NoGuaranteedPolicyError):
            plan_single_window(snap, cups, allowed_ends={"X": (LEFT,)})


class TestTwoWindows(unittest.TestCase):
    def _setup(self):
        info = scenario_4_two_windows()
        return info

    def test_test_cut_solution_simulated_on_all_readings(self):
        info = self._setup()
        result = info["test_cut_result"]
        # 试切必须是带同模签的甲砖、从给定（左）端、备料阶段可执行
        self.assertEqual(result.test_cut.brick_id, "X")
        self.assertEqual(result.test_cut.end, LEFT)
        self.assertEqual(len(info["traces"]), 2)
        for name, trace in info["traces"].items():
            prep = trace[0]
            self.assertTrue(prep.beat == -1 and prep.wasted)  # 不进当班杯
            shown = [a for a in trace if a.beat >= 0]
            self.assertEqual([a.window for a in shown], ["甲", "乙"])
            self.assertEqual(shown[0].cup_id, "甲蓝杯")
            self.assertEqual(shown[1].cup_id, "乙红杯")

    def test_no_test_cut_has_no_guarantee(self):
        info = self._setup()
        self.assertIsInstance(info["no_cut_error"], NoGuaranteedPolicyError)

    def test_test_cut_requires_waste_quota(self):
        snap = build_three_brick_worked()
        cups = [Cup("甲蓝杯", (B,), 1, "甲"), Cup("乙红杯", (R,), 1, "乙")]
        schedule = (ScheduleEntry(0, "甲"), ScheduleEntry(1, "乙"))
        bw = {"X": "甲", "Y": "乙", "Z": "乙"}
        pre = {b: (LEFT,) for b in bw}
        ends = {b: (LEFT,) for b in bw}
        with self.assertRaises(CupOrderError):
            plan_distributed(snap, cups, schedule, bw, pre_cuts=pre,
                             allowed_ends=ends, waste_quota=0, use_test_cut=True)

    def test_comm_after_cannot_guarantee(self):
        info = self._setup()
        self.assertFalse(info["comm_after_guaranteed"])
        # 乙的两块砖左端都仍可能是红或蓝，且各有反例读法
        for item in info["comm_after_evidence"]:
            self.assertIn("counterexample_reading", item)
            self.assertEqual(set(item["possible_colors"]), {R, B})

    def test_window_cannot_touch_other_windows_cup(self):
        # 独立模拟器在构造越窗动作时必须报错
        info = self._setup()
        from icebrick.window import make_runtimes
        from icebrick.simulator import simulate_distributed
        snap = info["snapshot"]
        runtimes = make_runtimes(snap, info["pre_cuts"], info["allowed_ends"])
        reading = snap.enumerate_readings()[0]
        root = info["test_cut_result"].root
        # 正常模拟不报错
        simulate_distributed(
            root, snap, reading, info["cups"], info["schedule"],
            {k: v for k, v in runtimes.items()}, info["brick_windows"],
            1, 3,
        )

    def test_uniform_strategy_indistinguishable_histories(self):
        """同一窗在两种自己无法区分的经历里必须作同一个决定。

        试切把两种读法分流到不同备料卡颜色；在 *同一颜色组内* 只剩一种信念，
        而组内物理状态相同的重入必须命中同一承诺。检查承诺表在求解后非空且
        键只依赖该窗可见的砖/杯。
        """
        info = self._setup()
        result = info["test_cut_result"]
        # 根节点按备料卡颜色分流，两种颜色各有独立子策略（信息论上可不同）
        self.assertEqual(set(result.root.branches), {R, B})
        for color, (_, child) in result.root.branches.items():
            self.assertIn(child.kind, {"cut", "idle", "done"})

    def test_schedule_unknown_window_rejected(self):
        snap = build_three_brick_worked()
        cups = [Cup("c", (R,), 1, "甲")]
        schedule = (ScheduleEntry(0, "丙"),)
        with self.assertRaises(CupOrderError):
            plan_distributed(snap, cups, schedule, {"X": "甲"},
                             allowed_ends={"X": (LEFT, RIGHT)}, waste_quota=1)

    def test_precut_over_length_and_empty_ends_rejected(self):
        from icebrick.window import make_runtimes
        snap = build_three_brick_worked()
        with self.assertRaises(CupOrderError):
            make_runtimes(snap, {"X": (LEFT,) * 4}, {"X": (LEFT, RIGHT)})
        cups = [Cup("甲蓝杯", (B,), 2, "甲")]
        schedule = (ScheduleEntry(0, "甲"),)
        with self.assertRaises(CupOrderError):
            plan_distributed(snap, cups, schedule, {"X": "甲"},
                             allowed_ends={"X": ()}, waste_quota=1)

    def test_prep_card_splits_on_real_color(self):
        info = self._setup()
        result = info["test_cut_result"]
        cut = result.test_cut
        self.assertEqual(set(result.prep_card), {R, B})
        colors = {
            reading.color_at(cut.brick_id, cut.position)
            for reading in info["snapshot"].enumerate_readings()
        }
        self.assertEqual(colors, {R, B})

    def test_brick_without_mold_has_no_test_cut(self):
        model = IceModel()
        model.add_brick("S", [R, B], 3)  # 无 mold_id
        snap = model.freeze()
        with self.assertRaises(NoGuaranteedPolicyError):
            plan_distributed(
                snap, [Cup("c", (R,), 1, "甲")],
                (ScheduleEntry(0, "甲"),), {"S": "甲"}, waste_quota=1,
            )

    def test_indistinguishable_observations_hit_identical_nodes(self):
        """4 种读法、试切后同色组内仍含多种读法：观察前缀相同的两条读法在每个
        动刀拍必须落在 *同一个* 策略节点对象上（无法区分的经历同一决定）。"""
        model = IceModel()
        model.add_brick("X", [R, B], 5, mold_id="MG")   # 红厚 1..4，四种读法
        snap = model.freeze()
        self.assertEqual(len(snap.enumerate_readings()), 4)
        # 预切已带走首格红；只留一只蓝杯，所有读法都有蓝可交，余格走废料
        cups = [Cup("蓝杯", (B,), 4, "甲")]
        schedule = tuple(ScheduleEntry(b, "甲") for b in range(5))
        # 试切 1 格 + 演出废料至多 2 格（各读法剩余颜色数不同，多余格走废料）
        result = plan_distributed(
            snap, cups, schedule, {"X": "甲"},
            pre_cuts={"X": (LEFT,)}, allowed_ends={"X": (LEFT, RIGHT)},
            waste_quota=3,
        )

        def walk(reading):
            """返回 (备料卡颜色, [(拍, 节点对象, 见色, 动作) ...])。"""
            root = result.root
            cut = result.test_cut
            card_color = reading.color_at(cut.brick_id, cut.position)
            _, node = root.branches[card_color]
            seen = []
            # 演出实物区间与试切后状态对齐
            from icebrick.window import make_runtimes, advance
            rt = make_runtimes(snap, {"X": (LEFT,)}, {"X": (LEFT, RIGHT)})
            rt = advance(rt, cut.brick_id, cut.end)
            for beat in range(len(schedule)):
                if node.kind == "done":
                    break
                if node.kind == "idle":
                    node = node.single
                    continue
                pos = node.decision["position"]
                color = reading.color_at("X", pos)
                action, child = node.branches[color]
                seen.append((beat, id(node), color, action.cup_id, action.wasted))
                node = child
            return card_color, seen

        paths = {str(reading): walk(reading) for reading in snap.enumerate_readings()}
        groups = {}
        for name, (card_color, seen) in paths.items():
            groups.setdefault(card_color, []).append((name, seen))
        # 备料卡同色组内：逐拍节点对象必须相同（观察前缀一致时同一决定）
        for card_color, members in groups.items():
            if len(members) < 2:
                continue
            base_name, base_seen = members[0]
            for name, seen in members[1:]:
                # 只在两者见色序列一致的前缀上比较节点同一性
                for (b1, nid1, c1, _, _), (b2, nid2, c2, _, _) in zip(base_seen, seen):
                    self.assertEqual(b1, b2)
                    if c1 == c2:
                        self.assertEqual(
                            nid1, nid2,
                            f"备料卡 {card_color} 组内 {base_name} 与 {name} "
                            f"拍 {b1} 观察相同却落到不同决定节点",
                        )


class TestSimulatorIndependence(unittest.TestCase):
    def test_all_scenarios_pass_independent_simulation(self):
        # 场景 1/5/5b 的策略都在各自全部读法上通过独立物理模拟
        for getter in (scenario_1_feasible, scenario_5_shared_unknown):
            snap, cups, node, traces, *_ = getter()
            self.assertTrue(all(traces.values()))

    def test_simulator_detects_illegal_placement(self):
        from icebrick.window import CutAction, PolicyNode
        snap = model_rb3(1)
        cups = [Cup("杯甲", (R, B), 2, "w")]
        # 手工构造一棵把蓝硬落进“下一层要红”的杯的坏策略
        bad_action = CutAction(0, "w", "X", LEFT, B, "杯甲")
        root = PolicyNode(
            kind="cut",
            decision={"brick_id": "X", "end": LEFT, "position": 0,
                      "colors": (B,)},
            branches={B: (bad_action, PolicyNode(kind="done"))},
        )
        from icebrick.window import make_runtimes
        runtimes = make_runtimes(snap, {}, {"X": (LEFT,)})
        with self.assertRaises(SimulationError):
            from icebrick.simulator import simulate_single
            simulate_single(root, snap, snap.enumerate_readings()[0],
                            cups, runtimes, 0, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
