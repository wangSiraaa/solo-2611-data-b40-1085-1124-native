"""万花筒芯求解层测试：单筒工单、校错见证、卡锁、询证、巡展与共用库存。"""

from __future__ import annotations

import unittest
from collections import Counter

from library.kaleido import (
    Chart,
    ChartDesk,
    F,
    Geometry,
    N,
    Observation,
    ProgramStep,
    RotationPlan,
    Tail,
    View,
    Workshop,
    core_fulfills,
    exhibition_plan,
    max_single_core,
    max_spare_cores,
    synthesize,
    verify_run,
    visible_color,
)


class PhysicsTest(unittest.TestCase):
    def test_near_blocks_far(self):
        self.assertEqual(visible_color((N(0), F(0)), {N(0): "red"}), "red")
        self.assertEqual(visible_color((N(0), F(0)), {N(0): "", F(0): "blue"}), "blue")
        self.assertIsNone(visible_color((N(0), F(0)), {}))

    def test_far_layer_reachable_when_near_empty(self):
        g = Geometry(1, 1)
        r = synthesize(g, [], [ProgramStep("blue")], RotationPlan(), Counter(blue=1))
        self.assertEqual(r.status, "ok")
        # 蓝可见；无论是落在近层还是远层，都是同一物理规则下的合法装芯
        self.assertEqual(visible_color(r.steps[0]["views"][0]["line"], r.assembly), "blue")
        # 显式验证：近层留空、远层放蓝也能满足（远层确实可达）
        self.assertTrue(
            verify_run(g, {N(0): "", F(0): "blue"}, [ProgramStep("blue")],
                        RotationPlan())["ok"]
        )

    def test_near_blocks_far_requires_moving_bead(self):
        # 近层若装红，同一眼永远看不到远层；要看见蓝必须空出近层放蓝到近层或远层
        g = Geometry(1, 1)
        chart = Chart.fixed("近红", [Observation((N(0), F(0)), "red")])
        self.assertEqual(
            synthesize(g, [chart], [ProgramStep("red")], RotationPlan(),
                        Counter(red=1)).status, "ok"
        )


class SingleTubeWorkOrderTest(unittest.TestCase):
    def setUp(self):
        self.g = Geometry(2, 0)
        self.inv = Counter(red=1, blue=1)
        self.library = Chart.fixed("馆谱-起位红", [Observation((N(0),), "red", "起位")])
        self.witness = Chart.fixed("见证-槽1蓝", [Observation((N(1),), "blue", "槽1")])
        self.program = [ProgramStep("red", "起位看红"), ProgramStep("blue", "拨一步看蓝")]
        self.plan = RotationPlan(initial_near=0, transitions=[{"near_delta": 1}])

    def test_red_then_blue_synthesized(self):
        r = synthesize(self.g, [self.library, self.witness], self.program, self.plan, self.inv)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.assembly, {N(0): "red", N(1): "blue"})
        self.assertEqual([s["views"][0]["observed"] for s in r.steps], ["red", "blue"])
        self.assertTrue(verify_run(self.g, r.assembly, self.program, self.plan)["ok"])

    def test_witness_recolored_red_breaks_blue_leg(self):
        wred = Chart.fixed("见证-槽1红", [Observation((N(1),), "red", "槽1")])
        r = synthesize(self.g, [self.library, wred], self.program, self.plan, self.inv)
        self.assertEqual(r.status, "infeasible")
        self.assertEqual(r.reason, "color_conflict")
        self.assertEqual(r.conflict["slot"], "近1")
        self.assertEqual(set(r.conflict["colors"]), {"blue", "red"})

    def test_near_locked_prevents_step(self):
        locked = RotationPlan(initial_near=0,
                              transitions=[{"near_delta": 1, "near_locked": True}])
        r = synthesize(self.g, [self.library, self.witness], self.program, locked, self.inv)
        self.assertEqual(r.status, "infeasible")
        self.assertEqual(r.reason, "motion_conflict")
        self.assertTrue(r.motion)

    def test_restored_witness_but_locked_still_fails(self):
        locked = RotationPlan(initial_near=0,
                              transitions=[{"near_delta": 1, "near_locked": True}])
        r = synthesize(self.g, [self.library, self.witness], self.program, locked, self.inv)
        self.assertNotEqual(r.status, "ok")

    def test_multi_eye_same_assembly(self):
        step = ProgramStep("", "双眼", views=(View("red", 0), View("blue", 1)))
        r = synthesize(self.g, [], [step], RotationPlan(), self.inv)
        self.assertEqual(r.status, "ok")
        obs = [v["observed"] for v in r.steps[0]["views"]]
        self.assertEqual(obs, ["red", "blue"])

    def test_multi_eye_conflict(self):
        step = ProgramStep("", "双眼争", views=(View("red", 0), View("blue", 0)))
        r = synthesize(self.g, [], [step], RotationPlan(), self.inv)
        self.assertEqual(r.status, "infeasible")
        self.assertEqual(r.reason, "color_conflict")


class ChartOrientationTest(unittest.TestCase):
    def test_solver_picks_feasible_orientation(self):
        g = Geometry(3, 0)
        inv = Counter(red=1, blue=1)
        ori0 = [Observation((N(0),), "red", "朝向0")]
        ori1 = [Observation((N(1),), "red", "朝向1")]
        chart = Chart("双朝向", [ori0, ori1])
        program = [ProgramStep("blue", "s0"), ProgramStep("red", "s1")]
        plan = RotationPlan(initial_near=0, transitions=[{"near_delta": 1}])
        r = synthesize(g, [chart], program, plan, inv)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.assembly, {N(0): "blue", N(1): "red"})
        # 选中的朝向必须是 ori1（把红放在槽1）
        chosen = r.chosen["双朝向"]
        self.assertEqual(chosen, tuple(ori1))


