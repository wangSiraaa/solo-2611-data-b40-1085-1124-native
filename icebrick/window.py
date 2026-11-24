"""窗口出摊规划：单窗信念规划器与甲乙两窗分布规划器。

物理规则（两种规划器共用）：

* 每拍全场至多切一刀，揭开的颜色在切下当拍才看见；
* 当拍必须把这片颜色落进一只 *尚缺该颜色作下一层* 的杯，或计入一格废料额；
* 不许先在台上存着；已切的左右端、已装杯实物都不得回退；
* 杯有交付班（deadline beat），到拍未交齐即失约。

策略树 :class:`PolicyNode` 的节点只描述“本拍决定”，观察边携带当拍动作：
``branches[颜色] = (动作, 子节点)``；因此同一信念/同一局部经历只会落到同一个
节点、作同一个决定（无法区分的经历同一决定）。

两窗规划器 :class:`TwoWindowPlanner` 只实现一种出摊办法——**开卖前领取一条额外
冷柜时段做试切**：选一块带同模签的实砖、从给定端试切一格，真实颜色印在两窗备料
卡上；该刀耗一格、不能进当班杯（必须有废料额接住），演出时不能还原此格、也不能
要求该窗照旧再切重取证据。临场联络拍方案不在实现之列；:func:`comm_after_analysis`
仅对题面示例显式核验“联络挪到切完后即不可保证”。
"""

from dataclasses import dataclass, field

from .errors import CupOrderError, NoGuaranteedPolicyError

LEFT = "L"
RIGHT = "R"


# --------------------------------------------------------------------------- 实物
@dataclass(frozen=True)
class Cup:
    """一只杯：自底向上的颜色序列、交付班与所属窗口。"""

    cup_id: str
    bottom_up: tuple[str, ...]
    deadline: int
    window: str


@dataclass(frozen=True)
class BrickRuntime:
    """演出中的一条砖：剩余格位是原砖格号区间 [left_pos, right_pos]。"""

    brick_id: str
    left_pos: int
    right_pos: int
    allowed_ends: frozenset[str]

    def alive(self):
        return self.left_pos <= self.right_pos

    def end_position(self, end):
        if end == LEFT:
            return self.left_pos
        if end == RIGHT:
            return self.right_pos
        raise CupOrderError(f"未知端 {end}")


@dataclass(frozen=True)
class ScheduleEntry:
    """轮班表一拍：指定由哪个窗口动刀（None 表示该拍全场空拍）。"""

    beat: int
    window: str | None


@dataclass(frozen=True)
class CutAction:
    beat: int
    window: str
    brick_id: str
    end: str
    color: str                      # 该分支上实际看见的颜色
    cup_id: str | None              # None 表示计入废料
    wasted: bool = False


@dataclass
class PolicyNode:
    """统一策略树节点。

    kind:
      * ``"cut"``  —— decision 记砖/端/位，branches: 颜色 -> (CutAction, 子节点)；
      * ``"idle"`` —— 本拍空拍，single 指向后继；
      * ``"done"`` —— 全部杯已交齐；
      * ``"test_cut"`` —— 两窗冷柜试切根，branches: 实见颜色 -> (试切动作, 子节点)。
    """

    kind: str
    decision: dict = field(default_factory=dict)
    branches: dict = field(default_factory=dict)   # 颜色 -> (CutAction, PolicyNode)
    single: "PolicyNode | None" = None
    note: str = ""

    def walk(self, prefix=()):
        if self.kind == "done":
            return
        if self.kind == "idle":
            yield from self.single.walk(prefix)
            return
        for color, (action, child) in sorted(self.branches.items()):
            yield prefix + (color,), action
            yield from child.walk(prefix + (color,))

    def used_bricks(self):
        return {action.brick_id for _, action in self.walk()}


