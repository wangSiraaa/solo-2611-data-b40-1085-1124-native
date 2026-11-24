"""契约级测试：平面秤称量、联合可行称量解释与两位物主分家协议。

覆盖题目各条硬合同：

- 完整托住（贴边允许）与正面积相交（碰边不算）的区分；半搭/进出窗作废，
  只采两端看不出中途半搭，必须逐共同细分 cell 精确求解；
- 校零窗固定零点、合重整数区间、联合可行域（禁止逐盒拼合）与合重排除解释；
- 静态分家单按“每个可行解释两人各自不眼红”判定；
- 当场分家的通用分包、受托选款、保密签一次性、既有签收与同值任挑影响。
"""

import random
import unittest
from fractions import Fraction as Q
from itertools import product

from cart_audit import (
    Design,
    FeasibleWeights,
    NoWorkableTemplate,
    Profile,
    ProtocolMisuse,
    Scale,
    ScaleAuditService,
    ScaleInputError,
    ScaleWindow,
    SealRegistry,
    SplitCase,
    SplitCounter,
    SplitInputError,
    WeighingCase,
    WeighingInconsistent,
    load_motions,
    load_profile_revision,
    load_scale_windows,
    load_split_case,
)
from cart_audit.models import Knot, Trajectory

H = (Q(0), Q(10))


def traj(cart_id, knots, pid="box", horizon=H):
    return Trajectory(
        cart_id, pid, [Knot(Q(t), Q(x), Q(y)) for t, x, y in knots], horizon
    )


def scale(half_w=2, half_h=2):
    return Scale("S", Q(0), Q(0), Q(half_w), Q(half_h))


def window(wid, t0, t1, lo, hi, calibration=None):
    return ScaleWindow(wid, Q(t0), Q(t1), lo, hi, calibration)


def weighing_case(box_ids, verdicts, **kwargs):
    return WeighingCase(box_ids=tuple(box_ids), window_verdicts=tuple(verdicts), **kwargs)


