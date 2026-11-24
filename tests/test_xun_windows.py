"""奏准音窗：闭区间、压线、插值反解与短十进制不洗白的精确测试。"""

from fractions import Fraction

import pytest

from xun.model import make_instrument
from xun.windows import display_probe, line_windows, pitch_report

F = Fraction


def _instrument():
    # 00 吹响 1->2->3（线性），收气恒 2；01 吹响 1->2->4，收气 0->2->4。
    return make_instrument(
        2,
        {
            "00": {
                "attack": [[0, 1, 2], [1, 2, 3]],
                "release": [[0, 1, 2], [2, 2, 2]],
            },
            "01": {
                "attack": [[0, 1, 2], [1, 2, 4]],
                "release": [[0, 1, 2], [0, 2, 4]],
            },
        },
    )


def test_zero_tolerance_hits_data_point_and_interior_point():
    inst = _instrument()
    # 目标 2：00 吹响在实测档 p=1。
    r2 = pitch_report(inst, F(2), F(0)).for_fingering(0b00)
    w = r2.attack.windows
    assert len(w) == 1 and (w[0].low, w[0].high) == (F(1), F(1))
    assert w[0].low_boundary.kind == "data"
    assert w[0].low_boundary.on_tolerance is True
    # 目标 5/2：段 [1,2] 内部反解 p=3/2。
    r_half = pitch_report(inst, F(5, 2), F(0)).for_fingering(0b00)
    wh = r_half.attack.windows
    assert len(wh) == 1 and wh[0].low == wh[0].high == F(3, 2)
    assert wh[0].low_boundary.kind == "interior"
    assert wh[0].low_boundary.segment == (1, 2)
    assert wh[0].low_boundary.on_tolerance is True


def test_horizontal_line_makes_full_segment_qualified():
    inst = _instrument()
    r = pitch_report(inst, F(2), F(0)).for_fingering(0b00)
    w = r.release.windows
    assert len(w) == 1 and (w[0].low, w[0].high) == (F(0), F(2))
    assert w[0].low_boundary.kind == "data" and w[0].high_boundary.kind == "data"


def test_tolerance_band_merges_adjacent_segments_at_shared_data_point():
    inst = _instrument()
    # 01 吹响，目标 2、容差 1/3：
    # 段[0,1] pitch=1+p  -> p∈[2/3, 1]
    # 段[1,2] pitch=2p   -> p∈[1, 7/6]
    # p=1 相接合并；两端压线且在段内部。
    r = pitch_report(inst, F(2), F(1, 3)).for_fingering(0b01)
    w = r.attack.windows
    assert len(w) == 1 and (w[0].low, w[0].high) == (F(2, 3), F(7, 6))
    assert w[0].low_boundary.on_tolerance and w[0].high_boundary.on_tolerance
    assert w[0].low_boundary.kind == "interior"


def test_closed_boundary_qualifies_exactly_on_edge():
    inst = _instrument()
    # 00 吹响 pitch=1+p，目标 2、容差 1：下界音高 1 对应 p=0（实测点，压线）。
    r = pitch_report(inst, F(2), F(1)).for_fingering(0b00)
    w = r.attack.windows
    assert w[0].low == F(0) and w[0].low_boundary.on_tolerance
    # 恰好压线的点必须判合格。
    assert w[0].contains(F(0))


def test_missing_level_splits_windows_and_blocks_cross_interpolation():
    inst = make_instrument(
        1,
        {
            "0": {
                "attack": [[0, 1, 2], [1, None, 3]],
                "release": [[0, 1, 2], [1, None, 3]],
            },
        },
    )
    # 若错误跨缺测档插值，目标 2 会落在 p=1；正确行为是该点无定义、无音窗。
    r = pitch_report(inst, F(2), F(0)).for_fingering(0)
    assert r.attack.windows == ()
    # 两侧孤立点各自可判：目标 1 命中 p=0，目标 3 命中 p=2。
    assert line_windows(inst.table(0).attack, F(1), F(0)).windows[0].low == F(0)
    assert line_windows(inst.table(0).attack, F(3), F(0)).windows[0].low == F(2)


def test_short_decimal_display_cannot_whitewash_outside_pitch():
    inst = _instrument()
    # 01 收气 pitch=2p，p=1/600 -> 精确 1/300；两位短十进制显示 0.00。
    probe = display_probe(F(1, 600), inst.table(0b01).release, F(0), F(0), decimals=2)
    assert probe["exact_pitch"] == F(1, 300)
    assert probe["exact_qualified"] is False
    assert probe["display_looks_qualified"] is True
    assert probe["whitewashed"] is True
    # 合格点不会被误报。
    ok = display_probe(F(1), inst.table(0b01).release, F(2), F(0), decimals=2)
    assert ok["exact_qualified"] is True and ok["whitewashed"] is False


def test_negative_tolerance_rejected():
    inst = _instrument()
    with pytest.raises(ValueError):
        pitch_report(inst, F(1), Fraction(-1, 10))
