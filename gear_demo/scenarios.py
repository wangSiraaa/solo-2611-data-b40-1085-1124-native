"""题面列出的具体装配案例工厂。

每个工厂返回可复现的规格对象；测试与 CLI 演示直接消费它们。
"""

from fractions import Fraction

from .bench import (
    BenchSpec,
    BenchShaft,
    SegmentPlan,
    ShiftRequest,
    Sleeve,
)
from .kinematics import (
    EXTERNAL,
    INTERNAL,
    Differential,
    Mesh,
    Observation,
    Segment,
)
from .tacho import Disc


# ============================================================ 测具岗（运动学）
def differential_balanced():
    """两侧轴速 1、−1，差动座对静止轴。座速 = (1 + −1)/2 = 0，与静止端同速。"""
    return Segment(
        name="diff_balanced",
        shafts=["L", "R", "C", "S"],
        differentials=[Differential(cage="C", left="L", right="R")],
        observations=[
            Observation("L", Fraction(1)),
            Observation("R", Fraction(-1)),
            Observation("S", Fraction(0)),
        ],
    )


def differential_same_sign():
    """仅把负向输入改为 +1：座速 = (1 + 1)/2 = 1，与静止端 0 不同速，合不上。"""
    return Segment(
        name="diff_same_sign",
        shafts=["L", "R", "C", "S"],
        differentials=[Differential(cage="C", left="L", right="R")],
        observations=[
            Observation("L", Fraction(1)),
            Observation("R", Fraction(1)),
            Observation("S", Fraction(0)),
        ],
    )


def differential_inputs_free():
    """恢复轴速但**不**钉死两端读数：座速与两端是否必同速？用于“必同速”判定。"""
    return Segment(
        name="diff_inputs_free",
        shafts=["L", "R", "C"],
        differentials=[Differential(cage="C", left="L", right="R")],
    )


def two_gears_fight_one_sleeve():
    """两套轮经同输入驱动、比值不同，争同一输出侧的同速要求。

    In 经外啮合分别带 A（10:20）与 B（10:30）；若 A、B 须装到同一滑座套上，
    则要求 ω_A=ω_B ⇒ −In/2 = −In/3 ⇒ In=0。In 允许非零时，二者永远抢不上同速。
    """
    return Segment(
        name="two_gears_fight",
        shafts=["In", "A", "B"],
        meshes=[
            Mesh("In", "A", 10, 20, EXTERNAL),
            Mesh("In", "B", 10, 30, EXTERNAL),
        ],
        observations=[Observation("In", Fraction(1))],
    )


def internal_mesh_pair():
    """内啮合同向：ω_b = +(z_a/z_b)·ω_a；配合一个外啮合构成混合链。"""
    return Segment(
        name="mixed_mesh",
        shafts=["A", "B", "C"],
        meshes=[
            Mesh("A", "B", 12, 24, INTERNAL),   # 同向，B = A/2
            Mesh("B", "C", 8, 16, EXTERNAL),    # 反向，C = -B/2
        ],
        observations=[Observation("A", Fraction(1))],
    )


# ============================================================ 演示台
def bench_basic():
    """基本演示台：两轴 + 两套，四刻一圈，首个窗内同速同相即合。

    段 0（3 拍，A=B=C=1）先合 sAB；随后若要进入段 1（A=1,B=C=2）必须先在准许窗
    脱开 sAB，再在段 1 的窗内合 sBC。
    """
    return BenchSpec(
        shafts=[BenchShaft("A", 0), BenchShaft("B", 0), BenchShaft("C", 0)],
        sleeves=[
            Sleeve("sAB", "seat1", "A", "B", phase_mod=4),
            Sleeve("sBC", "seat2", "B", "C", phase_mod=4),
        ],
        segments=[
            SegmentPlan("seg0", {"A": 1, "B": 1, "C": 1}, beats=3),
            SegmentPlan("seg1", {"A": 1, "B": 2, "C": 2}, beats=2),
        ],
        requests=[
            ShiftRequest("req_ab", "sAB", "engage", [0, 1]),
            ShiftRequest("req_ab_off", "sAB", "disengage", [2]),
            ShiftRequest("req_bc", "sBC", "engage", [4]),
        ],
        ticks_per_rev=4,
    )


def bench_basic_no_disengage():
    """同 :func:`bench_basic` 但删去脱开请求：后续异速段应被判违规。"""
    spec = bench_basic()
    spec.requests = [r for r in spec.requests if r.sleeve_id != "sAB"
                     or r.action != "disengage"]
    return spec


