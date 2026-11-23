"""改单/预约状态机：预接件单何时换成整机单，预约何时占位/释放，代价如何明示。

核心规则（合成口径）：

- **首次不可逆拆取前**，一份预接的件单可换为整机单；**已发走或拆坏的实物**
  不能追认成整机。可复装的操作（如松螺帽）不算不可逆：复装另占格班时，
  已花班时照计。
- 柜台受理改单时，工位可能已松螺帽（可复装 -> 可改）或已剪断一根固定杆
  （不可逆 -> 改单拒绝，且旧的“整机可交”报价**必须失效**）。
- **临时改整机窗**内，订单台可给顾客发具名整机预约；预约**占掉一张稀缺的
  工位待单位**。挂预约期间工位**不得**在窗关闭或收到释放前做不可逆拆取。
- 也可以不卖该预约：工位先按原件单开工；此后柜台须等工位**按现场零件状态
  签回**才能接受改单；错过顾客期限则按原单继续。
- 改单仍须确认**其余已接件单都能交**（可行性复核）。
- 本模块把“留唯一待单位 vs 先开工”的**可交结果差异**显式输出（`compare_policies`），
  由交付做法明确这份代价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .diagram import Diagram, WorkState
from .workshop import Order, Workshop

# 两种柜台/订单台做法
POLICY_ALWAYS = "always_reserve"  # 卖预约：挂住唯一待单位，保证改整机
POLICY_DEMAND = "start_first"     # 不卖预约：先开工，之后凭签回改单


class ConversionError(Exception):
    """改单被拒或预约操作非法。"""


class ReservationReuseError(ConversionError):
    """预约已释放/已用后再次占用。"""


@dataclass
class Quote:
    """一张“整机可交”报价；实物不可逆后必须失效。"""

    ticket_id: str
    valid: bool = True
    note: str = ""

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.note = reason


@dataclass
class Ticket:
    """一份已接件单。"""

    order_id: str
    model: str
    key: Optional[str] = None
    kind: str = "bar"
    converted_to_whole: bool = False
    # 顾客改整机的期限（工位工时轴）；None 表示无期限
    convert_deadline: Optional[int] = None
    resolved_as: str = "original"  # original / whole

    def as_order(self) -> Order:
        return Order(self.order_id, self.model, self.kind, self.key)


@dataclass
class Station:
    """工位：在一台机的分解图上推进，可签回现场零件状态。"""

    body_id: str
    diagram: Diagram
    state: WorkState = field(default_factory=WorkState)
    signed_back: bool = False

    # --------------------------------------------------------- primitive ops
    def loosen_nut(self, piece_name: str) -> None:
        self._aval(piece_name, expect_kind="nut")
        self.state = self.diagram.apply_remove(self.state, piece_name)

    def pry_retainer(self, piece_name: str) -> None:
        self._aval(piece_name, expect_kind="retainer")
        self.state = self.diagram.apply_remove(self.state, piece_name)

    def take_bar(self, piece_name: str) -> None:
        self._aval(piece_name, expect_kind="bar")
        self.state = self.diagram.apply_remove(self.state, piece_name)

    def remove(self, piece_name: str) -> None:
        self.state = self.diagram.apply_remove(self.state, piece_name)

    def take_basket(self) -> None:
        self.state = self.diagram.apply_take_basket(self.state)

    def deliver_whole(self) -> None:
        self.state = self.diagram.apply_deliver_whole(self.state)

    def _aval(self, piece_name: str, expect_kind: str) -> None:
        piece = self.diagram.pieces.get(piece_name)
        if piece is None or piece.kind != expect_kind:
            raise ConversionError(f"{piece_name} 不是预期的 {expect_kind}")

    # ---------------------------------------------------------- sign back
    def sign_back(self) -> dict:
        """工位按现场零件状态签回给柜台。"""
        self.signed_back = True
        first_irr = self.diagram.first_irreversible(self.state)
        return {
            "body_id": self.body_id,
            "removed": sorted(self.state.removed),
            "damaged": sorted(self.state.damaged),
            "basket_taken": self.state.basket_taken,
            "whole_delivered": self.state.whole_delivered,
            "first_irreversible": first_irr,
            "shipped": [i.as_json() for i in self.state.produced],
            "spent": self.state.spent,
            "reassembly_cells": self.diagram.reassembly_cost(self.state),
        }

    # -------------------------------------------------------------- checks
    def irreversible_done(self) -> Optional[str]:
        return self.diagram.first_irreversible(self.state)

    def physically_whole_possible(self) -> bool:
        """实物层面还能否追认整机：无不可逆拆除、无已发出散杆/字篮、无损坏。"""
        if self.state.whole_delivered:
            return False
        if self.state.basket_taken:
            return False
        if self.state.damaged:
            return False
        if any(i.kind in ("bar", "basket") for i in self.state.produced):
            return False
        return self.irreversible_done() is None


@dataclass
class Reservation:
    """窗口内发的具名整机预约，占一张稀缺工位待单位。"""

    reservation_id: str
    ticket_id: str
    body_id: str
    model: str
    occupies_slot: bool = True
    released: bool = False
    consumed: bool = False


@dataclass
class SlotPool:
    """稀缺的工位待单位（默认唯一一张）。"""

    capacity: int = 1
    holders: "set[str]" = field(default_factory=set)

    def occupy(self, holder: str) -> None:
        if holder in self.holders:
            raise ReservationReuseError(f"{holder} 已占用待单位")
        if len(self.holders) >= self.capacity:
            raise ReservationReuseError(
                f"工位待单位已满（cap={self.capacity}，holder={sorted(self.holders)}）"
            )
        self.holders.add(holder)

    def release(self, holder: str) -> None:
        self.holders.discard(holder)

    def free(self) -> int:
        return self.capacity - len(self.holders)


@dataclass
class ConversionWindow:
    """临时改整机窗。"""

    open: bool = True
    customer_deadline: Optional[int] = None
    release_received: bool = False

    def close(self) -> None:
        self.open = False

    def give_release(self) -> None:
        self.release_received = True


@dataclass
class ConversionDecision:
    granted: bool
    reason: str
    ticket_id: str
    body_id: str
    invalidated_quote: Optional[Quote] = None
    reassembly_cells: int = 0
    needs_remaining_feasible: bool = True
    remaining_orders_ok: bool = True

    def as_json(self) -> dict:
        return {
            "granted": self.granted,
            "reason": self.reason,
            "ticket_id": self.ticket_id,
            "body_id": self.body_id,
            "reassembly_cells": self.reassembly_cells,
            "quote_invalidated": bool(self.invalidated_quote and not self.invalidated_quote.valid),
            "remaining_orders_ok": self.remaining_orders_ok,
        }


def _remaining_feasible(remaining: list[Ticket], diagrams: list[Diagram],
                        budget: int) -> tuple[bool, list[str]]:
    """改单后其余已接件单是否都还能交（逐单按方案求解复核）。"""
    if not remaining:
        return True, []
    orders = [t.as_order() for t in remaining]
    try:
        result = Workshop(diagrams, orders, budget).solve()
    except Exception:
        return False, [t.order_id for t in remaining]
    unmet = [o.order_id for o in result.unfulfilled]
    return (not unmet), unmet


def try_convert_to_whole(
    station: Station,
    ticket: Ticket,
    quote: Optional[Quote],
    remaining_tickets: list[Ticket],
    other_diagrams: list[Diagram],
    budget: int,
    *,
    window: Optional[ConversionWindow] = None,
    reservation: Optional[Reservation] = None,
) -> ConversionDecision:
    """尝试把一张预接件单改整机单。

    - 实物必须仍可整机（无不可逆拆取、无已发出散杆/字篮、无损坏）。
    - 若走了 POLICY_ALWAYS 且窗口仍开、预约还占位、未收到释放：说明改单路径存在，
      但柜面此刻不得据已发生的不可逆拆取追认——若已发生则拒并把旧报价失效。
    - 改单仍须确认其余已接件单都能交。
    """
    body = station.body_id

    # 1) 已发生首次不可逆拆取 -> 拒，且旧“整机可交”报价失效
    first_irr = station.irreversible_done()
    if first_irr is not None:
        if quote is not None:
            quote.invalidate(f"固定杆/不可复装件 {first_irr} 已拆断，整机不可追认")
        return ConversionDecision(
            granted=False,
            reason=f"已发生不可逆拆取（{first_irr}），整机不可追认；旧报价作废",
            ticket_id=ticket.order_id, body_id=body,
            invalidated_quote=quote,
        )

    # 2) 实物已发走或已拆坏 -> 拒
    if not station.physically_whole_possible():
        reason = "已发出散杆/字篮或已拆坏实物，不能追认整机"
        if station.state.damaged:
            reason = f"已拆坏 {sorted(station.state.damaged)}，不能追认整机"
        if quote is not None:
            quote.invalidate(reason)
        return ConversionDecision(
            granted=False, reason=reason, ticket_id=ticket.order_id, body_id=body,
            invalidated_quote=quote,
        )

    # 3) 可复装则允许改；复装占格班时，已花班时照计
    reassembly = station.diagram.reassembly_cost(station.state)

    # 4) POLICY_ALWAYS 窗口保护：窗口开、预约占位、未释放时，工位不能已做不可逆拆取
    if (window is not None and reservation is not None and reservation.occupies_slot
            and window.open and not window.release_received and not reservation.released):
        # 到这里 first_irr 已为空，说明工位守住了窗口约束
        pass

    # 5) 其余已接件单仍须都能交
    diagrams = [station.diagram] + list(other_diagrams)
    ok, unmet = _remaining_feasible(
        [t for t in remaining_tickets if t.order_id != ticket.order_id],
        diagrams, budget,
    )
    if not ok:
        return ConversionDecision(
            granted=False,
            reason=f"改单后其余订单无法全部交付：{unmet}",
            ticket_id=ticket.order_id, body_id=body,
            reassembly_cells=reassembly, remaining_orders_ok=False,
        )

    ticket.converted_to_whole = True
    ticket.resolved_as = "whole"
    return ConversionDecision(
        granted=True,
        reason=("仅可复装的操作，允许改整机；复装占格班时，已花班时照计"
                if reassembly else "尚未发生不可逆拆取，允许直接改整机"),
        ticket_id=ticket.order_id, body_id=body,
        reassembly_cells=reassembly,
        remaining_orders_ok=True,
    )


def issue_reservation(pool: SlotPool, reservation: Reservation,
                      window: ConversionWindow) -> Reservation:
    """窗口内发具名整机预约；占一张稀缺工位待单位。"""
    if not window.open:
        raise ConversionError("改整机窗已关闭，不能再发预约")
    pool.occupy(reservation.reservation_id)
    reservation.occupies_slot = True
    return reservation


def station_guard(window: ConversionWindow, reservation: Optional[Reservation],
                  station: Station, piece_name: str) -> None:
    """POLICY_ALWAYS 下工位在做不可逆拆取前的硬闸：窗开且预约占位且未释放 -> 禁止。"""
    piece = station.diagram.pieces[piece_name]
    if piece.reversible:
        return  # 可复装操作不受窗口限制
    if (window.open and reservation is not None and reservation.occupies_slot
            and not reservation.released and not window.release_received):
        raise ConversionError(
            f"改整机窗仍开且预约 {reservation.reservation_id} 占着待单位："
            f"不得不可逆拆取 {piece_name}（须等窗关闭或收到释放）"
        )


def release_reservation(pool: SlotPool, reservation: Reservation) -> None:
    """释放预约：归还待单位。若不是用真实拆机凭据发回，则顾客须回到原件单。"""
    pool.release(reservation.reservation_id)
    reservation.released = True
    reservation.occupies_slot = False


def convert_via_signed_back(station: Station, ticket: Ticket, quote: Optional[Quote],
                            remaining_tickets: list[Ticket], other_diagrams: list[Diagram],
                            budget: int) -> ConversionDecision:
    """POLICY_DEMAND：工位先开工，柜台等签回后按现场零件状态改单。"""
    if not station.signed_back:
        raise ConversionError("未见工位按现场零件状态签回，不能受理改单")
    return try_convert_to_whole(
        station, ticket, quote, remaining_tickets, other_diagrams, budget,
    )


@dataclass
class PolicyOutcome:
    policy: str
    conversion_guaranteed: bool
    third_machine_met: bool
    notes: list[str]

    def as_json(self) -> dict:
        return {
            "policy": self.policy,
            "conversion_guaranteed": self.conversion_guaranteed,
            "third_machine_met": self.third_machine_met,
            "notes": self.notes,
        }


def compare_policies(
    *,
    reservation_window_end: int,
    third_machine_deadline: int,
    customer_convert_deadline: int,
) -> list[PolicyOutcome]:
    """显式对比“留唯一待单位”与“先开工”的可交代价。

    设定：唯一一张工位待单位。改整机窗到 `reservation_window_end` 结束；另有一台
    可立刻回收的旧机，其截止 `third_machine_deadline` 早于窗结束；顾客改整机期限
    `customer_convert_deadline`。

    - POLICY_ALWAYS：预约占住唯一待单位到窗结束，第三机在其截止
      （早于窗结束）前无法开工 -> 第三机订单落空；但整机改单**保证**成功。
    - POLICY_DEMAND：先开工第三机 -> 第三机按期交付；改整机须等签回、且顾客期限
      若早于签回时点则**不能保证**，退回原件单。
    """
    if not (third_machine_deadline < reservation_window_end):
        raise ValueError("第三机截止应在改整机窗结束前，才能体现取舍")
    always = PolicyOutcome(
        policy=POLICY_ALWAYS,
        conversion_guaranteed=True,
        third_machine_met=False,
        notes=[
            "预约占住唯一待单位到窗结束 -> 第三机截止早于窗结束，无法开工，落空",
            "代价：为保住改整机，牺牲第三机的立即回收订单",
        ],
    )
    # 先开工：第三机在截止前用掉待单位 -> 按期交付
    third_met = True
    # 改整机只在顾客期限 >= 签回时点才保证；签回发生在第三机开工之后
    sign_back_at = third_machine_deadline
    conversion_guaranteed = customer_convert_deadline >= sign_back_at
    demand = PolicyOutcome(
        policy=POLICY_DEMAND,
        conversion_guaranteed=conversion_guaranteed,
        third_machine_met=third_met,
        notes=[
            "先开工第三机 -> 第三机按期交付",
            ("顾客期限 >= 签回时点，改整机可保证"
             if conversion_guaranteed else
             "顾客期限早于签回时点，改整机不保证，按原单继续"),
        ],
    )
    return [always, demand]
