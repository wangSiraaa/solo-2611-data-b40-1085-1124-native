"""烘焙展盲评、颁章与复尝排位系统（构建在旧 ``budget_solver`` 的精确有理数核之上）。

分层：

* ``scores``     —— 轮次记分规则（整席回避 → 每家一去高去低 → 两人取均值 → 一人以下重尝）
* ``cards``      —— 原公式快照 / 留样卡：精确线性方程 + 整数盒域，联合补卡与同表核对
* ``award``      —— 颁章判定：每份合法补卡是否选出同一严格最高者，候选与支撑补卡
* ``scheduling`` —— 复尝机位：时段 + 容量 + 隔离标签 + 到场时窗，最少位覆盖
* ``appeal``     —— 迟来回避 / 申诉：先重判原始有效票，再为仍有资格者排有效复尝

所有数值仍是 :class:`fractions.Fraction`，不引入浮点；持久化复用
``budget_solver.util`` 的原子写与内容哈希。
"""

from .scores import (
    trimmed_mean,
    round_supplier_means,
    weighted_totals,
    validate_session,
)
from .cards import CardSystem, Record, VarSpec, CardError
from .award import decide_award
from .scheduling import Slot, Sample, plan_retaste
from .appeal import (
    Session,
    Booking,
    resolve_delayed_recusal,
    who_may_consume,
    viability,
    PENDING_POLICY,
)

__all__ = [
    "trimmed_mean",
    "round_supplier_means",
    "weighted_totals",
    "validate_session",
    "CardSystem",
    "Record",
    "VarSpec",
    "CardError",
    "decide_award",
    "Slot",
    "Sample",
    "plan_retaste",
    "Session",
    "Booking",
    "resolve_delayed_recusal",
    "who_may_consume",
    "viability",
    "PENDING_POLICY",
]