# --------------------------------------------------------------------------- 辅助
def make_runtimes(snapshot, pre_cuts, allowed_ends):
    """pre_cuts: 依法已切端序列（如 ("L",)）；allowed_ends: 每砖允许取的端。"""
    runtimes = {}
    for brick in snapshot.bricks:
        left, right = 0, brick.total_length - 1
        for end in pre_cuts.get(brick.brick_id, ()):
            if end == LEFT:
                left += 1
            elif end == RIGHT:
                right -= 1
            else:
                raise CupOrderError(f"砖 {brick.brick_id} 预切端非法：{end}")
        if left > right + 1:
            raise CupOrderError(f"砖 {brick.brick_id} 预切格数超过总长")
        ends = frozenset(allowed_ends.get(brick.brick_id, (LEFT, RIGHT)))
        if not ends or not ends <= {LEFT, RIGHT}:
            raise CupOrderError(f"砖 {brick.brick_id} 可切端配置非法")
        runtimes[brick.brick_id] = BrickRuntime(brick.brick_id, left, right, ends)
    return runtimes


def make_cup_state(cups):
    state = {}
    for cup in cups:
        if cup.deadline < 0:
            raise CupOrderError(f"杯 {cup.cup_id} 交付班非法")
        if not cup.bottom_up:
            raise CupOrderError(f"杯 {cup.cup_id} 杯单为空")
        state[cup.cup_id] = list(cup.bottom_up)
    return state


def possible_colors_here(readings, brick_id, position):
    return {r.color_at(brick_id, position) for r in readings}


def advance(runtimes, brick_id, end):
    rt = runtimes[brick_id]
    new_rt = BrickRuntime(
        rt.brick_id,
        rt.left_pos + (1 if end == LEFT else 0),
        rt.right_pos - (1 if end == RIGHT else 0),
        rt.allowed_ends,
    )
    out = dict(runtimes)
    out[brick_id] = new_rt
    return out


DONE = PolicyNode(kind="done", note="全部杯已交齐")


