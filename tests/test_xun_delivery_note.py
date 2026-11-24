"""读谱台短十进制展示的精确偶舍入测试（展示不参与合格判定）。"""

from fractions import Fraction

from xun.delivery_note import render_short_decimal, round_half_even, short_decimal

F = Fraction


def test_half_even_rounding_exact():
    assert round_half_even(F(1, 2), 0) == 0   # .5 取偶
    assert round_half_even(F(3, 2), 0) == 2
    assert round_half_even(F(5, 2), 0) == 2
    assert round_half_even(F(1, 4), 0) == 0
    assert round_half_even(F(3, 4), 0) == 1


def test_short_decimal_coefficients_and_rendering():
    assert short_decimal(F(1, 300), 2) == 0          # 0.0033... -> 0.00
    assert short_decimal(F(1, 3), 2) == 33           # 0.333... -> 0.33
    assert short_decimal(-F(1, 3), 2) == -33
    assert render_short_decimal(F(1, 3), 2) == "0.33"
    assert render_short_decimal(F(7, 4), 2) == "1.75"
    assert render_short_decimal(F(2), 0) == "2"
    assert render_short_decimal(-F(1, 2), 1) == "-0.5"


def test_no_float_in_representative_boundary_case():
    # 二进制浮点里 0.1+0.2 != 0.3；精确有理路径不受影响。
    value = F(1, 10) + F(2, 10)
    assert value == F(3, 10)
    assert short_decimal(value, 2) == 30
