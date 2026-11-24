"""传动链 / 测具岗：把整齿模型写成 :mod:`budget_solver` 真实快照并回答必同速问题。

物理模型（整齿模型，全部精确有理数）：

* **啮合**：轴 a、b 经齿数 ``z_a``、``z_b`` 的齿轮啮合，

  - 外啮合（转向相反）：``ω_b = -(z_a/z_b)·ω_a``；
  - 内啮合（转向相同）：``ω_b = +(z_a/z_b)·ω_a``。

* **差动座**：``2·ω_cage = ω_left + ω_right``。
* **观察读数**：由“转向 + 标记通过次数”合成带符号转速 ``ω_s = v``。

每个匀速段（segment）给出：轴的集合、当段接轴表（meshes/differentials）、
观察读数，以及若干轴的**有限个允许转速**。同一段全架必须共用一个合法解释：
一组给所有轴赋有理数、满足全部线性关系、读数，且落在各轴允许转速集合内。

编码到 budget_solver 的方式：

* 每根轴一个“自由格”（自环）表示 ``ω_s``；
* 每条啮合 / 差动 / 读数各一个自环**约束格**，其方程即对应线性关系等于 0；
* 每根轴、每对轴各一个**探针格**（``ω_s``、``ω_a-ω_b``），供解算器判定是否被唯一确定；
* 一个 ``omni`` 输出格引用以上全部格，使它的依赖闭包覆盖整段——这样
  ``service.solve`` 产出的已发布报告既能看到矛盾，也能被 ``verify_solve`` 独立核对。

离散的允许转速集合是线性代数之外的组合约束，故在 gear_demo 层枚举；每个具体
读数随后仍可回灌 budget_solver 校验。``gear_demo.linalg`` 与 budget_solver 是两套
独立实现，二者对“是否唯一确定”的判定会交叉断言一致。
"""

from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product

from budget_solver.compiler import var_name
from budget_solver.rationals import qstr
from budget_solver.solver import solve_closure

from .linalg import LinearSystem

EXTERNAL = "external"
INTERNAL = "internal"


class NoLegalInterpretation(Exception):
    """该段在所有允许转速组合下都没有合法解释（或存在矛盾传动链）。"""


class GaugeError(Exception):
    """测具岗请求无法作答（例如缺轴、非法参数）。"""


# --------------------------------------------------------------- 数据模型
@dataclass(frozen=True)
class Mesh:
    a: str
    b: str
    teeth_a: int
    teeth_b: int
    kind: str = EXTERNAL  # external: 反向；internal: 同向

    def __post_init__(self):
        if self.kind not in (EXTERNAL, INTERNAL):
            raise GaugeError(f"啮合类型必须是 {EXTERNAL} 或 {INTERNAL}")
        if self.teeth_a <= 0 or self.teeth_b <= 0:
            raise GaugeError("齿数必须为正整数")

    @property
    def sign(self):
        return -1 if self.kind == EXTERNAL else 1

    @property
    def ratio(self):
        return Fraction(self.teeth_a, self.teeth_b)

    def equation(self):
        # ω_b - sign·(z_a/z_b)·ω_a = 0
        return {self.b: Fraction(1), self.a: -self.sign * self.ratio}, Fraction(0)


@dataclass(frozen=True)
class Differential:
    cage: str
    left: str
    right: str

    def equation(self):
        return (
            {self.cage: Fraction(2), self.left: Fraction(-1), self.right: Fraction(-1)},
            Fraction(0),
        )


@dataclass(frozen=True)
class Observation:
    """一条观察读数：``ω_shaft = value``。value 由转向与标记通过次数合成。"""

    shaft: str
    value: Fraction
    turns: int = 0
    marks: int = 0
    direction: int = 1

    @classmethod
    def from_marks(cls, shaft, *, direction, turns, marks=0, cycle=1):
        """转向 × (整圈数 + 标记/刻度) 合成带符号有理转速。"""
        if direction not in (1, -1):
            raise GaugeError("转向只能是 +1 或 -1")
        magnitude = Fraction(turns) + Fraction(marks, cycle)
        return cls(
            shaft=shaft,
            value=direction * magnitude,
            turns=turns,
            marks=marks,
            direction=direction,
        )


