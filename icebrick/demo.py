"""题面全部示例的可跑杯单与核验（``python3 -m icebrick.demo``）。

场景索引：

* 场景 1-3：一砖红蓝共三格的三个单窗杯单（可交 / 少一蓝 / 红无处放）；
* 场景 4：三砖两窗示例，采用 **冷柜试切** 出摊办法，并显式核验
  “联络挪到乙下刀后即不可保证”以及“不试切也不可保证”；
* 场景 5：两砖共享未知厚度，同时考验从哪端先切与见色后去哪只杯；
* 附：备料台查询（可能口味、两处同见、矛盾凭据）。

规模限制遵守题面：至多三砖四杯，每砖不超过六格。
"""

from .errors import InfeasibleModelError, NoGuaranteedPolicyError
from .model import IceModel
from .simulator import (
    format_trace,
    verify_all_readings_distributed,
    verify_all_readings_single,
)
from .window import (
    LEFT,
    RIGHT,
    Cup,
    ScheduleEntry,
    comm_after_analysis,
    plan_distributed,
    plan_single_window,
)

R, B = "R", "B"


# ============================================================ 备料台小模型
def model_rb3(red_thickness):
    """一砖从左到右红、蓝，两层共三格；量条定红层厚度。"""
    model = IceModel()
    model.add_brick("X", [R, B], 3)
    model.add_sum_slip("S_red", "X", 0, 0, red_thickness)
    return model.freeze(label=f"红蓝三格，红={red_thickness}")


def build_shared_unknown():
    """用同模厚差量条精确钉出 t∈{1,2} 的两读法模型。

    P=R^t B^(3-t)（只从左切），Q=R^(3-t) B^t（只从右切）。
    量条：蓝P=红Q、红P=蓝Q；配合两砖各自总长 3，可行整数读法恰为 t=1 与 t=2。
    """
    model = IceModel()
    model.add_brick("P", [R, B], 3, mold_id="M1")
    model.add_brick("Q", [R, B], 3, mold_id="M1")
    model.add_diff_slip("D_blueP_redQ", "P", 1, "Q", 0, 0)   # 蓝P = 红Q
    model.add_diff_slip("D_redP_blueQ", "P", 0, "Q", 1, 0)   # 红P = 蓝Q
    snap = model.freeze(label="两砖同模 t∈{1,2}")
    readings = snap.enumerate_readings()
    assert len(readings) == 2, f"预期两种读法，实际 {len(readings)}"
    return snap


def build_three_brick_worked():
    """题面三砖：甲砖 X 与乙砖 Y 红层同厚 t；乙砖 Z 红层 3-t，t∈{1,2}。

    量条：红X=红Y（同模厚差 0）；红Z=蓝Y，而 Y 总长 3 即 红Y+蓝Y=3，
    故红Z=3-红Y=3-t。可行整数读法恰为 t=1 与 t=2。
    """
    model = IceModel()
    model.add_brick("X", [R, B], 3, mold_id="MT")   # 甲窗
    model.add_brick("Y", [R, B], 3, mold_id="MT")   # 乙窗
    model.add_brick("Z", [R, B], 3, mold_id="MT")   # 乙窗
    model.add_diff_slip("D_XY_red", "X", 0, "Y", 0, 0)      # 红X = 红Y = t
    model.add_diff_slip("D_Zred_Yblue", "Z", 0, "Y", 1, 0)  # 红Z = 蓝Y = 3-t
    snap = model.freeze(label="三砖示例 t∈{1,2}")
    readings = snap.enumerate_readings()
    assert len(readings) == 2, f"预期两种读法，实际 {len(readings)}"
    return snap


# ============================================================ 场景 1-3：单窗
def scenario_1_feasible():
    snap = model_rb3(1)
    cups = [
        Cup("杯甲", (R, B), deadline=2, window="单窗"),
        Cup("杯乙", (B,), deadline=2, window="单窗"),
    ]
    allowed_ends = {"X": (LEFT,)}   # 只准从左切、无废料
    node = plan_single_window(snap, cups, allowed_ends=allowed_ends)
    traces = verify_all_readings_single(
        snap, cups, node, allowed_ends=allowed_ends
    )
    return snap, cups, node, traces, allowed_ends


def scenario_2_missing_blue():
    snap = model_rb3(2)  # 红二蓝一
    cups = [
        Cup("杯甲", (R, B), deadline=2, window="单窗"),
        Cup("杯乙", (B,), deadline=2, window="单窗"),
    ]
    allowed_ends = {"X": (LEFT,)}
    try:
        plan_single_window(snap, cups, allowed_ends=allowed_ends)
    except NoGuaranteedPolicyError as exc:
        return snap, cups, exc, allowed_ends
    raise AssertionError("场景 2 应当无保证策略")


