"""以旧换新·旧打字机拆件回收（合成口径，不是现实会计或回收规范）。

模块划分：
- acceptance：验收台。跟踪新机内部发票对齐 + 两卖场真实外销 + 换购小票
  （现金/折价逐项、两台具名旧机）、钥匙存放、整单退机、应收挂账/收款结清、
  馆方入库（完成新机交付且约定现金入账）、按账头 + 交接件独立判定每台旧机
  可回收、跨卖场同名机身的双重认领拒绝。
- diagram：实机分解图。布局（格 -> 件）、相邻杆损毁、拆整副字篮、复装、
  工位占用、具名机型/键位/字篮订单、可交替的分解与整机交付。
- workshop：回收工场求解器。先求可完整兑现订单数，平手取工时最少，
  逐单给出真实领机、拆取次序、包内件、整机还是分解。
- convert：改单/预约状态机。首次不可逆拆取前的预接件单换整机单（含在途
  测试：只松螺帽可改；固定杆已剪断则旧“整机可交”报价作废），工位临时
  改整机窗、具名预约占位/释放、柜台等工位按现场状态签回、预算与工位冲突。
- audit：交叉核对甲机 Y-before-X 三视图（账头退回现金到账前、压条检验）。
"""

from .diagram import (
    CellLockError,
    Diagram,
    Item,
    Piece,
    WorkState,
)
from .workshop import (
    Assignment,
    Order,
    SolveResult,
    ScheduleError,
    Step,
    Workshop,
    build_order,
)
from .acceptance import AcceptanceDesk, ClaimError, InventoryGateError
from .convert import (
    Reservation,
    ReservationReuseError,
    SlotPool,
    Station,
    Ticket,
    ConversionError,
    POLICY_ALWAYS,
    POLICY_DEMAND,
    compare_policies,
)
from .audit import audit_jia_machine

__all__ = [
    "CellLockError",
    "Diagram",
    "Item",
    "Piece",
    "Step",
    "WorkState",
    "Assignment",
    "Order",
    "SolveResult",
    "ScheduleError",
    "Workshop",
    "build_order",
    "AcceptanceDesk",
    "ClaimError",
    "InventoryGateError",
    "Reservation",
    "ReservationReuseError",
    "Station",
    "Ticket",
    "ConversionError",
    "POLICY_ALWAYS",
    "POLICY_DEMAND",
    "compare_policies",
    "audit_jia_machine",
]