# --------------------------------------------------------------------- #
# 一、称量几何合同
# --------------------------------------------------------------------- #
class ScaleGeometryTests(unittest.TestCase):
    def setUp(self):
        self.svc = ScaleAuditService()
        self.prof = Profile("box", Q(1), Q(1))
        self.s = scale()
        self.w = window("w", 0, 10, 3, 3)

    def classify(self, w, boxes):
        return self.svc.classify_window(self.s, w, boxes)

    def test_flush_edge_counts_as_contained(self):
        # 盒边与秤边贴齐（x=1）：完整托住，窗合法。
        t = traj("E", [(0, 1, 0), (10, 1, 0)])
        r = self.classify(self.w, [("E", self.prof, t)])
        self.assertEqual(r["status"], "legal")
        self.assertEqual(r["contained"], ["E"])

    def test_edge_contact_only_is_zero(self):
        # x=3 恰为正面积相交边界：只碰边不算相交，贡献零，窗合法。
        t = traj("T", [(0, 3, 0), (10, 3, 0)])
        r = self.classify(self.w, [("T", self.prof, t)])
        self.assertEqual(r["status"], "legal")
        self.assertEqual(r["zero"], ["T"])

    def test_midway_half_on_voids_window_undetectable_by_endpoints(self):
        # x 从 4 线性到 -4：两端都在秤外，途中 (5/2, 15/2) 正面积相交。
        t = traj("C", [(0, 4, 0), (10, -4, 0)])
        r = self.classify(self.w, [("C", self.prof, t)])
        self.assertEqual(r["status"], "void")
        self.assertTrue(r["bad"])
        # 两端采样都“不在秤上”，但逐 cell 证据记录了中途 mixed。
        statuses = [c["status"] for c in r["bad"][0]["cells"]]
        self.assertIn("mixed", statuses)
        # 相交开始前的真零窗仍合法。
        r0 = self.classify(window("w0", 0, 1, 0, 0), [("C", self.prof, t)])
        self.assertEqual(r0["status"], "legal")
        self.assertEqual(r0["zero"], ["C"])

    def test_box_larger_than_scale_never_contained(self):
        big = Profile("big", Q(3), Q(1))
        t = traj("B", [(0, 0, 0), (10, 0, 0)], pid="big")
        r = self.classify(self.w, [("B", big, t)])
        self.assertEqual(r["status"], "void")

    def test_mixed_boxes_void_but_preserve_contained_and_zero(self):
        edge = traj("E", [(0, 1, 0), (10, 1, 0)])
        touch = traj("T", [(0, 3, 0), (10, 3, 0)])
        cross = traj("C", [(0, 4, 0), (10, -4, 0)])
        r = self.classify(
            self.w,
            [("E", self.prof, edge), ("T", self.prof, touch), ("C", self.prof, cross)],
        )
        self.assertEqual(r["status"], "void")
        self.assertEqual(r["contained"], ["E"])
        self.assertEqual(r["zero"], ["T"])
        self.assertEqual([b["box_id"] for b in r["bad"]], ["C"])

    def test_several_boxes_contained_together(self):
        e1 = traj("E1", [(0, 1, 0), (10, 1, 0)])
        e2 = traj("E2", [(0, 0, 0), (10, 0, 0)])
        r = self.classify(self.w, [("E1", self.prof, e1), ("E2", self.prof, e2)])
        self.assertEqual(r["status"], "legal")
        self.assertEqual(set(r["contained"]), {"E1", "E2"})

    def test_window_outside_horizon_rejected(self):
        t = traj("E", [(0, 0, 0), (10, 0, 0)])
        with self.assertRaises(ScaleInputError):
            self.classify(window("wx", 9, 11, 1, 1), [("E", self.prof, t)])

    def test_duplicate_window_id_rejected(self):
        t = traj("E", [(0, 0, 0), (10, 0, 0)])
        boxes = [("E", self.prof, t)]
        with self.assertRaises(ScaleInputError):
            self.svc.audit_windows(
                self.s, [window("w", 0, 1, 0, 0), window("w", 2, 3, 0, 0)], boxes
            )


