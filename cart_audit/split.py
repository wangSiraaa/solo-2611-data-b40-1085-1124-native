"""两位物主整分馆藏：静态分家单与当场分家协议。

术语：

- **设计（Design）**：一对互补实物包，记乙包 ``S``（盒集合），甲包为其余盒；
  每件原盒只出现在一个包里，整组遗赠不拆，既有签收固定的盒不可翻面。
- **静态分家单**：甲乙系数都公开时，只有在*每个可行称量解释*中两人各自
  看来自己所得均不少于另一份，两边才肯签。
- **当场分家**：甲只知道乙可能携带的系数单名单，不知道乙实际带哪张。

  - 通用分包（无保密签）：甲封一对包，须对*每张*可能私单、*每个*可行真重
    解释都成立；乙看到真重后取自己更值的一包，同值任挑。
  - 受托选款（一张有号保密签）：甲先登记至多两款纸包样，计量员在屏后对照
    乙私单只盖一次章，只回传被选中的款号，不传出任何数值；签一次性，
    隔室借过的签不得复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .weighing import FeasibleWeights, LinearConstraint

__all__ = [
    "Design",
    "SplitCase",
    "SplitInputError",
    "ProtocolMisuse",
    "NoWorkableTemplate",
    "SealRegistry",
    "SealStatus",
    "SplitCounter",
]


class SplitInputError(ValueError):
    """分家输入非法（盒、组、签收、系数等）。"""


class ProtocolMisuse(Exception):
    """违反当场分家协议：复用保密签、打听私单、纸包样不合规等。"""


class NoWorkableTemplate(Exception):
    """登记的纸包样没有一款能对乙的实际私单成立。"""


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SplitInputError(f"{name} 必须是非负整数系数: {value!r}")
    return value


@dataclass(frozen=True)
class Design:
    """一款互补实物包：``b_pack`` 归乙候选，其余盒归甲候选。"""

    name: str
    b_pack: frozenset

    def a_pack(self, box_ids: Sequence[str]) -> frozenset:
        return frozenset(box_ids) - self.b_pack

    def to_dict(self, box_ids: Sequence[str]) -> dict:
        return {
            "name": self.name,
            "b_pack": sorted(self.b_pack),
            "a_pack": sorted(self.a_pack(box_ids)),
        }


class SplitCase:
    """一次分家的完整题面与联合可行称量解释。"""

    def __init__(
        self,
        case_id: str,
        box_ids: Sequence[str],
        feasible: FeasibleWeights,
        alpha: Dict[str, int],
        beta_sheets: Dict[str, Dict[str, int]],
        groups: Optional[Sequence[Sequence[str]]] = None,
        fixed: Optional[Dict[str, str]] = None,
    ):
        if not isinstance(case_id, str) or not case_id:
            raise SplitInputError("case_id 必须是非空字符串")
        self.case_id = case_id
        self.box_ids = tuple(box_ids)
        if len(set(self.box_ids)) != len(self.box_ids):
            raise SplitInputError("盒名单重复")
        self.feasible = feasible
        if tuple(feasible.case.box_ids) != self.box_ids:
            raise SplitInputError("称量可行域的盒名单与分家题面不一致")
        self.alpha = {box: _nonnegative_int(alpha.get(box), f"alpha[{box}]") for box in self.box_ids}
        if not any(self.alpha.values()):
            raise SplitInputError("甲的系数全为 0，无法定义价值比较")
        if not beta_sheets:
            raise SplitInputError("至少要给出一张乙的可能系数单")
        self.beta_sheets = {}
        for sheet_id, coeffs in beta_sheets.items():
            if not isinstance(sheet_id, str) or not sheet_id:
                raise SplitInputError("乙系数单 id 必须是非空字符串")
            sheet = {
                box: _nonnegative_int(coeffs.get(box), f"beta[{sheet_id}][{box}]")
                for box in self.box_ids
            }
            if not any(sheet.values()):
                raise SplitInputError(f"乙系数单 {sheet_id!r} 全为 0，无法定义价值比较")
            self.beta_sheets[sheet_id] = sheet
        self.groups: Tuple[frozenset, ...] = tuple(
            self._validate_group(group, i) for i, group in enumerate(groups or ())
        )
        self.fixed: Dict[str, str] = {}
        for box, owner in (fixed or {}).items():
            if box not in set(self.box_ids):
                raise SplitInputError(f"既有签收指向未知盒 {box!r}")
            if owner not in ("A", "B"):
                raise SplitInputError(f"签收业主只能是 'A'/'B'，收到 {owner!r}")
            self.fixed[box] = owner
        self._check_group_fixed_consistency()
        if not feasible.is_feasible():
            raise SplitInputError("称量解释集合为空，无法分家")

    def _validate_group(self, group: Sequence[str], index: int) -> frozenset:
        members = frozenset(group)
        if not members or members - set(self.box_ids):
            raise SplitInputError(f"第 {index} 组遗赠为空或含未知盒: {list(group)!r}")
        return members

    def _check_group_fixed_consistency(self) -> None:
        # 组间不得重叠；同组若有签收固定，全组业主必须相同。
        seen = set()
        for group in self.groups:
            if seen & group:
                raise SplitInputError(f"遗赠组重叠: {sorted(seen & group)}")
            seen |= group
            owners = {self.fixed[box] for box in group if box in self.fixed}
            if len(owners) > 1:
                raise SplitInputError(f"遗赠组 {sorted(group)} 内既有签收分属两人，无法整组同人")

    # ---- 设计合规性 ----------------------------------------------------
    def validate_design(self, design: Design) -> None:
        unknown = design.b_pack - set(self.box_ids)
        if unknown:
            raise SplitInputError(f"设计 {design.name!r} 含未知盒: {sorted(unknown)}")
        for group in self.groups:
            if design.b_pack & group and not group <= design.b_pack:
                raise SplitInputError(
                    f"设计 {design.name!r} 拆散了必须整组同人的遗赠组 {sorted(group)}"
                )
        for box, owner in self.fixed.items():
            in_b = box in design.b_pack
            if owner == "B" and not in_b:
                raise SplitInputError(f"设计 {design.name!r} 违反既有签收：{box} 已固定给乙")
            if owner == "A" and in_b:
                raise SplitInputError(f"设计 {design.name!r} 违反既有签收：{box} 已固定给甲")

    def enumerate_designs(self) -> List[Design]:
        """枚举全部合规设计（按组块/固定块收缩，最多八盒 => 至多 256 款）。"""
        blocks: List[frozenset] = []
        covered = set()
        for group in self.groups:
            blocks.append(group)
            covered |= group
        for box in self.box_ids:
            if box not in covered:
                blocks.append(frozenset([box]))
                covered.add(box)
        # 固定业主的块只有一种朝向；自由块两种朝向。
        free_blocks = []
        forced_b = frozenset()
        for block in blocks:
            owners = {self.fixed[box] for box in block if box in self.fixed}
            if owners == {"B"}:
                forced_b |= block
            elif owners == {"A"}:
                continue
            else:
                free_blocks.append(block)
        designs: List[Design] = []
        for mask in range(2 ** len(free_blocks)):
            b_pack = set(forced_b)
            for i, block in enumerate(free_blocks):
                if mask & (1 << i):
                    b_pack |= block
            designs.append(Design(f"D{mask:0{len(free_blocks)}b}", frozenset(b_pack)))
        for design in designs:
            self.validate_design(design)
        return designs

    # ---- 线性式工具 ----------------------------------------------------
    def _box_constraint(self, coeff: Dict[str, int], low: int, high: int, name: str) -> LinearConstraint:
        full = [0] * (len(self.box_ids) + 1)  # 0 号位留给零点 z，盒重约束中 z 系数为 0
        for box, c in coeff.items():
            full[self.box_ids.index(box) + 1] = c
        return LinearConstraint(name=name, coeff=tuple(full), low=low, high=high)

    def _vectors(self, design: Design, beta: Dict[str, int]) -> Tuple[Dict[str, int], Dict[str, int]]:
        """返回 (g, L)：g=乙眼中(乙包-甲包)，L=甲眼中(甲包-乙包)。"""
        g_vec: Dict[str, int] = {}
        l_vec: Dict[str, int] = {}
        for box in self.box_ids:
            sign_b = 1 if box in design.b_pack else -1
            g_vec[box] = sign_b * beta[box]
            l_vec[box] = -sign_b * self.alpha[box]
        return g_vec, l_vec

    def _exists(self, constraints: Sequence[LinearConstraint]) -> bool:
        return self.feasible.feasible_with(list(constraints)) is not None

    def _witness(self, constraints: Sequence[LinearConstraint]) -> Optional[dict]:
        answer = self.feasible.feasible_with(list(constraints))
        return None if answer is None else self.feasible._as_mapping(answer)

    # ---- 静态分家单：系数公开，逐可行解释双方都不眼红 ------------------
    def static_signable(self, design: Design, beta: Dict[str, int]) -> dict:
        self.validate_design(design)
        g_vec, l_vec = self._vectors(design, beta)
        a_min, a_witness = self.feasible.extreme_box_sum(l_vec, find_min=True)
        b_min, b_witness = self.feasible.extreme_box_sum(g_vec, find_min=True)
        ok = a_min >= 0 and b_min >= 0
        report = {
            "design": design.to_dict(self.box_ids),
            "signable": ok,
            "a_margin_min": a_min,
            "b_margin_min": b_min,
        }
        if a_min < 0:
            report["a_counterexample"] = a_witness
        if b_min < 0:
            report["b_counterexample"] = b_witness
        return report

    def find_static_partition(self, beta: Dict[str, int]) -> dict:
        """在全部合规设计中找两边都肯签的静态分家单，找不到给无解依据。"""
        failures = []
        for design in self.enumerate_designs():
            report = self.static_signable(design, beta)
            if report["signable"]:
                return {"found": True, "partition": report}
            failures.append(
                {
                    "design": design.to_dict(self.box_ids),
                    "a_margin_min": report["a_margin_min"],
                    "b_margin_min": report["b_margin_min"],
                }
            )
        return {
            "found": False,
            "reason": "全部合规设计都存在某个可行称量解释使一方眼红",
            "checked": len(failures),
            "failures": failures,
        }

    # ---- 当场分家：对一张乙私单，承包乙任挑（含同值任挑）后甲仍成立 ----
    def works_for_sheet(self, design: Design, sheet_id: str) -> dict:
        self.validate_design(design)
        if sheet_id not in self.beta_sheets:
            raise SplitInputError(f"未知乙私单 {sheet_id!r}")
        beta = self.beta_sheets[sheet_id]
        g_vec, l_vec = self._vectors(design, beta)
        g_con = self._box_constraint(g_vec, -10 ** 9, 10 ** 9, "g")
        l_con = self._box_constraint(l_vec, -10 ** 9, 10 ** 9, "L")

        def tighten(con: LinearConstraint, low: Optional[int], high: Optional[int]) -> LinearConstraint:
            return LinearConstraint(
                name=con.name,
                coeff=con.coeff,
                low=con.low if low is None else low,
                high=con.high if high is None else high,
            )

        greater_exists = self._exists([tighten(g_con, 1, None)])
        less_exists = self._exists([tighten(g_con, None, -1)])
        tie_exists = self._exists([tighten(g_con, 0, 0)])

        violations: List[dict] = []
        # 乙取乙包（g>0）：甲包价值不得低于乙包 => L >= 0
        if greater_exists:
            witness = self._witness([tighten(g_con, 1, None), tighten(l_con, None, -1)])
            if witness is not None:
                violations.append({"region": "b_prefers_b_pack", "counterexample": witness})
        # 乙取甲包（g<0）：乙包留给甲，要求 -L >= 0（即 L <= 0）。
        # 既有签收已由 validate_design 在结构上强制（固定给乙的盒必在乙包，
        # 乙挑走甲包时碰不到它们），此处不再重复判定。
        if less_exists:
            witness = self._witness([tighten(g_con, None, -1), tighten(l_con, 1, None)])
            if witness is not None:
                violations.append({"region": "b_prefers_a_pack", "counterexample": witness})
        # 同值任挑（g=0）：两种取法都得让甲不眼红 => L=0。
        # 同值世界里乙可自由翻面，签收影响由 tie_impact 单独给出。
        if tie_exists:
            low_witness = self._witness([tighten(g_con, 0, 0), tighten(l_con, None, -1)])
            if low_witness is not None:
                violations.append({"region": "tie", "counterexample": low_witness})
            high_witness = self._witness([tighten(g_con, 0, 0), tighten(l_con, 1, None)])
            if high_witness is not None:
                violations.append({"region": "tie", "counterexample": high_witness})
        return {
            "sheet_id": sheet_id,
            "design": design.to_dict(self.box_ids),
            "works": not violations,
            "regions": {
                "b_prefers_b_pack_possible": greater_exists,
                "b_prefers_a_pack_possible": less_exists,
                "tie_possible": tie_exists,
            },
            "violations": violations,
        }

    def seal_without_disclosure(self, design: Design) -> dict:
        """通用分包：甲独立封包，须对乙名单上每张私单都成立。"""
        reports = {
            sheet_id: self.works_for_sheet(design, sheet_id)
            for sheet_id in self.beta_sheets
        }
        return {
            "mode": "generic_sealed_pack",
            "design": design.to_dict(self.box_ids),
            "works_for_all_sheets": all(r["works"] for r in reports.values()),
            "per_sheet": reports,
        }

    def find_generic_design(self) -> dict:
        """无保密签时寻找一款对所有可能私单都成立的封包；无解给依据。"""
        failures = []
        for design in self.enumerate_designs():
            report = self.seal_without_disclosure(design)
            if report["works_for_all_sheets"]:
                return {"found": True, **report}
            failures.append(
                {
                    "design": design.to_dict(self.box_ids),
                    "failing_sheets": [
                        sheet_id
                        for sheet_id, item in report["per_sheet"].items()
                        if not item["works"]
                    ],
                }
            )
        return {
            "found": False,
            "reason": "不存在对乙全部可能私单、全部可行真重解释都成立的单一封包",
            "checked": len(failures),
            "failures": failures,
        }

    def find_templates(self, limit: int = 2) -> dict:
        """受托选款：找至多 ``limit`` 款纸包样，使每张私单至少被一款覆盖。"""
        if limit < 1 or limit > 2:
            raise SplitInputError("一张保密签至多登记两款纸包样")
        sheet_ids = list(self.beta_sheets)
        coverage: List[Tuple[Design, List[str], dict]] = []
        for design in self.enumerate_designs():
            covered = []
            reports = {}
            for sheet_id in sheet_ids:
                report = self.works_for_sheet(design, sheet_id)
                reports[sheet_id] = report
                if report["works"]:
                    covered.append(sheet_id)
            if covered:
                coverage.append((design, covered, reports))
        # 先尝试单款覆盖全部，再尝试两款并集覆盖。
        for design, covered, _ in coverage:
            if set(covered) == set(sheet_ids):
                return {
                    "found": True,
                    "templates": [design.to_dict(self.box_ids)],
                    "coverage": {sheet_id: 0 for sheet_id in sheet_ids},
                }
        if limit == 2:
            for i, (d1, c1, _) in enumerate(coverage):
                for d2, c2, _ in coverage[i + 1:]:
                    if d1.b_pack == d2.b_pack:
                        continue  # 两款必须是不同的实物装法，不许同款登记两次
                    if set(c1) | set(c2) == set(sheet_ids):
                        templates = [d1.to_dict(self.box_ids), d2.to_dict(self.box_ids)]
                        return {
                            "found": True,
                            "templates": templates,
                            "coverage": {
                                sheet_id: [k for k, design in enumerate((d1, d2))
                                           if sheet_id in (c1, c2)[k]]
                                for sheet_id in sheet_ids
                            },
                        }
        return {
            "found": False,
            "reason": f"{limit} 款纸包样无法覆盖乙的全部可能私单",
            "sheet_ids": sheet_ids,
        }

    def tie_impact(self, design: Design, sheet_id: str) -> dict:
        """同值（g=0）可随意挑时，逐条实挑路径核对既有签收是否被翻面。

        乙面前是两个实物包：乙包 ``b_pack`` 与甲包 ``a_pack``。同值世界里
        乙可任挑一包，剩下的归甲。固定签收的盒若落在乙挑走的包里，其签收
        业主就必须是乙，否则该实挑路径无法兑现签收。
        """
        self.validate_design(design)
        beta = self.beta_sheets[sheet_id]
        g_vec, _ = self._vectors(design, beta)
        g_con = self._box_constraint(g_vec, 0, 0, "g=0")
        tie_world = self._witness([g_con])
        if tie_world is None or not self.fixed:
            return {
                "design": design.to_dict(self.box_ids),
                "sheet_id": sheet_id,
                "tie_possible": tie_world is not None,
                "tie_world": tie_world,
                "affected_fixed_boxes": [],
            }
        affected: List[dict] = []
        for box, owner in sorted(self.fixed.items()):
            # 乙挑乙包时带走 b_pack，挑甲包时带走 a_pack；被带走的盒
            # 签收业主必须是乙，固定给甲的盒在该路径上即被破坏。
            if box in design.b_pack:
                broken_pick = "b_pack"  # 乙挑走乙包 => 固定给甲的盒被带走
            else:
                broken_pick = "a_pack"  # 乙挑走甲包 => 固定给乙的盒被带走
            affected.append(
                {
                    "box_id": box,
                    "fixed_owner": owner,
                    "broken_when_b_takes": broken_pick,
                    "pack_holding_box": "b_pack" if box in design.b_pack else "a_pack",
                    "tie_world": tie_world,
                }
            )
        return {
            "design": design.to_dict(self.box_ids),
            "sheet_id": sheet_id,
            "tie_possible": True,
            "tie_world": tie_world,
            "affected_fixed_boxes": affected,
        }


class SealStatus:
    AVAILABLE = "available"
    CONSUMED = "consumed"


class SealRegistry:
    """有号保密签台账：签发、一次性使用、跨柜复用可检出。"""

    def __init__(self) -> None:
        self._seals: Dict[str, dict] = {}

    def issue(self, seal_id: str, counter: str = "本柜") -> None:
        if not isinstance(seal_id, str) or not seal_id:
            raise ProtocolMisuse("保密签号必须是非空字符串")
        if seal_id in self._seals:
            raise ProtocolMisuse(f"保密签 {seal_id!r} 已登记")
        self._seals[seal_id] = {"status": SealStatus.AVAILABLE, "counter": counter, "record": None}

    def status(self, seal_id: str) -> dict:
        if seal_id not in self._seals:
            raise ProtocolMisuse(f"未知保密签 {seal_id!r}")
        return dict(self._seals[seal_id])

    def consume(self, seal_id: str, record: dict) -> None:
        if seal_id not in self._seals:
            raise ProtocolMisuse(f"未知保密签 {seal_id!r}")
        if self._seals[seal_id]["status"] == SealStatus.CONSUMED:
            raise ProtocolMisuse(
                f"保密签 {seal_id!r} 已在 {self._seals[seal_id]['record']} 使用，不得复用"
            )
        self._seals[seal_id] = {
            "status": SealStatus.CONSUMED,
            "counter": self._seals[seal_id]["counter"],
            "record": record,
        }


class SplitCounter:
    """柜台办理：围绕通用分包或受托选款给出本单实际承诺。"""

    MAX_TEMPLATES = 2

    def __init__(self, split_case: SplitCase, seals: Optional[SealRegistry] = None):
        self.case = split_case
        self.seals = seals or SealRegistry()
        self._templates: Optional[List[Design]] = None

    # ---- 方式一：甲独立封包，不允许打听乙带哪张私单 --------------------
    def generic_seal(self, design: Design, actual_sheet: Optional[str] = None) -> dict:
        """甲独立封包渠道：不接收乙私单、不经过也不消耗有号保密签。

        保密签是受托选款渠道的专用凭证；通用分包靠“对所有可能私单都成立”
        自我担保，若把唯一的签用在这里，受托选款便无签可用。
        """
        if actual_sheet is not None:
            # 后台读到乙私单替甲选款属误办：本渠道根本不接收私单参数。
            raise ProtocolMisuse("通用封包不得使用乙的实际私单；私单只能交中立计量员")
        report = self.case.seal_without_disclosure(design)
        if not report["works_for_all_sheets"]:
            raise ProtocolMisuse(
                f"设计 {design.name!r} 不能对乙全部可能私单成立，甲不得封此包"
            )
        return {"seal_id": None, **report}

    # ---- 方式二：查验前登记至多两款纸包样 ------------------------------
    def register_templates(self, designs: Sequence[Design]) -> dict:
        if not designs or len(designs) > self.MAX_TEMPLATES:
            raise ProtocolMisuse(f"至多登记 {self.MAX_TEMPLATES} 款纸包样")
        if len({d.b_pack for d in designs}) != len(designs):
            raise ProtocolMisuse("两款纸包样必须是不同装法，不许把同一实盒装两次")
        for design in designs:
            self.case.validate_design(design)
        self._templates = list(designs)
        return {
            "case_id": self.case.case_id,
            "templates": [d.to_dict(self.case.box_ids) for d in designs],
        }

    def officer_stamp(self, seal_id: str, actual_sheet: str) -> dict:
        """计量员屏后核实私单并只盖一次章；只回传款号，不传出任何数值。"""
        if self._templates is None:
            raise ProtocolMisuse("尚未登记纸包样，无法盖章")
        if actual_sheet not in self.case.beta_sheets:
            raise SplitInputError(f"乙私单 {actual_sheet!r} 不在甲可见的可能名单内")
        # 先验签：复用的签在任何核查前就被拒绝。
        seal_state = self.seals.status(seal_id)
        if seal_state["status"] == SealStatus.CONSUMED:
            raise ProtocolMisuse(f"保密签 {seal_id!r} 已使用（含隔室借用），不得复用")
        chosen: Optional[Tuple[int, Design, dict]] = None
        for index, design in enumerate(self._templates):
            report = self.case.works_for_sheet(design, actual_sheet)
            if report["works"] and chosen is None:
                chosen = (index, design, report)
        if chosen is None:
            raise NoWorkableTemplate(
                f"登记的 {len(self._templates)} 款纸包样没有一款对私单 {actual_sheet!r} 成立"
            )
        index, design, report = chosen
        record = {
            "mode": "officer_selected_template",
            "case": self.case.case_id,
            "template_index": index,
        }
        self.seals.consume(seal_id, record)
        # 回传不含乙私单 id 之外的任何数值；私单内容不出现。
        return {
            "seal_id": seal_id,
            "stamped_template_index": index,
            "design_to_pack": design.to_dict(self.case.box_ids),
            "sheet_used_behind_screen": actual_sheet,
        }
