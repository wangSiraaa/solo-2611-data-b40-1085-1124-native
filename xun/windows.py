"""奏准音窗：在试吹线上精确求出“能奏准某音”的闭压力区间。

规则（题面冻结语义）：

* 只使用合格读数；相邻**实测**点之间作精确线性插值，缺测档不被跨越；
* 给定音高 ``target`` 与容差 ``tolerance``，合格条件是
  ``|pitch(p) - target| <= tolerance``，恰好压线（等号）算合格；
* 全部判定在 ``Fraction`` 上完成。读谱台只显示短十进制只影响展示，
  绝不能把窗外音高“洗”成合格——见 :func:`display_probe`。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .delivery_note import short_decimal
from .model import (
    ATTACK,
    DATA,
    INTERIOR,
    RELEASE,
    FingeringTable,
    Instrument,
    PressureLine,
    format_fingering,
)


@dataclass(frozen=True)
class Boundary:
    """闭区间一个端点的来源证据。

    ``kind`` 记录边界在试吹线上的位置：实测档（``DATA``）或相邻实测点
    插值段的内部反解点（``INTERIOR``）；``on_tolerance`` 独立记录该点音高
    是否恰好压在 target±tolerance 上——零容差命中实测点时两者同时为真，
    证据互不覆盖。
    """

    pressure: Fraction
    pitch: Fraction
    kind: str          # DATA / INTERIOR
    line: str          # ATTACK / RELEASE
    segment: tuple[int, int] | None  # 落在哪个插值段（实测点下标），孤立点为 None
    on_tolerance: bool = False

    def evidence(self) -> dict:
        return {
            "pressure": _frac_text(self.pressure),
            "pitch": _frac_text(self.pitch),
            "kind": self.kind,
            "line": self.line,
            "segment": self.segment,
            "on_tolerance": self.on_tolerance,
        }


@dataclass(frozen=True)
class Window:
    """一段闭压力区间及其两端的支撑证据。"""

    low: Fraction
    high: Fraction
    low_boundary: Boundary
    high_boundary: Boundary

    def contains(self, pressure: Fraction) -> bool:
        return self.low <= pressure <= self.high

    def touches(self, other: "Window") -> bool:
        return not (self.high < other.low or other.high < self.low)

    def evidence(self) -> dict:
        return {
            "low": _frac_text(self.low),
            "high": _frac_text(self.high),
            "low_boundary": self.low_boundary.evidence(),
            "high_boundary": self.high_boundary.evidence(),
        }


@dataclass(frozen=True)
class LineWindows:
    """一条试吹线（吹响或收气）上的全部合格闭区间。"""

    line: str
    target: Fraction
    tolerance: Fraction
    windows: tuple[Window, ...]

    @property
    def feasible(self) -> bool:
        return bool(self.windows)


@dataclass(frozen=True)
class FingeringWindows:
    """一套指法在两条试吹线上的奏准音窗。"""

    fingering: int
    holes: int
    attack: LineWindows
    release: LineWindows

    @property
    def fingering_text(self) -> str:
        return format_fingering(self.fingering, self.holes)

    def line_windows(self, line: str) -> LineWindows:
        if line == ATTACK:
            return self.attack
        if line == RELEASE:
            return self.release
        raise ValueError(f"未知试吹线 {line!r}")


@dataclass(frozen=True)
class PitchReport:
    """制笛师所需的完整报告。"""

    target: Fraction
    tolerance: Fraction
    per_fingering: tuple[FingeringWindows, ...]

    def for_fingering(self, fingering: int) -> FingeringWindows:
        for fw in self.per_fingering:
            if fw.fingering == fingering:
                return fw
        raise KeyError(fingering)

    def render_text(self) -> str:
        lines = [
            f"目标音高 {_frac_text(self.target)}，容差 ±{_frac_text(self.tolerance)}"
            "（闭区间，压线算合格）",
        ]
        for fw in self.per_fingering:
            lines.append(f"  指法 {fw.fingering_text}")
            for lw in (fw.attack, fw.release):
                name = "吹响线" if lw.line == ATTACK else "收气线"
                if not lw.windows:
                    lines.append(f"    {name}: 无合格压力区间")
                    continue
                for w in lw.windows:
                    lines.append(
                        f"    {name}: [{_frac_text(w.low)}, {_frac_text(w.high)}]  "
                        f"低端={_boundary_text(w.low_boundary)} "
                        f"高端={_boundary_text(w.high_boundary)}"
                    )
        return "\n".join(lines)


def _frac_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _boundary_text(b: Boundary) -> str:
    where = {DATA: "实测点", INTERIOR: "插值段内"}[b.kind]
    name = "吹响" if b.line == ATTACK else "收气"
    tol = "（压线）" if b.on_tolerance else ""
    return f"{name}{where} p={_frac_text(b.pressure)} 音高={_frac_text(b.pitch)}{tol}"


def _clip_affine(
    lo_p: Fraction,
    hi_p: Fraction,
    lo_r: Fraction,
    hi_r: Fraction,
    low_note: Fraction,
    high_note: Fraction,
) -> tuple[Fraction, Fraction] | None:
    """在插值段 ``p in [lo_p, hi_p]`` 上把音高夹到 ``[low_note, high_note]``。

    音高是气压的仿射函数（线性插值）。对单调段直接用端点；对水平段
    （常数音高）整段要么全合格要么全不合格。返回气压上的闭子区间。
    """
    if lo_r == hi_r:
        return (lo_p, hi_p) if low_note <= lo_r <= high_note else None

    def invert(note: Fraction) -> Fraction:
        # pitch(p) = lo_r + (note - lo_r) * (hi_p - lo_p) / (hi_r - lo_r)
        return lo_p + (note - lo_r) * (hi_p - lo_p) / (hi_r - lo_r)

    p_at_low_note = invert(low_note)
    p_at_high_note = invert(high_note)
    cand_low = min(p_at_low_note, p_at_high_note)
    cand_high = max(p_at_low_note, p_at_high_note)
    low = max(lo_p, cand_low)
    high = min(hi_p, cand_high)
    if low > high:
        return None
    return low, high


def _boundary_kind(pressure: Fraction, lo_p: Fraction, hi_p: Fraction) -> str:
    if pressure == lo_p or pressure == hi_p:
        return DATA
    return INTERIOR


def line_windows(line: PressureLine, target: Fraction, tolerance: Fraction) -> LineWindows:
    """求一条试吹线上所有奏准闭区间（不跨缺测档）。"""
    if tolerance < 0:
        raise ValueError("容差必须非负")
    low_note, high_note = target - tolerance, target + tolerance
    windows: list[Window] = []

    def add(p_low: Fraction, p_high: Fraction, segment: tuple[int, int] | None) -> None:
        pitch_low = line.pitch_at(p_low)
        pitch_high = line.pitch_at(p_high)
        low_kind = DATA if segment is None else _boundary_kind(p_low, line.levels[segment[0]], line.levels[segment[1]])
        high_kind = DATA if segment is None else _boundary_kind(p_high, line.levels[segment[0]], line.levels[segment[1]])
        windows.append(
            Window(
                low=p_low,
                high=p_high,
                low_boundary=Boundary(
                    p_low, pitch_low, low_kind, line.kind, segment,
                    on_tolerance=pitch_low in (low_note, high_note),
                ),
                high_boundary=Boundary(
                    p_high, pitch_high, high_kind, line.kind, segment,
                    on_tolerance=pitch_high in (low_note, high_note),
                ),
            )
        )

    # 孤立实测点：单档定压也能给出一个退化闭区间 [p, p]。
    for i in line.isolated_points():
        if low_note <= line.readings[i] <= high_note:
            add(line.levels[i], line.levels[i], None)

    # 相邻实测点构成的插值段。
    for lo_i, hi_i in line.segments():
        lo_p, hi_p = line.levels[lo_i], line.levels[hi_i]
        lo_r, hi_r = line.readings[lo_i], line.readings[hi_i]
        clipped = _clip_affine(lo_p, hi_p, lo_r, hi_r, low_note, high_note)
        if clipped is not None:
            add(clipped[0], clipped[1], (lo_i, hi_i))

    # 合并同线内首尾相接（共享端点）的区间，保留各自边界证据。
    merged = _merge_windows(windows)
    return LineWindows(line.kind, target, tolerance, tuple(merged))


def _merge_windows(windows: list[Window]) -> list[Window]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w.low, w.high))
    out: list[Window] = [ordered[0]]
    for w in ordered[1:]:
        last = out[-1]
        if w.low <= last.high:  # 闭区间：端点相接即合并
            out[-1] = Window(
                low=last.low,
                high=max(last.high, w.high),
                low_boundary=last.low_boundary,
                high_boundary=w.high_boundary if w.high >= last.high else last.high_boundary,
            )
        else:
            out.append(w)
    return out


def fingering_windows(
    table: FingeringTable, holes: int, target: Fraction, tolerance: Fraction
) -> FingeringWindows:
    return FingeringWindows(
        fingering=table.fingering,
        holes=holes,
        attack=line_windows(table.attack, target, tolerance),
        release=line_windows(table.release, target, tolerance),
    )


def pitch_report(instrument: Instrument, target: Fraction, tolerance: Fraction) -> PitchReport:
    """制笛师主入口：列出每套指法在两条线上的合格闭压力区间。"""
    return PitchReport(
        target=target,
        tolerance=tolerance,
        per_fingering=tuple(
            fingering_windows(table, instrument.holes, target, tolerance)
            for table in instrument.fingerings
        ),
    )


def display_probe(
    pressure: Fraction,
    line: PressureLine,
    target: Fraction,
    tolerance: Fraction,
    decimals: int,
) -> dict:
    """读谱台探针：短十进制显示与精确合格判定分开，显示不能洗白音高。

    返回精确音高、短十进制显示、精确误差与合格结论；当短十进制看上去
    等于目标（或落在容差内）但精确音高在窗外时，``whitewashed`` 置真。
    """
    pressure = Fraction(pressure)
    pitch = line.pitch_at(pressure)
    if pitch is None:
        raise ValueError("该气压在本条试吹线上没有定义（缺测或落在缺测档之间）")
    exact_ok = abs(pitch - target) <= tolerance
    shown = short_decimal(pitch, decimals)
    shown_value = Fraction(shown) / Fraction(10 ** decimals)
    looks_ok = abs(shown_value - target) <= tolerance
    return {
        "pressure": pressure,
        "exact_pitch": pitch,
        "exact_error": pitch - target,
        "shown": f"{shown} / 10^{decimals}",
        "shown_value": shown_value,
        "exact_qualified": exact_ok,
        "display_looks_qualified": looks_ok,
        # 窗外音高不会因为短十进制而变成合格。
        "whitewashed": looks_ok and not exact_ok,
    }