# --------------------------------------------------------------------- #
# 二、联合可行称量解释
# --------------------------------------------------------------------- #
class WeighingSolverTests(unittest.TestCase):
    CAL = {
        "window_id": "cal",
        "status": "legal",
        "contained": [],
        "zero": ["P", "Q"],
        "reading": [0, 0],
        "calibration": True,
    }

    def test_point_readings_pin_weights(self):
        verdicts = (
            self.CAL,
            {"window_id": "w1", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [6, 6], "calibration": None},
            {"window_id": "w2", "status": "legal", "contained": ["Q"], "zero": ["P"],
             "reading": [2, 2], "calibration": None},
        )
        fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
        self.assertTrue(fw.is_feasible())
        self.assertEqual(fw.projected_ranges(), {"z": (0, 0), "P": (4, 4), "Q": (2, 2)})

    def test_interval_reading_joint_domain_not_independent_box_bounds(self):
        # 单窗 P+Q ∈ [5,7]：逐盒独立界都是 1..6，但联合解只有 15 组且和受限。
        verdicts = (
            self.CAL,
            {"window_id": "w", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [5, 7], "calibration": None},
        )
        fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
        self.assertEqual(fw.projected_ranges()["P"], (1, 6))
        sols = fw.enumerate_solutions()
        self.assertEqual(len(sols), 15)
        self.assertTrue(all(5 <= s["weights"]["P"] + s["weights"]["Q"] <= 7 for s in sols))
        # 逐盒取各自上界 6+6=12 不是联合可行解。
        self.assertFalse(
            any(s["weights"] == {"P": 6, "Q": 6} for s in sols)
        )

    def test_exclusion_report_names_excluding_window(self):
        verdicts = (
            self.CAL,
            {"window_id": "w1", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [6, 6], "calibration": None},
            {"window_id": "w2", "status": "legal", "contained": ["Q"], "zero": ["P"],
             "reading": [2, 2], "calibration": None},
        )
        fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
        # (1,5)：合重 6 恰好过 W1，但单盒 5 被 W2（读数 2）排除。
        report = fw.exclusion_report({"P": 1, "Q": 5})
        self.assertFalse(report["jointly_feasible"])
        self.assertEqual(report["excluding_windows"], ["w2"])
        # (4,2) 全部通过。
        self.assertTrue(fw.exclusion_report({"P": 4, "Q": 2})["jointly_feasible"])
        explained = fw.explains("w2", {"P": 1, "Q": 5})
        self.assertFalse(explained["admitted"])
        self.assertIn("读数上界", explained["excluded_by"])

    def test_void_window_reading_ignored(self):
        verdicts = (
            self.CAL,
            {"window_id": "w1", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [6, 6], "calibration": None},
            {"window_id": "bad", "status": "void", "contained": ["P"], "zero": [],
             "reading": [99, 99], "calibration": None},
        )
        fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
        self.assertTrue(fw.is_feasible())  # 作废窗的离谱读数不参与约束

    def test_missing_zero_basis_rejected(self):
        verdicts = (
            {"window_id": "w", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [6, 6], "calibration": None},
        )
        with self.assertRaises(ScaleInputError):
            FeasibleWeights(weighing_case(["P", "Q"], verdicts))

    def test_conflicting_calibration_rejected(self):
        verdicts = (
            {"window_id": "c1", "status": "legal", "contained": [], "zero": ["P", "Q"],
             "reading": [0, 0], "calibration": True},
            {"window_id": "c2", "status": "legal", "contained": [], "zero": ["P", "Q"],
             "reading": [1, 1], "calibration": True},
        )
        with self.assertRaises(WeighingInconsistent):
            FeasibleWeights(weighing_case(["P", "Q"], verdicts))

    def test_infeasible_readings_raise_on_require(self):
        verdicts = (
            self.CAL,
            {"window_id": "w", "status": "legal", "contained": ["P", "Q"], "zero": [],
             "reading": [100, 100], "calibration": None},
        )
        fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
        self.assertFalse(fw.is_feasible())
        with self.assertRaises(WeighingInconsistent):
            fw.require_feasible()

    def test_weight_bounds_and_eight_box_limit(self):
        with self.assertRaises(ScaleInputError):
            WeighingCase(box_ids=tuple("ABCDEFGHI"), window_verdicts=())
        with self.assertRaises(ScaleInputError):
            WeighingCase(box_ids=("P",), window_verdicts=(), max_weight=13)


# --------------------------------------------------------------------- #
# 三、静态分家单
# --------------------------------------------------------------------- #
def two_box_case(w2_reading=2, beta_q=3, beta_p=1):
    verdicts = (
        {"window_id": "cal", "status": "legal", "contained": [], "zero": ["P", "Q"],
         "reading": [0, 0], "calibration": True},
        {"window_id": "w1", "status": "legal", "contained": ["P", "Q"], "zero": [],
         "reading": [6, 6], "calibration": None},
        {"window_id": "w2", "status": "legal", "contained": ["Q"], "zero": ["P"],
         "reading": [w2_reading, w2_reading], "calibration": None},
    )
    fw = FeasibleWeights(weighing_case(["P", "Q"], verdicts))
    return fw, {"P": beta_p, "Q": beta_q}


