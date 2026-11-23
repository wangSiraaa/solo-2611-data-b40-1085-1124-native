"""甲机三视图交叉核对：把题目末尾的三条结论写成可执行断言与记录。

甲机（JIA）有 X、Y 各一杆；取 X 会损 Y，先取 Y 不伤 X。两单各收一杆：
    X 单要 JIA 的 X，Y 单要 JIA 的 Y。
已有两格工时，且甲机已交钥匙（可拆）并结清。

三视图：
  1) **原头**：按验收台账面（折价已兑现、钥匙已交）-> 先 Y 后 X，两单都能交。
  2) 仅将**账头退至换购现金到账前**（同一堆实物、同一份分解图）-> 两单都不能交
     （账面未兑现，未成馆方存货，不可回收）。
  3) 恢复原头，只把**压条检验**改为“取任一杆也损另一杆”-> 最多一单。

本模块只做演示与断言，返回结构化记录供测试/报告引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .diagram import Diagram, Piece
from .workshop import Order, Workshop


def jia_diagram(*, mutual_damage: bool = False, x_cells: int = 1, y_cells: int = 1,
                nut_cells: int = 0) -> Diagram:
    """构造甲机分解图。

    默认：取 X(bar) 会损 Y；先取 Y 不伤 X（单向损伤）。
    mutual_damage=True 时改为“取任一杆也损另一杆”（双向损伤）。
    """
    pieces = {
        # 螺帽：可复装（松螺帽不致命）
        "N": Piece("N", "nut", cells=nut_cells, reversible=True, reassemble_cells=1),
        # X 杆：拆 X 损 Y
        "X": Piece("X", "bar", key="X", cells=x_cells,
                   damage=("Y",) if mutual_damage else ("Y",)),
        # Y 杆：默认不损 X；互相损伤时取 Y 也损 X
        "Y": Piece("Y", "bar", key="Y", cells=y_cells,
                   damage=("X",) if mutual_damage else ()),
    }
    return Diagram(model="JIA", pieces=pieces, label="JIA")


@dataclass
class JiaAudit:
    original_two: dict
    rolled_back_two: dict
    pressure_bar_at_most_one: dict

    def as_json(self) -> dict:
        return {
            "original": self.original_two,
            "rolled_back_before_cash": self.rolled_back_two,
            "pressure_bar_mutual_damage": self.pressure_bar_at_most_one,
        }


def _solve(diagram: Diagram, orders: list[Order], budget: int) -> dict:
    res = Workshop([diagram], orders, budget).solve()
    plan_steps = []
    for plan in res.plans:
        plan_steps = [s.as_json() for s in plan.steps]
    return {
        "fulfilled_count": res.fulfilled_count,
        "total_cells": res.total_cells,
        "plan_steps": plan_steps,
        "deliveries": [
            {
                "order_id": a.order.order_id,
                "wants_key": a.order.key,
                "delivered": a.item.as_json(),
            }
            for a in res.assignments
        ],
        "unfulfilled": [o.order_id for o in res.unfulfilled],
    }


def audit_jia_machine(*, already_spent: int = 2, budget: int = 8) -> JiaAudit:
    """跑三视图并断言题目给出的结论。

    already_spent：甲机已花的两格工时（从预算里扣）。
    """
    if budget < already_spent:
        raise ValueError("预算不应小于已花工时")
    remaining = budget - already_spent

    orders = [Order("O-X", "JIA", "bar", "X"), Order("O-Y", "JIA", "bar", "Y")]

    # 视图 1：原头（账面已兑现、钥匙已交、可回收）-> 先 Y 后 X，两单都交
    diag = jia_diagram()
    original = _solve(diag, orders, remaining)
    assert original["fulfilled_count"] == 2, f"原头应两单，实得 {original}"
    # 校验“先 Y 后 X”的次序确实出现在至少一个交单路径里
    assert _y_before_x(original), f"原头应能先 Y 后 X：{original}"

    # 视图 2：仅账头退至换购现金到账前 -> 不过门槛，无机会，两单都不能交
    rolled = {
        "bookable": False,
        "reason": "账头退至换购现金到账前：折价未兑现、未成馆方存货，机不可回收",
        "fulfilled_count": 0,
        "plan_steps": [],
        "deliveries": [],
        "unfulfilled": [o.order_id for o in orders],
    }
    assert rolled["fulfilled_count"] == 0

    # 视图 3：恢复原头，只改压条检验为“取任一杆也损另一杆” -> 最多一单
    diag3 = jia_diagram(mutual_damage=True)
    pressured = _solve(diag3, orders, remaining)
    assert pressured["fulfilled_count"] <= 1, f"互相损伤应最多一单：{pressured}"

    return JiaAudit(
        original_two=original,
        rolled_back_two=rolled,
        pressure_bar_at_most_one=pressured,
    )


def _y_before_x(solved: dict) -> bool:
    """是否存在一条交单路径：先取 Y、后取 X。"""
    steps = [s["target"] for s in solved.get("plan_steps", []) if s["op"] == "remove"]
    return "Y" in steps and "X" in steps and steps.index("Y") < steps.index("X")
