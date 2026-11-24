"""Exact tests for axis/span pairing against per-inch closed intervals."""

from fractions import Fraction

import pytest

from obsdelivery.errors import ValidationError
from obsdelivery.ropes import (
    HEAD,
    TAIL,
    Axis,
    Interval,
    Span,
    evaluate_pair,
    make_intervals,
)


def axis(name, residuals):
    return Axis.from_readings(name, name, residuals)


ZERO2 = [Interval(Fraction(0), Fraction(0)), Interval(Fraction(0), Fraction(0))]


def test_jia_yi_head_to_head_passes():
    jia = Axis.from_cumulative("jia", "JY", [0, 2, 1])
    yi = Axis.from_cumulative("yi", "YY", [0, -2, -1])
    ev = evaluate_pair(jia, Span(0, 2, HEAD), yi, Span(0, 2, HEAD), ZERO2)
    assert ev.ok
    assert [r.total for r in ev.rows] == [Fraction(0), Fraction(0)]


def test_middle_change_keeps_total_but_fails_per_inch():
    jia = Axis.from_cumulative("jia", "JY", [0, 2, 1])
    yi_bad = Axis.from_cumulative("yi", "YY", [0, -1, -1])  # residuals [-1,0]
    # whole-length totals still cancel (Jia total +1, Yi total -1)
    assert sum(jia.residuals) + sum(yi_bad.residuals) == 0
    ev = evaluate_pair(jia, Span(0, 2, HEAD), yi_bad, Span(0, 2, HEAD), ZERO2)
    assert not ev.ok
    assert [r.ok for r in ev.rows] == [False, False]


def test_tail_attachment_reverses_order_and_fails():
    jia = Axis.from_cumulative("jia", "JY", [0, 2, 1])
    yi = Axis.from_cumulative("yi", "YY", [0, -2, -1])
    ev = evaluate_pair(jia, Span(0, 2, HEAD), yi, Span(0, 2, TAIL), ZERO2)
    assert not ev.ok
    # reversed Yi [-2,1] -> [1,-2]; paired with [2,-1] gives [3,-3]
    assert [r.total for r in ev.rows] == [Fraction(3), Fraction(-3)]


def test_tail_reversal_only_flips_segment_order():
    a = axis("A", [1, 2, 3])
    head = Span(1, 2, HEAD).slice(a)
    tail = Span(1, 2, TAIL).slice(a)
    assert head == (Fraction(2), Fraction(3))
    assert tail == (Fraction(3), Fraction(2))


def test_closed_interval_endpoints_inclusive():
    a = axis("A", [1])
    b = axis("B", [1])
    # sum 2; interval [1,2] and [2,3] both include 2
    ev = evaluate_pair(a, Span(0, 1), b, Span(0, 1), [Interval(Fraction(1), Fraction(2))])
    assert ev.ok
    ev2 = evaluate_pair(a, Span(0, 1), b, Span(0, 1), [Interval(Fraction(2), Fraction(3))])
    assert ev2.ok
    # [0,1] excludes 2 by the high edge
    ev3 = evaluate_pair(a, Span(0, 1), b, Span(0, 1), [Interval(Fraction(0), Fraction(1))])
    assert not ev3.ok


def test_same_axis_pairing_refused():
    a = axis("A", [1, 2])
    with pytest.raises(ValidationError):
        evaluate_pair(a, Span(0, 1), a, Span(1, 1), [Interval(Fraction(0), Fraction(0))])


def test_length_mismatch_and_interval_count():
    a = axis("A", [1, 2])
    b = axis("B", [1])
    with pytest.raises(ValidationError):
        evaluate_pair(a, Span(0, 2), b, Span(0, 1), ZERO2)
    with pytest.raises(ValidationError):
        evaluate_pair(a, Span(0, 2), b, Span(0, 2), ZERO2)  # b too short


def test_span_exceeds_axis():
    a = axis("A", [1, 2])
    with pytest.raises(ValidationError):
        Span(1, 2).slice(a)


def test_make_intervals_forms():
    ivs = make_intervals([["0", "0"], {"lo": "-1", "hi": "2"}, [Fraction(1, 2), 1]])
    assert ivs[0] == Interval(Fraction(0), Fraction(0))
    assert ivs[1] == Interval(Fraction(-1), Fraction(2))
    assert ivs[2] == Interval(Fraction(1, 2), Fraction(1))


def test_interval_validation():
    with pytest.raises(ValidationError):
        Interval(Fraction(2), Fraction(1))
    with pytest.raises(ValidationError):
        Span(0, 1, "sideways")
    with pytest.raises(ValidationError):
        Span(0, 0)
