"""验收台：把“以旧换新已开单”这件事落到可核对的账头与交接件上。

关键口径（合成，非现实准则）：

1. **新机来自哪张已对齐的内部发票、在哪个卖场真实外销**
   - 内部发票须两侧对齐（seller/buyer/q/T/C 一致）。
   - 真实外销凭证独立登记；一卖场一张外销单，标 outlet 与现金/折价拆分。

2. **换购小票逐项对上对外售价**
   - 小票列出：现金（cash）+ 每台旧机的折价（trade_values）。
   - 恒等式：`cash + Σ trade_values == external_sale_price`，逐项列示。

3. **两台具名旧机；未交钥匙的先存放、柜方无权拆**
   - 每台旧机登记 model / body_id / key_handed_in。
   - 未交钥匙期间 `may_dismantle == False`，回收工场不得据此机取件。

4. **结算前反悔须整单还机**
   - 撤销小票 = 整单撤销（状态 cancelled）；现金与折价一并退回、旧机全部还回。

5. **同一外销单可以先挂应收、以后凭收款凭证结清**
   - 外销单初始 status=receivable；`settle(outlet, receipt)` 用收款凭证结清。

6. **已完成新机交付且约定现金入账后才成馆方存货**
   - 旧机仅当 `new_delivered_and_cash_booked` 时进入 `inventory`。

7. **按指定账头和交接件独立判断每台旧机是否可回收，列已兑现折价**
   - 每台机独立：账头已兑现（折价已入账且未撤销）+ 钥匙已交（可拆）+ 物理可拆。

8. **严禁两处卖场同时认领同一机身**
   - body_id 全局唯一；两个卖场登记同一 body_id 即拒（ClaimError）。

本模块只跟踪状态与守恒关系，不实现真实收入确认/税务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .diagram import Diagram


class ClaimError(Exception):
    """两处卖场同时认领同一机身，或重复登记。"""


class InventoryGateError(Exception):
    """未满足入库门槛（新机未交付或现金未入账）却试图入库/回收。"""


@dataclass
class OldMachine:
    """一台具名旧机（交接件）。"""

    model: str
    body_id: str
    key_handed_in: bool = False
    stored: bool = True  # 未交钥匙先存放
    diagram: Optional[Diagram] = None
    _claimed_outlet: Optional[str] = field(default=None, repr=False)

    def can_be_dismantled(self) -> bool:
        """柜方无权拆未交钥匙的机。"""
        return self.key_handed_in


@dataclass
class TradeTicket:
    """换购小票：现金 + 折价逐项，对上一笔对外售价。"""

    ticket_id: str
    outlet: str
    external_sale_id: str
    external_sale_price: int
    cash: int
    trade_values: "dict[str, int]"  # body_id -> 折价
    old_bodies: list[str]
    new_delivered: bool = False
    cash_booked: bool = False
    cancelled: bool = False

    def total(self) -> int:
        return self.cash + sum(self.trade_values.values())

    def reconciled(self) -> bool:
        return self.total() == self.external_sale_price

    def inventory_unlocked(self) -> bool:
        """完成新机交付 + 约定现金入账 -> 馆方存货。"""
        return self.new_delivered and self.cash_booked and not self.cancelled


@dataclass
class ExternalSale:
    """一卖场对外售价单；可先挂应收，后凭收款凭证结清。"""

    sale_id: str
    outlet: str
    price: int
    status: str = "receivable"  # receivable / settled / cancelled
    settled_amount: int = 0

    def settle(self, receipt: int) -> None:
        if self.status == "cancelled":
            raise InventoryGateError("外销单已撤销，不能结清")
        if receipt <= 0:
            raise ValueError("收款凭证金额必须为正")
        if self.settled_amount + receipt > self.price:
            raise InventoryGateError(
                f"结清金额溢出：已 {self.settled_amount}，本次 {receipt}，限额 {self.price}"
            )
        self.settled_amount += receipt
        if self.settled_amount == self.price:
            self.status = "settled"


@dataclass
class AcceptanceDesk:
    """验收台聚合。"""

    invoices: dict[str, dict] = field(default_factory=dict)  # invoice_id -> {seller,buyer,q,T,C}
    external_sales: dict[str, ExternalSale] = field(default_factory=dict)
    tickets: dict[str, TradeTicket] = field(default_factory=dict)
    machines: dict[str, OldMachine] = field(default_factory=dict)  # body_id -> machine
    body_outlet: dict[str, str] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)  # 已入库 body_id

    # ------------------------------------------------------------ invoice
    def align_invoice(self, invoice_id: str, seller: str, buyer: str, q: int, T: int, C: int) -> None:
        """登记一张已对齐的内部发票（供新机来源追溯）。"""
        if q <= 0 or T <= 0 or C <= 0:
            raise ValueError("q/T/C 必须为正整数")
        self.invoices[invoice_id] = {"seller": seller, "buyer": buyer, "q": q, "T": T, "C": C}

    def open_external_sale(self, sale_id: str, outlet: str, price: int,
                           invoice_id: str) -> ExternalSale:
        """在真实卖场开一张对外售价单；先挂应收。"""
        if invoice_id not in self.invoices:
            raise ValueError(f"新机来源未对齐：{invoice_id} 不在已对齐发票中")
        if price <= 0:
            raise ValueError("对外售价必须为正")
        sale = ExternalSale(sale_id=sale_id, outlet=outlet, price=price)
        self.external_sales[sale_id] = sale
        return sale

    def settle_external_sale(self, sale_id: str, receipt: int) -> ExternalSale:
        sale = self._sale(sale_id)
        sale.settle(receipt)
        return sale

    # ------------------------------------------------------------- machines
    def register_machine(self, outlet: str, model: str, body_id: str,
                         diagram: Optional[Diagram] = None,
                         key_handed_in: bool = False) -> OldMachine:
        """登记一台具名旧机；严禁跨卖场重复认领同一机身。"""
        if body_id in self.body_outlet:
            existing = self.body_outlet[body_id]
            raise ClaimError(
                f"机身 {body_id} 已被卖场 {existing} 认领，卖场 {outlet} 不得同时认领"
            )
        machine = OldMachine(model=model, body_id=body_id, key_handed_in=key_handed_in,
                             diagram=diagram)
        self.machines[body_id] = machine
        self.body_outlet[body_id] = outlet
        return machine

    def hand_over_key(self, body_id: str) -> None:
        machine = self.machines[body_id]
        machine.key_handed_in = True
        machine.stored = False

    # --------------------------------------------------------------- ticket
    def record_ticket(self, ticket_id: str, outlet: str, sale_id: str, cash: int,
                      trade_values: "dict[str, int]", old_bodies: list[str]) -> TradeTicket:
        sale = self._sale(sale_id)
        if sale.outlet != outlet:
            raise ClaimError(f"外销单 {sale_id} 属卖场 {sale.outlet}，与小票卖场 {outlet} 不符")
        if len(old_bodies) > 2:
            raise ValueError("一张小票最多两台具名旧机")
        # 旧机必须已在该卖场登记、且折价逐项给出
        for body_id in old_bodies:
            if body_id not in self.machines:
                raise ValueError(f"旧机 {body_id} 未登记交接件")
            if self.body_outlet.get(body_id) != outlet:
                raise ClaimError(f"旧机 {body_id} 不属于卖场 {outlet}")
        if set(trade_values) != set(old_bodies):
            raise ValueError("折价逐项必须与小票所列旧机一一对应")
        ticket = TradeTicket(
            ticket_id=ticket_id, outlet=outlet, external_sale_id=sale_id,
            external_sale_price=sale.price, cash=cash,
            trade_values=dict(trade_values), old_bodies=list(old_bodies),
        )
        if not ticket.reconciled():
            raise ValueError(
                f"小票现金 {cash} + 折价 {sum(trade_values.values())} != 对外售价 {sale.price}"
            )
        self.tickets[ticket_id] = ticket
        return ticket

    def deliver_new(self, ticket_id: str) -> None:
        self._ticket(ticket_id).new_delivered = True

    def book_cash(self, ticket_id: str) -> None:
        self._ticket(ticket_id).cash_booked = True

    def cancel_ticket(self, ticket_id: str) -> None:
        """结算前反悔：整单还机（现金 + 折价 + 旧机全部退回）。"""
        ticket = self._ticket(ticket_id)
        if ticket.new_delivered and ticket.cash_booked:
            # 已交付并入账后再反悔不再是“结算前”，此处保守拒绝
            raise InventoryGateError("新机已交付且现金已入账，不能按结算前整单退机处理")
        ticket.cancelled = True
        sale = self.external_sales.get(ticket.external_sale_id)
        if sale is not None and sale.status == "receivable":
            sale.status = "cancelled"

    # ---------------------------------------------------------- recoverable
    def recoverable_bodies(self) -> list[dict]:
        """按账头 + 交接件 + 物理可拆，独立判断每台旧机是否可回收，列已兑现折价。

        可回收 = 存在一张未撤销小票，其折价含该机、且该小票已完成新机交付与
                现金入账（否则未成馆方存货）；且该机已交钥匙（可拆）。
        返回逐机明细（含 verbal（账面）/ physical（可拆）/ realized_value 折价）。
        """
        report = []
        for body_id, machine in self.machines.items():
            tickets = [t for t in self.tickets.values()
                       if body_id in t.trade_values and not t.cancelled]
            unlocked = [t for t in tickets if t.inventory_unlocked()]
            realized = sum(t.trade_values[body_id] for t in unlocked)
            bookable = bool(unlocked)  # 账头：折价已兑现且可入账
            physical = machine.can_be_dismantled()
            report.append({
                "body_id": body_id,
                "model": machine.model,
                "outlet": self.body_outlet.get(body_id),
                "realized_value": realized,
                "bookable": bookable,
                "physical": physical,
                "recoverable": bookable and physical,
            })
        return report

    def admit_to_inventory(self, ticket_id: str) -> list[str]:
        """完成新机交付 + 现金入账 -> 馆方存货。返回本次入库 body 列表。"""
        ticket = self._ticket(ticket_id)
        if not ticket.inventory_unlocked():
            raise InventoryGateError("未完成新机交付或现金未入账，不能入库")
        added = []
        for body_id in ticket.old_bodies:
            if body_id not in self.inventory:
                self.inventory.append(body_id)
                added.append(body_id)
        return added

    # --------------------------------------------------------------- helpers
    def _sale(self, sale_id: str) -> ExternalSale:
        if sale_id not in self.external_sales:
            raise ValueError(f"未知外销单 {sale_id}")
        return self.external_sales[sale_id]

    def _ticket(self, ticket_id: str) -> TradeTicket:
        if ticket_id not in self.tickets:
            raise ValueError(f"未知小票 {ticket_id}")
        return self.tickets[ticket_id]
