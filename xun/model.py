"""陶埙试音台的精确数据模型（纯函数，无存储）。

孔位用二进制串表示（左 -> 右对应高位 -> 低位），例如三孔 ``"011"``。
每套开孔（一个固定指位）在若干档气压下各保留两条试吹线：

* ``attack`` 吹响线：气压上升或持平时使用；
* ``release`` 收气线：气压下降时使用。

每档读数要么是一个精确的有理音高（``Fraction``），要么是缺测（``None``）。
“冻结语义产品的合格读数”意味着本模块只接受已经定型的精确读数；全部计算
（含线性插值、容差判定）都在有理数上完成，绝不引入宿主浮点。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

# 两条试吹线的方向标识。
ATTACK = "attack"    # 吹响线
RELEASE = "release"  # 收气线
LINES = (ATTACK, RELEASE)

# 边界在试吹线上的位置：实测档 / 相邻实测点间的插值段内部。
DATA = "data"          # 边界落在实测档上
INTERIOR = "interior"  # 边界由相邻实测点间的精确线性插值反解得到


def parse_fingering(bits: str) -> int:
    """把二进制孔位串解析成整数指位（最左字符为最高位）。"""
    if not isinstance(bits, str) or not bits or any(ch not in "01" for ch in bits):
        raise ValueError(f"fingering 必须是非空二进制串，收到 {bits!r}")
    return int(bits, 2)


def format_fingering(fingering: int, holes: int) -> str:
    """整数指位格式化回定宽二进制串。"""
    if not (0 <= fingering < 1 << holes):
        raise ValueError(f"指位 {fingering} 超出 {holes} 孔范围")
    return format(fingering, f"0{holes}b")


def popcount_changed(a: int, b: int) -> int:
    """两个指位之间动了几根手指（异或后数 1）。"""
    return (a ^ b).bit_count()


def _to_fraction(value: object) -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, tuple) and len(value) == 2:
        return Fraction(value[0], value[1])
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"试吹读数必须是 Fraction/int/分数字符串/None，收到 {value!r}")


@dataclass(frozen=True)
class PressureLine:
    """一条试吹线在各气压档上的读数序列，``None`` 表示该档缺测。

    ``levels`` 是严格递增的非负有理气压档；``readings[i]`` 与
    ``levels[i]`` 一一对应。缺测档不参与插值，也绝不允许被跨越。
    """

    kind: str
    levels: tuple[Fraction, ...]
    readings: tuple[Fraction | None, ...]

    def __post_init__(self) -> None:
        if self.kind not in LINES:
            raise ValueError(f"未知试吹线 {self.kind!r}")
        if len(self.levels) != len(self.readings):
            raise ValueError("气压档与读数必须一一对应")
        if len(self.levels) < 1:
            raise ValueError("至少需要一档气压")
        prev = None
        for level in self.levels:
            level = _to_fraction(level)
            if prev is not None and level <= prev:
                raise ValueError("气压档必须严格递增")
            prev = level
        if not any(reading is not None for reading in self.readings):
            raise ValueError("一条试吹线至少要有一个合格读数")
        # dataclass(frozen=True) 下用 object.__setattr__ 做规范化。
        object.__setattr__(self, "levels", tuple(_to_fraction(v) for v in self.levels))
        object.__setattr__(
            self,
            "readings",
            tuple(_to_fraction(v) for v in self.readings),
        )

    @property
    def present_indices(self) -> tuple[int, ...]:
        return tuple(i for i, r in enumerate(self.readings) if r is not None)

    def segments(self) -> tuple[tuple[int, int], ...]:
        """返回可作精确线性插值的段：只连接“相邻实测点”。

        两个连续的非缺测下标构成一段；中间隔着缺测档则不连，即不跨缺测档。
        """
        idxs = self.present_indices
        return tuple((idxs[k], idxs[k + 1]) for k in range(len(idxs) - 1)
                     if idxs[k + 1] == idxs[k] + 1)

    def isolated_points(self) -> tuple[int, ...]:
        """没有任何相邻实测点的孤立合格读数（单档定压等情形）。"""
        idxs = set(self.present_indices)
        return tuple(i for i in sorted(idxs)
                     if (i - 1) not in idxs and (i + 1) not in idxs)

    def pitch_at(self, pressure: Fraction) -> Fraction | None:
        """精确求某气压下的音高；落在缺测区外或无定义时返回 ``None``。

        只有实测点本身，或相邻实测点之间的线性插值点才有定义。
        """
        pressure = _to_fraction(pressure)
        for i, level in enumerate(self.levels):
            if pressure == level:
                return self.readings[i]
        for lo_i, hi_i in self.segments():
            lo_p, hi_p = self.levels[lo_i], self.levels[hi_i]
            if lo_p < pressure < hi_p:
                lo_r, hi_r = self.readings[lo_i], self.readings[hi_i]
                t = (pressure - lo_p) / (hi_p - lo_p)
                return lo_r + t * (hi_r - lo_r)
        return None


@dataclass(frozen=True)
class FingeringTable:
    """一套开孔（一个指位）的吹响/收气两条试吹线。"""

    fingering: int
    attack: PressureLine
    release: PressureLine

    def line(self, kind: str) -> PressureLine:
        if kind == ATTACK:
            return self.attack
        if kind == RELEASE:
            return self.release
        raise ValueError(f"未知试吹线 {kind!r}")


@dataclass(frozen=True)
class Instrument:
    """一支合成陶埙：孔数、气压档与各指位的试音台原始位型。"""

    holes: int
    fingerings: tuple[FingeringTable, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.holes <= 6:
            raise ValueError("本库限定至多六孔")
        seen: set[int] = set()
        for table in self.fingerings:
            if not (0 <= table.fingering < 1 << self.holes):
                raise ValueError(f"指位 {table.fingering} 超出 {self.holes} 孔范围")
            if table.fingering in seen:
                raise ValueError(f"指位 {table.fingering} 重复")
            seen.add(table.fingering)

    @property
    def fingering_set(self) -> frozenset[int]:
        return frozenset(t.fingering for t in self.fingerings)

    def table(self, fingering: int) -> FingeringTable:
        for table in self.fingerings:
            if table.fingering == fingering:
                return table
        raise KeyError(f"本埙没有指位 {fingering} 的试音数据")

    def all_levels(self) -> tuple[Fraction, ...]:
        levels: set[Fraction] = set()
        for table in self.fingerings:
            for line in (table.attack, table.release):
                levels.update(line.levels)
        return tuple(sorted(levels))


def make_line(kind: str, levels: Iterable, readings: Iterable) -> PressureLine:
    return PressureLine(kind, tuple(levels), tuple(readings))


def make_instrument(holes: int, specs: dict[str, dict[str, list]]) -> Instrument:
    """便捷构造器。

    ``specs`` 形如::

        {
            "000": {
                "attack":  [[0, 1, 2], [1, None, 3]],   # [levels, readings]
                "release": [[0, 1, 2], [2, 2, 2]],
            },
            ...
        }
    """
    tables: list[FingeringTable] = []
    for bits, lines in specs.items():
        fingering = parse_fingering(bits)
        if len(bits) != holes:
            raise ValueError(f"{bits} 与孔数 {holes} 不一致")
        attack = make_line(ATTACK, lines["attack"][0], lines["attack"][1])
        release = make_line(RELEASE, lines["release"][0], lines["release"][1])
        tables.append(FingeringTable(fingering, attack, release))
    return Instrument(holes, tuple(sorted(tables, key=lambda t: t.fingering)))