# =========================================================================== 单窗
class SingleWindowPlanner:
    """单窗信念 MDP 搜索。

    记忆化键只含 *物理状态*（节拍、各砖剩余区间、各杯剩余层、废料额）。已切走的
    格位与见到的颜色完整决定当前信念，而这些事实已体现在物理区间与杯/废料的去向
    中；把“读法 id 集合”另放进键反而会让同一物理状态下的不同分支错误共享结果。
    失败状态不记忆化：同一物理局面在一条决策路径上失败、在另一条路径上可能成功。
    """

    def __init__(self, snapshot, cups, runtimes, waste_quota, horizon):
        self.snapshot = snapshot
        self.cups_meta = tuple(cups)
        self.runtimes0 = runtimes
        self.waste0 = waste_quota
        self.horizon = horizon
        self.memo = {}

    def solve(self):
        readings = tuple(self.snapshot.enumerate_readings())
        node = self._solve(
            0, self.runtimes0, make_cup_state(self.cups_meta), self.waste0, readings
        )
        if node is None:
            branch = self.failure_branch(
                0, self.runtimes0, make_cup_state(self.cups_meta),
                self.waste0, readings,
            )
            raise NoGuaranteedPolicyError(
                "不存在在所有可行读法下准点交齐的统一策略", branch
            )
        return node

    def _key(self, beat, runtimes, cup_state, waste):
        rt = tuple(sorted((b, v.left_pos, v.right_pos) for b, v in runtimes.items()))
        cups = tuple(sorted((c, tuple(s)) for c, s in cup_state.items()))
        return beat, rt, cups, waste

    def _solve(self, beat, runtimes, cup_state, waste, belief):
        if any(cup_state[c.cup_id] and c.deadline < beat for c in self.cups_meta):
            return None
        if all(not seq for seq in cup_state.values()):
            return DONE
        if beat > self.horizon:
            return None
        key = self._key(beat, runtimes, cup_state, waste)
        if key in self.memo:
            return self.memo[key]
        node = self._search(beat, runtimes, cup_state, waste, belief)
        if node is not None:
            self.memo[key] = node  # 只缓存成功节点；None 不缓存以免跨路径污染
        return node

    def _search(self, beat, runtimes, cup_state, waste, belief):
        # 先逐一切的决定；都失败再试空拍
        for brick_id, rt in sorted(runtimes.items()):
            if not rt.alive():
                continue
            for end in sorted(rt.allowed_ends):
                position = rt.end_position(end)
                colors = tuple(sorted(possible_colors_here(belief, brick_id, position)))
                branches = {}
                for color in colors:
                    sub_belief = tuple(
                        r for r in belief if r.color_at(brick_id, position) == color
                    )
                    if not sub_belief:
                        branches = None
                        break
                    routed = self._route(
                        beat, brick_id, end, color, runtimes,
                        cup_state, waste, sub_belief,
                    )
                    if routed is None:
                        branches = None
                        break
                    branches[color] = routed  # (CutAction, child)
                if branches is not None:
                    return PolicyNode(
                        kind="cut",
                        decision={"brick_id": brick_id, "end": end,
                                  "position": position, "colors": colors},
                        branches=branches,
                        note=f"拍{beat} {brick_id} 从{end}切，按见色分流",
                    )
        idle_child = self._solve(beat + 1, runtimes, cup_state, waste, belief)
        if idle_child is not None:
            return PolicyNode(kind="idle", single=idle_child, note=f"拍{beat}空拍")
        return None

    def _route(self, beat, brick_id, end, color, runtimes, cup_state,
               waste, belief):
        for cup in self.cups_meta:
            seq = cup_state[cup.cup_id]
            if seq and seq[0] == color:
                new_cups = {k: list(v) for k, v in cup_state.items()}
                new_cups[cup.cup_id] = seq[1:]
                child = self._solve(
                    beat + 1, advance(runtimes, brick_id, end),
                    new_cups, waste, belief,
                )
                if child is not None:
                    return CutAction(beat, cup.window, brick_id, end,
                                     color, cup.cup_id), child
        if waste > 0:
            child = self._solve(
                beat + 1, advance(runtimes, brick_id, end),
                cup_state, waste - 1, belief,
            )
            if child is not None:
                return CutAction(beat, "废料", brick_id, end, color,
                                 None, wasted=True), child
        return None

    def failure_branch(self, beat, runtimes, cup_state, waste, belief):
        """沿任一具体读法贪心执行，给出首个失约点的凭据。"""
        reading = belief[0]
        rt = {k: v for k, v in runtimes.items()}
        cups = {k: list(v) for k, v in cup_state.items()}
        trace = []
        for b in range(beat, self.horizon + 2):
            overdue = [c.cup_id for c in self.cups_meta if cups[c.cup_id] and c.deadline < b]
            if overdue:
                return {"beat": b, "overdue_cups": overdue, "trace": trace,
                        "reading": str(reading),
                        "reason": f"杯 {overdue} 到拍未交齐"}
            move = self._greedy_move(rt, cups, reading, waste)
            if move is None:
                stuck = self._stuck_reason(rt, cups, reading, waste)
                if stuck is not None:
                    return {"beat": b, "trace": trace, "reading": str(reading),
                            "reason": stuck}
                trace.append({"beat": b, "idle": True})
                continue
            brick_id, end, pos, color, target = move
            trace.append({"beat": b, "brick": brick_id, "end": end,
                          "color": color, "cup": target, "wasted": target is None})
            if target is None:
                waste -= 1
            else:
                cups[target] = cups[target][1:]
            rt = advance(rt, brick_id, end)
        return {"beat": self.horizon, "trace": trace, "reading": str(reading),
                "overdue_cups": [c.cup_id for c in self.cups_meta if cups[c.cup_id]],
                "reason": "时限内未交齐"}

    def _stuck_reason(self, rt, cups, reading, waste):
        """还有活砖却无任何合法落杯/废料去向时，给出具体颜色凭据。"""
        if waste > 0:
            return None  # 还有废料就不算堵死（贪心未选只是暂无合适杯）
        for brick_id, runtime in sorted(rt.items()):
            if not runtime.alive():
                continue
            for end in sorted(runtime.allowed_ends):
                pos = runtime.end_position(end)
                color = reading.color_at(brick_id, pos)
                match = any(cups[c.cup_id] and cups[c.cup_id][0] == color
                            for c in self.cups_meta)
                if not match:
                    return (f"切出 {color}（{brick_id} {end}端）无处可落且无废料额；"
                            f"在切杯下一层均不匹配")
        return None

    def _greedy_move(self, rt, cups, reading, waste):
        """返回 (砖,端,位,色,目标杯)；目标杯为 None 表示只能走废料。

        只枚举合法去向：颜色匹配某只杯的下一层，或废料额尚有余量。
        """
        fallback = None
        for brick_id, runtime in sorted(rt.items()):
            if not runtime.alive():
                continue
            for end in sorted(runtime.allowed_ends):
                pos = runtime.end_position(end)
                color = reading.color_at(brick_id, pos)
                target = next(
                    (c.cup_id for c in self.cups_meta
                     if cups[c.cup_id] and cups[c.cup_id][0] == color),
                    None,
                )
                if target is not None:
                    return (brick_id, end, pos, color, target)
                if fallback is None and waste > 0:
                    fallback = (brick_id, end, pos, color, None)
        return fallback