class StaticSplitTests(unittest.TestCase):
    def test_canonical_two_box_signable(self):
        fw, beta = two_box_case()
        sc = SplitCase(
            "two", ("P", "Q"), fw,
            alpha={"P": 1, "Q": 1}, beta_sheets={"b": beta},
        )
        result = sc.find_static_partition(beta)
        self.assertTrue(result["found"])
        self.assertEqual(result["partition"]["design"]["b_pack"], ["Q"])
        # 甲留 P(=4) vs 让 Q(=2)：甲余量 2；乙视角 3*2=6 vs 4：乙余量 2。
        self.assertEqual(result["partition"]["a_margin_min"], 2)
        self.assertEqual(result["partition"]["b_margin_min"], 2)

    def test_corrected_reading_one_no_partition(self):
        fw, beta = two_box_case(w2_reading=1)  # wQ=1, wP=5，双方都更看重 P
        sc = SplitCase(
            "two1", ("P", "Q"), fw,
            alpha={"P": 1, "Q": 1}, beta_sheets={"b": beta},
        )
        self.assertFalse(sc.find_static_partition(beta)["found"])

    def test_beta_equal_one_no_partition(self):
        fw, beta = two_box_case(beta_q=1)  # 乙系数全 1，4 与 2 无法两边都不眼红
        sc = SplitCase(
            "two2", ("P", "Q"), fw,
            alpha={"P": 1, "Q": 1}, beta_sheets={"b": beta},
        )
        self.assertFalse(sc.find_static_partition(beta)["found"])


# --------------------------------------------------------------------- #
# 四、当场分家协议（红白青三盒，各重 1）
# --------------------------------------------------------------------- #
def rgb_case():
    verdicts = (
        {"window_id": "cal", "status": "legal", "contained": [],
         "zero": ["R", "W", "G"], "reading": [0, 0], "calibration": True},
        {"window_id": "all", "status": "legal", "contained": ["R", "W", "G"],
         "zero": [], "reading": [3, 3], "calibration": None},
    )
    fw = FeasibleWeights(weighing_case(["R", "W", "G"], verdicts))
    return SplitCase(
        "rgb", ("R", "W", "G"), fw,
        alpha={"R": 3, "W": 2, "G": 2},
        beta_sheets={
            "b_cyan": {"R": 1, "W": 1, "G": 3},
            "b_white": {"R": 1, "W": 3, "G": 1},
        },
    )


class OnsiteProtocolTests(unittest.TestCase):
    def setUp(self):
        self.sc = rgb_case()
        self.d_cyan = next(d for d in self.sc.enumerate_designs() if d.b_pack == frozenset(["G"]))
        self.d_white = next(d for d in self.sc.enumerate_designs() if d.b_pack == frozenset(["W"]))

    def test_no_single_generic_pack_works_for_both_sheets(self):
        result = self.sc.find_generic_design()
        self.assertFalse(result["found"])
        # 各款单独只对一类乙成立。
        self.assertTrue(self.sc.works_for_sheet(self.d_cyan, "b_cyan")["works"])
        self.assertFalse(self.sc.works_for_sheet(self.d_cyan, "b_white")["works"])
        self.assertTrue(self.sc.works_for_sheet(self.d_white, "b_white")["works"])

    def test_two_templates_cover_both_sheets(self):
        result = self.sc.find_templates(limit=2)
        self.assertTrue(result["found"])
        self.assertEqual(len(result["templates"]), 2)

    def test_officer_stamps_once_without_disclosing_values(self):
        seals = SealRegistry()
        seals.issue("K1")
        seals.issue("K2")
        counter = SplitCounter(self.sc, seals)
        counter.register_templates([self.d_cyan, self.d_white])
        s1 = counter.officer_stamp("K1", "b_cyan")
        s2 = counter.officer_stamp("K2", "b_white")
        self.assertEqual(s1["design_to_pack"]["b_pack"], ["G"])
        self.assertEqual(s2["design_to_pack"]["b_pack"], ["W"])
        # 回传不含乙系数数值；同一张签不得再盖。
        with self.assertRaises(ProtocolMisuse):
            counter.officer_stamp("K1", "b_white")

    def test_borrowed_seal_from_another_counter_rejected(self):
        seals = SealRegistry()
        seals.issue("K9")
        seals.consume("K9", {"counter": "隔壁柜"})  # 隔室已用
        counter = SplitCounter(self.sc, seals)
        counter.register_templates([self.d_cyan, self.d_white])
        with self.assertRaises(ProtocolMisuse):
            counter.officer_stamp("K9", "b_cyan")

    def test_single_template_without_generic_fit_fails(self):
        seals = SealRegistry()
        seals.issue("K0")
        counter = SplitCounter(self.sc, seals)
        counter.register_templates([self.d_cyan])
        with self.assertRaises(NoWorkableTemplate):
            counter.officer_stamp("K0", "b_white")

    def test_backstage_sheet_must_not_pick_generic_pack(self):
        seals = SealRegistry()
        seals.issue("KX")
        counter = SplitCounter(self.sc, seals)
        with self.assertRaises(ProtocolMisuse):
            counter.generic_seal(self.d_cyan, actual_sheet="b_cyan")
        # 通用封包渠道不经过、也不消耗有号保密签。
        self.assertEqual(seals.status("KX")["status"], "available")

    def test_duplicate_template_rejected(self):
        seals = SealRegistry()
        seals.issue("K")
        counter = SplitCounter(self.sc, seals)
        with self.assertRaises(ProtocolMisuse):
            counter.register_templates([self.d_cyan, self.d_cyan])


