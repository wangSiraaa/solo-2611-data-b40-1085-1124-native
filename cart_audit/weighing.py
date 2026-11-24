"""合重读数下的整数称量约束与联合可行域。

合法读数窗只给出“毛重整数区间”，而秤在同一班有固定零点偏差 ``z``：

- 校零窗（完整托住盒集合为空的合法窗，或显式标 ``calibration=true``）
  直接限制 ``z``；其他窗的约束形如
  ``reading_low <= z + sum(盒真重) <= reading_high``。
- 每盒真重是 1..12 的正整数（题目上限），最多八盒。
- 可行域是**所有线性不等式的共同解**，不能把逐盒上下界任意拼合：
  本模块统一在同一套约束上做带界传播 + 回溯，逐盒投影范围只作为
  联合可行域的投影输出，另附“某次合重排除某组重量”的解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .scale import ScaleInputError

__all__ = [
    "LinearConstraint",
    "WeighingCase",
    "WeighingInconsistent",
    "FeasibleWeights",
    "build_weighing_constraints",
]


class WeighingInconsistent(ValueError):
    """读数与零点约束互相矛盾，不存在任何可行称量解释。"""


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ScaleInputError(f"{name} 不接受布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    raise ScaleInputError(f"{name} 必须是整数")


def _ceil_div(a: int, b: int) -> int:
    # b > 0
    return -((-a) // b)


@dataclass(frozen=True)
class LinearConstraint:
    """``low <= sum(coeff[i] * var[i]) + offset <= high`` 的整数线性约束。"""

    name: str
    coeff: Tuple[int, ...]
    low: int
    high: int
    offset: int = 0
    window_id: Optional[str] = None

    def evaluate(self, values: Sequence[int]) -> int:
        return sum(c * v for c, v in zip(self.coeff, values)) + self.offset

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "coeff": list(self.coeff),
            "low": self.low,
            "high": self.high,
            "offset": self.offset,
            "window_id": self.window_id,
        }


@dataclass(frozen=True)
class WeighingCase:
    """一次班的称量题：盒名单、合法窗审核结论与零点限制。"""

    box_ids: Tuple[str, ...]
    window_verdicts: Tuple[dict, ...]
    min_weight: int = 1
    max_weight: int = 12
    zero_low: Optional[int] = None
    zero_high: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.box_ids or len(set(self.box_ids)) != len(self.box_ids):
            raise ScaleInputError("box_ids 必须是非空且无重复的盒名单")
        if len(self.box_ids) > 8:
            raise ScaleInputError("一次称量最多八盒")
        lo = _as_int(self.min_weight, "min_weight")
        hi = _as_int(self.max_weight, "max_weight")
        if lo < 1 or hi < lo:
            raise ScaleInputError("盒重范围非法，要求 1 <= min_weight <= max_weight <= 12")
        if hi > 12:
            raise ScaleInputError("每盒不得超过 12 个虚构重量单位")
        object.__setattr__(self, "min_weight", lo)
        object.__setattr__(self, "max_weight", hi)
        for name, bound in (("zero_low", self.zero_low), ("zero_high", self.zero_high)):
            if bound is not None:
                object.__setattr__(self, name, _as_int(bound, name))
        if self.zero_low is not None and self.zero_high is not None and self.zero_high < self.zero_low:
            raise ScaleInputError("零点限制颠倒：zero_high < zero_low")
        verdict_ids = [v["window_id"] for v in self.window_verdicts]
        if len(set(verdict_ids)) != len(verdict_ids):
            raise ScaleInputError("窗审核结论中 window_id 重复")
        known = set(self.box_ids)
        for verdict in self.window_verdicts:
            for key in ("contained", "zero"):
                unknown = set(verdict[key]) - known
                if unknown:
                    raise ScaleInputError(f"窗 {verdict['window_id']!r} 出现未知盒: {sorted(unknown)}")

    def is_calibration(self, verdict: dict) -> bool:
        if verdict.get("calibration") is True:
            return True
        if verdict.get("calibration") is False:
            return False
        # 默认：完整托住盒集合为空的合法窗即校零窗。
        return verdict["status"] == "legal" and not verdict["contained"]


def build_weighing_constraints(case: WeighingCase) -> Tuple[List[LinearConstraint], Tuple[int, int]]:
    """把合法窗编译为线性约束，并汇总零点偏差 z 的整数范围。

    作废窗不产生约束（其读数不可用）。变量排列为
    ``[z, w_0, w_1, ..., w_{n-1}]``。
    """
    n = len(case.box_ids)
    index = {box_id: i + 1 for i, box_id in enumerate(case.box_ids)}
    constraints: List[LinearConstraint] = []
    # 零点偏差 z 在同一班固定：显式零界与每个校零窗都必须同时满足，
    # 因此零界按**交集**收紧，而不是把各窗读数并起来。
    z_low = case.zero_low
    z_high = case.zero_high

    def narrow_zero(lo: Optional[int], hi: Optional[int], rlo: int, rhi: int) -> Tuple[int, int]:
        new_lo = rlo if lo is None else max(lo, rlo)
        new_hi = rhi if hi is None else min(hi, rhi)
        return new_lo, new_hi

    for verdict in case.window_verdicts:
        if verdict["status"] != "legal":
            continue
        rlo, rhi = verdict["reading"]
        if case.is_calibration(verdict):
            if verdict["contained"]:
                raise ScaleInputError(
                    f"窗 {verdict['window_id']!r} 被显式标为校零窗却完整托住了盒"
                )
            z_low, z_high = narrow_zero(z_low, z_high, rlo, rhi)
            if z_high < z_low:
                raise WeighingInconsistent(
                    f"校零窗 {verdict['window_id']!r} 读数 [{rlo}, {rhi}] 与既有零界矛盾"
                )
            constraints.append(
                LinearConstraint(
                    name=f"zero:{verdict['window_id']}",
                    coeff=(1,) + (0,) * n,
                    low=rlo,
                    high=rhi,
                    window_id=verdict["window_id"],
                )
            )
            continue
        coeff = [0] * (n + 1)
        coeff[0] = 1  # 零点偏差
        for box_id in verdict["contained"]:
            coeff[index[box_id]] = 1
        constraints.append(
            LinearConstraint(
                name=f"window:{verdict['window_id']}",
                coeff=tuple(coeff),
                low=rlo,
                high=rhi,
                window_id=verdict["window_id"],
            )
        )

    if z_low is None or z_high is None:
        raise ScaleInputError(
            "缺少零点依据：必须给出 zero_low/zero_high 或至少一个校零窗"
        )
    if z_high < z_low:
        raise WeighingInconsistent("校零窗给出的零点范围互相矛盾")
    return constraints, (z_low, z_high)

@dataclass
class _Solver:
    case: WeighingCase
    constraints: List[LinearConstraint]
    zero_bounds: Tuple[int, int]
    domains: List[List[int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        n = len(self.case.box_ids)
        z_lo, z_hi = self.zero_bounds
        self.domains = [[z_lo, z_hi]] + [
            [self.case.min_weight, self.case.max_weight] for _ in range(n)
        ]

    # ---- 界传播：对每条约束，按其余变量当前域收紧目标变量域 ------------
    def propagate(self, domains: List[List[int]]) -> bool:
        changed = True
        while changed:
            changed = False
            for con in self.constraints:
                for k, c in enumerate(con.coeff):
                    if c == 0:
                        continue
                    rest_low = 0
                    rest_high = 0
                    for j, (a, d) in enumerate(zip(con.coeff, domains)):
                        if j == k:
                            continue
                        if a > 0:
                            rest_low += a * d[0]
                            rest_high += a * d[1]
                        else:
                            rest_low += a * d[1]
                            rest_high += a * d[0]
                    # low <= c*x + rest + offset <= high
                    # 收紧 x 下界用其余项上界（low - rest_high），
                    # 收紧 x 上界用其余项下界（high - rest_low）。
                    need_low = con.low - rest_high - con.offset
                    need_high = con.high - rest_low - con.offset
                    if c > 0:
                        new_lo = max(domains[k][0], _ceil_div(need_low, c))
                        new_hi = min(domains[k][1], need_high // c)
                    else:
                        # c < 0：不等式翻转
                        cpos = -c
                        new_lo = max(domains[k][0], _ceil_div(-need_high, cpos))
                        new_hi = min(domains[k][1], -need_low // cpos)
                    if new_hi < new_lo:
                        return False
                    if new_lo != domains[k][0] or new_hi != domains[k][1]:
                        domains[k] = [new_lo, new_hi]
                        changed = True
        return True

    def _all_satisfied(self, values: Sequence[int]) -> bool:
        return all(con.low <= con.evaluate(values) <= con.high for con in self.constraints)

    def dfs(
        self,
        domains: List[List[int]],
        fixed: Dict[int, int],
        *,
        limit: Optional[int] = None,
        sink: Optional[Callable[[Tuple[int, ...]], None]] = None,
        collect: Optional[List[Tuple[int, ...]]] = None,
        count: List[int],
    ) -> None:
        if limit is not None and count[0] >= limit:
            return
        if not self.propagate(domains):
            return
        unassigned = [i for i in range(len(domains)) if i not in fixed]
        if not unassigned:
            values = tuple(fixed[i] for i in range(len(domains)))
            if self._all_satisfied(values):
                count[0] += 1
                if collect is not None:
                    collect.append(values)
                if sink is not None:
                    sink(values)
            return
        # MRV：先分支当前域最小的未定值变量。
        i = min(unassigned, key=lambda j: domains[j][1] - domains[j][0])
        for value in range(domains[i][0], domains[i][1] + 1):
            next_domains = [d[:] for d in domains]
            next_domains[i] = [value, value]
            next_fixed = dict(fixed)
            next_fixed[i] = value
            self.dfs(
                next_domains,
                next_fixed,
                limit=limit,
                sink=sink,
                collect=collect,
                count=count,
            )
            if limit is not None and count[0] >= limit:
                return

    def solve_all(self, limit: Optional[int] = None) -> List[Tuple[int, ...]]:
        collected: List[Tuple[int, ...]] = []
        self.dfs([d[:] for d in self.domains], {}, limit=limit, collect=collected, count=[0])
        return collected

    def solve_one(self) -> Optional[Tuple[int, ...]]:
        answers = self.solve_all(limit=1)
        return answers[0] if answers else None

    def extreme(self, objective: Sequence[int], find_min: bool) -> Tuple[int, Tuple[int, ...]]:
        """带剪枝求 ``sum(objective[i]*var[i])`` 的最小/最大值及一个取得值。"""
        best: List[Optional[int]] = [None]
        witness: List[Optional[Tuple[int, ...]]] = [None]

        def branch_and_bound(domains: List[List[int]], fixed: Dict[int, int]) -> None:
            if not self.propagate(domains):
                return
            # 目标界（系数可能为负，按域逐项求界）
            bound_low = 0
            bound_high = 0
            for c, d in zip(objective, domains):
                if c >= 0:
                    bound_low += c * d[0]
                    bound_high += c * d[1]
                else:
                    bound_low += c * d[1]
                    bound_high += c * d[0]
            if best[0] is not None:
                if find_min and bound_low >= best[0]:
                    return
                if not find_min and bound_high <= best[0]:
                    return
            unassigned = [i for i in range(len(domains)) if i not in fixed]
            if not unassigned:
                values = tuple(fixed[i] for i in range(len(domains)))
                if self._all_satisfied(values):
                    score = sum(c * v for c, v in zip(objective, values))
                    if best[0] is None or (find_min and score < best[0]) or (not find_min and score > best[0]):
                        best[0] = score
                        witness[0] = values
                return
            i = min(unassigned, key=lambda j: domains[j][1] - domains[j][0])
            values = range(domains[i][0], domains[i][1] + 1)
            if objective[i] != 0:
                # 朝有利方向先搜，尽快拿到紧界。
                values = sorted(values, reverse=(not find_min) if objective[i] > 0 else find_min)
            for value in values:
                next_domains = [d[:] for d in domains]
                next_domains[i] = [value, value]
                next_fixed = dict(fixed)
                next_fixed[i] = value
                branch_and_bound(next_domains, next_fixed)

        branch_and_bound([d[:] for d in self.domains], {})
        if witness[0] is None:
            raise WeighingInconsistent("约束无可行解，无法求极值")
        return best[0], witness[0]  # type: ignore[return-value]


class FeasibleWeights:
    """联合可行称量解释集合（变量序：零点 z 在前，盒重按 box_ids 排列）。"""

    def __init__(self, case: WeighingCase):
        self.case = case
        self.constraints, self.zero_bounds = build_weighing_constraints(case)
        self._solver = _Solver(case, self.constraints, self.zero_bounds)

    # ---- 基本可行性 ----------------------------------------------------
    def is_feasible(self) -> bool:
        return self._solver.solve_one() is not None

    def witness(self) -> Optional[dict]:
        answer = self._solver.solve_one()
        return None if answer is None else self._as_mapping(answer)

    def require_feasible(self) -> dict:
        answer = self._solver.solve_one()
        if answer is None:
            raise WeighingInconsistent("没有任何正整数盒重与零点解释能同时满足全部合法窗")
        return self._as_mapping(answer)

    def enumerate_solutions(self, limit: Optional[int] = None) -> List[dict]:
        return [self._as_mapping(ans) for ans in self._solver.solve_all(limit=limit)]

    # ---- 联合可行域上的投影范围（不是逐盒独立界拼合） ------------------
    def projected_ranges(self) -> dict:
        ranges = {"z": self._range_for((1,) + (0,) * len(self.case.box_ids))}
        for i, box_id in enumerate(self.case.box_ids):
            objective = tuple(1 if j == i + 1 else 0 for j in range(len(self.case.box_ids) + 1))
            ranges[box_id] = self._range_for(objective)
        return ranges

    def _range_for(self, objective: Sequence[int]) -> Tuple[int, int]:
        lo, _ = self._solver.extreme(objective, find_min=True)
        hi, _ = self._solver.extreme(objective, find_min=False)
        return lo, hi

    def feasible_with(self, extra_constraints: Sequence[LinearConstraint]) -> Optional[Tuple[int, ...]]:
        """在原约束再附加线性条件后判可行，返回一个完整变量赋值（z 在前）。

        用于分家模块把“乙更值哪一包”“同值任挑”等线性区域编码为附加约束；
        附加条件与原称量约束在同一联合域上求解，不做逐盒拼合。
        """
        merged = _Solver(
            self.case,
            list(self.constraints) + list(extra_constraints),
            self.zero_bounds,
        )
        return merged.solve_one()

    def extreme_box_sum(self, coeff_by_box: Dict[str, int], find_min: bool) -> Tuple[int, dict]:
        """对盒重线性式求联合可行域上的极值，返回极值与取得它的完整解释。"""
        objective = [0] * (len(self.case.box_ids) + 1)
        for box_id, c in coeff_by_box.items():
            objective[self.case.box_ids.index(box_id) + 1] = _as_int(c, f"coeff[{box_id}]")
        value, answer = self._solver.extreme(objective, find_min)
        return value, self._as_mapping(answer)

    # ---- 合重排除解释 --------------------------------------------------
    def explains(self, window_id: str, candidate_weights: Dict[str, int]) -> dict:
        """判断某组盒重被哪次合重（或零点）排除。

        候选只给盒重；零点在其可行范围内任取。返回该窗约束下是否存在
        任意零点使读数成立，并给出最近边界与完整反例解释。
        """
        target = next(
            (c for c in self.constraints if c.window_id == window_id),
            None,
        )
        if target is None:
            raise ScaleInputError(f"窗 {window_id!r} 没有产生约束（作废或未知）")
        values = self._candidate_values(candidate_weights)
        box_part = sum(target.coeff[i] * values[i] for i in range(1, len(values)))
        z_lo, z_hi = self.zero_bounds
        gross_low = box_part + z_lo + target.offset
        gross_high = box_part + z_hi + target.offset
        admitted = not (gross_high < target.low or gross_low > target.high)
        result = {
            "window_id": window_id,
            "candidate": dict(candidate_weights),
            "admitted": admitted,
            "gross_range_with_zero": [gross_low, gross_high],
            "reading": [target.low, target.high],
        }
        if not admitted:
            if gross_high < target.low:
                reason = f"即使零点取最大 {z_hi}，毛重上界 {gross_high} 仍低于读数下界 {target.low}"
            else:
                reason = f"即使零点取最小 {z_lo}，毛重下界 {gross_low} 仍高于读数上界 {target.high}"
            result["excluded_by"] = reason
        return result

    def exclusion_report(self, candidate_weights: Dict[str, int]) -> dict:
        """对全部约束逐一解释，指出第一处（及所有）排除该候选的合重。"""
        values = self._candidate_values(candidate_weights)
        checks = []
        for con in self.constraints:
            box_part = sum(con.coeff[i] * values[i] for i in range(1, len(values)))
            z_lo, z_hi = self.zero_bounds
            gross_low = box_part + z_lo + con.offset
            gross_high = box_part + z_hi + con.offset
            ok = not (gross_high < con.low or gross_low > con.high)
            checks.append(
                {
                    "constraint": con.to_dict(),
                    "gross_range_with_zero": [gross_low, gross_high],
                    "admitted": ok,
                }
            )
        return {
            "candidate": dict(candidate_weights),
            "jointly_feasible": all(item["admitted"] for item in checks),
            "checks": checks,
            "excluding_windows": [
                item["constraint"]["window_id"] for item in checks if not item["admitted"]
            ],
        }

    def _candidate_values(self, candidate_weights: Dict[str, int]) -> List[int]:
        missing = set(self.case.box_ids) - set(candidate_weights)
        unknown = set(candidate_weights) - set(self.case.box_ids)
        if missing or unknown:
            raise ScaleInputError(
                f"候选盒重必须恰好覆盖全部盒；缺 {sorted(missing)}，多 {sorted(unknown)}"
            )
        return [0] + [_as_int(candidate_weights[b], b) for b in self.case.box_ids]

    def _as_mapping(self, answer: Sequence[int]) -> dict:
        return {
            "z": answer[0],
            "weights": {box_id: answer[i + 1] for i, box_id in enumerate(self.case.box_ids)},
        }