def plan_single_window(snapshot, cups, *, pre_cuts=None, allowed_ends=None,
                       waste_quota=0, horizon=None):
    """单窗统一策略入口；失败抛 :class:`NoGuaranteedPolicyError` 并附失约分支。"""
    runtimes = make_runtimes(snapshot, pre_cuts or {}, allowed_ends or {})
    if horizon is None:
        horizon = max((c.deadline for c in cups), default=0) + 1
    return SingleWindowPlanner(snapshot, cups, runtimes, waste_quota, horizon).solve()


# =========================================================================== 两窗
@dataclass(frozen=True)
class TestCut:
    """冷柜时段试切：从指定端取一格，颜色按真实读法印备料卡。"""

    brick_id: str
    end: str
    position: int


@dataclass
class TwoWindowResult:
    test_cut: TestCut
    prep_card: dict                 # 颜色 -> 相容读法描述
    root: PolicyNode
    window_cups: dict
    schedule: tuple[ScheduleEntry, ...]


class TwoWindowPlanner:
    """甲乙两窗分布规划（仅试切方案）。

    窗口只能看到自己的砖/杯历史；轮班表给定每拍由哪窗动。承诺表 ``commitments``
    以“(节拍, 窗口, 该窗砖区间, 该窗杯剩余)”为键，强制同一窗在两种自己无法区分
    的经历里作同一个决定；节拍是轮班表上的公共知识，纳入键以区分不同拍的局面。
    """

    def __init__(self, snapshot, cups, schedule, runtimes, waste_quota,
                 window_bricks, horizon):
        self.snapshot = snapshot
        self.cups_meta = tuple(cups)
        self.schedule = tuple(schedule)
        self.runtimes0 = runtimes
        self.waste0 = waste_quota
        self.window_bricks = window_bricks          # window -> frozenset(brick_id)
        self.horizon = horizon
        self.commitments = {}

    # --------------------------------------------------------------- 试切枚举
    def candidate_test_cuts(self, runtimes):
        out = []
        for brick in self.snapshot.bricks:
            if brick.mold_id is None:
                continue
            rt = runtimes[brick.brick_id]
            if not rt.alive():
                continue
            for end in sorted(rt.allowed_ends):
                out.append(TestCut(brick.brick_id, end, rt.end_position(end)))
        return out

    def plan_with_test_cut(self):
        readings = tuple(self.snapshot.enumerate_readings())
        if self.waste0 <= 0:
            raise CupOrderError("试切方案要求至少一格废料额接住不能进杯的试切片")
        for cut in self.candidate_test_cuts(self.runtimes0):
            groups = {}
            for reading in readings:
                color = reading.color_at(cut.brick_id, cut.position)
                groups.setdefault(color, []).append(reading)
            post_runtimes = advance(self.runtimes0, cut.brick_id, cut.end)
            roots_by_color = {}
            ok = True
            for color, group in sorted(groups.items()):
                self.commitments = {}
                node = self._solve(
                    0, post_runtimes, make_cup_state(self.cups_meta),
                    self.waste0 - 1, tuple(group),
                )
                if node is None:
                    ok = False
                    break
                prep_action = CutAction(-1, "冷柜试切", cut.brick_id, cut.end,
                                        color, None, wasted=True)
                roots_by_color[color] = (prep_action, node)
            if ok:
                prep_card = {
                    color: [str(r) for r in group]
                    for color, group in sorted(groups.items())
                }
                root = PolicyNode(
                    kind="test_cut",
                    decision={"cut": cut,
                              "prep_card_colors": sorted(groups)},
                    branches=roots_by_color,
                    note=(f"冷柜时段试切 {cut.brick_id} {cut.end}端第{cut.position}格；"
                          f"备料卡按实见颜色分流，试切片耗 1 格废料额、不进当班杯"),
                )
                return TwoWindowResult(
                    test_cut=cut, prep_card=prep_card, root=root,
                    window_cups={w: [c.cup_id for c in self.cups_meta if c.window == w]
                                 for w in sorted(self.window_bricks)},
                    schedule=self.schedule,
                )
        branch = self.failure_branch(
            0, self.runtimes0, make_cup_state(self.cups_meta),
            self.waste0, readings,
        )
        raise NoGuaranteedPolicyError("任何合法试切都无法保证两窗准点交齐", branch)

    def plan_without_test_cut(self):
        """不领冷柜时段：开摊信念为全部读法（题面示例通常由此证明无保证）。"""
        readings = tuple(self.snapshot.enumerate_readings())
        self.commitments = {}
        node = self._solve(
            0, self.runtimes0, make_cup_state(self.cups_meta),
            self.waste0, readings,
        )
        if node is None:
            raise NoGuaranteedPolicyError(
                "不试切时不存在保证策略",
                self.failure_branch(0, self.runtimes0,
                                    make_cup_state(self.cups_meta),
                                    self.waste0, readings),
            )
        return node

    # --------------------------------------------------------------- 分布搜索
    def _local_key(self, beat, window, runtimes, cup_state):
        own_bricks = self.window_bricks[window]
        rt = tuple(sorted(
            (b, v.left_pos, v.right_pos)
            for b, v in runtimes.items() if b in own_bricks
        ))
        cups = tuple(sorted(
            (c.cup_id, tuple(cup_state[c.cup_id]))
            for c in self.cups_meta if c.window == window
        ))
        return beat, window, rt, cups

    def _solve(self, beat, runtimes, cup_state, waste, belief):
        if any(cup_state[c.cup_id] and c.deadline < beat for c in self.cups_meta):
            return None
        if all(not seq for seq in cup_state.values()):
            return DONE
        if beat > self.horizon or beat >= len(self.schedule):
            return None
        window = self.schedule[beat].window
        if window is None:
            return self._solve(beat + 1, runtimes, cup_state, waste, belief)
        if window not in self.window_bricks:
            raise CupOrderError(f"轮班表拍 {beat} 指定了未知窗口 {window}")

        local_key = self._local_key(beat, window, runtimes, cup_state)
        if local_key in self.commitments:
            decision = self.commitments[local_key]
        else:
            decision = self._choose(beat, window, runtimes, cup_state, waste, belief)
            if decision is None:
                return None
            self.commitments[local_key] = decision  # 只承诺可行决定

        if decision["kind"] == "idle":
            child = self._solve(beat + 1, runtimes, cup_state, waste, belief)
            if child is None:
                return None
            return PolicyNode(kind="idle", single=child, note=f"拍{beat}{window}空拍")
        return self._apply_cut(decision, beat, window, runtimes,
                               cup_state, waste, belief)

    def _choose(self, beat, window, runtimes, cup_state, waste, belief):
        for brick_id in sorted(self.window_bricks[window]):
            rt = runtimes[brick_id]
            if not rt.alive():
                continue
            for end in sorted(rt.allowed_ends):
                position = rt.end_position(end)
                colors = tuple(sorted(possible_colors_here(belief, brick_id, position)))
                decision = {"kind": "cut", "brick_id": brick_id, "end": end,
                            "position": position, "colors": colors}
                node = self._apply_cut(decision, beat, window, runtimes,
                                       cup_state, waste, belief)
                if node is not None:
                    return decision
        return {"kind": "idle"}

    def _apply_cut(self, decision, beat, window, runtimes, cup_state, waste, belief):
        brick_id, end, position = decision["brick_id"], decision["end"], decision["position"]
        branches = {}
        for color in decision["colors"]:
            sub_belief = tuple(
                r for r in belief if r.color_at(brick_id, position) == color
            )
            if not sub_belief:
                return None
            routed = self._route(beat, window, brick_id, end, color,
                                 runtimes, cup_state, waste, sub_belief)
            if routed is None:
                return None
            branches[color] = routed
        return PolicyNode(
            kind="cut",
            decision=decision,
            branches=branches,
            note=f"拍{beat} {window}窗 {brick_id} 从{end}切，按见色分流",
        )

    def _route(self, beat, window, brick_id, end, color,
               runtimes, cup_state, waste, belief):
        # 对窗看不到这一刀，片只能落本窗的杯
        for cup in self.cups_meta:
            if cup.window != window:
                continue
            seq = cup_state[cup.cup_id]
            if seq and seq[0] == color:
                new_cups = {k: list(v) for k, v in cup_state.items()}
                new_cups[cup.cup_id] = seq[1:]
                child = self._solve(
                    beat + 1, advance(runtimes, brick_id, end),
                    new_cups, waste, belief,
                )
                if child is not None:
                    return CutAction(beat, window, brick_id, end, color,
                                     cup.cup_id), child
        if waste > 0:
            child = self._solve(
                beat + 1, advance(runtimes, brick_id, end),
                cup_state, waste - 1, belief,
            )
            if child is not None:
                return CutAction(beat, window, brick_id, end, color, None,
                                 wasted=True), child
        return None

    def failure_branch(self, beat, runtimes, cup_state, waste, belief):
        reading = belief[0]
        rt = {k: v for k, v in runtimes.items()}
        cups = {k: list(v) for k, v in cup_state.items()}
        trace = []
        for b in range(beat, min(self.horizon + 2, len(self.schedule) + 1)):
            overdue = [c.cup_id for c in self.cups_meta if cups[c.cup_id] and c.deadline < b]
            if overdue:
                return {"beat": b, "overdue_cups": overdue, "trace": trace,
                        "reading": str(reading), "reason": "杯到拍未交齐"}
            entry = self.schedule[b] if b < len(self.schedule) else None
            window = entry.window if entry else None
            if window is None:
                trace.append({"beat": b, "idle": True})
                continue
            move = self._greedy_move(window, rt, cups, reading, waste)
            if move is None:
                stuck = self._stuck_reason(window, rt, cups, reading, waste)
                if stuck is not None:
                    return {"beat": b, "trace": trace, "reading": str(reading),
                            "reason": stuck}
                trace.append({"beat": b, "window": window, "idle": True})
                continue
            brick_id, end, pos, color, target = move
            trace.append({"beat": b, "window": window, "brick": brick_id,
                          "end": end, "color": color, "cup": target,
                          "wasted": target is None})
            if target is None:
                waste -= 1
            else:
                cups[target] = cups[target][1:]
            rt = advance(rt, brick_id, end)
        return {"beat": self.horizon, "trace": trace, "reading": str(reading),
                "overdue_cups": [c.cup_id for c in self.cups_meta if cups[c.cup_id]],
                "reason": "时限内未交齐"}

    def _greedy_move(self, window, rt, cups, reading, waste):
        """只枚举本窗合法去向：本窗杯颜色匹配，或废料额有余量。"""
        fallback = None
        for brick_id in sorted(self.window_bricks[window]):
            runtime = rt[brick_id]
            if not runtime.alive():
                continue
            for end in sorted(runtime.allowed_ends):
                pos = runtime.end_position(end)
                color = reading.color_at(brick_id, pos)
                target = next(
                    (c.cup_id for c in self.cups_meta
                     if c.window == window and cups[c.cup_id]
                     and cups[c.cup_id][0] == color),
                    None,
                )
                if target is not None:
                    return (brick_id, end, pos, color, target)
                if fallback is None and waste > 0:
                    fallback = (brick_id, end, pos, color, None)
        return fallback

    def _stuck_reason(self, window, rt, cups, reading, waste):
        if waste > 0:
            return None
        for brick_id in sorted(self.window_bricks[window]):
            runtime = rt[brick_id]
            if not runtime.alive():
                continue
            for end in sorted(runtime.allowed_ends):
                pos = runtime.end_position(end)
                color = reading.color_at(brick_id, pos)
                match = any(
                    c.window == window and cups[c.cup_id]
                    and cups[c.cup_id][0] == color
                    for c in self.cups_meta
                )
                if not match:
                    return (f"{window}窗切出 {color}（{brick_id} {end}端）"
                    f"无处可落且无废料额；本窗杯下一层均不匹配")
        return None