def bench_two_sleeves_one_seat():
    """两套共抢一只滑座：sPQ 先在窗内合，须先脱开，sPR 才能在后续窗抢到同一座。"""
    return BenchSpec(
        shafts=[BenchShaft("P", 0), BenchShaft("Q", 0), BenchShaft("R", 0)],
        sleeves=[
            Sleeve("sPQ", "shared", "P", "Q", phase_mod=4),
            Sleeve("sPR", "shared", "P", "R", phase_mod=4),
        ],
        segments=[
            SegmentPlan("seg0", {"P": 1, "Q": 1, "R": 1}, beats=2),
            SegmentPlan("seg1", {"P": 1, "Q": 0, "R": 1}, beats=2),
        ],
        requests=[
            ShiftRequest("req_pq", "sPQ", "engage", [0, 1]),
            ShiftRequest("req_pq_off", "sPQ", "disengage", [1]),
            ShiftRequest("req_pr", "sPR", "engage", [2, 3]),
        ],
        ticks_per_rev=4,
    )


def bench_two_sleeves_one_seat_clash():
    """两套共抢一只滑座但缺脱开：sPQ 未脱开前 sPR 抢不到座，应判违规/未兑现。"""
    spec = bench_two_sleeves_one_seat()
    spec.requests = [r for r in spec.requests if r.action != "disengage"]
    return spec


def bench_phase_off_by_tooth():
    """静止端初位差一齿（1 刻）：四刻一圈，永远不同位，窗内合不上。"""
    return BenchSpec(
        shafts=[BenchShaft("A", 0), BenchShaft("B", 1)],
        sleeves=[Sleeve("sAB", "seat1", "A", "B", phase_mod=4)],
        segments=[SegmentPlan("seg0", {"A": 1, "B": 1}, beats=6)],
        requests=[ShiftRequest("req_ab", "sAB", "engage", list(range(6)))],
        ticks_per_rev=4,
    )


# ----------------------- 差动座演示（四轴：L、R、座 C、静止端 S） -----------
def bench_differential_balanced():
    """两侧 1、−1：座速 (1−1)/2 = 0 = 静止端，初相位同为 0，第一拍末唯一窗口可合。"""
    return BenchSpec(
        shafts=[BenchShaft("L", 0), BenchShaft("R", 0),
                BenchShaft("C", 0), BenchShaft("S", 0)],
        sleeves=[Sleeve("sCS", "seat1", "C", "S", phase_mod=4)],
        segments=[SegmentPlan("seg0", {"L": 1, "R": -1, "C": 0, "S": 0}, beats=3)],
        requests=[ShiftRequest("req_cs", "sCS", "engage", [0])],
        ticks_per_rev=4,
    )


def bench_differential_same_sign():
    """仅把负向输入改为 +1：座速 (1+1)/2 = 1 ≠ 静止端 0，合不上。"""
    return BenchSpec(
        shafts=[BenchShaft("L", 0), BenchShaft("R", 0),
                BenchShaft("C", 0), BenchShaft("S", 0)],
        sleeves=[Sleeve("sCS", "seat1", "C", "S", phase_mod=4)],
        segments=[SegmentPlan("seg0", {"L": 1, "R": 1, "C": 1, "S": 0}, beats=3)],
        requests=[ShiftRequest("req_cs", "sCS", "engage", [0, 1, 2])],
        ticks_per_rev=4,
    )


def bench_differential_one_tooth():
    """恢复轴速（座速 0），仅把静止端初位改为相差一齿：四刻一圈永远不同位。"""
    return BenchSpec(
        shafts=[BenchShaft("L", 0), BenchShaft("R", 0),
                BenchShaft("C", 0), BenchShaft("S", 1)],
        sleeves=[Sleeve("sCS", "seat1", "C", "S", phase_mod=4)],
        segments=[SegmentPlan("seg0", {"L": 1, "R": -1, "C": 0, "S": 0}, beats=6)],
        requests=[ShiftRequest("req_cs", "sCS", "engage", list(range(6)))],
        ticks_per_rev=4,
    )


# ============================================================ 测速盘
def old_disc():
    """已标定旧盘：正向安装、零位对齐真齿。"""
    return Disc("old", orientation=1, zero_shift=0)


def new_disc_aligned():
    return Disc("new", orientation=1, zero_shift=0)


def new_disc_shifted():
    """新盘零位相对真齿错 2 刻（半圈）。"""
    return Disc("new", orientation=1, zero_shift=2)


def new_disc_flipped():
    """新盘反向安装。"""
    return Disc("new", orientation=-1, zero_shift=0)
