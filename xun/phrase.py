"""谱单逐拍可行性：指位、试吹线与气压的精确回溯。

每拍给音名（目标音高）、可选气压（``None`` 表示乐手自选）与可动指位。
方向规则：

* 气压上升或持平 → 吹响线（``ATTACK``）；气压下降 → 收气线（``RELEASE``）；
* 首拍方向由谱面给出；换气休止后重新放手，下一奏拍视同首拍；
* 谱面可显式指定某拍的试吹线，显式指定优先。

连音（相邻两个奏拍之间没有休止）至多动一指，且变动的孔必须落在该拍
``movable_mask`` 允许的位上；换气休止可以重新放手，不受限。
所有音高/气压比较都在 ``Fraction`` 上完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .model import ATTACK, RELEASE, Instrument, popcount_changed
from .windows import Window, line_windows


@dataclass(frozen=True)
class Beat:
    """谱单上的一拍。

    ``rest`` 为真时是换气休止：不发音、无气压，乐手放手，后续奏拍重置。
    ``pressure`` 给定则单档定压；``None`` 表示该拍气压可选。
    ``allowed`` 限定本拍可用指位；``movable_mask`` 限定相对上一奏拍
    允许变动的孔（其余孔必须保持）；``forced_line`` 显式指定试吹线。
    """

    note: Fraction | None = None
    pressure: Fraction | None = None
    allowed: frozenset[int] | None = None
    movable_mask: int | None = None
    forced_line: str | None = None
    rest: bool = False
    label: str = ""

    def __post_init__(self) -> None:
        if self.rest:
            if self.note is not None or self.pressure is not None:
                raise ValueError("换气休止不携带音高与气压")
            return
        if self.note is None:
            raise ValueError("奏拍必须给音名")
        if self.forced_line is not None and self.forced_line not in (ATTACK, RELEASE):
            raise ValueError(f"未知试吹线 {self.forced_line!r}")
        if self.movable_mask is not None and self.movable_mask < 0:
            raise ValueError("movable_mask 不能为负")


@dataclass(frozen=True)
class BeatChoice:
    """一拍的实际演奏决策。"""

    fingering: int
    line: str
    pressure: Fraction
    windows: tuple[Window, ...]  # 命中的音窗证据（恰在其中之一）

    def render(self, holes: int) -> str:
        from .model import format_fingering

        name = "吹响" if self.line == ATTACK else "收气"
        return f"{format_fingering(self.fingering, holes)}/{name}/p={_f(self.pressure)}"


@dataclass(frozen=True)
class Phrase:
    beats: tuple[Beat, ...]
    first_line: str = ATTACK
    tolerance: Fraction = Fraction(0)

    def __post_init__(self) -> None:
        if self.first_line not in (ATTACK, RELEASE):
            raise ValueError(f"未知试吹线 {self.first_line!r}")
        if len(self.beats) > 8:
            raise ValueError("本库限定短句至多八拍")
        if not self.beats:
            raise ValueError("空谱单无法演奏")
        if self.tolerance < 0:
            raise ValueError("容差必须非负")


@dataclass(frozen=True)
class PhraseResult:
    feasible: bool
    path: tuple[BeatChoice | None, ...]
    failure_beat: int | None
    reason: str | None

    def render(self, holes: int) -> str:
        if not self.feasible:
            return f"不可演：第 {self.failure_beat} 拍（{self.reason}）"
        parts = []
        for i, choice in enumerate(self.path):
            if choice is None:
                parts.append(f"拍{i}: 换气休止（放手重置）")
            else:
                parts.append(f"拍{i}: {choice.render(holes)}")
        return "\n".join(parts)


def _f(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _line_for_pressure(prev_pressure: Fraction, pressure: Fraction) -> str:
    """气压上升或持平走吹响线，下降走收气线。"""
    return ATTACK if pressure >= prev_pressure else RELEASE


def _move_allowed(prev_fingering: int, fingering: int, mask: int | None) -> bool:
    changed = prev_fingering ^ fingering
    if changed == 0:
        return True
    if mask is None:
        return popcount_changed(prev_fingering, fingering) <= 1
    # 谱面限定可动指位：只允许 mask 内的孔变化，且至多一指。
    return (changed & ~mask) == 0 and popcount_changed(prev_fingering, fingering) == 1


def _window_cache(instrument: Instrument, phrase: Phrase):
    """返回 (拍, 指位, 试吹线) -> 该拍音高下的音窗 的惰性查询函数。"""
    cache: dict[tuple[int, int, str], tuple[Window, ...]] = {}

    def windows_for(beat_index: int, fingering: int, line: str) -> tuple[Window, ...]:
        key = (beat_index, fingering, line)
        if key not in cache:
            table = instrument.table(fingering)
            lw = line_windows(table.line(line), phrase.beats[beat_index].note, phrase.tolerance)
            cache[key] = lw.windows
        return cache[key]

    return windows_for


def _candidate_pressures(
    windows: tuple[Window, ...],
    fixed: Fraction | None,
    prev_pressure: Fraction | None,
    line: str,
) -> list[tuple[Fraction, tuple[Window, ...]]]:
    """枚举该拍该指位在给定试吹线上的候选气压（含命中音窗证据）。

    可选气压只需试：各音窗端点，以及上一拍气压（若落在某音窗内，对应持平）。
    方向约束在此一并施加：吹响线要求 p ≥ 上一拍气压，收气线要求 p < 上一拍气压。
    """
    found: dict[Fraction, tuple[Window, ...]] = {}

    def consider(pressure: Fraction) -> None:
        if prev_pressure is not None:
            if line == ATTACK and pressure < prev_pressure:
                return
            if line == RELEASE and pressure >= prev_pressure:
                return
        hits = tuple(w for w in windows if w.contains(pressure))
        if hits:
            found.setdefault(pressure, hits)

    if fixed is not None:
        consider(fixed)
        return [(p, w) for p, w in found.items()]

    for w in windows:
        consider(w.low)
        consider(w.high)
    if prev_pressure is not None:
        # 与上一拍持平（吹响线）：若上拍气压落在某音窗内部，它不是任何
        # 音窗的端点，必须单独枚举，否则会漏掉合法的持平走吹响线路径。
        consider(prev_pressure)
    return [(p, w) for p, w in sorted(found.items(), key=lambda item: item[0])]


def solve_phrase(instrument: Instrument, phrase: Phrase) -> PhraseResult:
    """回溯求一条可演路径；不可演时给出第一处失败拍与原因。"""
    windows_for = _window_cache(instrument, phrase)

    def eligible_fingerings(beat: Beat) -> list[int]:
        allowed = instrument.fingering_set if beat.allowed is None else (beat.allowed & instrument.fingering_set)
        return sorted(allowed)

    def backtrack(
        index: int,
        prev: BeatChoice | None,
        path: list[BeatChoice | None],
    ) -> PhraseResult | None:
        if index == len(phrase.beats):
            return PhraseResult(True, tuple(path), None, None)

        beat = phrase.beats[index]
        if beat.rest:
            return backtrack(index + 1, None, path + [None])

        fresh = prev is None
        # 决定本拍要尝试的试吹线：显式指定唯一；首拍/重置拍用谱面方向；
        # 否则按气压关系决定，自选气压时两条线都试（各自带方向约束）。
        if beat.forced_line is not None:
            lines = (beat.forced_line,)
        elif fresh:
            lines = (phrase.first_line,)
        elif beat.pressure is not None:
            lines = (_line_for_pressure(prev.pressure, beat.pressure),)
        else:
            lines = (ATTACK, RELEASE)

        # 追踪本拍之下触及的最深失败拍与原因，避免浅层覆盖真正的根因。
        deepest = {"beat": -1, "reason": None}

        def note_deep(result: PhraseResult) -> None:
            if result.failure_beat is not None and result.failure_beat > deepest["beat"]:
                deepest["beat"] = result.failure_beat
                deepest["reason"] = result.reason

        any_reason: str | None = None
        for fingering in eligible_fingerings(beat):
            if not fresh and not _move_allowed(prev.fingering, fingering, beat.movable_mask):
                continue
            for line in lines:
                windows = windows_for(index, fingering, line)
                if not windows:
                    any_reason = f"指位 {fingering} 在{line}线上无奏准音窗"
                    continue
                candidates = _candidate_pressures(
                    windows,
                    beat.pressure,
                    None if fresh else prev.pressure,
                    line,
                )
                for pressure, hits in candidates:
                    if not fresh and beat.pressure is not None:
                        # 定压拍再核一次方向，保持与所选线一致。
                        if _line_for_pressure(prev.pressure, pressure) != line:
                            continue
                    choice = BeatChoice(fingering, line, pressure, hits)
                    result = backtrack(index + 1, choice, path + [choice])
                    # 失败也返回 PhraseResult（非 None），必须看 feasible 标志，
                    # 否则会把后续拍的失败误当成功而停止回溯。
                    if result is not None and result.feasible:
                        return result
                    if result is not None:
                        note_deep(result)
                any_reason = any_reason or "候选气压全部不满足方向/音窗约束"

        # 本拍本身无候选时根因在本拍；否则保留递归触及的更深失败拍。
        if deepest["beat"] >= 0:
            fail_beat, fail_reason = deepest["beat"], deepest["reason"]
        else:
            fail_beat, fail_reason = index, any_reason or "没有满足指位与可动指位约束的候选"
        return PhraseResult(
            False,
            tuple(path + [None] * (len(phrase.beats) - index)),
            fail_beat,
            fail_reason,
        )

    result = backtrack(0, None, [])
    assert result is not None
    return result


def enumerate_beat_options(
    instrument: Instrument,
    beat: Beat,
    line: str,
    tolerance: Fraction,
) -> dict[int, list[Fraction]]:
    """供指挥规划复用：返回某拍某条线上 {指位: [可行气压...]}（不含跨拍约束）。"""
    options: dict[int, list[Fraction]] = {}
    allowed = instrument.fingering_set if beat.allowed is None else (beat.allowed & instrument.fingering_set)
    for fingering in sorted(allowed):
        windows = line_windows(instrument.table(fingering).line(line), beat.note, tolerance)
        if beat.pressure is not None:
            pressures = [beat.pressure] if any(w.contains(beat.pressure) for w in windows.windows) else []
        else:
            pressures = list(dict.fromkeys(p for w in windows.windows for p in (w.low, w.high)))
        if pressures:
            options[fingering] = pressures
    return options