# --------------------------------------------------------------------- #
# 五、整组遗赠、既有签收、同值任挑
# --------------------------------------------------------------------- #
class GroupAndFixedTests(unittest.TestCase):
    def _case(self, readings=(4,), fixed=None, groups=(("A", "B"),)):
        boxes = ("A", "B", "C", "D")
        verdicts = [
            {"window_id": "cal", "status": "legal", "contained": [], "zero": list(boxes),
             "reading": [0, 0], "calibration": True},
            {"window_id": "all", "status": "legal", "contained": list(boxes), "zero": [],
             "reading": [readings[0], readings[0]], "calibration": None},
        ]
        fw = FeasibleWeights(weighing_case(list(boxes), verdicts))
        return SplitCase(
            "grp", boxes, fw,
            alpha={k: 1 for k in boxes},
            beta_sheets={"b": {k: 1 for k in boxes}},
            groups=[list(g) for g in groups],
            fixed=fixed,
        )

    def test_group_kept_together_and_fixed_owner_forced(self):
        sc = self._case(fixed={"A": "A"})
        for d in sc.enumerate_designs():
            self.assertNotIn("A", d.b_pack)
            self.assertNotIn("B", d.b_pack)  # 整组随 A 固定给甲
        with self.assertRaises(SplitInputError):
            sc.validate_design(Design("split", frozenset(["B"])))
        with self.assertRaises(SplitInputError):
            sc.validate_design(Design("moveA", frozenset(["A"])))

    def test_conflicting_fixed_inside_group_rejected(self):
        with self.assertRaises(SplitInputError):
            self._case(fixed={"A": "A", "B": "B"})

    def test_tie_breaks_fixed_receipt_when_b_may_pick_freely(self):
        # 四盒等重、系数全 1 => 任一半分包在同值世界 g=0，乙可任挑。
        sc = self._case(fixed={"A": "A"})
        design = Design("half", frozenset(["C", "D"]))
        impact = sc.tie_impact(design, "b")
        self.assertTrue(impact["tie_possible"])
        affected = {item["box_id"]: item["broken_when_b_takes"]
                    for item in impact["affected_fixed_boxes"]}
        # A 固定给甲且在甲包：乙只有挑走甲包（a_pack）才会破坏该签收。
        self.assertEqual(affected.get("A"), "a_pack")
        # 再验固定给乙、且落在乙包的盒：乙挑走乙包即破坏。
        sc2 = self._case(fixed={"C": "B"})
        design2 = Design("withC", frozenset(["C", "D"]))
        impact2 = sc2.tie_impact(design2, "b")
        affected2 = {item["box_id"]: item["broken_when_b_takes"]
                     for item in impact2["affected_fixed_boxes"]}
        self.assertEqual(affected2.get("C"), "b_pack")

    def test_no_tie_when_values_always_differ(self):
        fw, beta = two_box_case()  # wP=4,wQ=2，乙系数 (1,3) => 6 vs 4，不会同值
        sc = SplitCase("fx", ("P", "Q"), fw,
                       alpha={"P": 1, "Q": 1}, beta_sheets={"b": beta},
                       fixed={"Q": "B"})
        impact = sc.tie_impact(Design("g", frozenset(["Q"])), "b")
        self.assertFalse(impact["tie_possible"])
        self.assertEqual(impact["affected_fixed_boxes"], [])