def plan_distributed(snapshot, cups, schedule, brick_windows, *,
                     pre_cuts=None, allowed_ends=None, waste_quota=0,
                     horizon=None, use_test_cut=True):
    """两窗规划门面；``brick_windows`` 显式给出每块砖归属窗口。

    ``use_test_cut=True``（唯一实现的出摊办法）枚举合法冷柜试切并返回
    :class:`TwoWindowResult`；``False`` 不领冷柜时段、保留全部砖格，找不到保证策略
    时抛 :class:`NoGuaranteedPolicyError`。
    """
    pre_cuts = pre_cuts or {}
    allowed_ends = allowed_ends or {}
    for brick_id, window in brick_windows.items():
        snapshot.brick(brick_id)
    windows = set(brick_windows.values()) | {c.window for c in cups}
    schedule_windows = {e.window for e in schedule if e.window is not None}
    unknown = schedule_windows - windows
    if unknown:
        raise CupOrderError(f"轮班表含未知窗口 {sorted(unknown)}")
    window_bricks = {w: frozenset(b for b, x in brick_windows.items() if x == w)
                     for w in windows}
    for cup in cups:
        if cup.window not in windows:
            raise CupOrderError(f"杯 {cup.cup_id} 归属未知窗口 {cup.window}")
    runtimes = make_runtimes(snapshot, pre_cuts, allowed_ends)
    if horizon is None:
        horizon = max([c.deadline for c in cups] + [len(schedule) - 1]) + 1
    planner = TwoWindowPlanner(
        snapshot, cups, schedule, runtimes, waste_quota, window_bricks, horizon,
    )
    if use_test_cut:
        return planner.plan_with_test_cut()
    return planner.plan_without_test_cut()


