"""陶埙试音台：精确音窗、谱单可行性与尾声交付规划。

纯 Python 3.11 + 标准库（``fractions.Fraction``）。不引入宿主浮点：
线性插值、容差判定、气压方向与短十进制显示全部在有理数上完成。

模块边界：

* :mod:`xun.model`        孔位/试吹线数据模型与精确插值；
* :mod:`xun.windows`      奏准音窗（闭压力区间 + 边界支撑证据）；
* :mod:`xun.phrase`       谱单逐拍可行性（方向选线、连音限动、休止重置）；
* :mod:`xun.coda`         指挥尾声规划（实牌暗号 / 整拍静默的最小代价）；
* :mod:`xun.delivery_note` 读谱台短十进制展示（只展示，不参与判定）。
"""

from .coda import (
    CardLibrary,
    Coda,
    InfeasibleCoda,
    Plan,
    PlannerCase,
    SilenceSlot,
    describe_cost,
    evaluate_plan,
    plan_minimum,
)
from .delivery_note import render_short_decimal, round_half_even, short_decimal
from .model import (
    ATTACK,
    RELEASE,
    FingeringTable,
    Instrument,
    PressureLine,
    format_fingering,
    make_instrument,
    parse_fingering,
    popcount_changed,
)
from .phrase import Beat, Phrase, PhraseResult, solve_phrase
from .windows import (
    Boundary,
    FingeringWindows,
    LineWindows,
    PitchReport,
    Window,
    display_probe,
    fingering_windows,
    line_windows,
    pitch_report,
)

__all__ = [
    "ATTACK",
    "RELEASE",
    "FingeringTable",
    "Instrument",
    "PressureLine",
    "format_fingering",
    "make_instrument",
    "parse_fingering",
    "popcount_changed",
    "Boundary",
    "FingeringWindows",
    "LineWindows",
    "PitchReport",
    "Window",
    "display_probe",
    "fingering_windows",
    "line_windows",
    "pitch_report",
    "Beat",
    "Phrase",
    "PhraseResult",
    "solve_phrase",
    "CardLibrary",
    "Coda",
    "InfeasibleCoda",
    "Plan",
    "PlannerCase",
    "SilenceSlot",
    "describe_cost",
    "evaluate_plan",
    "plan_minimum",
    "render_short_decimal",
    "round_half_even",
    "short_decimal",
]
