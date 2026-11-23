"""两间刻牌室作业模型。

两种可选模式（实现其一思路、另一模式给出等价事件对照）：
- ONLINE_SEAL 联网签印台：馆拿到逐次最终授权前只能预排；授权基于签印台
  已受理的退盘与真实落印回执。
- OFFLINE_MOLD 离线具名模具：馆与馆离线，具名联号模具由一馆独占；
  码组换手只在已登记的实物交班点发生。

刻入即永久：落印后码状态为 ENGRAVED；退盘只能把牌记为 WITHDRAWN（撤证），
码不能再刻给另一张牌。未刻码遇退盘为 REVOKED（资格撤销，未被刻过）。
退单先到非持馆不产生暂停效力；自持馆收到并办理的时刻起，暂停其后新刻，
未知退单不得提前阻止已到点的合法承诺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Mode(str, Enum):
    ONLINE_SEAL = "online_seal"
    OFFLINE_MOLD = "offline_mold"


class CodeState(str, Enum):
    UNENGRAVED = "unengraved"      # 未刻、仍有效
    REVOKED = "revoked"            # 未刻但资格已撤销（退盘）
    ENGRAVED = "engraved"          # 已永久刻入
    WITHDRAWN = "withdrawn"        # 已刻，牌后被撤证（码仍不可再刻）


class Location(str, Enum):
    WEST = "west"
    EAST = "east"
    IN_TRANSIT = "in_transit"


@dataclass
class Order:
    order_id: str
    hall: str
    plaque_id: str
    codes: tuple[str, ...]
    scheduled: bool = False
    authorized: bool = False
    stamped: bool = False
    group_id: Optional[str] = None


@dataclass
class ExitRow:
    code: str
    state: str
    location: str
    plaque_id: Optional[str]
    withdrawn: bool
    classification: str  # 永久不可再刻 / 已撤销资格 / 可用 / 在途 / 仅预排


class HallError(RuntimeError):
    pass


class HallNetwork:
    def __init__(self, mode: Mode, linkage: dict[str, tuple[str, ...]] | None = None):
        self.mode = mode
        self.linkage: dict[str, tuple[str, ...]] = {g: tuple(c) for g, c in (linkage or {}).items()}
        self.code_to_group = {c: g for g, codes in self.linkage.items() for c in codes}
        self.location: dict[str, Location] = {}
        self.state: dict[str, CodeState] = {}
        self.plaque_of: dict[str, str] = {}
        self.orders: dict[str, Order] = {}
        self.events: list[tuple[int, str, str]] = []
        # 联网模式：签印台已受理的退盘 / 落印回执
        self.platform_returns: set[str] = set()
        self.stamp_receipts: list[str] = []
        # 离线模式：联号模具独占持有馆（未联号码视为各馆自持）
        self.mold_holder: dict[str, str] = {}
        self.holder_hall = Location.WEST.value  # 持馆（办理退单的责任馆）
        self.pause_after: Optional[int] = None  # 持馆最近一次办理退盘的时刻
        self.paused_codes: set[str] = set()     # 退盘暂停效力只及于这些码
        self.clock = 0
        self.handover_points: set[str] = {"HP-1"}
        self.in_transit: dict[str, tuple[tuple[str, ...], str, str]] = {}

    # ---- 基础登记 ---------------------------------------------------------
    def admit_code(self, code: str, hall: str = Location.WEST.value) -> None:
        self.location[code] = Location(hall)
        self.state[code] = CodeState.UNENGRAVED

    def set_mold_holder(self, group_id: str, hall: str) -> None:
        if self.mode != Mode.OFFLINE_MOLD:
            raise HallError("模具独占仅离线模式适用")
        self.mold_holder[group_id] = hall

    def _tick(self, t: Optional[int]) -> int:
        if t is None:
            self.clock += 1
            return self.clock
        self.clock = max(self.clock, t)
        return t

    def _log(self, t: int, kind: str, detail: str) -> None:
        self.events.append((t, kind, detail))

    def _group_of_order(self, codes: tuple[str, ...]) -> Optional[str]:
        groups = {self.code_to_group[c] for c in codes if c in self.code_to_group}
        return next(iter(groups)) if len(groups) == 1 else None

    def _check_group_integrity(self, codes: tuple[str, ...]) -> None:
        """联号组不可拆交：订单要么含整组，要么不含该组。"""
        for c in codes:
            gid = self.code_to_group.get(c)
            if gid is not None:
                if not set(self.linkage[gid]).issubset(set(codes)):
                    raise HallError(f"联号组 {gid} 不可拆交，订单须含整组 {self.linkage[gid]}")

    # ---- 预排与授权 -------------------------------------------------------
    def schedule(self, order_id: str, hall: str, plaque_id: str,
                 codes: tuple[str, ...], t: Optional[int] = None) -> Order:
        t = self._tick(t)
        codes = tuple(codes)
        for c in codes:
            if c not in self.state:
                raise HallError(f"未知码 {c}")
            if self.state[c] in (CodeState.REVOKED, CodeState.ENGRAVED, CodeState.WITHDRAWN):
                raise HallError(f"码 {c} 状态 {self.state[c].value}，不得接单")
        self._check_group_integrity(codes)
        order = Order(order_id, hall, plaque_id, codes, scheduled=True,
                      group_id=self._group_of_order(codes))
        self.orders[order_id] = order
        self._log(t, "schedule", f"{hall} 预排 {order_id}（{plaque_id}）码 {codes}")
        return order

    def authorize(self, order_id: str, t: Optional[int] = None) -> None:
        t = self._tick(t)
        order = self.orders[order_id]
        # 两种模式共同前置：实物必须在承办馆、码仍未刻且未撤销
        for c in order.codes:
            if self.state[c] != CodeState.UNENGRAVED:
                raise HallError(f"码 {c} 状态 {self.state[c].value}，不能授权")
            if self.location[c] != Location(order.hall):
                raise HallError(
                    f"实物未到 {order.hall}（码 {c} 当前 {self.location[c].value}），只能预排"
                )
        if self.mode == Mode.ONLINE_SEAL:
            blocked = [c for c in order.codes if c in self.platform_returns]
            if blocked:
                raise HallError(f"签印台已受理退盘 {blocked}，拒绝最终授权 {order_id}")
            order.authorized = True
            self._log(t, "authorize", f"签印台基于已受理退盘/落印回执下发最终授权 {order_id}")
        else:
            self._assert_offline_ready(order)
            order.authorized = True
            self._log(t, "authorize", f"{order.hall} 持具名模具且实物到馆，获得最终授权 {order_id}")

    def _assert_offline_ready(self, order: Order) -> None:
        for c in order.codes:
            if self.location[c] != Location(order.hall):
                raise HallError(f"离线模式：码 {c} 不在 {order.hall}（当前 {self.location[c].value}），无实物不可授权")
        if order.group_id is not None:
            if self.mold_holder.get(order.group_id) != order.hall:
                raise HallError(
                    f"联号组 {order.group_id} 的具名模具由 "
                    f"{self.mold_holder.get(order.group_id)} 独占，{order.hall} 无权作业"
                )

    # ---- 实物交班 ---------------------------------------------------------
    def handover(self, codes: tuple[str, ...], src: str, dst: str,
                 point: str = "HP-1", t: Optional[int] = None) -> None:
        t = self._tick(t)
        codes = tuple(codes)
        if point not in self.handover_points:
            raise HallError(f"未登记的交班点 {point}，离线码组换手无效")
        self._check_group_integrity(codes)
        for c in codes:
            self.location[c] = Location.IN_TRANSIT
        handover_id = f"HO-{len(self.in_transit) + 1}"
        self.in_transit[handover_id] = (codes, src, dst)
        self._log(t, "handover_out", f"{src} 在登记点 {point} 交出码 {codes}，在途")
        return handover_id

    def complete_handover(self, handover_id: str, t: Optional[int] = None) -> None:
        if handover_id not in self.in_transit:
            raise HallError(f"没有在途交班 {handover_id}")
        codes, src, dst = self.in_transit.pop(handover_id)
        t = self._tick(t)
        for c in codes:
            self.location[c] = Location(dst)
        gids = {self.code_to_group[c] for c in codes if c in self.code_to_group}
        for gid in gids:  # 模具随实物组在交班点换手
            if self.mode == Mode.OFFLINE_MOLD and gid in self.mold_holder:
                self.mold_holder[gid] = dst
        self._log(t, "handover_in", f"{dst} 接收码 {codes}（模具随组交接）")

    # ---- 退单 -------------------------------------------------------------
    def return_arrives_non_holder(self, code: str, hall: str,
                                  t: Optional[int] = None) -> None:
        """退单先到非持馆：只登记，不暂停任何新刻。"""
        t = self._tick(t)
        self._log(t, "return_notice", f"退单到达非持馆 {hall}（码 {code}），持馆尚未知，不产生暂停")

    def holder_processes_return(self, code: str, t: Optional[int] = None) -> None:
        """持馆收到并办理：此刻起暂停其后新刻；未刻撤销、已刻撤证。

        若码属联号组，整包/整组退盘连带撤销同组仍未刻的码（不可拆交条款）。
        """
        t = self._tick(t)
        st = self.state[code]
        if st in (CodeState.REVOKED, CodeState.WITHDRAWN):
            raise HallError(f"码 {code} 已处理过退盘")
        gid = self.code_to_group.get(code)
        group_codes = self.linkage[gid] if gid is not None else (code,)
        if self.mode == Mode.ONLINE_SEAL:
            self.platform_returns.add(code)  # 签印台此刻只受理被退的码
        # 未刻的同组码：联号不可拆，随退盘整组撤销资格；
        # 已刻码只处理被退的那张牌（撤证），其余已刻码不因邻码退盘而失效。
        for c in group_codes:
            cst = self.state[c]
            if cst == CodeState.UNENGRAVED:
                self.state[c] = CodeState.REVOKED
                if self.mode == Mode.ONLINE_SEAL:
                    self.platform_returns.add(c)
                # 预排单中含该码的授权作废
                for o in self.orders.values():
                    if c in o.codes and not o.stamped:
                        o.authorized = False
                self._log(t, "return_processed",
                          f"持馆办理退盘：未刻联号码 {c} 撤销资格（整组不拆），暂停其后新刻")
        if st == CodeState.ENGRAVED:
            self.state[code] = CodeState.WITHDRAWN
            plaque = self.plaque_of[code]
            self._log(t, "return_processed",
                      f"持馆办理退盘：牌 {plaque} 销量证明撤回（撤证），码 {code} 永久不还")
        # 暂停其后新刻：效力仅及于退盘涉及的码，不波及其它独立码
        self.paused_codes.update(group_codes)
        self.pause_after = t

    # ---- 落印 -------------------------------------------------------------
    def stamp(self, order_id: str, t: Optional[int] = None) -> None:
        t = self._tick(t)
        order = self.orders[order_id]
        if order.stamped:
            raise HallError(f"{order_id} 已落印")
        if not order.authorized:
            raise HallError(f"{order_id} 无最终授权，只能预排，不能落印")
        if self.pause_after is not None and t >= self.pause_after:
            hit = [c for c in order.codes if c in self.paused_codes]
            if hit:
                raise HallError(f"持馆已就码 {hit} 办理退盘，其后新刻暂停，本单不得落印")
        for c in order.codes:
            if self.state[c] != CodeState.UNENGRAVED:
                raise HallError(f"码 {c} 状态 {self.state[c].value}，无法落印")
        if self.mode == Mode.OFFLINE_MOLD:
            self._assert_offline_ready(order)
        for c in order.codes:
            self.state[c] = CodeState.ENGRAVED
            self.plaque_of[c] = order.plaque_id
            self.location[c] = Location(order.hall)
        order.stamped = True
        self.stamp_receipts.append(order_id)
        self._log(t, "stamp", f"{order.hall} 真实落印 {order_id}：码 {order.codes} 永久刻入牌 {order.plaque_id}")

    def shift_change(self, hall: str, t: Optional[int] = None) -> None:
        """换班：旧刻单随档保留，新申请须重新走授权。"""
        t = self._tick(t)
        old_stamped = [oid for oid, o in self.orders.items() if o.hall == hall and o.stamped]
        self._log(t, "shift_change", f"{hall} 换班：旧刻单 {old_stamped} 存档，新申请另行授权")

    # ---- 联号解除 ---------------------------------------------------------
    def dissolve_group(self, group_id: str, t: Optional[int] = None) -> None:
        t = self._tick(t)
        codes = self.linkage.pop(group_id)
        for c in codes:
            self.code_to_group.pop(c, None)
        self.mold_holder.pop(group_id, None)
        self._log(t, "dissolve", f"联号组 {group_id} 解除，码 {codes} 成为独立码，可单独换馆")

    # ---- 出库名单 ---------------------------------------------------------
    def outbound_list(self) -> list[ExitRow]:
        rows: list[ExitRow] = []
        scheduled_codes = {c for o in self.orders.values() if o.scheduled and not o.stamped for c in o.codes}
        for code, st in self.state.items():
            loc = self.location[code].value
            plaque = self.plaque_of.get(code)
            if st == CodeState.ENGRAVED:
                kind = "真的永远不能再刻（已落印）"
            elif st == CodeState.WITHDRAWN:
                kind = "真的永远不能再刻（已落印，牌已撤证）"
            elif st == CodeState.REVOKED:
                kind = "已撤销资格（未刻，退盘）"
            elif loc == Location.IN_TRANSIT.value:
                kind = "在途未刻（仍有效）"
            elif code in scheduled_codes:
                kind = "仅预排未授权（仍有效）"
            else:
                kind = "可用未刻"
            rows.append(ExitRow(code, st.value, loc, plaque,
                                st == CodeState.WITHDRAWN, kind))
        return sorted(rows, key=lambda r: r.code)