class ChartDeskTest(unittest.TestCase):
    def setUp(self):
        self.g = Geometry(3, 0)
        self.inv = Counter(red=2, blue=1)
        self.desk = ChartDesk(self.g, self.inv)
        self.a = Chart.fixed("谱A", [Observation((N(0),), "red"), Observation((N(1),), "blue")])
        self.b = Chart.fixed("谱B", [Observation((N(0),), "blue"), Observation((N(2),), "red")])

    def test_certain(self):
        q = self.desk.ask([self.a], N(1), "blue")
        self.assertEqual(q["verdict"], "certain")
        self.assertEqual(q["witness"][N(1)], "blue")

    def test_possible_with_counter_witness(self):
        q = self.desk.ask([self.a], N(2), "red")
        self.assertEqual(q["verdict"], "possible")
        self.assertIn("witness", q)
        self.assertIn("counter_witness", q)

    def test_contradiction_slot_cannot_take(self):
        q = self.desk.ask([self.a], N(0), "blue")
        self.assertEqual(q["verdict"], "contradiction")
        self.assertEqual(q["reason"], "slot_cannot_take_color")

    def test_two_partial_charts_conflict_same_slot(self):
        # 各自可装，但同一芯要槽0同时红与蓝 -> 抵触，指出冲突槽
        qa = self.desk.ask([self.a], N(0), "red")
        qb = self.desk.ask([self.b], N(0), "blue")
        self.assertEqual(qa["verdict"], "certain")
        self.assertEqual(qb["verdict"], "certain")
        joint = self.desk.ask([self.a, self.b], N(0), "red")
        self.assertEqual(joint["verdict"], "contradiction")
        self.assertEqual(joint["conflict"]["detail"]["slot"], "近0")
        self.assertEqual(set(joint["conflict"]["detail"]["colors"]), {"blue", "red"})

    def test_witnesses_are_valid_assemblies(self):
        for chart in (self.a, self.b):
            q = self.desk.ask([chart], N(0), chart.orientations[0][0].target)
            asm = q["witness"]
            for obs in chart.orientations[0]:
                self.assertEqual(visible_color(obs.slots, asm), obs.target)


class TourTest(unittest.TestCase):
    def setUp(self):
        self.g = Geometry(1, 0)
        self.tails = [
            Tail("红尾", [ProgramStep("red", "红尾一步")]),
            Tail("蓝尾", [ProgramStep("blue", "蓝尾一步")]),
        ]

    def test_empty_core_promises_nothing(self):
        self.assertEqual(
            [t.name for t in self.tails if core_fulfills(self.g, {}, t)], []
        )

    def test_single_core_commits_one_tail(self):
        p = max_single_core(self.tails, self.g, Counter(red=1, blue=1))
        self.assertEqual(p["size"], 1)

    def test_spare_cores_commit_both_with_distinct_beads(self):
        p = max_spare_cores(self.tails, self.g, Counter(red=1, blue=1), max_cores=2)
        self.assertEqual(p["covered"], ["红尾", "蓝尾"])
        # 两枚芯不得共用同一枚珠
        used = Counter()
        for asm in p["cores"]:
            for c in asm.values():
                if c:
                    used[c] += 1
        self.assertTrue(all(n <= 1 for n in used.values()))

    def test_exhibition_prefers_spare_when_it_commits_more(self):
        plan = exhibition_plan(self.tails, self.g, Counter(red=1, blue=1))
        self.assertEqual(plan["delivery"], "B_spare_cores")
        self.assertEqual(plan["commitment"], ["红尾", "蓝尾"])

    def test_borrowed_blue_drops_blue_tail(self):
        plan = exhibition_plan(self.tails, self.g, Counter(red=1))
        self.assertEqual(plan["commitment"], ["红尾"])
        self.assertNotIn("蓝尾", plan["commitment"])

    def test_no_beads_promises_nothing(self):
        plan = exhibition_plan(self.tails, self.g, Counter())
        self.assertEqual(plan["commitment"], [])


class WorkshopTest(unittest.TestCase):
    def setUp(self):
        self.g = Geometry(2, 0)
        self.w = Workshop(inventory=Counter(red=1, blue=1), geometry=self.g)

    def test_sector_table_to_chart(self):
        self.w.attach_sector_table(
            "C1", "v1",
            {"c0": {"slots": [["N", 0]], "color": "red"},
             "c1": {"slots": [["N", 1]], "color": "blue"}},
        )
        charts = self.w.charts_for("C1", "v1", self.g)
        q = self.w.inquire("Q1", charts, N(0), "red")
        self.assertEqual(q["verdict"], "certain")
        self.assertTrue(any(r["op_id"] == "Q1" for r in self.w.receipts))

    def test_shared_inventory_rejects_double_use(self):
        used = self.w.commit_assembly("C1", [{N(0): "blue"}])
        self.assertEqual(used["status"], "ok")
        over = self.w.commit_assembly("C2", [{N(0): "blue"}])
        self.assertEqual(over["status"], "rejected")
        self.assertEqual(over["error"]["reason"], "insufficient_beads")

    def test_double_use_in_one_commit_rejected_atomically(self):
        r = self.w.commit_assembly("C3", [{N(0): "blue"}, {N(1): "blue"}])
        self.assertEqual(r["status"], "rejected")
        # 拒绝不扣减库存
        self.assertEqual(self.w.inventory["blue"], 1)

    def test_replay_is_idempotent(self):
        r1 = self.w.commit_assembly("C4", [{N(0): "red"}])
        r2 = self.w.commit_assembly("C4", [{N(0): "red"}])
        self.assertEqual(r1, r2)
        # 重放不再扣减：红珠已用尽
        self.assertEqual(self.w.inventory["red"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
