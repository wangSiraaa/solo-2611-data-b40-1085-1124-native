"""指挥的尾声交付规划：实牌暗号与整拍静默的最小代价策略。

背景规则：

* 指挥开演前拿到本次的尾声版本；乐手只在谱面规定的拍点（``reveal_gap``）
  才看见新的谱牌；
* 馆里有一张**有号双面牌**，可在授权空拍（``signal_gaps``）给一位乐手一个
  预先约定的二值暗号；同一队借走后不能同场再借另一队（本模块只服务一队，
  额外用 :class:`CardLibrary` 登记借用关系）；
* 也可不提前示意，只在观众约定可作间奏的拍位（``silence_gaps``）加入整拍
  静默一次，放手后重置指法；若静默落在末音之后，必须把结束窗让出一拍，
  已有入场的观众不会多等；
* 演奏中的选择只能依据当时已见谱牌、已经听完的静默和实手势。

规划器对每个候选方案做因果（信息分区）校验：同一知识单元（partition 的一
块）里的若干尾声在该拍必须采取完全相同的（指位, 试吹线, 气压）动作，否则
乐手无法在当时区分它们。暗号在授权空拍细化分区；静默只重置手部，不携带
尾声身份信息。DP 状态全部不可变，可精确回溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .model import Instrument
from .phrase import Beat, BeatChoice, _line_for_pressure, _move_allowed
from .windows import line_windows

# “无先行奏拍（首拍或刚放手）”的哨兵。
FRESH = "fresh"


@dataclass(frozen=True)
class Coda:
    """一个合法尾声。

    ``allowed_fingerings`` 是该尾声在**受约束拍**上的指位限制（如尾音只准
    某指位）；``restricted_beats`` 标出这些拍——其余拍（揭示前的共同前缀）
    回落谱面自身的 ``allowed``，两个尾声在共同拍上可以共用任一合格指位。
    谱牌在 ``reveal_gap`` 间隙后才对乐手可见。
    """

    name: str
    allowed_fingerings: frozenset[int]
    reveal_gap: int
    restricted_beats: frozenset[int] = frozenset()

    def allows(self, fingering: int) -> bool:
        return fingering in self.allowed_fingerings

    def fingerings_for_beat(self, beat_index: int, beat_allowed: frozenset[int] | None) -> frozenset[int]:
        """本尾声在某拍上的可用指位：受约束拍用尾声限制，否则用谱面允许集。"""
        if beat_index in self.restricted_beats:
            return self.allowed_fingerings
        return beat_allowed if beat_allowed is not None else None  # type: ignore[return-value]


@dataclass(frozen=True)
class SilenceSlot:
    """一次整拍静默：落在第 ``gap`` 个间隙，是否需要把结束窗让出一拍。"""

    gap: int
    window_extension_required: bool = False


@dataclass(frozen=True)
class Plan:
    """一条候选交付路线。"""

    signal_gap: int | None
    signal_bits: int
    assignment: tuple[int, ...] | None
    silences: tuple[SilenceSlot, ...]
    feasible: bool
    failure_reason: str | None = None
    witness: tuple[BeatChoice | None, ...] = ()

    @property
    def uses_card(self) -> bool:
        return self.signal_gap is not None

    @property
    def cost_text(self) -> str:
        if self.signal_gap is not None:
            return f"{self.signal_bits} 位二值暗号（实牌，空拍 {self.signal_gap}）"
        if self.silences:
            slots = "、".join(
                f"间隙 {s.gap}" + ("（结束窗让出一拍）" if s.window_extension_required else "")
                for s in self.silences
            )
            return f"{len(self.silences)} 个整拍静默（{slots}）"
        return "零代价（自然揭示已足够）"


@dataclass(frozen=True)
class PlannerCase:
    instrument: Instrument
    beats: tuple[Beat, ...]
    codas: tuple[Coda, ...]
    first_line: str
    tolerance: Fraction
    signal_gaps: frozenset[int]
    silence_gaps: frozenset[int]
    lock_after_beat: int | None = None  # 该拍起锁指：之后同尾声指位不得再变
    musician: str = "陶埙乐手"

    def __post_init__(self) -> None:
        n = len(self.beats)
        for gap in self.signal_gaps | self.silence_gaps:
            if not (0 <= gap <= n):
                raise ValueError(f"间隙 {gap} 超出谱单范围 0..{n}")
        for coda in self.codas:
            if not (0 <= coda.reveal_gap <= n):
                raise ValueError(f"尾声 {coda.name} 的揭示点越界")
            if not coda.allowed_fingerings:
                raise ValueError(f"尾声 {coda.name} 没有允许指位")


@dataclass(frozen=True)
class CardLibrary:
    """馆里的有号双面牌借用登记：一张牌同场只能借给一队。"""

    card_id: str
    borrower: str | None = None

    def lend(self, team: str) -> "CardLibrary":
        if self.borrower is not None and self.borrower != team:
            raise RuntimeError(
                f"牌 {self.card_id} 已借给 {self.borrower}，同场不能再借 {team}"
            )
        return CardLibrary(self.card_id, team)

    def take_back(self) -> "CardLibrary":
        return CardLibrary(self.card_id, None)


class InfeasibleCoda(Exception):
    """某个尾声在物理上就无法交付（与暗号/静默无关）。"""


# ---- 预计算：每个 (尾声, 拍, 试吹线) 的 {指位: [可行气压...]} ----

def _build_option_index(case: PlannerCase) -> dict[tuple[str, int, str], dict[int, list[Fraction]]]:
    index: dict[tuple[str, int, str], dict[int, list[Fraction]]] = {}
    for coda in case.codas:
        for beat_index, beat in enumerate(case.beats):
            if beat.rest:
                continue
            for line in ("attack", "release"):
                out: dict[int, list[Fraction]] = {}
                coda_allowed = coda.fingerings_for_beat(beat_index, beat.allowed)
                pool = (
                    case.instrument.fingering_set
                    if coda_allowed is None
                    else (coda_allowed & case.instrument.fingering_set)
                )
                for fingering in sorted(pool):
                    windows = line_windows(
                        case.instrument.table(fingering).line(line),
                        beat.note,
                        case.tolerance,
                    ).windows
                    if beat.pressure is not None:
                        pressures = (
                            [beat.pressure]
                            if any(w.contains(beat.pressure) for w in windows)
                            else []
                        )
                    else:
                        pressures = list(dict.fromkeys(p for w in windows for p in (w.low, w.high)))
                    if pressures:
                        out[fingering] = pressures
                index[(coda.name, beat_index, line)] = out
    return index


def _assignments(coda_count: int, signal_bits: int) -> tuple[tuple[int, ...], ...]:
    """把 ``signal_bits`` 位二值暗号互异地分给各尾声的全部赋值。"""
    total = 1 << signal_bits
    if coda_count > total:
        return ()
    results: list[tuple[int, ...]] = []

    def build(prefix: tuple[int, ...], used: frozenset[int]) -> None:
        if len(prefix) == coda_count:
            results.append(prefix)
            return
        for code in range(total):
            if code not in used:
                build(prefix + (code,), used | {code})

    build((), frozenset())
    return tuple(results)


def _lines_for_beat(case: PlannerCase, beat: Beat, prev_state) -> tuple[str, ...]:
    if beat.forced_line is not None:
        return (beat.forced_line,)
    if prev_state == FRESH:
        return (case.first_line,)
    if beat.pressure is not None:
        return (_line_for_pressure(prev_state[1], beat.pressure),)
    return ("attack", "release")


def _solo_feasible(
    case: PlannerCase,
    index: dict[tuple[str, int, str], dict[int, list[Fraction]]],
    coda: Coda,
) -> bool:
    """单尾声物理可行性：满足指位限制、可动指位、锁指与方向约束的一条路径。"""

    def backtrack(beat_index: int, prev) -> bool:
        if beat_index == len(case.beats):
            return True
        beat = case.beats[beat_index]
        if beat.rest:
            return backtrack(beat_index + 1, FRESH)
        for line in _lines_for_beat(case, beat, prev):
            for fingering, pressures in index[(coda.name, beat_index, line)].items():
                local = list(pressures)
                if prev != FRESH:
                    prev_f, prev_p = prev
                    if not _move_allowed(prev_f, fingering, beat.movable_mask):
                        continue
                    if line == "attack":
                        local = [p for p in local if p >= prev_p]
                    else:
                        local = [p for p in local if p < prev_p]
                if case.lock_after_beat is not None and beat_index > case.lock_after_beat:
                    if prev != FRESH and prev_f != fingering:
                        continue
                if any(backtrack(beat_index + 1, (fingering, p)) for p in local):
                    return True
        return False

    return backtrack(0, FRESH)


def evaluate_plan(
    case: PlannerCase,
    signal_gap: int | None,
    signal_bits: int,
    assignment: tuple[int, ...] | None,
    silences: tuple[SilenceSlot, ...],
) -> Plan:
    """对一条具体路线做因果策略校验，成功时返回共享策略的逐拍见证。"""
    index = _build_option_index(case)
    names = tuple(c.name for c in case.codas)
    coda_by_name = {c.name: c for c in case.codas}
    silence_gaps = {s.gap for s in silences}

    for coda in case.codas:
        if not _solo_feasible(case, index, coda):
            raise InfeasibleCoda(coda.name)

    code_of: dict[str, int] = {}
    if assignment is not None:
        code_of = dict(zip(names, assignment))

    # 状态：(拍号, 分区, 各尾声先行状态, 各尾声锁指, 本拍是否静默后自由重起)
    # 先行状态/锁指均按 names 顺序排列；FRESH 表示无先行奏拍。
    initial_partition = (frozenset(names),)
    initial_prev = tuple(FRESH for _ in names)
    initial_locked = tuple(-1 for _ in names)

    memo: dict = {}

    def recurse(beat_index: int, partition, prevs, locked, restart) -> tuple | None:
        if beat_index == len(case.beats):
            return ()
        key = (beat_index, partition, prevs, locked, restart)
        if key in memo:
            return memo[key]

        # 间隙 g 位于第 g-1 拍与第 g 拍之间（gap 0 在首拍之前）。
        # 进入第 beat_index 拍前，先处理该间隙上的全部事件，顺序：
        # 静默放手 → 实牌暗号 → 谱牌揭示。
        gap = beat_index
        if gap in silence_gaps:
            # 真实静默才允许放手。静默把本拍标记为“自由重起拍”：手已放开，
            # 且同间隙的揭谱把信息送到，尚未被暗号区分的单元也可在本拍按
            # 各自尾声自由选指（不再要求公共动作）。
            prevs = tuple(FRESH for _ in names)
            restart = True

        if signal_gap == gap and assignment is not None:
            partition = _refine_by_signal(partition, code_of)

        partition = _refine_by_reveal(partition, coda_by_name, gap)

        beat = case.beats[beat_index]
        prev_by_name = dict(zip(names, prevs))
        locked_by_name = dict(zip(names, locked))

        if beat.rest:
            tail = recurse(
                beat_index + 1,
                partition,
                tuple(FRESH for _ in names),
                locked,
                False,
            )
            memo[key] = None if tail is None else (("rest", None),) + tail
            return memo[key]

        # 逐知识单元求公共动作：单元内所有尾声的合法动作集合必须有交集。
        # 静默后的自由重起拍：事件阶段已按本间隙的揭谱把到点尾声拆成独立
        # 单元（手已放开 + 信息已到），这些单元可各自选指；仍隐藏（尚未
        # 揭示）的单元保持为整块，依旧必须采取公共动作。
        # 每个单元有若干候选公共动作时必须**回溯**：本拍任选其一都可能让
        # 后续拍（尤其锁指拍）无路，不能固定取排序第一个。
        unit_options: list[tuple[frozenset[str], list[tuple]]] = []
        for unit in partition:
            sub_units = [frozenset({n}) for n in unit] if restart else [unit]
            for sub in sub_units:
                common: list[tuple] | None = None
                for name in sub:
                    coda = coda_by_name[name]
                    prev = prev_by_name[name]
                    legal = _legal_actions(
                        case, index, coda, beat_index, beat, prev,
                        locked_by_name[name],
                    )
                    common = legal if common is None else [a for a in common if a in legal]
                    if not common:
                        memo[key] = None
                        return None
                unit_options.append((sub, common))

        def try_units(position: int, chosen: dict[str, tuple]) -> tuple | None:
            if position == len(unit_options):
                next_prevs = tuple(
                    (chosen[name][0], chosen[name][2]) for name in names
                )
                # 锁指拍完成后才把指位记入锁定状态，供其后的拍读取。
                next_locked = tuple(
                    chosen[name][0]
                    if case.lock_after_beat is not None and beat_index >= case.lock_after_beat
                    else locked_by_name[name]
                    for name in names
                )
                return recurse(beat_index + 1, partition, next_prevs, next_locked, False)

            sub, common = unit_options[position]
            for action in common:
                local = dict(chosen)
                for name in sub:
                    local[name] = action
                tail = try_units(position + 1, local)
                if tail is not None:
                    return tail
            return None

        tail = try_units(0, {})
        if tail is None:
            memo[key] = None
            return None

        # 见证取单元代表（同单元动作相同），按单元首元素记录。回溯成功后
        # 重新走一遍取首个成功组合，仅用于可读见证。
        chosen_witness: dict[str, tuple] = {}
        for sub, common in unit_options:
            chosen_witness[next(iter(sub))] = common[0]
        witness_step = tuple(
            (unit_name, BeatChoice(chosen_witness[unit_name][0],
                                   chosen_witness[unit_name][1],
                                   chosen_witness[unit_name][2], ()))
            for unit, _common in unit_options
            for unit_name in (next(iter(unit)),)
        )
        memo[key] = (("play", witness_step),) + tail
        return memo[key]

    tokens = recurse(0, initial_partition, initial_prev, initial_locked, False)
    if tokens is None:
        return Plan(signal_gap, signal_bits, assignment, silences, False,
                    "知识单元内无公共动作（暗号/静默不足）")
    witness = tuple(step[1] for step in tokens)
    return Plan(signal_gap, signal_bits, assignment, silences, True, None, witness)


def _refine_by_signal(partition, code_of: dict[str, int]):
    refined: list[frozenset[str]] = []
    for unit in partition:
        buckets: dict[int, set[str]] = {}
        for name in unit:
            buckets.setdefault(code_of[name], set()).add(name)
        refined.extend(frozenset(group) for group in buckets.values())
    return tuple(sorted(refined, key=lambda block: tuple(sorted(block))))


def _refine_by_reveal(partition, coda_by_name: dict[str, Coda], gap: int):
    # 谱牌在 reveal_gap 间隙揭示：进入第 gap 拍时，只有 reveal_gap < gap
    # 的尾声已经可见（间隙 g 位于第 g-1 拍与第 g 拍之间）。
    refined: list[frozenset[str]] = []
    for unit in partition:
        still_hidden = {name for name in unit if coda_by_name[name].reveal_gap >= gap}
        for name in unit:
            if coda_by_name[name].reveal_gap < gap:
                refined.append(frozenset({name}))
        if still_hidden:
            refined.append(frozenset(still_hidden))
    return tuple(sorted(refined, key=lambda block: tuple(sorted(block))))


def _legal_actions(
    case: PlannerCase,
    index: dict[tuple[str, int, str], dict[int, list[Fraction]]],
    coda: Coda,
    beat_index: int,
    beat: Beat,
    prev,
    locked_fingering: int,
) -> list[tuple[int, str, Fraction]]:
    actions: list[tuple[int, str, Fraction]] = []
    # 锁指只约束“锁指拍之后”的拍；锁指拍本身建立锁定，不在此过滤。
    lock_active = (
        case.lock_after_beat is not None
        and beat_index > case.lock_after_beat
        and locked_fingering >= 0
    )
    for line in _lines_for_beat(case, beat, prev):
        for fingering, pressures in index[(coda.name, beat_index, line)].items():
            if lock_active and fingering != locked_fingering:
                continue
            local = list(pressures)
            if prev != FRESH:
                prev_f, prev_p = prev
                if not _move_allowed(prev_f, fingering, beat.movable_mask):
                    continue
                if line == "attack":
                    local = [p for p in local if p >= prev_p]
                else:
                    local = [p for p in local if p < prev_p]
            for pressure in local:
                actions.append((fingering, line, pressure))
    return actions


def plan_minimum(case: PlannerCase) -> dict[str, Plan | None]:
    """分别求“最少暗号”与“最少静默”两条路线（含零代价基线）。

    若某个尾声在物理上就无法交付（与暗号/静默无关），直接抛
    :class:`InfeasibleCoda`，调用方据此判断“给牌、挪拍均救不了”。
    """
    coda_count = len(case.codas)
    index = _build_option_index(case)
    for coda in case.codas:
        if not _solo_feasible(case, index, coda):
            raise InfeasibleCoda(coda.name)

    base = evaluate_plan(case, None, 0, None, ())
    if base.feasible:
        return {"card": base, "silence": base}

    card_plan: Plan | None = None
    for bits in range(1, coda_count.bit_length() + 1):
        if card_plan is not None:
            break
        for assignment in _assignments(coda_count, bits):
            for gap in sorted(case.signal_gaps):
                plan = evaluate_plan(case, gap, bits, assignment, ())
                if plan.feasible:
                    card_plan = plan
                    break
            if card_plan is not None:
                break

    silence_plan: Plan | None = None
    for gap in sorted(case.silence_gaps):
        # 题面：整拍静默只在观众明确接受该间奏并把结束窗让出一拍时可交，
        # 无论落在末音前的间隙还是末音之后，都占用一个额外时窗。
        slot = SilenceSlot(gap, True)
        plan = evaluate_plan(case, None, 0, None, (slot,))
        if plan.feasible:
            silence_plan = plan
            break

    return {"card": card_plan, "silence": silence_plan}


def describe_cost(plan: Plan | None, case: PlannerCase) -> str:
    """说明代价落在哪位乐手或哪个首末拍。"""
    if plan is None:
        return "无可行路线（存在物理上无法交付的尾声）"
    if plan.uses_card:
        when = "首拍之前的空拍" if plan.signal_gap == 0 else f"第 {plan.signal_gap} 个间隙的空拍"
        return (
            f"代价落在{case.musician}：{when}接收 {plan.signal_bits} 位二值实牌暗号；"
            "本队借用期间该牌不借另一队。"
        )
    if plan.silences:
        slot = plan.silences[0]
        where = "末音之后" if slot.gap >= len(case.beats) else f"第 {slot.gap} 个间隙"
        extra = "，结束窗须让出一拍（观众已明确接受该间奏）" if slot.window_extension_required else ""
        return f"代价落在{where}的整拍静默：乐手真实静默后放手重置{extra}。"
    return "无额外代价：自然揭示点足以区分全部尾声。"
