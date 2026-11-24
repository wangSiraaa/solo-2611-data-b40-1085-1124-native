"""齿轮教具 / 演示台 / 测速盘端到端测试（题面逐条断言）。

运行：python3 -m tests.test_gear_demo（仓库根目录）。
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.service import BudgetService
from gear_demo import scenarios as S
from gear_demo.bench import (
    BenchShaft,
    BenchSpec,
    SegmentPlan,
    ShiftRequest,
    Sleeve,
    common_action_table,
    find_lockable_beats,
    simulate,
)
from gear_demo.kinematics import (
    GaugeError,
    KinematicSolver,
    Mesh,
    NoLegalInterpretation,
    Observation,
    Segment,
    EXTERNAL,
)
from gear_demo.tacho import (
    DUAL_MEASURE,
    PAUSE_FIXTURE,
    TachoSwap,
)

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="gear-test-")
        self.service = BudgetService(self.tmp)
        self.kin = KinematicSolver(self.service)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp)


# ================================================================ 测具岗
def test_differential_balanced_locks():
    """两侧 1、−1，差动座对静止轴：座速 0 = 静止端，第一拍末唯一窗口可合。"""
    with Env() as env:
        analysis = env.kin.analyze(S.differential_balanced())
        check(analysis.consistent, "平衡差动：传动链一致")
        check(analysis.speed("C") == 0, "平衡差动：座速 = (1 + (−1))/2 = 0")
        check(analysis.speed("C") == analysis.speed("S"),
              "平衡差动：座与静止端同速")
        same, note, _ = analysis.must_same_speed("C", "S")
        check(same, "平衡差动：必同速（C≡S）")
        check(env.service.verify_solve(analysis.solve_id)["ok"],
              "平衡差动报告独立验证通过")


def test_differential_same_sign_fails():
    """仅把负向输入改为 +1：座速 1，与静止端 0 不同速，合不上。"""
    with Env() as env:
        analysis = env.kin.analyze(S.differential_same_sign())
        check(analysis.consistent, "同号差动：传动链本身仍一致")
        check(analysis.speed("C") == 1, "同号差动：座速 = (1 + 1)/2 = 1")
        same, _, _ = analysis.must_same_speed("C", "S")
        check(not same, "同号差动：座与静止端不必同速（C≠S），合不上")


def test_differential_free_requires_opposite():
    """轴速自由（不钉读数）时：座速与两端不必然同速，需给反例读数。"""
    with Env() as env:
        analysis = env.kin.analyze(S.differential_inputs_free())
        same, note, witness = analysis.must_same_speed("L", "R")
        check(not same, "自由差动：L 与 R 不必同速")
        check(witness is not None and witness["equal"] and witness["unequal"],
              "自由差动：给出支持相反答复的两份读数")
        check(witness["equal"]["L"] == witness["equal"]["R"],
              "自由差动反例：同速读数成立")
        check(witness["unequal"]["L"] != witness["unequal"]["R"],
              "自由差动反例：不同速读数成立")
        # 独立核对：两份读数都必须满足差动方程 2C = L + R
        for key in ("equal", "unequal"):
            w = witness[key]
            check(2 * w["C"] == w["L"] + w["R"],
                  f"自由差动读数（{key}）满足差动方程")


def test_differential_bench_integration():
    """测具岗与演示台贯通：座速结论直接决定座/静止端能否在首拍锁合。"""
    # 平衡：座速 0 = 静止端，初相位同 0，首拍末即可合
    balanced = S.bench_differential_balanced()
    rb = simulate(balanced)
    check(rb.ok and rb.action_table["req_cs"]["beat"] == 0,
          "差动演示：平衡态首拍即锁合 C 与 S")
    # 同号：座速 1 ≠ 静止端 0，任何拍都合不上
    same_sign = S.bench_differential_same_sign()
    rs = simulate(same_sign)
    check(not rs.ok and any(v.code == "missed_window" for v in rs.violations),
          "差动演示：同号时座与静止端永远不同速，错过窗口")
    # 差一齿：转速相同（座速 0）但静止端初位差 1 刻，四刻一圈永远不同位
    off = S.bench_differential_one_tooth()
    ro = simulate(off)
    check(not ro.ok and find_lockable_beats(off, "sCS", list(range(6))) == [],
          "差动演示：座速虽为 0，静止端差一齿仍永不同位")


def test_one_tooth_off_forever():
    """静止端初位差一齿（1 刻），四刻一圈：永远不同位（纯相位情形）。"""
    spec = S.bench_phase_off_by_tooth()
    result = simulate(spec)
    check(not result.ok, "差一齿：窗口内永远合不上")
    check(any(v.code == "missed_window" for v in result.violations),
          "差一齿：报出错过窗口")
    check(find_lockable_beats(spec, "sAB", list(range(6))) == [],
          "差一齿：无任何可锁合拍")


def test_two_gears_fight_one_sleeve_kinematics():
    """两套轮比值不同争同一同速要求：In 非零时 A、B 永不同速。"""
    with Env() as env:
        analysis = env.kin.analyze(S.two_gears_fight_one_sleeve())
        check(analysis.speed("A") == Fraction(-1, 2), "争抢：A = −In/2 = −1/2")
        check(analysis.speed("B") == Fraction(-1, 3), "争抢：B = −In/3 = −1/3")
        same, _, _ = analysis.must_same_speed("A", "B")
        check(not same, "争抢：A、B 不必同速，抢不上同一同速要求")


def test_internal_external_mesh_chain():
    """内啮合同向 + 外啮合反向的混合链。"""
    with Env() as env:
        analysis = env.kin.analyze(S.internal_mesh_pair())
        # A=1; B = +(12/24)·1 = 1/2（内啮合同向）; C = -(8/16)·B = -1/4（外啮合反向）
        check(analysis.speed("B") == Fraction(1, 2), "内啮合：B = +A/2")
        check(analysis.speed("C") == Fraction(-1, 4), "外啮合：C = −B/2 = −1/4")


def test_conflicting_chain_is_contradiction():
    """互相矛盾的传动链（同一轴被两读数强制两个值）→ 无解。

    段内有两根轴（含一条啮合），故也覆盖“不一致路径下的探针/配对接口”。
    """
    with Env() as env:
        seg = Segment(
            name="contra",
            shafts=["X", "Y"],
            meshes=[Mesh("X", "Y", 1, 2, EXTERNAL)],
            observations=[Observation("X", Fraction(1)), Observation("X", Fraction(2))],
        )
        with_analysis = env.kin.analyze(seg)
        check(not with_analysis.consistent, "矛盾链：解算判为无解")
        same, note, witness = with_analysis.must_same_speed("X", "Y")
        check(not same and "矛盾" in note, "矛盾链：必同速一问如实回答为无解释")
        try:
            env.kin.legal_readings(seg)
            raise AssertionError("矛盾链必须抛 NoLegalInterpretation")
        except NoLegalInterpretation:
            PASS.append("矛盾链：legal_readings 抛 NoLegalInterpretation")


def test_allowed_speeds_and_legal_interpretation():
    """有限允许转速：整段共用合法解释；无解释时报 NoLegalInterpretation。"""
    with Env() as env:
        seg = Segment(
            name="allow",
            shafts=["In", "A"],
            meshes=[Mesh("In", "A", 1, 2, EXTERNAL)],
            allowed_speeds={"In": [Fraction(1), Fraction(2)]},
        )
        readings = env.kin.legal_readings(seg)
        check(len(readings) == 2, "允许转速：两组合法解释")
        for r in readings:
            check(r["assignment"]["A"] == -r["assignment"]["In"] / 2,
                  "允许转速：每份解释满足啮合关系")
        # 无解组合
        bad = Segment(
            name="allow_bad",
            shafts=["In", "A"],
            meshes=[Mesh("In", "A", 1, 2, EXTERNAL)],
            observations=[Observation("In", Fraction(1))],
            allowed_speeds={"In": [Fraction(3)]},
        )
        try:
            env.kin.legal_readings(bad)
            raise AssertionError("冲突读数+允许转速应无解释")
        except NoLegalInterpretation:
            PASS.append("允许转速：读数与允许集冲突时无合法解释")


# ================================================================ 演示台
def test_bench_basic_successful_shift():
    spec = S.bench_basic()
    result = simulate(spec)
    check(result.ok, "演示台：先合后脱再合，动作表全部兑现")
    check(result.action_table["req_ab"]["beat"] == 0, "演示台：sAB 首拍锁合")
    check(result.action_table["req_bc"]["beat"] == 4, "演示台：sBC 在段 1 窗口锁合")
    check(result.engaged_at_end("sBC"), "演示台：末态 sBC 锁合")


def test_bench_missing_disengage_violates():
    spec = S.bench_basic_no_disengage()
    result = simulate(spec)
    check(not result.ok, "演示台：缺脱开在异速段判违规")
    check(any(v.code == "engaged_speed_mismatch" for v in result.violations),
          "演示台：违规代码指出已合却异速")


def test_bench_disengage_must_precede_speed_change():
    """“须先脱开”：在异速段之前的拍脱开则不违规；拖到异速当拍才脱开已太晚。"""
    def build(off_beat):
        return BenchSpec(
            shafts=[BenchShaft("A", 0), BenchShaft("B", 0)],
            sleeves=[Sleeve("s", "seat", "A", "B")],
            segments=[
                SegmentPlan("seg0", {"A": 1, "B": 1}, beats=2),
                SegmentPlan("seg1", {"A": 1, "B": 2}, beats=2),
            ],
            requests=[
                ShiftRequest("e", "s", "engage", [0]),
                ShiftRequest("d", "s", "disengage", [off_beat]),
            ],
            ticks_per_rev=4,
        )
    early = simulate(build(1))   # 段边界前脱开
    late = simulate(build(2))    # 异速段当拍才脱开
    check(early.ok, "先脱开：段边界前脱开不违规")
    check(not late.ok and any(v.code == "engaged_speed_mismatch"
                              for v in late.violations),
          "先脱开：拖到异速当拍才脱开判违规")


def test_bench_two_sleeves_shared_seat():
    spec = S.bench_two_sleeves_one_seat()
    result = simulate(spec)
    check(result.ok, "双套抢座：先脱开再抢座，成功")
    check(result.seats["shared"] == "sPR", "双套抢座：末态滑座归 sPR")
    clash = S.bench_two_sleeves_one_seat_clash()
    result2 = simulate(clash)
    check(not result2.ok, "双套抢座缺脱开：判违规")


def test_bench_never_resets_marker():
    """换挡不可趁机动重置任何轴的标记：相位只由累加转速决定。"""
    spec = S.bench_basic()
    result = simulate(spec)
    # 轴 A 段0(3拍,1)+段1(2拍,1) => 5 mod 4 = 1；中途换挡不得重置
    check(result.final_phases["A"] == (0 + 5) % 4,
          "演示台：换挡不复位 A 的相位（0+5 ≡ 1）")
    # 轴 B：段0 3拍×1 + 段1 2拍×2 = 3+4=7 ≡ 3
    check(result.final_phases["B"] == (0 + 3 + 4) % 4,
          "演示台：换挡不复位 B 的相位（0+7 ≡ 3）")


def test_bench_common_action_table():
    """共同动作表：必须在所有尚可成立的读数下兑现。"""
    spec = S.bench_two_sleeves_one_seat()
    reading = {"seg0": {"P": 1, "Q": 1, "R": 1}, "seg1": {"P": 1, "Q": 0, "R": 1}}
    verdict = common_action_table(spec, [reading])
    check(verdict["ok"], "共同动作表：单读数下兑现")
    # 第二个读数让 Q 在段0不同速 → 无法共用同一动作拍
    bad_reading = {"seg0": {"P": 1, "Q": 2, "R": 1}}
    verdict2 = common_action_table(spec, [reading, bad_reading])
    check(not verdict2["ok"], "共同动作表：读数冲突时不成立")


def test_bench_integer_ticks_only():
    spec = S.bench_basic()
    spec.segments[0].speeds["A"] = Fraction(1, 2)
    try:
        simulate(spec)
        raise AssertionError("分数刻/拍必须被拒绝")
    except GaugeError:
        PASS.append("演示台：拒绝分数刻/拍（只采用整数刻）")


def test_bench_phase_wraps_mod_cycle():
    """相位按整数刻度绕回：四刻一圈严格取模。"""
    spec = BenchSpec(
        shafts=[BenchShaft("A", 0), BenchShaft("B", 0)],
        sleeves=[Sleeve("sAB", "seat", "A", "B", phase_mod=4)],
        segments=[SegmentPlan("seg0", {"A": 3, "B": 3}, beats=4)],
        requests=[ShiftRequest("req", "sAB", "engage", [0, 1, 2, 3])],
        ticks_per_rev=4,
    )
    result = simulate(spec)
    check(all(0 <= p < 4 for p in result.final_phases.values()),
          "演示台：相位始终落在 [0, ticks) 内")
    check(result.final_phases["A"] == (3 * 4) % 4, "演示台：4 拍×3 刻绕回为 0")


# ================================================================ 测速盘
def test_tacho_no_evidence_refuses():
    """没有交接证据、两显示同为 0 仍不足以放行。"""
    swap = TachoSwap(ticks_per_rev=4)
    spec = S.bench_basic()
    result = simulate(spec)
    # 两种零位误差都让某真实齿位显示 0
    z0_phase = (0 - 0) % 4
    z2_phase = (0 - 2) % 4
    check(z0_phase != z2_phase, "双显示 0 对应两种不同真实齿位")
    report = swap.plan(
        PAUSE_FIXTURE, swap_beat=2, trace=result.trace,
        shift_windows=[[0, 1], [4]], calibration=None,
    )
    check(not report.released, "无证据：拒绝放行")
    check("歧义" in report.reason, "无证据：说明零位误差 0/2 歧义")


def test_tacho_pause_fixture_calibrates():
    swap = TachoSwap(ticks_per_rev=4)
    spec = S.bench_basic()
    result = simulate(spec)
    true_phase = result.trace[2]["phases"]["A"]
    evidence = swap.make_evidence_pause_fixture(
        beat=2, true_phase=true_phase, orientation=1
    )
    report = swap.plan(
        PAUSE_FIXTURE, swap_beat=2, trace=result.trace,
        shift_windows=[[0, 1], [4]], calibration=evidence,
    )
    check(report.released, "停台工装：补校准后放行")
    check(report.pause_beats == [2], "停台工装：占一拍")
    check(report.calibration.meets_true_teeth, "停台工装：证据来自真齿定位")


def test_tacho_dual_measure_calibrates():
    swap = TachoSwap(ticks_per_rev=4)
    spec = S.bench_basic()
    result = simulate(spec)
    true_phase = result.trace[1]["phases"]["A"]
    old = S.old_disc()
    old_display = old.display(true_phase, 4)
    new = S.new_disc_shifted()  # 零位错 2 刻
    new_display = new.display(true_phase, 4)
    evidence = swap.make_evidence_dual_measure(
        beat=1, true_phase=true_phase, old_disc=old, old_display=old_display,
        new_display=new_display, orientation=new.orientation,
    )
    check(evidence.zero_shift == 2, "双盘联测：反解出新盘零位误差 2")
    report = swap.plan(
        DUAL_MEASURE, swap_beat=1, trace=result.trace,
        shift_windows=[[0, 1], [4]], calibration=evidence,
    )
    check(report.released, "双盘联测：补校准后放行")
    check(report.pause_beats == [], "双盘联测：不停台")
    # 联测位当拍不能再承接同拍其他校准
    report2 = swap.plan(
        DUAL_MEASURE, swap_beat=1, trace=result.trace,
        shift_windows=[[0, 1], [4]], calibration=evidence,
        other_calibration_beats=[1],
    )
    check(not report2.released, "双盘联测：联测拍被独占，不能再承接同拍其他校准")
    check(report2.misses, "双盘联测：记录联测拍冲突")


def test_tacho_seats_preserved():
    """交接前后既有滑座保持真实锁合。"""
    swap = TachoSwap(ticks_per_rev=4)
    result = simulate(S.bench_basic())
    true_phase = result.trace[2]["phases"]["A"]
    evidence = swap.make_evidence_pause_fixture(2, true_phase, 1)
    report = swap.plan(
        PAUSE_FIXTURE, swap_beat=2, trace=result.trace,
        shift_windows=[[0, 1], [4]], calibration=evidence,
        engaged_seats_before={"seat1": "sAB"}, engaged_seats_after={"seat1": "sAB"},
    )
    check(report.seats_preserved, "测速盘：交接前后滑座真实锁合保持一致")


def test_tacho_pause_beat_unusable_miss():
    """停台占一拍：若唯一换挡窗恰是停台换盘拍，该拍无走齿无锁合，错过演示。"""
    swap = TachoSwap(ticks_per_rev=4)
    # 4 拍演示，换盘停台在拍 2，唯一窗只覆盖拍 2
    spec = BenchSpec(
        shafts=[BenchShaft("A", 0), BenchShaft("B", 0)],
        sleeves=[Sleeve("s", "seat", "A", "B")],
        segments=[SegmentPlan("seg0", {"A": 1, "B": 1}, beats=4)],
        requests=[],
        ticks_per_rev=4,
    )
    result = simulate(spec)
    evidence = swap.make_evidence_pause_fixture(2, 0, 1)
    report = swap.plan(
        PAUSE_FIXTURE, swap_beat=2, trace=result.trace,
        shift_windows=[[2]], calibration=evidence,
    )
    check(not report.released, "停台：唯一窗落在停台拍，不能放行")
    check(report.misses, "停台：记录错过原因")
    notes = swap.explain_miss(report, result.trace)
    check(any("停台" in n for n in notes), "停台：解释何时错过演示")


def test_tacho_pause_before_calibration_untrusted_miss():
    """换盘后、校准完成前的换挡窗读数不可信 → 错过。"""
    swap = TachoSwap(ticks_per_rev=4)
    spec = S.bench_basic()
    result = simulate(spec)
    # 换盘在拍 1，但工装校准到拍 3 才完成；窗 [2] 落在期间
    evidence = swap.make_evidence_pause_fixture(3, 1, 1)
    report = swap.plan(
        PAUSE_FIXTURE, swap_beat=1, trace=result.trace,
        shift_windows=[[2]], calibration=evidence,
    )
    check(not report.released, "停台：校准前窗读数不可信，不能放行")


def test_tacho_dual_measure_bad_old_reading():
    """双盘联测：旧盘读数与标定不符则不能作为证据。"""
    swap = TachoSwap(ticks_per_rev=4)
    old = S.old_disc()
    try:
        swap.make_evidence_dual_measure(
            beat=1, true_phase=1, old_disc=old, old_display=3,
            new_display=1, orientation=1,
        )
        raise AssertionError("旧盘读数不自洽必须拒绝")
    except GaugeError:
        PASS.append("双盘联测：旧盘不自洽读数被拒绝")


def run_all():
    tests = [
        test_differential_balanced_locks,
        test_differential_same_sign_fails,
        test_differential_free_requires_opposite,
        test_differential_bench_integration,
        test_one_tooth_off_forever,
        test_two_gears_fight_one_sleeve_kinematics,
        test_internal_external_mesh_chain,
        test_conflicting_chain_is_contradiction,
        test_allowed_speeds_and_legal_interpretation,
        test_bench_basic_successful_shift,
        test_bench_missing_disengage_violates,
        test_bench_disengage_must_precede_speed_change,
        test_bench_two_sleeves_shared_seat,
        test_bench_never_resets_marker,
        test_bench_common_action_table,
        test_bench_integer_ticks_only,
        test_bench_phase_wraps_mod_cycle,
        test_tacho_no_evidence_refuses,
        test_tacho_pause_fixture_calibrates,
        test_tacho_dual_measure_calibrates,
        test_tacho_seats_preserved,
        test_tacho_pause_beat_unusable_miss,
        test_tacho_pause_before_calibration_untrusted_miss,
        test_tacho_dual_measure_bad_old_reading,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n齿轮演示全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
