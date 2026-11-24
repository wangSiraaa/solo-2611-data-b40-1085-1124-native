"""试音台数据模型与精确线性插值的测试。"""

from fractions import Fraction

import pytest

from xun.model import (
    format_fingering,
    make_instrument,
    parse_fingering,
    popcount_changed,
)

F = Fraction


def test_fingering_roundtrip_and_move_count():
    assert parse_fingering("011") == 0b011
    assert format_fingering(0b011, 3) == "011"
    assert popcount_changed(0b000, 0b001) == 1
    assert popcount_changed(0b000, 0b011) == 2
    assert popcount_changed(0b101, 0b101) == 0
    with pytest.raises(ValueError):
        parse_fingering("")
    with pytest.raises(ValueError):
        parse_fingering("012")


def test_exact_linear_interpolation_between_adjacent_readings():
    inst = make_instrument(
        1,
        {
            "0": {
                "attack": [[0, 1, 2], [1, 2, 4]],
                "release": [[0, 1, 2], [4, 2, 1]],
            },
        },
    )
    line = inst.table(0).attack
    # 实测点原样返回。
    assert line.pitch_at(F(0)) == F(1)
    assert line.pitch_at(F(1)) == F(2)
    assert line.pitch_at(F(2)) == F(4)
    # 段 [0,1] 中点：1 + 1/2 = 3/2；段 [1,2] 四分之一点：2 + (4-2)/4 = 5/2。
    assert line.pitch_at(F(1, 2)) == F(3, 2)
    assert line.pitch_at(F(5, 4)) == F(5, 2)
    # 收气线是另一条曲线，同气压不同音高。
    assert inst.table(0).release.pitch_at(F(1, 2)) == F(3)
    assert inst.table(0).release.pitch_at(F(1, 2)) != line.pitch_at(F(1, 2))


def test_missing_level_is_never_crossed():
    inst = make_instrument(
        1,
        {
            "0": {
                "attack": [[0, 1, 2], [1, None, 5]],
                "release": [[0, 1, 2], [1, None, 5]],
            },
        },
    )
    line = inst.table(0).attack
    # 缺测档无定义；两侧都是孤立实测点，没有插值段。
    assert line.pitch_at(F(1)) is None
    assert line.segments() == ()
    assert line.isolated_points() == (0, 2)
    # 缺测档两侧之间的任何气压都不能被插值“跨越”。
    assert line.pitch_at(F(3, 2)) is None


def test_line_requires_strict_levels_and_a_reading():
    with pytest.raises(ValueError):
        make_instrument(1, {"0": {"attack": [[1, 1], [1, 2]],
                                  "release": [[1], [1]]}})
    with pytest.raises(ValueError):
        make_instrument(1, {"0": {"attack": [[0, 1], [None, None]],
                                  "release": [[0, 1], [1, 2]]}})


def test_instrument_rejects_duplicate_and_out_of_range_fingering():
    with pytest.raises(KeyError):
        make_instrument(1, {"0": {"attack": [[1], [1]], "release": [[1], [1]]}}).table(1)
    with pytest.raises(ValueError):
        make_instrument(
            1,
            {
                "00": {"attack": [[1], [1]], "release": [[1], [1]]},
            },
        )
