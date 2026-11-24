"""尾声交付规划：知识分区、实牌暗号、静默放手与物理不可交的测试。"""

from fractions import Fraction

import pytest

from xun.coda import (
    CardLibrary,
    Coda,
    InfeasibleCoda,
    PlannerCase,
    SilenceSlot,
    describe_cost,
    evaluate_plan,
    plan_minimum,
)
from xun.model import ATTACK, make_instrument
from xun.phrase import Beat

F = Fraction


def _fork_instrument(include_11=True):
    if include_11:
        levels, readings = [1], [F(1)]
    else:
        # 撤掉 11 在目标档的实测音窗：p=1 缺测，远端 p=9 仅保留不相关读数。
        levels, readings = [1, 9], [None, F(99)]
    return make_instrument(
        2,
        {
            "00": {"attack": [[1], [F(1)]], "release": [[1], [F(1)]]},
            "11": {"attack": [levels, readings], "release": [levels, readings]},
        },
    )


def _case(include_11=True):
    beats = (
        Beat(note=F(1), pressure=F(1), allowed=frozenset({0b00, 0b11})),
        Beat(note=F(1), pressure=F(1), movable_mask=0),
    )
    codas = (
        Coda("甲尾", frozenset({0b00}), reveal_gap=1, restricted_beats=frozenset({1})),
        Coda("乙尾", frozenset({0b11}), reveal_gap=1, restricted_beats=frozenset({1})),
    )
    return PlannerCase(
        instrument=_fork_instrument(include_11),
        beats=beats,
        codas=codas,
        first_line=ATTACK,
        tolerance=F(0),
        signal_gaps=frozenset({0, 1}),
        silence_gaps=frozenset({1, 2}),
        lock_after_beat=1,
    )


def test_baseline_without_signal_or_silence_is_infeasible():
    plan = evaluate_plan(_case(), None, 0, None, ())
    assert not plan.feasible


def test_one_bit_card_before_first_beat_delivers_both_codas():
    case = _case()
    plan = evaluate_plan(case, 0, 1, (0, 1), ())
    assert plan.feasible and plan.signal_bits == 1 and plan.signal_gap == 0
    minimum = plan_minimum(case)
    assert minimum["card"].feasible and minimum["card"].signal_bits == 1
    assert "首拍之前" in describe_cost(minimum["card"], case)


def test_silence_after_reveal_resets_hand_and_needs_window():
    case = _case()
    plan = evaluate_plan(case, None, 0, None, (SilenceSlot(1, True),))
    assert plan.feasible
    assert plan.silences[0].gap == 1
    assert plan.silences[0].window_extension_required is True
    minimum = plan_minimum(case)
    assert minimum["silence"].feasible
    assert minimum["silence"].silences[0].window_extension_required is True


def test_late_silence_after_locked_final_cannot_save_anything():
    case = _case()
    plan = evaluate_plan(case, None, 0, None, (SilenceSlot(2, True),))
    assert not plan.feasible


def test_removing_11_window_makes_coda_physically_impossible():
    case = _case(include_11=False)
    with pytest.raises(InfeasibleCoda) as exc:
        plan_minimum(case)
    assert str(exc.value) == "乙尾"
    # 给早牌也救不回来。
    with pytest.raises(InfeasibleCoda):
        evaluate_plan(case, 0, 1, (0, 1), ())
    # 挪出静默拍同样救不回来。
    with pytest.raises(InfeasibleCoda):
        evaluate_plan(case, None, 0, None, (SilenceSlot(1, True),))


def test_card_library_one_card_one_team_per_show():
    library = CardLibrary("牌-柒")
    lent = library.lend("甲队")
    assert lent.borrower == "甲队"
    with pytest.raises(RuntimeError):
        lent.lend("乙队")
    # 同队重放是幂等的；归还后可再借。
    assert lent.lend("甲队").borrower == "甲队"
    assert lent.take_back().borrower is None
    assert lent.take_back().lend("乙队").borrower == "乙队"


def test_signal_cannot_arrive_outside_authorized_gap():
    case = _case()
    # 间隙 2 没有授权给牌；规划器只会在 signal_gaps 内寻找，故最小路线仍只
    # 能落在授权空拍 0 或 1。
    minimum = plan_minimum(case)
    assert minimum["card"].signal_gap in case.signal_gaps


def test_assignments_are_distinct_so_each_coda_gets_unique_signal():
    from xun.coda import _assignments

    codes = _assignments(2, 1)
    assert (0, 1) in codes and (1, 0) in codes
    # 一位暗号最多区分两支尾声；三支需要两位（4 个码取 3 的排列 P(4,3)=24）。
    assert _assignments(3, 1) == ()
    assert len(_assignments(3, 2)) == 24