# --------------------------------------------------------------------- #
# 六、materials/729 端到端载入
# --------------------------------------------------------------------- #
class Materials729Tests(unittest.TestCase):
    def test_load_and_full_pipeline(self):
        from pathlib import Path

        m = Path(__file__).resolve().parent.parent / "materials" / "729"
        _, profiles = load_profile_revision(m / "profiles.json")
        _, trajectories = load_motions(m / "motions.json")
        scale_obj, windows = load_scale_windows(m / "scale_windows.json")
        prof = profiles[0]
        boxes = [(t.cart_id, prof, t) for t in trajectories]
        verdicts = ScaleAuditService().audit_windows(scale_obj, windows, boxes)
        by_id = {v["window_id"]: v for v in verdicts}
        self.assertEqual(by_id["W_cal"]["status"], "legal")
        self.assertEqual(by_id["W1"]["status"], "legal")
        self.assertEqual(set(by_id["W1"]["contained"]), {"J", "Y"})
        self.assertEqual(by_id["W2"]["status"], "legal")
        self.assertEqual(by_id["W2"]["contained"], ["Y"])
        self.assertEqual(by_id["W_void"]["status"], "void")  # H 中途半搭
        fw = FeasibleWeights(
            weighing_case([t.cart_id for t in trajectories], verdicts)
        )
        self.assertEqual(fw.projected_ranges()["J"], (4, 4))
        self.assertEqual(fw.projected_ranges()["Y"], (2, 2))
        sc = load_split_case(
            m / "split.json", [t.cart_id for t in trajectories], fw
        )
        result = sc.find_static_partition(sc.beta_sheets["b1"])
        self.assertTrue(result["found"])