# ------------------------------------------------------- 题面示例的联络拍核验
def comm_after_analysis(snapshot, cups, schedule, brick_windows, *,
                        pre_cuts=None, allowed_ends=None):
    """显式核验：联络机会从乙下刀前挪到切完后，乙无法保证杯底红。

    乙下刀前没有任何来自甲的新观察（联络已挪后），乙对自己两砖左端的信念仍覆盖
    两种读法；无论选哪块砖左切，都存在一种相容读法让该格不是杯底所需颜色，且当拍
    无处可放即失约。返回 ``(可保证与否, 凭据)``。
    """
    runtimes = make_runtimes(snapshot, pre_cuts or {}, allowed_ends or {})
    readings = tuple(snapshot.enumerate_readings())
    target_window = next(iter(sorted({w for w in brick_windows.values() if w != "甲"})),
                         "乙")
    needed = {c.cup_id: c.bottom_up[0] for c in cups if c.window == target_window}
    evidence = []
    for brick_id in sorted(brick_windows):
        if brick_windows[brick_id] != target_window:
            continue
        rt = runtimes[brick_id]
        if not rt.alive() or LEFT not in rt.allowed_ends:
            continue
        pos = rt.left_pos
        colors = possible_colors_here(readings, brick_id, pos)
        item = {"brick": brick_id, "left_position": pos,
                "possible_colors": sorted(colors)}
        for cup_id, need in needed.items():
            bad = next((r for r in readings
                        if r.color_at(brick_id, pos) != need), None)
            if bad is not None:
                item["counterexample_cup"] = cup_id
                item["counterexample_reading"] = str(bad)
        evidence.append(item)
    guaranteed = bool(evidence) and all(
        "counterexample_cup" not in e for e in evidence
    )
    return guaranteed, evidence