def scenario_3_red_nowhere():
    snap = model_rb3(1)
    cups = [
        Cup("杯甲", (B, R), deadline=2, window="单窗"),  # 蓝开头
        Cup("杯乙", (B,), deadline=2, window="单窗"),
    ]
    # 沿用场景 1 的“只准从左切、无废料”：第一片必然是红，两杯都先要蓝 -> 无处放
    allowed_ends = {"X": (LEFT,)}
    try:
        plan_single_window(snap, cups, allowed_ends=allowed_ends)
    except NoGuaranteedPolicyError as exc:
        return snap, cups, exc, allowed_ends
    raise AssertionError("场景 3 应当无保证策略")


# ============================================================ 场景 5：两砖共享未知
def scenario_5_shared_unknown():
    """两砖同模 t∈{1,2}：P 只从左切、Q 只从右切，固定切序，见色决定去哪只杯。

    两砖共 6 格且无废料，四只杯容量恰为 6；颜色在第几拍出现取决于 t，策略必须
    按 *实际看见的颜色* 分流到对应杯，才能在两种读法下都准点交齐。截止班按两种
    读法里该颜色最晚出现的拍配置。
    """
    snap = build_shared_unknown()
    cups = [
        Cup("红杯1", (R,), deadline=3, window="单窗"),
        Cup("红杯2", (R,), deadline=4, window="单窗"),
        Cup("蓝杯1", (B,), deadline=4, window="单窗"),
        Cup("蓝杯2", (B,), deadline=5, window="单窗"),
    ]
    allowed_ends = {"P": (LEFT,), "Q": (RIGHT,)}
    node = plan_single_window(snap, cups, allowed_ends=allowed_ends)
    traces = verify_all_readings_single(
        snap, cups, node, allowed_ends=allowed_ends
    )
    return snap, cups, node, traces, allowed_ends


def scenario_5b_end_choice():
    """两端皆可时规划器自行决定先切哪一端（从哪端先切的例子）。

    砖 E=R^t B^(3-t)，t∈{1,2}，两端都可切：左端首格恒红、右端首格恒蓝。急蓝杯
    拍 0 就要蓝，左端首格在两种读法下都是红，左先切拿不到蓝；规划器必须自行
    选择 *先从右端切蓝*，再从左端取红。留一格废料额备用，两种读法下实际只切两格
    即可交齐两只杯、无需动用废料。
    """
    model = IceModel()
    model.add_brick("E", [R, B], 3)
    snap = model.freeze(label="端切选择：红厚未知 t∈{1,2}")
    cups = [
        Cup("急蓝杯", (B,), deadline=0, window="单窗"),
        Cup("红杯", (R,), deadline=2, window="单窗"),
    ]
    node = plan_single_window(snap, cups, waste_quota=1)  # 两端均允许
    traces = verify_all_readings_single(
        snap, cups, node, waste_quota=1
    )
    assert node.decision["brick_id"] == "E" and node.decision["end"] == RIGHT, node.decision
    return snap, cups, node, traces


# ============================================================ 场景 4：三砖两窗
def scenario_4_two_windows():
    snap = build_three_brick_worked()
    # 三砖此前各已依法切走第一格红（左端预切一格）
    pre_cuts = {"X": (LEFT,), "Y": (LEFT,), "Z": (LEFT,)}
    allowed_ends = {
        "X": (LEFT,),       # 甲：先从左再切一刀
        "Y": (LEFT,),       # 乙：只许两砖各左切一次
        "Z": (LEFT,),
    }
    cups = [
        Cup("甲蓝杯", (B,), deadline=1, window="甲"),
        Cup("乙红杯", (R,), deadline=1, window="乙"),
    ]
    schedule = (
        ScheduleEntry(0, "甲"),
        ScheduleEntry(1, "乙"),
    )
    brick_windows = {"X": "甲", "Y": "乙", "Z": "乙"}

    # ---- 4a. 不领冷柜时段：保留全部演出砖格、无废料额，应当无保证
    no_cut_error = None
    try:
        plan_distributed(
            snap, cups, schedule, brick_windows,
            pre_cuts=pre_cuts, allowed_ends=allowed_ends,
            waste_quota=0, use_test_cut=False,
        )
    except NoGuaranteedPolicyError as exc:
        no_cut_error = exc

    # ---- 4b. 冷柜试切（唯一实现的出摊办法）：甲砖备料再试取一格，留一格废额
    result = plan_distributed(
        snap, cups, schedule, brick_windows,
        pre_cuts=pre_cuts, allowed_ends=allowed_ends,
        waste_quota=1, use_test_cut=True,
    )
    traces = verify_all_readings_distributed(
        snap, cups, schedule, brick_windows, result.root,
        pre_cuts=pre_cuts, allowed_ends=allowed_ends, waste_quota=1,
    )

    # ---- 4c. 联络拍核验：机会从乙下刀前挪到切完后，乙无法保证杯底红
    guaranteed, evidence = comm_after_analysis(
        snap, cups, schedule, brick_windows,
        pre_cuts=pre_cuts, allowed_ends=allowed_ends,
    )
    return {
        "snapshot": snap,
        "cups": cups,
        "schedule": schedule,
        "pre_cuts": pre_cuts,
        "allowed_ends": allowed_ends,
        "brick_windows": brick_windows,
        "no_cut_error": no_cut_error,
        "test_cut_result": result,
        "traces": traces,
        "comm_after_guaranteed": guaranteed,
        "comm_after_evidence": evidence,
    }


