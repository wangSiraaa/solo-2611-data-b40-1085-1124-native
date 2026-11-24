"""读谱台的短十进制显示（纯展示，从不参与合格判定）。

与主仓库 ``obsdelivery.delivery_algo`` 同一套精确偶舍入：返回整数系数
``coefficient``，显示值为 ``coefficient / 10**decimals``。展示层与判定层
严格分离——陶埙音高是否合格只看精确 ``Fraction`` 与容差。
"""

from __future__ import annotations

from fractions import Fraction


def round_half_even(value: Fraction, decimals: int) -> int:
    """``value * 10**decimals`` 的精确最近整数，恰好 .5 取偶。"""
    if decimals < 0:
        raise ValueError("decimals 必须非负")
    num = value.numerator * (10 ** decimals)
    den = value.denominator
    q, r = divmod(num, den)
    twice = 2 * r
    if twice < den:
        return q
    if twice > den:
        return q + 1
    return q if q % 2 == 0 else q + 1


def short_decimal(value: Fraction, decimals: int) -> int:
    """短十进制系数（带符号），显示时再除以 ``10**decimals``。"""
    if value < 0:
        return -round_half_even(-value, decimals)
    return round_half_even(value, decimals)


def render_short_decimal(value: Fraction, decimals: int) -> str:
    coefficient = short_decimal(value, decimals)
    if decimals == 0:
        return str(coefficient)
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient)).zfill(decimals + 1)
    return f"{sign}{digits[:-decimals]}.{digits[-decimals:]}"
