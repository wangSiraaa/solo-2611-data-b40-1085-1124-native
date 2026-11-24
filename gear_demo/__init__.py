"""透明齿轮教具：运动学、演示台与测速盘维护。

本包把“整齿模型”里的线性转速关系、每段匀速的接轴表、观察读数编码成
:mod:`budget_solver` 的真实公式快照，再由演示台与测速盘模块把解算结果落成
逐拍换挡、锁合与校准流程。

模块划分：

* :mod:`gear_demo.kinematics`  传动链 → 线性方程快照；测具岗（必同速 / 反例读数）
* :mod:`gear_demo.bench`       演示台逐拍仿真：挡位、犬牙、滑座、换挡动作表
* :mod:`gear_demo.tacho`       测速盘换盘：定位工装（停一拍）/ 双盘联测两种安排
* :mod:`gear_demo.scenarios`   题面列出的具体装配案例工厂
"""

from .bench import (
    BenchResult,
    BenchSpec,
    BenchShaft,
    SegmentPlan,
    ShiftRequest,
    SimViolation,
    Sleeve,
    common_action_table,
    find_lockable_beats,
    simulate,
)
from .kinematics import (
    Differential,
    GaugeError,
    KinematicAnalysis,
    KinematicSolver,
    Mesh,
    NoLegalInterpretation,
    Observation,
    Segment,
)
from .tacho import (
    CalibrationEvidence,
    Disc,
    PAUSE_FIXTURE,
    DUAL_MEASURE,
    SwapReport,
    TachoSwap,
)

__all__ = [
    "BenchResult",
    "BenchSpec",
    "BenchShaft",
    "SegmentPlan",
    "ShiftRequest",
    "SimViolation",
    "Sleeve",
    "common_action_table",
    "simulate",
    "find_lockable_beats",
    "Differential",
    "GaugeError",
    "KinematicAnalysis",
    "KinematicSolver",
    "Mesh",
    "NoLegalInterpretation",
    "Observation",
    "Segment",
    "CalibrationEvidence",
    "Disc",
    "PAUSE_FIXTURE",
    "DUAL_MEASURE",
    "SwapReport",
    "TachoSwap",
]