# --------------------------------------------------------------------- #
# 七、随机化暴力交叉验证（求解器、静态单、当场承包判定）
# --------------------------------------------------------------------- #
class BruteForceCrossCheckTests(unittest.TestCase):
    def test_solver_matches_brute_enumeration(self):
        rng = random.Random(729)

        def brute(ids, windows, z_hi):
            sols = set()
            for vals in product(range(1, 13), repeat=len(ids)):
                for z in range(z_hi + 1):
                    if all(
                        wnd["reading"][0]
                        <= z + sum(w for w, b in zip(vals, ids) if b in wnd["contained"])
                        <= wnd["reading"][1]
                        for wnd in windows
                    ):
                        sols.add((z,) + vals)
            return sols

        for _ in range(60):
            n = rng.randint(1, 4)
            ids = tuple(f"B{i}" for i in range(n))
            z_hi = rng.randint(0, 2)
            windows = [
                {"window_id": "cal", "status": "legal", "contained": [], "zero": list(ids),
                 "reading": [0, z_hi], "calibration": True}
            ]
            for k in range(rng.randint(1, 3)):
                contained = [b for b in ids if rng.random() < 0.6] or [ids[0]]
                target = rng.randint(n, 3 * n + z_hi + 1)
                spread = rng.choice([0, 0, 1, 2])
                windows.append(
                    {"window_id": f"w{k}", "status": "legal", "contained": contained,
                     "zero": [b for b in ids if b not in contained],
                     "reading": [target - spread, target + spread], "calibration": None}
                )
            fw = FeasibleWeights(weighing_case(list(ids), windows))
            got = {
                (s["z"],) + tuple(s["weights"][b] for b in ids)
                for s in fw.enumerate_solutions()
            }
            self.assertEqual(got, brute(ids, windows[1:], z_hi))

    def test_static_split_matches_brute(self):
        rng = random.Random(2026)
        for _ in range(40):
            n = rng.randint(2, 4)
            ids = tuple(f"X{i}" for i in range(n))
            windows = [
                {"window_id": "cal", "status": "legal", "contained": [], "zero": list(ids),
                 "reading": [0, 0], "calibration": True},
                {"window_id": "all", "status": "legal", "contained": list(ids), "zero": [],
                 "reading": [n, 8 * n], "calibration": None},
            ]
            fw = FeasibleWeights(weighing_case(list(ids), windows))
            worlds = [s["weights"] for s in fw.enumerate_solutions()]
            alpha = {b: rng.randint(1, 4) for b in ids}
            beta = {b: rng.randint(1, 4) for b in ids}
            groups = [[ids[0], ids[1]]] if n >= 3 and rng.random() < 0.5 else []
            fixed = {rng.choice(ids): rng.choice(["A", "B"])} if rng.random() < 0.4 else {}
            sc = SplitCase("s", ids, fw, alpha=alpha,
                           beta_sheets={"b": beta}, groups=groups, fixed=fixed)
            group_sets = [frozenset(g) for g in groups]
            brute_signable = {}
            for d in sc.enumerate_designs():
                ok = True
                for w in worlds:
                    va_keep = sum(alpha[x] * w[x] for x in ids if x not in d.b_pack)
                    va_give = sum(alpha[x] * w[x] for x in ids if x in d.b_pack)
                    vb_take = sum(beta[x] * w[x] for x in ids if x in d.b_pack)
                    vb_left = sum(beta[x] * w[x] for x in ids if x not in d.b_pack)
                    if not (va_keep >= va_give and vb_take >= vb_left):
                        ok = False
                        break
                brute_signable[d.b_pack] = ok
            for d in sc.enumerate_designs():
                self.assertEqual(
                    sc.static_signable(d, beta)["signable"], brute_signable[d.b_pack]
                )
            self.assertEqual(
                sc.find_static_partition(beta)["found"],
                any(brute_signable.values()),
            )
            # 组约束确实被枚举遵守
            for d in sc.enumerate_designs():
                for g in group_sets:
                    self.assertFalse(bool(d.b_pack & g) and not g <= d.b_pack)

    def test_onsite_works_matches_brute(self):
        rng = random.Random(11)
        for _ in range(50):
            n = rng.randint(2, 4)
            ids = tuple(f"X{i}" for i in range(n))
            windows = [
                {"window_id": "cal", "status": "legal", "contained": [], "zero": list(ids),
                 "reading": [0, 0], "calibration": True},
                {"window_id": "all", "status": "legal", "contained": list(ids), "zero": [],
                 "reading": [n, 6 * n], "calibration": None},
            ]
            fw = FeasibleWeights(weighing_case(list(ids), windows))
            worlds = [s["weights"] for s in fw.enumerate_solutions()]
            alpha = {b: rng.randint(0, 3) for b in ids}
            beta = {b: rng.randint(0, 3) for b in ids}
            alpha[ids[0]] = max(1, alpha[ids[0]])
            beta[ids[0]] = max(1, beta[ids[0]])
            fixed = {rng.choice(ids): rng.choice(["A", "B"])} if rng.random() < 0.4 else {}
            sc = SplitCase("o", ids, fw, alpha=alpha,
                           beta_sheets={"b": beta}, fixed=fixed)
            for d in sc.enumerate_designs():
                expect = True
                for w in worlds:
                    g = sum((beta[x] if x in d.b_pack else -beta[x]) * w[x] for x in ids)
                    margin = sum((-alpha[x] if x in d.b_pack else alpha[x]) * w[x] for x in ids)
                    if g > 0 and margin < 0:
                        expect = False
                    if g < 0 and margin > 0:
                        expect = False
                    if g == 0 and margin != 0:
                        expect = False
                self.assertEqual(sc.works_for_sheet(d, "b")["works"], expect)


if __name__ == "__main__":
    unittest.main()