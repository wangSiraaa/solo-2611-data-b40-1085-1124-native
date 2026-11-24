"""谱单逐拍可行性：方向选线、连音限动、换气重置与定压的测试。"""

from fractions import Fraction

import pytest

from xun.model import ATTACK, RELEASE, make_instrument
from xun.phrase import Beat, Phrase, solve_phrase

F = Fraction


def _three_hole():
    return make_instrument(
        3,
        {
            "000": {
                "attack": [[0, 1, 2], [1, 2, 3]],
                "release": [[0, 1, 2], [2, F(3, 2), 1]],
            },
            "001": {
                "attack": [[0, 1, 2], [3, 4, 5]],
                "release": [[0, 1, 2], [2, F(3, 2), 1]],
            },
        },
    )


def test_rise_uses_attack_and_fall_uses_release():
    inst = _three_hole()
    # 拍0 吹响目标 2（p=1）；拍1 下降到 p=1/2 走收气线，目标 7/4。
    beats = (
        Beat(note=F(2), allowed=frozenset({0b000})),
        Beat(note=F(7, 4), allowed=frozenset({0b000}), forced_line=RELEASE),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK))
    assert result.feasible
    assert result.path[0].line == ATTACK and result.path[0].pressure == F(1)
    assert result.path[1].line == RELEASE and result.path[1].pressure == F(1, 2)


def test_slur_allows_only_one_movable_finger():
    inst = _three_hole()
    beats = (
        Beat(note=F(3), allowed=frozenset({0b000})),       # p=2
        Beat(note=F(5), allowed=frozenset({0b000, 0b001}), movable_mask=0b001),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK))
    assert result.feasible and result.path[1].fingering == 0b001
    # 同指位变化若被 mask 禁止，则无解。
    blocked = (
        beats[0],
        Beat(note=F(5), allowed=frozenset({0b000, 0b001}), movable_mask=0b010),
    )
    assert not solve_phrase(inst, Phrase(blocked, first_line=ATTACK)).feasible


def test_rest_releases_hand_and_resets_direction():
    inst = _three_hole()
    beats = (
        Beat(note=F(2), allowed=frozenset({0b000})),
        Beat(rest=True),
        # 休止后视同首拍：可直接用首拍方向（吹响）选任意允许指位。
        Beat(note=F(4), allowed=frozenset({0b001})),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK))
    assert result.feasible
    assert result.path[1] is None
    assert result.path[2].fingering == 0b001 and result.path[2].line == ATTACK


def test_fixed_pressure_equal_keeps_attack_line():
    inst = _three_hole()
    # 两拍都 p=2：持平走吹响线；000 目标 3、001 目标 5。
    beats = (
        Beat(note=F(3), pressure=F(2), allowed=frozenset({0b000})),
        Beat(note=F(5), pressure=F(2), allowed=frozenset({0b000, 0b001}),
             movable_mask=0b001),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK))
    assert result.feasible
    assert result.path[1].line == ATTACK and result.path[1].pressure == F(2)


def test_two_hole_single_level_retest_breaks_phrase():
    def build(pitch_01):
        return make_instrument(
            2,
            {
                "00": {"attack": [[1], [1]], "release": [[1], [1]]},
                "01": {"attack": [[1], [pitch_01]], "release": [[1], [pitch_01]]},
                "11": {"attack": [[1], [2]], "release": [[1], [2]]},
            },
        )

    beats = (
        Beat(note=F(1), pressure=F(1), allowed=frozenset({0b00})),
        Beat(note=F(2), pressure=F(1), allowed=frozenset({0b00, 0b01, 0b11}),
             movable_mask=0b01),
    )
    assert solve_phrase(build(F(2)), Phrase(beats, first_line=ATTACK)).feasible
    retest = solve_phrase(build(F(3, 2)), Phrase(beats, first_line=ATTACK))
    assert not retest.feasible and retest.failure_beat == 1


def test_first_beat_direction_follows_score():
    inst = _three_hole()
    # 首拍强制收气：000 收气 p=1/2 音高 7/4。
    beats = (Beat(note=F(7, 4), allowed=frozenset({0b000}), forced_line=RELEASE),)
    result = solve_phrase(inst, Phrase(beats, first_line=RELEASE))
    assert result.feasible and result.path[0].line == RELEASE


def test_phrase_length_capped_at_eight():
    beats = tuple(Beat(rest=True) for _ in range(9))
    with pytest.raises(ValueError):
        Phrase(beats, first_line=ATTACK)


def test_backtrack_revisits_first_beat_fingering_when_first_choice_dead_ends():
    # 回归：首拍排序第一的指位在第二拍走入死胡同时，求解器必须回溯到
    # 首拍的另一指位，而不是把后续失败误当成功或提前判否。
    inst = make_instrument(
        2,
        {
            "00": {"attack": [[0, 1, 2], [0, 1, 2]], "release": [[0, 1, 2], [2, 1, 0]]},
            "01": {"attack": [[0, 1, 2], [1, 2, 3]], "release": [[0, 1, 2], [3, 2, 1]]},
            "10": {"attack": [[0, 1, 2], [2, 3, 4]], "release": [[0, 1, 2], [4, 3, 2]]},
            "11": {"attack": [[0, 1, 2], [3, 4, 5]], "release": [[0, 1, 2], [5, 4, 3]]},
        },
    )
    all_fingerings = frozenset(range(4))
    # 目标 1 再 0：唯一通路是首拍 01@0、次拍 00@0（持平吹响、动右指）；
    # 首拍排序第一的 00@1 在次拍对音 0 无任何合法延续。
    beats = (
        Beat(note=F(1), allowed=all_fingerings),
        Beat(note=F(0), allowed=all_fingerings, movable_mask=0b11),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK, tolerance=F(0)))
    assert result.feasible
    assert result.path[0].fingering == 0b01 and result.path[0].pressure == F(0)
    assert result.path[1].fingering == 0b00 and result.path[1].pressure == F(0)
    assert result.path[1].line == ATTACK


def test_failure_reports_deepest_blocking_beat_not_shallow_one():
    # 首拍固定可演、次拍才无解时，失败拍必须报次拍（根因）而非首拍。
    inst = make_instrument(
        2,
        {
            "00": {"attack": [[1], [1]], "release": [[1], [1]]},
            "01": {"attack": [[1], [F(3, 2)]], "release": [[1], [F(3, 2)]]},
            "11": {"attack": [[1], [2]], "release": [[1], [2]]},
        },
    )
    beats = (
        Beat(note=F(1), pressure=F(1), allowed=frozenset({0b00})),
        Beat(note=F(2), pressure=F(1), allowed=frozenset({0b00, 0b01, 0b11}),
             movable_mask=0b01),
    )
    result = solve_phrase(inst, Phrase(beats, first_line=ATTACK))
    assert not result.feasible and result.failure_beat == 1