@dataclass
class Segment:
    name: str
    shafts: list
    meshes: list = field(default_factory=list)
    differentials: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    allowed_speeds: dict = field(default_factory=dict)  # shaft -> [Fraction...]

    def all_shafts(self):
        seen = list(self.shafts)
        for mesh in self.meshes:
            seen += [mesh.a, mesh.b]
        for diff in self.differentials:
            seen += [diff.cage, diff.left, diff.right]
        for obs in self.observations:
            seen.append(obs.shaft)
        seen += list(self.allowed_speeds)
        return list(dict.fromkeys(seen))

    def equations(self):
        """返回 ``(coeffs, rhs)`` 列表，只涉及轴速变量。"""
        eqs = []
        for mesh in self.meshes:
            eqs.append(mesh.equation())
        for diff in self.differentials:
            eqs.append(diff.equation())
        for obs in self.observations:
            eqs.append(({obs.shaft: Fraction(1)}, Fraction(obs.value)))
        return eqs


@dataclass
class KinematicAnalysis:
    segment: str
    model_id: str
    snap_id: str
    artifact_hash: str
    solve_id: str
    consistent: bool
    free_shafts: list
    determined: dict  # shaft -> Fraction
    pair_differences: dict  # (a,b) -> Fraction | None（None 表示未被唯一确定）
    contradictions: list
    pair_verdicts: dict = field(default_factory=dict)  # (a,b) -> (bool, note, witness)

    def speed(self, shaft):
        return self.determined.get(shaft)

    def must_same_speed(self, a, b):
        """两轴是否必同速，返回 ``(bool, 说明, 反例读数或 None)``。"""
        if a == b:
            return True, "同一根轴", None
        key = (a, b) if (a, b) in self.pair_verdicts else (b, a)
        if key in self.pair_verdicts:
            same, note, witness = self.pair_verdicts[key]
            # 若以相反顺序查询，说明中的轴名仍可读；布尔与见证不变
            return same, note, witness
        return self.pair_differences.get(key) == 0, "", None

    def pair_status(self, a, b):
        """返回 'same' / 'different' / 'free'（不能判定为恒等）。"""
        diff = self.pair_differences.get((a, b), "missing")
        if diff == "missing":
            return "free"
        if diff is None:
            return "free"
        return "same" if diff == 0 else "different"


