"""策略树的独立模拟核验与文本化。

模拟器不参与搜索，只按策略树在 *指定真实读法* 下逐拍执行，并逐条复算物理规则：
每拍至多一刀、当拍落杯或废料、杯的下一层颜色匹配、截止班、两窗轮班与杯归属、
不回退。搜索器给出的保证因此有一条独立核验路径。
"""

from dataclasses import replace


class SimulationError(AssertionError):
    """策略树在某读法下违反物理规则（若出现即规划器 bug）。"""


def _advance_runtime(rt, brick_id, end):
    runtime = rt[brick_id]
    if end == "L":
        rt[brick_id] = replace(runtime, left_pos=runtime.left_pos + 1)
    else:
        rt[brick_id] = replace(runtime, right_pos=runtime.right_pos - 1)


def _place(action, rt, cups_left, waste):
    if action.wasted:
        if waste <= 0:
            raise SimulationError("废料额不足")
        waste -= 1
    else:
        seq = cups_left.get(action.cup_id)
        if seq is None:
            raise SimulationError(f"动作落向不存在的杯 {action.cup_id}")
        if not seq or seq[0] != action.color:
            raise SimulationError(
                f"杯 {action.cup_id} 下一层需要 {seq[:1]}，却落入 {action.color}"
            )
        cups_left[action.cup_id] = seq[1:]
    _advance_runtime(rt, action.brick_id, action.end)
    return waste


def simulate_single(node, snapshot, reading, cups, runtimes, waste_quota, horizon):
    """单窗策略在一份具体读法上的执行轨迹。"""
    rt = {k: v for k, v in runtimes.items()}
    cups_left = {c.cup_id: list(c.bottom_up) for c in cups}
    waste = waste_quota
    trace = []
    for beat in range(horizon + 1):
        if node.kind == "done" or all(not v for v in cups_left.values()):
            break
        if node.kind == "idle":
            node = node.single
            continue
        if node.kind != "cut":
            raise SimulationError(f"拍 {beat} 节点类型非法：{node.kind}")
        brick_id, end, position = (
            node.decision["brick_id"], node.decision["end"], node.decision["position"]
        )
        runtime = rt[brick_id]
        if not runtime.alive() or end not in runtime.allowed_ends:
            raise SimulationError(f"拍 {beat} 从不可用端切 {brick_id} {end}")
        if position != runtime.end_position(end):
            raise SimulationError(f"拍 {beat} 格位与实物区间不一致")
        color = reading.color_at(brick_id, position)
        if color not in node.branches:
            raise SimulationError(f"策略缺少见色 {color} 的分支")
        action, child = node.branches[color]
        if action.color != color or action.brick_id != brick_id:
            raise SimulationError("策略分支动作与观察不一致")
        waste = _place(action, rt, cups_left, waste)
        trace.append(action)
        node = child
    overdue = [cid for cid, seq in cups_left.items() if seq]
    if overdue:
        raise SimulationError(f"模拟结束仍有杯未交齐：{overdue}")
    return trace


def simulate_distributed(root, snapshot, reading, cups, schedule, runtimes,
                         brick_windows, waste_quota, horizon, test_cut_color=None):
    """两窗策略（含试切根节点）在一份具体读法上的执行轨迹。"""
    rt = {k: v for k, v in runtimes.items()}
    cups_left = {c.cup_id: list(c.bottom_up) for c in cups}
    waste = waste_quota
    trace = []
    node = root
    if node.kind == "test_cut":
        cut = node.decision["cut"]
        if test_cut_color is None:
            test_cut_color = reading.color_at(cut.brick_id, cut.position)
        if waste <= 0:
            raise SimulationError("试切片无废料额可接")
        if test_cut_color not in node.branches:
            raise SimulationError("备料卡缺少该试切颜色的分流策略")
        prep_action, node = node.branches[test_cut_color]
        waste -= 1  # 试切耗格、不进当班杯
        _advance_runtime(rt, cut.brick_id, cut.end)
        trace.append(prep_action)
    for beat in range(min(horizon + 1, len(schedule))):
        if node.kind == "done" or all(not v for v in cups_left.values()):
            break
        window = schedule[beat].window
        if window is None:
            continue
        if node.kind == "idle":
            node = node.single
            continue
        if node.kind != "cut":
            raise SimulationError(f"拍 {beat} 节点类型非法：{node.kind}")
        brick_id = node.decision["brick_id"]
        end, position = node.decision["end"], node.decision["position"]
        if brick_windows[brick_id] != window:
            raise SimulationError(f"拍 {beat} {window}窗动了别窗的砖 {brick_id}")
        runtime = rt[brick_id]
        if not runtime.alive() or end not in runtime.allowed_ends:
            raise SimulationError(f"拍 {beat} 端不可用")
        if position != runtime.end_position(end):
            raise SimulationError("格位与实物区间不一致")
        color = reading.color_at(brick_id, position)
        if color not in node.branches:
            raise SimulationError(f"策略缺少见色 {color} 的分支")
        action, child = node.branches[color]
        if action.window != window:
            raise SimulationError("动作窗口与轮班表不符")
        if not action.wasted:
            cup = next(c for c in cups if c.cup_id == action.cup_id)
            if cup.window != window:
                raise SimulationError(f"{window}窗把片落进对窗杯 {cup.cup_id}")
        waste = _place(action, rt, cups_left, waste)
        trace.append(action)
        node = child
    overdue = [cid for cid, seq in cups_left.items() if seq]
    if overdue:
        raise SimulationError(f"两窗模拟结束仍有杯未交齐：{overdue}")
    return trace


def verify_all_readings_single(snapshot, cups, node, *, pre_cuts=None,
                               allowed_ends=None, waste_quota=0, horizon=None):
    """对单窗策略的全部可行读法各跑一遍独立模拟。"""
    from .window import make_runtimes
    runtimes = make_runtimes(snapshot, pre_cuts or {}, allowed_ends or {})
    horizon = horizon or max(c.deadline for c in cups) + 1
    return {
        str(reading): simulate_single(
            node, snapshot, reading, cups,
            {k: v for k, v in runtimes.items()}, waste_quota, horizon,
        )
        for reading in snapshot.enumerate_readings()
    }


def verify_all_readings_distributed(snapshot, cups, schedule, brick_windows, root,
                                    *, pre_cuts=None, allowed_ends=None,
                                    waste_quota=0, horizon=None):
    """对两窗策略的全部可行读法各跑一遍独立模拟（试切颜色按真实读法得出）。"""
    from .window import make_runtimes
    runtimes = make_runtimes(snapshot, pre_cuts or {}, allowed_ends or {})
    horizon = horizon or max(
        [c.deadline for c in cups] + [len(schedule) - 1]
    ) + 1
    return {
        str(reading): simulate_distributed(
            root, snapshot, reading, cups, schedule,
            {k: v for k, v in runtimes.items()}, brick_windows,
            waste_quota, horizon,
        )
        for reading in snapshot.enumerate_readings()
    }


def format_trace(trace):
    lines = []
    for action in trace:
        if action.beat < 0:
            lines.append(
                f"  [冷柜] {action.brick_id} {action.end}端试切 -> {action.color}"
                f"（废料，不进杯）"
            )
        elif action.wasted:
            lines.append(
                f"  拍{action.beat} {action.window}: {action.brick_id} {action.end}端"
                f" -> {action.color}（废料）"
            )
        else:
            lines.append(
                f"  拍{action.beat} {action.window}: {action.brick_id} {action.end}端"
                f" -> {action.color} 落杯 {action.cup_id}"
            )
    return "\n".join(lines)
