"""回收工场求解器：在预算内选出最值得拆成字杆（或交整机/字篮）的旧机。

目标（严格字典序）：
    1. 先求能**完整兑现的订单数**最大；
    2. 平手取**总工时（格班时）最少**。

模型：
- 每台旧机按 `Diagram` 的实机分解图展开。一次拆取占一个工位、若干整格；
  阶段依序、同阶段自由、字篮整体、损伤落在仍到场的相邻杆上、可复装件。
- 每份订单要求 **具名机型** + 一个键位（一根散杆），或一份**完整字篮**，或**整机**。
  不兼容的杆不可顶（机型/键位逐一匹配）。
- 一台机可同时兑现多份订单（产出多根杆），但同一根实物只能认领一次；
  “两处卖场同时认领同一机身”由验收台拒绝，这里只保证实物唯一。
- 也可整机交走（不再拆任何散杆/字篮）。

求解：对每台机枚举全部**终止方案**（任意时刻停止 / 整机交 / 拆篮后停），
再做跨机组合 + 订单二分匹配；按上述字典序取最优。规模上限 3 机 / 6 单 / 8 格，
枚举为毫秒级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Optional

from .diagram import Diagram, Item, WorkState


class ScheduleError(Exception):
    """超出预算或订单/方案非法。"""


@dataclass(frozen=True)
class Order:
    """一份件单：具名机型 + 键位 / 完整字篮 / 整机。"""

    order_id: str
    model: str
    kind: str  # "bar" | "basket" | "whole_machine"
    key: Optional[str] = None

    def __post_init__(self):
        if self.kind not in ("bar", "basket", "whole_machine"):
            raise ValueError(f"未知订单类型 {self.kind!r}")
        if self.kind == "bar" and not self.key:
            raise ValueError(f"杆订单 {self.order_id} 必须给键位 key")

    def matches(self, item: Item) -> bool:
        return item.matches(self.model, self.kind, self.key)


def build_order(order_id: str, model: str, *, key: Optional[str] = None,
                basket: bool = False, whole: bool = False) -> Order:
    """便捷构造：默认一根具名键位的杆。"""
    if basket and whole:
        raise ValueError("一份订单不能既是字篮又是整机")
    kind = "basket" if basket else "whole_machine" if whole else "bar"
    return Order(order_id=order_id, model=model, kind=kind, key=key)


@dataclass(frozen=True)
class Step:
    """一次可交付动作的记录，供“交取次序”复现。"""

    op: str  # "remove" | "basket" | "whole"
    target: str  # 件名 / 字篮名 / "whole"
    cells: int

    def as_json(self) -> dict:
        return {"op": self.op, "target": self.target, "cells": self.cells}


@dataclass
class MachinePlan:
    """一台机的一个终止方案。"""

    machine: str  # 机型（Diagram.model），多机同型时另用 label 区分
    label: str
    steps: tuple[Step, ...]
    items: tuple[Item, ...]
    cells: int
    delivered_whole: bool
    final_spent: int
    state_removed: frozenset
    state_produced: tuple[Item, ...]

    def as_json(self) -> dict:
        return {
            "machine": self.machine,
            "label": self.label,
            "cells": self.cells,
            "delivered_whole": self.delivered_whole,
            "steps": [s.as_json() for s in self.steps],
            "items": [i.as_json() for i in self.items],
        }


@dataclass
class Assignment:
    order: Order
    machine_label: str
    item: Item
    steps: tuple[Step, ...]

    def as_json(self) -> dict:
        return {
            "order_id": self.order.order_id,
            "wants": {"model": self.order.model, "kind": self.order.kind, "key": self.order.key},
            "machine": self.machine_label,
            "delivered": self.item.as_json(),
            "claim_steps": [s.as_json() for s in self.steps],
        }


@dataclass
class SolveResult:
    assignments: list[Assignment]
    unfulfilled: list[Order]
    plans: list[MachinePlan]
    total_cells: int
    fulfilled_count: int

    def as_json(self) -> dict:
        return {
            "fulfilled_count": self.fulfilled_count,
            "total_cells": self.total_cells,
            "assignments": [a.as_json() for a in self.assignments],
            "unfulfilled": [o.order_id for o in self.unfulfilled],
            "plans": [p.as_json() for p in self.plans],
        }


class Workshop:
    """解决方案求解。机器与订单都在构造时给定。"""

    def __init__(self, diagrams: list[Diagram], orders: list[Order], budget: int):
        if len(diagrams) > 3:
            raise ValueError("规模上限：最多三台机")
        if len(orders) > 6:
            raise ValueError("规模上限：最多六张工单")
        if budget < 0:
            raise ValueError("预算不能为负")
        labels = [d.label or f"{d.model}#{i}" for i, d in enumerate(diagrams)]
        if len(set(labels)) != len(labels):
            raise ValueError(f"机器标签必须唯一：{labels}")
        self.diagrams = diagrams
        self.labels = labels
        self.orders = orders
        self.budget = budget

    # ------------------------------------------------------------ enumeration
    def _enumerate_plans(self, diag: Diagram, label: str) -> list[MachinePlan]:
        """枚举该机的全部非支配终止方案（同状态取最低工时）。"""
        best: dict[tuple, MachinePlan] = {}

        def record(state: WorkState, steps: list[Step], delivered_whole: bool):
            key = (state.fingerprint(), delivered_whole)
            plan = MachinePlan(
                machine=diag.model,
                label=label,
                steps=tuple(steps),
                items=tuple(state.produced),
                cells=state.spent,
                delivered_whole=delivered_whole,
                final_spent=state.spent,
                state_removed=frozenset(state.removed),
                state_produced=tuple(state.produced),
            )
            prev = best.get(key)
            if prev is None or plan.cells < prev.cells:
                best[key] = plan

        # 依阶段/名字固定遍历顺序，保证可复现；同阶段顺序自由由 DFS 覆盖
        order_names = sorted(diag.pieces)

        stack: list[tuple[WorkState, tuple[Step, ...]]] = [(diag.initial_state(), ())]
        seen_paths: set[tuple] = set()
        while stack:
            state, steps = stack.pop()
            path_key = (state.fingerprint(), steps)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            # 终止：停在当前状态
            record(state, list(steps), False)
            # 整机交走（合法时）
            try:
                whole_state = diag.apply_deliver_whole(state)
            except Exception:
                whole_state = None
            if whole_state is not None and whole_state.spent <= self.budget:
                ws = list(steps) + [Step("whole", "whole", whole_state.spent - state.spent)]
                record(whole_state, ws, True)
            # 拆字篮
            try:
                basket_state = diag.apply_take_basket(state)
            except Exception:
                basket_state = None
            if basket_state is not None and basket_state.spent <= self.budget:
                bs = list(steps) + [Step("basket", diag.basket.name,
                                          basket_state.spent - state.spent)]
                stack.append((basket_state, tuple(bs)))
            # 拆单件
            for name in order_names:
                try:
                    nxt = diag.apply_remove(state, name)
                except Exception:
                    continue
                if nxt.spent > self.budget:
                    continue
                ns = list(steps) + [Step("remove", name, nxt.spent - state.spent)]
                stack.append((nxt, tuple(ns)))
        return list(best.values())

    # ---------------------------------------------------------------- solving
    def solve(self) -> SolveResult:
        per_machine_plans = [
            self._enumerate_plans(diag, label)
            for diag, label in zip(self.diagrams, self.labels)
        ]
        # 允许“闲置”方案：不拆不交，0 工时
        idle = [MachinePlan(d.model, l, (), (), 0, False, 0, frozenset(), ())
                for d, l in zip(self.diagrams, self.labels)]
        choices = [[idle[i]] + per_machine_plans[i]
                   for i in range(len(self.diagrams))]

        best_key = None
        best_combo: Optional[tuple[MachinePlan, ...]] = None
        best_assign: list[Assignment] = []

        for combo in product(*choices):
            total = sum(p.cells for p in combo)
            if total > self.budget:
                continue
            assignments, fulfilled = self._match(combo)
            key = (-fulfilled, total)
            if best_key is None or key < best_key:
                best_key = key
                best_combo = combo
                best_assign = assignments

        if best_combo is None:
            # 无任何组合可行（预算连 0 工时都不行不可能，因 idle 恒可行）
            raise ScheduleError("没有任何可行方案")
        used_orders = {a.order.order_id for a in best_assign}
        unfulfilled = [o for o in self.orders if o.order_id not in used_orders]
        return SolveResult(
            assignments=sorted(best_assign, key=lambda a: a.order.order_id),
            unfulfilled=unfulfilled,
            plans=[p for p in best_combo if p.steps],  # 只列真正用到的机
            total_cells=best_key[1],
            fulfilled_count=-best_key[0],
        )

    def _match(self, combo: tuple[MachinePlan, ...]) -> tuple[list[Assignment], int]:
        """把各机产出的实物与订单做最大二分匹配；返回 (分配, 兑现数)。"""
        # 实物清单：每个 item 来自哪台机哪个计划
        items: list[tuple[Item, MachinePlan]] = []
        for plan in combo:
            for item in plan.state_produced:
                items.append((item, plan))
        # 订单 -> 可匹配实物下标
        n_orders = len(self.orders)
        adj: list[list[int]] = [[] for _ in range(n_orders)]
        for oi, order in enumerate(self.orders):
            for ii, (item, _plan) in enumerate(items):
                if order.matches(item):
                    adj[oi].append(ii)

        match_item_to_order = [-1] * len(items)

        def augment(oi: int, visited: set[int]) -> bool:
            for ii in adj[oi]:
                if ii in visited:
                    continue
                visited.add(ii)
                if match_item_to_order[ii] == -1 or augment(match_item_to_order[ii], visited):
                    match_item_to_order[ii] = oi
                    return True
            return False

        # 稳定的匹配顺序
        for oi in range(n_orders):
            augment(oi, set())

        assignments: list[Assignment] = []
        for ii, oi in enumerate(match_item_to_order):
            if oi == -1:
                continue
            item, plan = items[ii]
            assignments.append(
                Assignment(order=self.orders[oi], machine_label=plan.label,
                           item=item, steps=plan.steps)
            )
        return assignments, len(assignments)
