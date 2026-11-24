"""陶埙题本地复跑：音窗、短句、尾声分叉与短十进制探针。

运行::

    python3 -m xun.demo
"""

from __future__ import annotations

from fractions import Fraction

from .coda import (
    CardLibrary,
    Coda,
    InfeasibleCoda,
    PlannerCase,
    SilenceSlot,
    describe_cost,
    evaluate_plan,
    plan_minimum,
)
from .model import ATTACK, RELEASE, make_instrument
from .phrase import Beat, Phrase, solve_phrase
from .windows import display_probe, pitch_report

F = Fraction


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def demo_windows() -> None:
    section("A. 制笛师音窗：相邻实测点精确线性插值，不跨缺测档，压线合格")
    # 三档气压 0/1/2：
    # 000 吹响线 1 -> 2 -> 3：目标 2 在实测档 p=1，目标 5/2 落在段[1,2]内部
    # 插值点 p=3/2；另有一条带缺测档的线验证“不跨缺测档”；
    # 收气线 2 -> 2 -> 2 为水平线；001 吹响线 1 -> 2 -> 4。
    instrument = make_instrument(
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
    report = pitch_report(instrument, target=F(2), tolerance=F(0))
    print(report.render_text())

    fw00 = report.for_fingering(0b00)
    # 吹响线目标 2 是实测档 p=1（退化闭区间）。
    attack = fw00.attack.windows
    assert len(attack) == 1 and attack[0].low == F(1) and attack[0].high == F(1)
    assert attack[0].low_boundary.kind == "data"
    assert attack[0].low_boundary.line == ATTACK
    # 目标 5/2 在段 [1,2] 内部：pitch = 1+p，反解 p=3/2；压线点保留段内位置。
    half_report = pitch_report(instrument, target=F(5, 2), tolerance=F(0))
    inner = half_report.for_fingering(0b00).attack.windows
    assert len(inner) == 1 and inner[0].low == inner[0].high == F(3, 2)
    assert inner[0].low_boundary.kind == "interior"
    assert inner[0].low_boundary.on_tolerance is True
    assert inner[0].low_boundary.segment == (1, 2)  # 位置证据：插值段 [1,2]
    # 正容差区间两端贴容差界，同样带压线证据；区间端点在段内部。
    band_report = pitch_report(instrument, target=F(5, 2), tolerance=F(1, 4))
    band = band_report.for_fingering(0b00).attack.windows
    # pitch=1+p，合格 [9/4, 11/4] 对应 p∈[5/4, 7/4]，两端都在段[1,2]内部。
    assert len(band) == 1 and (band[0].low, band[0].high) == (F(5, 4), F(7, 4))
    assert band[0].low_boundary.on_tolerance and band[0].high_boundary.on_tolerance
    assert band[0].low_boundary.kind == "interior"
    # 收气线水平音高 2 整段合格，两个端点都是实测点。
    release = fw00.release.windows
    assert len(release) == 1 and (release[0].low, release[0].high) == (F(0), F(2))
    assert release[0].low_boundary.kind == "data" and release[0].high_boundary.kind == "data"

    # 压线：目标 2、容差 1/3，001 吹响线 1->2->4。
    # 段[0,1]: pitch=1+p，合格 p∈[2/3, 4/3]∩[0,1] = [2/3,1]；
    # 段[1,2]: pitch=2p，合格 p∈[5/6, 7/6]∩[1,2] = [1, 7/6]；
    # 两段在 p=1 相接，合并为 [2/3, 7/6]，压线边界带 tolerance 证据。
    tol_report = pitch_report(instrument, target=F(2), tolerance=F(1, 3))
    fw01 = tol_report.for_fingering(0b01)
    w = fw01.attack.windows
    assert len(w) == 1 and (w[0].low, w[0].high) == (F(2, 3), F(7, 6))
    assert w[0].low_boundary.on_tolerance and w[0].low_boundary.kind == "interior"
    assert w[0].high_boundary.on_tolerance and w[0].high_boundary.kind == "interior"
    print(f"\n压线合并区间: [{w[0].low}, {w[0].high}]，两端均压线（段内插值反解）")

    # 读谱台短十进制探针：精确窗外音高不会被显示洗白。
    # 001 收气线在 p=1/600 的精确音高 = 1/300，两位短十进制显示 0.00；
    # 对目标 0、零容差：显示看着合格（0.00-0=0），精确判定仍 OUTSIDE。
    probe = display_probe(F(1, 600), instrument.table(0b01).release, F(0), F(0), decimals=2)
    print("\n短十进制探针（目标 0、零容差）:")
    for key, value in probe.items():
        print(f"  {key}: {value}")
    assert probe["exact_qualified"] is False
    assert probe["display_looks_qualified"] is True
    assert probe["whitewashed"] is True

    # 缺测档：1 -> 缺测 -> 5。缺测档不允许跨越，两侧只是孤立实测点，
    # 目标 3（若强行跨段插值会落在 p=1）在本线上没有任何合格区间。
    gapped = make_instrument(
        2,
        {
            "00": {
                "attack": [[0, 1, 2], [1, None, 5]],
                "release": [[0, 1, 2], [1, None, 5]],
            },
        },
    )
    gap_report = pitch_report(gapped, target=F(3), tolerance=F(0))
    assert not gap_report.for_fingering(0b00).attack.windows
    assert gapped.table(0b00).attack.pitch_at(F(1)) is None  # 缺测档无定义
    print("\n缺测档校验：目标 3 无合格区间，p=1 音高为 None（不跨缺测档）")


def demo_two_hole_phrase() -> None:
    section("B. 单档定压两孔短句：精确 1 再 2，首拍固定 00，次拍只能动右指")

    def build(pitch_01):
        # 单档（气压恒为 1）两孔只准 00/01/11；试吹音高 1、pitch_01、2。
        return make_instrument(
            2,
            {
                "00": {
                    "attack": [[1], [1]],
                    "release": [[1], [1]],
                },
                "01": {
                    "attack": [[1], [pitch_01]],
                    "release": [[1], [pitch_01]],
                },
                "11": {
                    "attack": [[1], [2]],
                    "release": [[1], [2]],
                },
            },
        )

    beats = (
        Beat(note=F(1), pressure=F(1), allowed=frozenset({0b00}), label="首拍固定00"),
        Beat(note=F(2), pressure=F(1), allowed=frozenset({0b00, 0b01, 0b11}),
             movable_mask=0b01, label="次拍只动右指"),
    )

    ok = solve_phrase(build(F(2)), Phrase(beats, first_line=ATTACK))
    print("原始测音（00/01/11 = 1/2/2）:")
    print(ok.render(2))
    assert ok.feasible
    assert ok.path[1].fingering == 0b01

    retest = solve_phrase(build(F(3, 2)), Phrase(beats, first_line=ATTACK))
    print("\n仅重测 01 成 3/2（11 仍准）:")
    print(retest.render(2))
    assert not retest.feasible  # 01 奏不准 2；11 要动两指（或动左指），接不过去

    # 恢复试吹，只锁住次拍右指（可动位仍只有右指）：同样接不上。
    locked = (
        beats[0],
        Beat(note=F(2), pressure=F(1), allowed=frozenset({0b00, 0b01, 0b11}),
             movable_mask=0b01),
    )
    relock = solve_phrase(build(F(2)), Phrase(locked, first_line=ATTACK))
    print("\n恢复试吹但次拍只锁右指:", relock.render(2))
    assert not relock.feasible or relock.path[1].fingering != 0b11


def demo_custom_phrase() -> None:
    section("C. 补句：中间档插值得音 + 下降收气线另有音高（三孔五拍，含换气）")
    # 气压档 0/1/2：
    # 000 吹响 1 -> 2 -> 3；收气 2 -> 3/2 -> 1（下降时另一条音高曲线）；
    # 001 吹响 3 -> 4 -> 5；收气与 000 同形（数据可不同，这里简化）。
    instrument = make_instrument(
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
    beats = (
        # 拍0：000 吹响，目标 2，p=1（实测档；该点也是后续插值的中间档）。
        Beat(note=F(2), allowed=frozenset({0b000}), label="吹响 2"),
        # 拍1：气压下降到 p=1/2，走收气线；收气音高 = 7/4（中间档插值），
        # 且收气线此点与吹响线音高不同，体现“另一条线另有音高”。
        Beat(note=F(7, 4), allowed=frozenset({0b000}), forced_line=RELEASE,
             label="收气 7/4（下降）"),
        # 拍2：换气休止，放手重置。
        Beat(rest=True, label="换气"),
        # 拍3：重置后 000 吹响目标 3（实测点 p=2）。
        Beat(note=F(3), allowed=frozenset({0b000}), label="重置后吹响 3"),
        # 拍4：连音动最右一指到 001，目标 5（001 吹响 p=2 实测点），
        # 气压与上一拍持平 → 仍走吹响线，只需动右指。
        Beat(note=F(5), allowed=frozenset({0b000, 0b001}), movable_mask=0b001,
             label="动右指到 001（持平吹响）"),
    )
    result = solve_phrase(instrument, Phrase(beats, first_line=ATTACK))
    print(result.render(3))
    assert result.feasible
    assert result.path[0].pressure == F(1) and result.path[0].line == ATTACK
    assert result.path[1].pressure == F(1, 2) and result.path[1].line == RELEASE
    assert result.path[2] is None
    assert result.path[4].fingering == 0b001 and result.path[4].pressure == F(2)
    # 收气线 p=1/2 的精确音高与吹响线不同：吹响 p=1/2 为 3/2，收气为 7/4。
    assert instrument.table(0b000).release.pitch_at(F(1, 2)) == F(7, 4)
    assert instrument.table(0b000).attack.pitch_at(F(1, 2)) == F(3, 2)


def _fork_instrument(include_11_window: bool = True):
    # 撤掉 11 的实测音窗：目标气压档 p=1 缺测，只在远端 p=9 留一个不相关
    # 合格读数（音高 99），保证试吹线本身合法、但目标音 1 的音窗为空。
    if include_11_window:
        levels_11, readings_11 = [1], [F(1)]
    else:
        levels_11, readings_11 = [1, 9], [None, F(99)]
    return make_instrument(
        2,
        {
            "00": {"attack": [[1], [F(1)]], "release": [[1], [F(1)]]},
            "11": {"attack": [levels_11, readings_11], "release": [levels_11, readings_11]},
        },
    )


def demo_coda_fork() -> None:
    section("D. 尾声分叉：首拍 00/11 同音，下一奏拍锁指，甲尾只准 00、乙尾只准 11")
    beats = (
        Beat(note=F(1), pressure=F(1), allowed=frozenset({0b00, 0b11}), label="共同首拍"),
        Beat(note=F(1), pressure=F(1), movable_mask=0, label="锁指末音"),
    )
    codas = (
        Coda("甲尾", frozenset({0b00}), reveal_gap=1, restricted_beats=frozenset({1})),
        Coda("乙尾", frozenset({0b11}), reveal_gap=1, restricted_beats=frozenset({1})),
    )

    def case_for(instrument):
        return PlannerCase(
            instrument=instrument,
            beats=beats,
            codas=codas,
            first_line=ATTACK,
            tolerance=F(0),
            signal_gaps=frozenset({0, 1}),       # 首拍前空拍、两拍之间的间隙
            silence_gaps=frozenset({1, 2}),      # 揭谱后间隙、末音之后
            lock_after_beat=1,
        )

    case = case_for(_fork_instrument(True))

    # 基线：不提前示意、不静默 -> 首拍无公共指位，不可交。
    base = evaluate_plan(case, None, 0, None, ())
    print("基线（无暗号无静默）:", "可交" if base.feasible else f"不可交（{base.failure_reason}）")
    assert not base.feasible

    plans = plan_minimum(case)
    print("\n最少暗号路线:", plans["card"].cost_text)
    print(describe_cost(plans["card"], case))
    print("最少静默路线:", plans["silence"].cost_text)
    print(describe_cost(plans["silence"], case))
    assert plans["card"].feasible and plans["card"].signal_bits == 1 and plans["card"].signal_gap == 0
    assert plans["silence"].feasible and plans["silence"].silences[0].gap == 1
    # 间奏静默必须由观众明确接受并把结束窗让出一拍。
    assert plans["silence"].silences[0].window_extension_required is True

    # 早牌等价：首拍前给牌，同一套两拍表即可，无需让窗、无需静默。
    early = evaluate_plan(case, 0, 1, (0, 1), ())
    assert early.feasible and not early.silences

    # 若把静默放到末音之后（间隙 2），它只能在两拍都锁死后才出现，救不回
    # 任何一版；让窗也无济于事。
    late_silence = evaluate_plan(case, None, 0, None, (SilenceSlot(2, True),))
    assert not late_silence.feasible
    assert late_silence.silences[0].window_extension_required is True
    print("末音之后静默：结束窗虽让出一拍，但锁指已成形，仍不可交")

    # 一张有号双面牌同场不能借两队。
    library = CardLibrary("牌-柒").lend("陶乐坊甲队")
    try:
        library.lend("陶乐坊乙队")
        raise AssertionError("应当拒绝同场转借")
    except RuntimeError as exc:
        print("\n实牌借用登记:", exc)

    # 撤掉 11 的实测音窗：乙尾物理不可交，给牌、挪拍都救不了。
    broken = case_for(_fork_instrument(False))
    try:
        plan_minimum(broken)
        raise AssertionError("应当报 InfeasibleCoda")
    except InfeasibleCoda as exc:
        print(f"撤掉 11 音窗后：尾声 {exc} 物理不可交，暗号/静默均无解")
        assert str(exc) == "乙尾"
    # 显式核验：即便给早牌也失败。
    try:
        evaluate_plan(broken, 0, 1, (0, 1), ())
        raise AssertionError("早牌不应救活乙尾")
    except InfeasibleCoda as exc:
        assert str(exc) == "乙尾"


def main() -> None:
    demo_windows()
    demo_two_hole_phrase()
    demo_custom_phrase()
    demo_coda_fork()
    print("\n全部本地复跑通过（精确有理数断言）。")


if __name__ == "__main__":
    main()