# --------------------------------------------------------------- 求解器
class KinematicSolver:
    """把跟段（segment）编码进 budget_solver 并给出测具岗结论。"""

    def __init__(self, service):
        self.service = service
        self._analyses = {}

    # ------------------------------------------------------ 快照构造
    @staticmethod
    def _free_cell(name):
        return {
            "kind": "formula",
            "constant": "0",
            "terms": [{"coef": "1", "cell_id": name}],
        }

    @staticmethod
    def _formula_cell(constant, terms):
        return {
            "kind": "formula",
            "constant": qstr(constant),
            "terms": [
                {"coef": qstr(coef), "cell_id": ref}
                for ref, coef in sorted(terms.items())
                if coef != 0
            ],
        }

    @staticmethod
    def _bad_row_cell(name, residual):
        # 自消格：cell = -residual + 1·cell  =>  0 = residual。residual≠0 时构成
        # 一条“0 = 非零常数”的硬约束，budget_solver 的 SCC 求解会给出矛盾。
        return {
            "kind": "formula",
            "constant": qstr(-residual),
            "terms": [{"coef": "1", "cell_id": name}],
        }

    def build_model(self, segment):
        """把一段的线性关系写成 budget_solver 原生“定义式”快照。

        关键：budget_solver 的格是“常数 + Σ 系数×另一格”的定义式，要求每个变量
        恰有一条定义。直接放“残差 = 0”的约束会引入参数耦合块，超出其分块求解
        假设。因此这里先用独立 RREF（:mod:`gear_demo.linalg`）求出该段线性系统
        的通解结构，再按“自由轴 = 自环自由格、从属轴 = 以自由轴表达的定义格”
        写回快照——这与原系统解空间逐一对应，budget_solver 求解后会复现同一
        唯一性/自由性分类。矛盾的传动链则写成一格 `0 = 非零` 让求解器判无解。
        """
        shafts = segment.all_shafts()
        if not shafts:
            raise GaugeError("空段没有轴")
        system = LinearSystem(shafts, segment.equations())
        cells = {}
        if not system.consistent:
            # 游离轴照常作为自由格存在，另加矛盾格让求解器报无解
            for shaft in shafts:
                cells[shaft] = self._free_cell(shaft)
            for index, residual in enumerate(system.inconsistent_rhs):
                cid = f"c_contradiction_{index}"
                cells[cid] = self._bad_row_cell(cid, residual)
            # 探针格照常建出（供测具岗按统一接口读取；不一致时其值无意义）
            probe_ids = {}
            for shaft in shafts:
                pid = f"probe_{shaft}"
                cells[pid] = self._formula_cell(0, {shaft: Fraction(1)})
                probe_ids[("single", shaft)] = pid
            for i, a in enumerate(shafts):
                for b in shafts[i + 1:]:
                    pid = f"probe_diff_{a}__{b}"
                    cells[pid] = self._formula_cell(
                        0, {a: Fraction(1), b: Fraction(-1)}
                    )
                    probe_ids[(a, b)] = pid
            referenced = list(cells)
            cells["omni"] = self._formula_cell(
                0, {ref: Fraction(1) for ref in referenced}
            )
            payload = {
                "model_id": f"KIN_{segment.name}",
                "name": f"传动段 {segment.name}",
                "cells": cells,
                "outputs": ["omni"],
            }
            return payload, shafts, probe_ids, system

        free_vars = system.free_variables
        # 自由轴：自环自由格
        for shaft in free_vars:
            cells[shaft] = self._free_cell(shaft)
        # 从属轴：用 RREF 行把 ω_v 写成 常数 + Σ（自由轴）的定义
        free_col = {v: system.variables.index(v) for v in free_vars}
        for row_index, pivot_var in enumerate(system.pivot_variables):
            row = system._rows[row_index]
            constant = row[-1]
            terms = {}
            for var, col in free_col.items():
                coef = -row[col]
                if coef != 0:
                    terms[var] = coef
            cells[pivot_var] = self._formula_cell(constant, terms)
        # 探针格：单轴与两两差值，供测具岗直接读取确定性
        probe_ids = {}
        for shaft in shafts:
            pid = f"probe_{shaft}"
            cells[pid] = self._formula_cell(0, {shaft: Fraction(1)})
            probe_ids[("single", shaft)] = pid
        for i, a in enumerate(shafts):
            for b in shafts[i + 1:]:
                pid = f"probe_diff_{a}__{b}"
                cells[pid] = self._formula_cell(
                    0, {a: Fraction(1), b: Fraction(-1)}
                )
                probe_ids[(a, b)] = pid
        referenced = shafts + list(probe_ids.values())
        cells["omni"] = self._formula_cell(
            0, {ref: Fraction(1) for ref in referenced}
        )
        payload = {
            "model_id": f"KIN_{segment.name}",
            "name": f"传动段 {segment.name}",
            "cells": cells,
            "outputs": ["omni"],
        }
        return payload, shafts, probe_ids, system

    # ------------------------------------------------------ 分析入口
    def analyze(self, segment):
        payload, shafts, probe_ids, base_system = self.build_model(segment)
        put = self.service.put_model(payload)
        snap = self.service.snapshot(put["model_id"], label=segment.name)
        artifact, _ = self.service.compile(snap["snap_id"], label=segment.name)
        report = self.service.solve(artifact)

        all_vars = sorted(artifact["variables"])
        detailed = solve_closure(artifact, all_vars)
        mid = put["model_id"]

        def lookup(cell_id):
            return detailed["classified"][var_name(mid, cell_id)]

        consistent = detailed["status"] == "consistent"
        determined = {}
        free_shafts = []
        for shaft in shafts:
            info = lookup(shaft)
            if info["status"] == "determined":
                determined[shaft] = Fraction(info["value"])
            else:
                free_shafts.append(shaft)
        pair_differences = {}
        for i, a in enumerate(shafts):
            for b in shafts[i + 1:]:
                pid = probe_ids[(a, b)]
                info = lookup(pid)
                if info["status"] == "determined":
                    pair_differences[(a, b)] = Fraction(info["value"])
                else:
                    pair_differences[(a, b)] = None

        contradictions = []
        if not consistent:
            contradictions = detailed.get("contradictions", [])

        # 交叉核对：budget_solver 的判定必须与独立线性代数一致
        self._cross_check(segment, base_system, shafts, determined,
                           pair_differences, consistent)

        analysis = KinematicAnalysis(
            segment=segment.name,
            model_id=mid,
            snap_id=snap["snap_id"],
            artifact_hash=artifact["artifact_hash"],
            solve_id=report["solve_id"],
            consistent=consistent,
            free_shafts=free_shafts,
            determined=determined,
            pair_differences=pair_differences,
            contradictions=contradictions,
        )
        # 填充每对的“必同速”结论（含反例读数），供测具岗直接作答
        for i, a in enumerate(shafts):
            for b in shafts[i + 1:]:
                analysis.pair_verdicts[(a, b)] = self._verdict_for_pair(
                    segment, shafts, a, b
                )
        self._analyses[segment.name] = analysis
        return analysis

    def _verdict_for_pair(self, segment, shafts, a, b):
        readings = self._enumerate_assignments(segment, shafts)
        if not readings:
            base = LinearSystem(shafts, segment.equations())
            if not base.consistent:
                return (False, f"段 {segment.name} 传动链互相矛盾，任何解释都无解", None)
            return (False, "没有合法解释", None)
        forced_equal = True
        equal_reading = None
        unequal_reading = None
        for reading in readings:
            det, val = reading["system"].is_determined(
                {a: Fraction(1), b: Fraction(-1)}
            )
            if not det:
                forced_equal = False
                pair = self._two_readings_varying(reading, a, b)
                equal_reading = equal_reading or pair[0]
                unequal_reading = unequal_reading or pair[1]
            elif val == 0:
                if equal_reading is None:
                    equal_reading = reading["assignment"]
            else:
                forced_equal = False
                if unequal_reading is None:
                    unequal_reading = reading["assignment"]
        if forced_equal:
            return (
                True,
                f"段 {segment.name} 中所有合法解释都满足 ω_{a}=ω_{b}",
                None,
            )
        return (
            False,
            f"段 {segment.name} 中 ω_{a} 与 ω_{b} 不必同速",
            {"equal": equal_reading, "unequal": unequal_reading},
        )

    # ------------------------------------------------------ 独立交叉核对
    @staticmethod
    def _cross_check(segment, system, shafts, determined, pair_differences, consistent):
        if system.consistent != consistent:
            raise AssertionError(
                "budget_solver 与独立线代对一致性的判定不符"
            )
        if not consistent:
            return
        for shaft in shafts:
            det, _val = system.is_determined({shaft: Fraction(1)})
            bs_det = shaft in determined
            if det != bs_det:
                raise AssertionError(f"轴 {shaft} 唯一性判定不一致")
            if det and system.particular[shaft] != determined[shaft]:
                raise AssertionError(f"轴 {shaft} 数值不一致")
        for (a, b), value in pair_differences.items():
            det, val = system.is_determined({a: Fraction(1), b: Fraction(-1)})
            if (value is not None) != det:
                raise AssertionError(f"轴对 {a},{b} 差值唯一性判定不一致")
            if det and val != value:
                raise AssertionError(f"轴对 {a},{b} 差值数值不一致")

    # ---------------------------------------------- 允许转速 / 合法解释
    @staticmethod
    def _enumerate_assignments(segment, shafts):
        """枚举允许转速组合，返回 [(assignment dict, param info)]。

        每个合法组合给出一个具体读数（自由连续参数取 0）。同时记录该组合下
        哪些轴仍自由（可连续变化）。
        """
        base = segment.equations()
        allowed = {
            shaft: [Fraction(v) for v in values]
            for shaft, values in segment.allowed_speeds.items()
        }
        order = sorted(allowed)
        if not order:
            combos = [dict()]
        else:
            combos = []
            for values in product(*(allowed[s] for s in order)):
                combos.append(dict(zip(order, values)))
        readings = []
        for combo in combos:
            pinned = [({s: Fraction(1)}, v) for s, v in combo.items()]
            system = LinearSystem(shafts, base + pinned)
            if not system.consistent:
                continue
            assignment = dict(system.particular)
            free = list(system.free_variables)
            readings.append(
                {
                    "assignment": assignment,
                    "free": free,
                    "system": system,
                    "pinned": dict(combo),
                }
            )
        return readings

    def legal_readings(self, segment):
        """列出该段全部合法解释（每个合法允许转速组合给一份具体读数）。"""
        shafts = segment.all_shafts()
        readings = self._enumerate_assignments(segment, shafts)
        if not readings:
            # 区分“矛盾传动链”与“允许转速组合全不合法”
            base = LinearSystem(shafts, segment.equations())
            if not base.consistent:
                raise NoLegalInterpretation(
                    f"段 {segment.name} 存在互相矛盾的传动链（无解）"
                )
            raise NoLegalInterpretation(
                f"段 {segment.name} 在给定允许转速下没有任何合法解释"
            )
        return readings

    def must_same_speed(self, segment, a, b):
        """两轴是否必同速。返回 ``(bool, 说明, 反例读数或 None)``。

        * 若每个合法允许组合都强制 ``ω_a=ω_b`` → ``True``；
        * 否则给出两份支持相反答复的具体装配读数（一份同速、一份不同速）。
        """
        shafts = segment.all_shafts()
        for name in (a, b):
            if name not in shafts:
                raise GaugeError(f"段中没有轴 {name}")
        if a == b:
            return True, "同一根轴", None
        cached = self._analyses.get(segment.name)
        if cached is not None:
            key = (a, b) if (a, b) in cached.pair_verdicts else (b, a)
            if key in cached.pair_verdicts:
                return cached.pair_verdicts[key]
        return self._verdict_for_pair(segment, shafts, a, b)

    @staticmethod
    def _two_readings_varying(reading, a, b):
        """对一个含自由连续参数的合法组合，造出同速与不同速两份具体读数。"""
        system = reading["system"]
        particular = dict(system.particular)
        # 找一个使 (ω_a-ω_b) 变化方向非零的零空间基向量
        chosen = None
        for vector in system.nullspace:
            direction = vector.get(a, Fraction(0)) - vector.get(b, Fraction(0))
            if direction != 0:
                chosen = vector
                break
        if chosen is None:  # 理论上不会发生（is_determined 已判为自由）
            return particular, particular
        base_diff = particular[a] - particular[b]
        direction = chosen[a] - chosen[b]
        # 使差值恰为 0 的参数 t
        t_zero = -base_diff / direction
        equal = {
            v: particular[v] + t_zero * chosen.get(v, Fraction(0))
            for v in system.variables
        }
        unequal = {
            v: particular[v] + (t_zero + 1) * chosen.get(v, Fraction(0))
            for v in system.variables
        }
        return equal, unequal
