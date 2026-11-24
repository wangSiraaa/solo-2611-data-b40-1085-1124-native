"""万花筒芯：花谱约束、制谱台询证、装配/拨动综合与巡展尾签承诺。

物理模型
--------
- 槽 Slot 记作 ``("N", i)`` 近层 / ``("F", i)`` 远层；每槽一种色珠或留空 ``EMPTY``。
- 视线由近到远排列；可见色 = 视线中第一个非空槽的颜色（近层有珠就遮掉远层）。
- 花谱 Chart 给出若干观察；同一张谱可有多个安装朝向（每个朝向是一组观察）。
- 多张谱、两只眼必须由**同一组槽内实珠**解释：同一份组装珠要同时满足所有观察。
- 工单/节目 Program 逐步骤要求当前卡位看到某色；RotationPlan 决定各步卡位，
  带卡锁的步不能拨动近层。
- 库存 Inventory 共用；一枚珠只能进一枚芯，装配用珠数不得超过库存。

本模块只做求解与核验，不改写目录/台账的既有不变量。
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass, field

from .errors import Rejection

EMPTY = ""


# ---------------------------------------------------------------------------
# 基本构件
# ---------------------------------------------------------------------------

def N(i: int) -> tuple[str, int]:
    """近层第 i 个槽。"""
    return ("N", i)


def F(i: int) -> tuple[str, int]:
    """远层第 i 个槽。"""
    return ("F", i)


def _slot_text(slot: tuple[str, int]) -> str:
    return f"{'近' if slot[0] == 'N' else '远'}{slot[1]}"


def _line_text(slots: tuple) -> str:
    return "→".join(_slot_text(s) for s in slots)


def format_assembly(assembly: dict) -> str:
    """把装配写成可复看的“装配单”：近/远槽 = 色名，空槽略过。"""
    parts = [f"{_slot_text(s)}={c}" for s, c in sorted(assembly.items()) if c]
    return ", ".join(parts) if parts else "(全空)"


@dataclass(frozen=True)
class Observation:
    """一条可见性要求：看着某条视线（近→远），最终应呈出 target 色。

    ``source`` 只用于诊断/复看，不参与求解。
    """

    slots: tuple
    target: str
    source: str = ""

    def label(self) -> str:
        return self.source or _line_text(self.slots)


def visible_color(slots: tuple, assembly: dict) -> str | None:
    """近层有珠就遮掉远层：返回视线中第一个非空槽的颜色，全空返回 None。"""
    for slot in slots:
        color = assembly.get(slot, EMPTY)
        if color != EMPTY:
            return color
    return None


def holds(obs: Observation, assembly: dict) -> bool:
    return visible_color(obs.slots, assembly) == obs.target


@dataclass
class Chart:
    """一张花谱：``orientations`` 的每个元素是一种安装朝向下的观察组。"""

    name: str
    orientations: list  # list[list[Observation]]

    @staticmethod
    def fixed(name: str, observations: list) -> "Chart":
        return Chart(name, [list(observations)])


@dataclass(frozen=True)
class Geometry:
    """芯盘几何：近层近盘槽数、远层远盘槽数（远层可为 0）。"""

    near_size: int
    far_size: int = 0

    def slots(self) -> list:
        return [N(i) for i in range(self.near_size)] + [
            F(i) for i in range(self.far_size)
        ]

    def line(self, near_tick: int, far_tick: int, eye: int = 0) -> tuple:
        """当前卡位下，某只眼看到的槽序列（近层在前，远层在后）。

        万花筒的两只眼各自落在圆周的相邻格上：``eye`` 作为整体平移量，
        近/远盘同步偏移，从而不同眼看到不同的格、却仍由同一组装珠解释。
        """
        out = [N((near_tick + eye) % self.near_size)]
        if self.far_size:
            out.append(F((far_tick + eye) % self.far_size))
        return tuple(out)


@dataclass(frozen=True)
class View:
    """一步中某只眼要求呈出的颜色。"""

    target: str
    eye: int = 0


@dataclass(frozen=True)
class ProgramStep:
    """一步演出。

    单眼时用 ``target``（默认 eye=0）；双眼时给 ``views``，两只眼必须由同一
    组装珠同时解释。``views`` 非空时优先于 ``target``。
    """

    target: str = ""
    label: str = ""
    eye: int = 0
    views: tuple = ()

    def resolved_views(self) -> tuple:
        if self.views:
            return tuple(self.views)
        return (View(self.target, self.eye),)


@dataclass
class RotationPlan:
    """按卡位的相对转动。

    - ``initial_near`` / ``initial_far``：起位。
    - ``transitions[t]``：从第 t 步到第 t+1 步的转动，字段 ``near_delta`` /
      ``far_delta`` / ``near_locked``。``near_locked`` 为真时近层不许拨动。
    """

    initial_near: int = 0
    initial_far: int = 0
    transitions: list = field(default_factory=list)

    def ticks(self, geometry: Geometry, n_steps: int) -> list:
        near = self.initial_near
        far = self.initial_far
        nsz = geometry.near_size
        fsz = geometry.far_size
        out = []
        for t in range(n_steps):
            if t > 0:
                tr = self.transitions[t - 1] if t - 1 < len(self.transitions) else {}
                near_delta = 0 if tr.get("near_locked") else tr.get("near_delta", 1)
                far_delta = tr.get("far_delta", 1 if fsz else 0)
                near += near_delta
                far += far_delta
            out.append((near % nsz, (far % fsz) if fsz else 0))
        return out


@dataclass
class Tail:
    """节目尾签：一串步骤 + 默认拨动表（每尾可由一枚芯当场演出）。"""

    name: str
    program: list
    rotation: RotationPlan = field(default_factory=RotationPlan)


# ---------------------------------------------------------------------------
# 约束求解
# ---------------------------------------------------------------------------

def _all_solutions(geometry, observations, inventory, limit=5000, extra_slots=()) -> list:
    """枚举满足全部观察、且不超库存的装配。

    默认只覆盖被观察到的槽；``extra_slots`` 用于把询问用的槽也纳入枚举
    （例如问一个尚未被任何谱约束的槽是否可能取某色）。
    """
    slots = sorted({s for o in observations for s in o.slots} | set(extra_slots))
    colors = sorted(c for c, n in inventory.items() if n > 0)
    domain = [EMPTY] + colors
    solutions: list = []
    assign: dict = {}
    used: Counter = Counter()

    def rec(i: int) -> None:
        if len(solutions) >= limit:
            return
        if i == len(slots):
            if all(holds(o, assign) for o in observations):
                solutions.append({s: assign.get(s, EMPTY) for s in slots})
            return
        slot = slots[i]
        for color in domain:
            if color != EMPTY and used[color] >= inventory.get(color, 0):
                continue
            assign[slot] = color
            if color != EMPTY:
                used[color] += 1
            rec(i + 1)
            if color != EMPTY:
                used[color] -= 1
            del assign[slot]
            if len(solutions) >= limit:
                return

    rec(0)
    return solutions


def _candidate_assemblies(geometry, inventory) -> list:
    """枚举几何下所有具体装芯（含全空），每枚芯单独不超库存。"""
    colors = sorted(c for c, n in inventory.items() if n > 0)
    domain = [EMPTY] + colors
    out = []
    for combo in itertools.product(domain, repeat=len(geometry.slots())):
        used = Counter(c for c in combo if c)
        if all(used[c] <= inventory.get(c, 0) for c in used):
            out.append({s: c for s, c in zip(geometry.slots(), combo)})
    return out


@dataclass
class SynthesisResult:
    status: str
    assembly: dict | None = None
    steps: list = field(default_factory=list)
    chosen: dict = field(default_factory=dict)
    reason: str | None = None
    conflict: dict | None = None
    motion: bool = False
    notes: list = field(default_factory=list)


def _sat(geometry, observations, inventory) -> bool:
    return bool(_all_solutions(geometry, observations, inventory, limit=1))


def _units_feasible(units, geometry, inventory) -> bool:
    fixed: list = []
    charts: list = []
    for _, kind, payload in units:
        if kind == "obs":
            fixed.extend(payload)
        else:
            charts.append(payload)
    combos = itertools.product(*[c.orientations for c in charts]) if charts else [()]
    for combo in combos:
        obs = list(fixed)
        for ori in combo:
            obs.extend(ori)
        if _sat(geometry, obs, inventory):
            return True
    return False


def _minimal_infeasible_units(units, geometry, inventory) -> list:
    for r in range(1, len(units) + 1):
        for subset in itertools.combinations(units, r):
            if not _units_feasible(list(subset), geometry, inventory):
                return list(subset)
    return []


def _conflict_info(units) -> dict | None:
    """从单槽观察里找出争同槽的不同色要求。"""
    req: dict = {}
    for label, kind, payload in units:
        if kind == "obs":
            obs_list = payload
        else:
            chart = payload
            if len(chart.orientations) != 1:
                continue
            obs_list = chart.orientations[0]
        for obs in obs_list:
            if len(obs.slots) == 1:
                req.setdefault(obs.slots[0], []).append((label, obs.target))
    for slot, lst in req.items():
        colors = {c for _, c in lst}
        if len(colors) > 1:
            return {
                "slot": _slot_text(slot),
                "colors": sorted(colors),
                "requirements": [{"label": l, "color": c} for l, c in lst],
            }
    return None


def synthesize(geometry, charts, program, plan, inventory) -> SynthesisResult:
    """综合装配 + 拨动次序：所有步骤、所有眼与所有谱由同一组装珠解释。"""
    n_steps = len(program)
    ticks = plan.ticks(geometry, n_steps)
    program_obs = []
    for i, step in enumerate(program):
        nt, ft = ticks[i]
        label = step.label or f"step{i}"
        for view in step.resolved_views():
            program_obs.append(
                Observation(
                    geometry.line(nt, ft, view.eye), view.target, label
                )
            )

    combos = itertools.product(*[c.orientations for c in charts]) if charts else [()]
    for combo in combos:
        obs = list(program_obs)
        chosen: dict = {}
        for chart, ori in zip(charts, combo):
            obs.extend(ori)
            chosen[chart.name] = tuple(ori)
        sols = _all_solutions(geometry, obs, inventory, limit=1)
        if sols:
            assembly = sols[0]
            steps = []
            for i, step in enumerate(program):
                nt, ft = ticks[i]
                views = [
                    {
                        "eye": v.eye,
                        "line": geometry.line(nt, ft, v.eye),
                        "target": v.target,
                        "observed": visible_color(
                            geometry.line(nt, ft, v.eye), assembly
                        ),
                    }
                    for v in step.resolved_views()
                ]
                steps.append({
                    "step": i,
                    "label": step.label or f"step{i}",
                    "near_tick": nt,
                    "far_tick": ft,
                    "views": views,
                })
            return SynthesisResult("ok", assembly, steps, chosen)

    # ---- 无解：先用“同一槽被要求两种颜色”定位本质冲突，再回退到最小子集 ----
    units = []
    for i, step in enumerate(program):
        nt, ft = ticks[i]
        label = step.label or f"step{i}"
        units.append((
            label,
            "obs",
            [
                Observation(geometry.line(nt, ft, v.eye), v.target, label)
                for v in step.resolved_views()
            ],
        ))
    for chart in charts:
        units.append((chart.name, "chart", chart))

    conflict = _conflict_info(units)  # 全量同槽争色（与库存无关）

    motion = False
    for t in range(1, n_steps):
        tr = plan.transitions[t - 1] if t - 1 < len(plan.transitions) else {}
        if tr.get("near_locked") and ticks[t][0] == ticks[t - 1][0]:
            motion = True

    if conflict is not None:
        reason = "motion_conflict" if motion else "color_conflict"
        labels = sorted({r["label"] for r in conflict["requirements"]})
        return SynthesisResult(
            "infeasible", reason=reason, conflict=conflict,
            motion=motion, notes=labels,
        )

    # 没有显式同槽争色时，退回最小不可行子集（可能是库存或视线结构所致）
    minimal = _minimal_infeasible_units(units, geometry, inventory)
    reason = "motion_conflict" if motion else "perception_conflict"
    labels = [label for label, _, _ in minimal]
    return SynthesisResult(
        "infeasible", reason=reason, conflict={"labels": labels},
        motion=motion, notes=labels,
    )


# ---------------------------------------------------------------------------
# 制谱台询证：必定 / 可能 / 抵触 + 可复看的装芯例
# ---------------------------------------------------------------------------

class ChartDesk:
    def __init__(self, geometry: Geometry, inventory: Counter) -> None:
        self.geometry = geometry
        self.inventory = inventory

    def _solutions(self, charts, extra=(), extra_slots=()) -> list:
        combos = itertools.product(*[c.orientations for c in charts]) if charts else [()]
        out = []
        for combo in combos:
            obs = list(extra)
            chosen: dict = {}
            for chart, ori in zip(charts, combo):
                obs.extend(ori)
                chosen[chart.name] = tuple(ori)
            for asm in _all_solutions(
                self.geometry, obs, self.inventory, limit=2000,
                extra_slots=extra_slots,
            ):
                out.append((asm, chosen))
        return out

    def ask(self, charts, slot, color) -> dict:
        """问：这些谱由同一枚芯照出时，槽 ``slot`` 是否必为 ``color``？"""
        sols = self._solutions(charts, extra_slots=(slot,))
        if not sols:
            return {
                "verdict": "contradiction",
                "reason": "charts_jointly_infeasible",
                "conflict": self.minimal_conflict(charts),
            }
        with_c = [s for s in sols if s[0].get(slot, EMPTY) == color]
        without = [s for s in sols if s[0].get(slot, EMPTY) != color]
        if not with_c:
            others = sorted({s[0].get(slot, EMPTY) for s in without})
            return {
                "verdict": "contradiction",
                "reason": "slot_cannot_take_color",
                "possible_colors": others,
                "witness": without[0][0],
                "chosen": without[0][1],
            }
        if not without:
            return {"verdict": "certain", "witness": with_c[0][0], "chosen": with_c[0][1]}
        return {
            "verdict": "possible",
            "witness": with_c[0][0],
            "chosen": with_c[0][1],
            "counter_witness": without[0][0],
            "counter_chosen": without[0][1],
        }

    def minimal_conflict(self, charts) -> dict | None:
        for r in range(1, len(charts) + 1):
            for subset in itertools.combinations(charts, r):
                if not self._solutions(list(subset)):
                    units = [(c.name, "chart", c) for c in subset]
                    return {
                        "charts": [c.name for c in subset],
                        "detail": _conflict_info(units),
                    }
        return None


def _conflict_info(units) -> dict | None:
    req: dict = {}
    for label, kind, payload in units:
        if kind == "obs":
            obs_list = payload
        else:
            chart = payload
            if len(chart.orientations) != 1:
                continue
            obs_list = chart.orientations[0]
        for obs in obs_list:
            if len(obs.slots) == 1:
                req.setdefault(obs.slots[0], []).append((label, obs.target))
    for slot, lst in req.items():
        colors = {c for _, c in lst}
        if len(colors) > 1:
            return {
                "slot": _slot_text(slot),
                "colors": sorted(colors),
                "requirements": [{"label": l, "color": c} for l, c in lst],
            }
    return None


# ---------------------------------------------------------------------------
# 巡展：单封芯（A）/ 备用芯（B）两种交付下的最大可承诺尾签集
# ---------------------------------------------------------------------------

def core_fulfills(geometry, assembly, tail: Tail, plans=None) -> bool:
    plans = plans or [tail.rotation]
    for plan in plans:
        ticks = plan.ticks(geometry, len(tail.program))
        ok = True
        for i, step in enumerate(tail.program):
            nt, ft = ticks[i]
            for view in step.resolved_views():
                if visible_color(
                    geometry.line(nt, ft, view.eye), assembly
                ) != view.target:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return True
    return False


def _coverage(candidates, geometry, tails):
    return [[t.name for t in tails if core_fulfills(geometry, asm, t)] for asm in candidates]


def max_single_core(tails, geometry, inventory) -> dict:
    """交付 A：只封一枚芯，必须用同一具体装配兑现集合内每一尾。

    注意：这里枚举的是具体装芯，而非“可能红也可能蓝”的未装芯——
    后者封筒后只剩一种颜色，不能算作两种都能兑现。
    """
    candidates = _candidate_assemblies(geometry, inventory)
    covers = _coverage(candidates, geometry, tails)
    best = {"size": -1, "assembly": None, "covered": []}
    for asm, cov in zip(candidates, covers):
        if len(cov) > best["size"]:
            best = {"size": len(cov), "assembly": asm, "covered": sorted(cov)}
    return best


def max_spare_cores(tails, geometry, inventory, max_cores: int) -> dict:
    """交付 B：预先封若干备用芯，现场换整筒；真实珠子跨芯不重复占用。"""
    candidates = _candidate_assemblies(geometry, inventory)
    covers = _coverage(candidates, geometry, tails)
    best = {"size": -1, "cores": [], "covered": []}
    for r in range(1, max_cores + 1):
        for combo in itertools.combinations(range(len(candidates)), r):
            used: Counter = Counter()
            ok = True
            for idx in combo:
                for color in candidates[idx].values():
                    if color:
                        used[color] += 1
                        if used[color] > inventory.get(color, 0):
                            ok = False
                            break
                if not ok:
                    break
            if not ok:
                continue
            covered = set()
            for idx in combo:
                covered.update(covers[idx])
            if len(covered) > best["size"]:
                best = {
                    "size": len(covered),
                    "cores": [candidates[i] for i in combo],
                    "covered": sorted(covered),
                }
    return best


def exhibition_plan(tails, geometry, inventory, max_cores: int = 2) -> dict:
    """巡展决策：分别求交付 A（单封芯）与 B（备用芯）的最大可承诺尾签集，
    按可承诺数量择优，并给出装配单与承诺尾签。

    两者都只在**具体装芯**上计数：一枚封筒后的芯只有一种实际颜色，
    未装芯的“也许红也许蓝”不计入任何尾签。
    """
    single = max_single_core(tails, geometry, inventory)
    spare = max_spare_cores(tails, geometry, inventory, max_cores)
    if spare["size"] > single["size"]:
        chosen = "B_spare_cores"
        best = spare
        assemblies = spare["cores"]
    else:
        chosen = "A_single_core"
        best = single
        assemblies = [single["assembly"]]
    return {
        "delivery": chosen,
        "commitment": best["covered"],
        "size": best["size"],
        "assemblies": assemblies,
        "single": single,
        "spare": spare,
    }


def verify_run(geometry, assembly, program, plan) -> dict:
    """复看一次演出：装配固定（不换珠），逐步核对所见颜色。"""
    ticks = plan.ticks(geometry, len(program))
    steps = []
    ok = True
    for i, step in enumerate(program):
        nt, ft = ticks[i]
        views = []
        for view in step.resolved_views():
            line = geometry.line(nt, ft, view.eye)
            observed = visible_color(line, assembly)
            good = observed == view.target
            ok = ok and good
            views.append({
                "eye": view.eye,
                "line": line,
                "target": view.target,
                "observed": observed,
                "ok": good,
            })
        steps.append({
            "step": i,
            "label": step.label or f"step{i}",
            "near_tick": nt,
            "far_tick": ft,
            "views": views,
        })
    return {"ok": ok, "assembly": dict(assembly), "steps": steps}


# ---------------------------------------------------------------------------
# 教具坊：借用馆谱（按领到副本的制版扇区表）、共用实珠库存与取件回执
# ---------------------------------------------------------------------------

class Workshop:
    """把万花筒求解接到 library 的目录/副本/回执之上。

    与台账一致的做法：
    - 花谱按“本次领到的副本”所附**扇区表**选取制版；不同副本可有不同制版。
    - 实珠库存是全局共用的；登记装配时按真实珠子扣减，不能把同一枚珠
      填进两枚芯（``commit_assembly`` 会校验并抛出 ``Rejection``）。
    - 每次询证/综合/交付都落一条回执，可复看。
    """

    def __init__(self, system=None, inventory=None, geometry: Geometry | None = None) -> None:
        self.system = system
        self.geometry = geometry or Geometry(1, 0)
        self.inventory: Counter = Counter(inventory or {})
        self.formats: dict = {}          # copy_id -> {revision: sector table}
        self.receipts: list = []

    # ---- 制版：按副本领到的扇区表取花谱 ----
    def attach_sector_table(self, copy_id: str, revision: str, table: dict) -> None:
        """登记副本在某制版下的扇区表（表给出每眼看到的格如何回映到槽）。"""
        self.formats.setdefault(copy_id, {})[revision] = table

    def charts_for(self, copy_id: str, revision: str, geometry: Geometry,
                   eye: int = 0, near_tick: int = 0, far_tick: int = 0) -> list:
        """把副本扇区表转成该副本的若干花谱（每个可见格一条观察）。"""
        table = self.formats[copy_id][revision]
        obs = []
        for cell, spec in sorted(table.items()):
            slots = tuple(
                N((near_tick + int(s)) % geometry.near_size)
                if layer == "N" else F((far_tick + int(s)) % geometry.far_size)
                for layer, s in spec["slots"]
            )
            obs.append(Observation(slots, spec["color"], f"{copy_id}:{cell}"))
        return [Chart.fixed(f"{copy_id}@{revision}", obs)]

    # ---- 库存：真实珠子扣减 ----
    def _consume(self, assemblies: list) -> Counter:
        need: Counter = Counter()
        for asm in assemblies:
            for color in asm.values():
                if color:
                    need[color] += 1
        for color, n in need.items():
            if n > self.inventory.get(color, 0):
                raise Rejection(
                    "insufficient_beads", color=color,
                    need=n, have=self.inventory.get(color, 0),
                )
        return need

    def commit_assembly(self, op_id: str, assemblies: list) -> dict:
        """按一枚或多枚芯扣减共用库存并落回执；珠子不足则拒绝且不扣减。"""
        if any(r["op_id"] == op_id for r in self.receipts):
            existing = next(r for r in self.receipts if r["op_id"] == op_id)
            return existing
        try:
            need = self._consume(assemblies)
        except Rejection as rej:
            result = {"status": "rejected", "op_id": op_id, "error": rej.as_dict()}
            self.receipts.append(result)
            return result
        for color, n in need.items():
            self.inventory[color] -= n
        result = {
            "status": "ok",
            "op_id": op_id,
            "assemblies": [dict(a) for a in assemblies],
            "consumed": dict(need),
            "inventory_after": {k: v for k, v in self.inventory.items() if v},
        }
        self.receipts.append(result)
        return result

    def borrow_from_copy(self, copy_id: str, revision: str) -> dict:
        """从馆藏实物的副本取件；若挂了 catalog/ledger 则走取件规则校验。"""
        if self.system is not None:
            copy = self.system.ledger._copy_snapshot(revision, copy_id)
        else:
            copy = {"copy_id": copy_id, "revision": revision}
        out = {"status": "ok", "copy": copy_id, "revision": revision,
               "sector_table_available": revision in self.formats.get(copy_id, {})}
        self.receipts.append({"op_id": f"borrow-{copy_id}-{revision}", **out})
        return out

    # ---- 询证 / 综合 / 巡展：透传并落回执 ----
    def inquire(self, op_id: str, charts, slot, color) -> dict:
        desk = ChartDesk(self.geometry, self.inventory)
        result = desk.ask(charts, slot, color)
        self.receipts.append({"op_id": op_id, "kind": "inquire", **result})
        return result

    def synthesize(self, op_id: str, charts, program, plan) -> dict:
        result = synthesize(self.geometry, charts, program, plan, self.inventory)
        self.receipts.append({
            "op_id": op_id, "kind": "synthesize", "status": result.status,
            "assembly": result.assembly, "reason": result.reason,
        })
        return result

    def plan_tour(self, op_id: str, tails, max_cores: int = 2) -> dict:
        result = exhibition_plan(tails, self.geometry, self.inventory, max_cores)
        self.receipts.append({"op_id": op_id, "kind": "plan_tour", **result})
        return result