# ============================================================ 备料台查询演示
def demo_prep_queries():
    snap = build_three_brick_worked()
    out = []
    out.append(snap.describe_readings())
    out.append(f"X 第 1 格（预切后余下首格，原格号 1）可能口味："
               f"{sorted(snap.possible_colors('X', 1))}")
    out.append(f"X@1=R 与 Y@1=R 能否同见："
               f"{snap.can_see_together([('X', 1, R), ('Y', 1, R)])}")
    out.append(f"X@1=B 与 Z@1=R 能否同见："
               f"{snap.can_see_together([('X', 1, B), ('Z', 1, R)])}")
    return "\n".join(out)


def demo_contradiction():
    model = IceModel()
    model.add_brick("C", [R, B], 3)
    model.add_sum_slip("S1", "C", 0, 0, 2)   # 红 2
    model.add_sum_slip("S2", "C", 1, 1, 2)   # 蓝 2 -> 和为 4 ≠ 3
    snap = model.freeze(label="矛盾模型 红2+蓝2≠3")
    try:
        snap.enumerate_readings()
    except InfeasibleModelError as exc:
        return exc.witness.describe()
    raise AssertionError("矛盾模型应当抛出 InfeasibleModelError")


# ============================================================ 主入口
def main():
    print("=" * 70)
    print("场景 1：红蓝三格、红=1；杯甲自底红蓝、杯乙只要蓝 —— 三拍可交")
    snap, cups, node, traces, _ = scenario_1_feasible()
    print(snap.describe_readings())
    for name, trace in traces.items():
        print(f"  {name}")
        print(format_trace(trace))

    print("=" * 70)
    print("场景 2：只改量条为红二格 —— 必少一蓝（无保证策略，附失约分支）")
    snap, cups, exc, _ = scenario_2_missing_blue()
    print(snap.describe_readings())
    print(f"  失约凭据：{exc.failure_branch['reason']}")
    for step in exc.failure_branch["trace"]:
        print(f"    {step}")

    print("=" * 70)
    print("场景 3：甲单改蓝红、两杯都蓝开头 —— 第一片红无处放")
    snap, cups, exc, _ = scenario_3_red_nowhere()
    print(f"  失约凭据：{exc.failure_branch['reason']}")
    for step in exc.failure_branch["trace"]:
        print(f"    {step}")

    print("=" * 70)
    print("场景 4：三砖两窗 —— 选用冷柜试切出摊办法")
    info = scenario_4_two_windows()
    result = info["test_cut_result"]
    cut = result.test_cut
    print(f"  试切：{cut.brick_id} 从{cut.end}端取原格号 {cut.position}（备料阶段，耗 1 格废料额）")
    print(f"  备料卡分流：{ {c: len(v) for c, v in result.prep_card.items()} }")
    assert info["no_cut_error"] is not None, "不试切应当无保证"
    print(f"  不试切核验：{info['no_cut_error'].failure_branch['reason']}")
    assert info["comm_after_guaranteed"] is False
    print("  联络挪后核验：乙无法保证杯底红")
    for e in info["comm_after_evidence"]:
        print(f"    {e}")
    for name, trace in info["traces"].items():
        print(f"  {name}")
        print(format_trace(trace))

    print("=" * 70)
    print("场景 5：两砖共享未知厚度 —— 固定端切顺序 + 见色落杯")
    snap, cups, node, traces, allowed_ends = scenario_5_shared_unknown()
    print(snap.describe_readings())
    print(f"  允许端：{allowed_ends}")
    for name, trace in traces.items():
        print(f"  {name}")
        print(format_trace(trace))

    print("=" * 70)
    print("场景 5b：两端皆可时规划器自选先切端")
    snap, cups, node, traces = scenario_5b_end_choice()
    print(snap.describe_readings())
    print(f"  首刀决定：{node.decision}")
    for name, trace in traces.items():
        print(f"  {name}")
        print(format_trace(trace))

    print("=" * 70)
    print("附 A：备料台查询")
    print(demo_prep_queries())
    print("=" * 70)
    print("附 B：矛盾凭据")
    print(demo_contradiction())


if __name__ == "__main__":
    main()
