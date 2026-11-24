"""小管风琴调律工单规划：站在 ``budget_solver`` 精确有理数内核之上。

两层职责，边界清晰：

1. **测量层（真实快照）** —— 合奏测音卡只给若干管的差值或和，连同“所有管的
   共同零位 ``t`` 是自由参数”一起登记为一个 ``budget_solver`` 模型，固定成真实
   快照后求解。于是每根管自身**不确定**（parametric，随 ``t`` 变），某些差值/和却
   被**唯一确定**——这正是“两管各有不确定偏差也可能音程完全确定”。同一份快照
   只解一次，前后查询共用同一 ``solve_id``，不会“各挑一次误差”。

2. **装配层（精确 Fraction）** —— 在已确定的相对零位上，按**真实联动**模拟拨栓、
   脱开、封区，规划班次工单并沿真实装配复核两套和弦。全程 ``Fraction``，无浮点。

物理与规则（均按真实装配，而非旧联接图）：

* 管音高 ``pitch = zero + offset``；一班至多一个动作（拨一枚栓走一个准许档 /
  装一次工装 / 转接脱开一次 / 空班）。
* 每枚“调音栓”(knob) 有联动系数 ``coef`` 与若干准许档 ``detents``；拨一格让该
  杆上**仍跟随**的管各走 ``coef·detent``。
* “可脱接头”(detachable connector)：在尚开盖的联杆接点装上（占一班），之后可
  转接脱开（占一班）；脱开后该管停在脱开当刻音高，其余管仍跟原杆。
* “独立微调栓”(micro-knob)：在尚开盖的单管上装上（占一班），只动该管，档位
  限 ±1 音分（每动一次占一班）。
* “封区”：某簧区在 ``seal_after`` 这一班结束后封住（``0`` = 首班开始前即封）；
  封住后凡会**实际带动**该区任何一根管的操作一律禁止。已脱开的管即使挂同杆也
  不被带动，故不受该区封印约束。
* 交付：在某个不晚于全部截止班的班次末，两套和弦同时成立即可交付（“封琴前
  留下两套和弦”）；若某截止班已过，该和弦从截止起每个班次边界都必须继续成立。

工具选型：默认采用 **可脱接头** 一种工装（题面“只配一套工装，选一种实现”）。
``allow_micro=True`` 仅用于对比演示——单枚微调栓无法同时抬两根管。
"""

from fractions import Fraction

from .rationals import parse_rational

INSTALL_CONNECTOR = "install_connector"
INSTALL_MICRO = "install_micro"
DETACH = "detach"
KNOB = "knob"
MICRO_MOVE = "micro"
NOOP = "noop"


class OrganError(Exception):
    """领域层可预期错误（参数越界、工单非法等）。"""


# ===========================================================================
# 测量层：合奏测音卡 -> budget_solver 真实快照
# ===========================================================================
ZERO_CELL = "__zero__"


def build_card_payload(model_id, roots, differences=(), sums=(), queries=(),
                       name="合奏测音卡"):
    """把“合奏测音卡”构造成一个 ``budget_solver`` 模型负载。

    测音卡只给若干管的**差值或和**，不求绝对。构造方式：

    * ``roots``：若干“基准管”，各写成自环自由格 ``p = 0 + 1·t``（``t`` 是共同
      零位，自由参数）。所有基准管共享同一 ``ZERO_CELL``。
    * ``differences``：``[(target, base, c)]`` 表示测到 ``target - base = c``，写成
      ``target = c + 1·base``（``base`` 须是基准管或先前已定义的目标管）。
    * ``sums``：``[(a, b)]`` 额外输出 ``a + b`` 的和格（和一般仍随 ``t`` 变）。
    * ``queries``：``[(a, b)]`` 额外输出音程格 ``b - a``。

    这样每根管自身 parametric（随 ``t``），而差值/和是否被唯一确定由真实求解
    判定——正是“各有不确定偏差，音程却可能完全确定”。

    返回的负载额外带 ``_relatives``：相对零位（取第一个基准管为 0），供装配层
    直接用；以及 ``_intervals``/``_sums`` 输出索引。
    """
    roots = list(roots)
    if not roots:
        raise OrganError("测音卡至少需要一个基准管")
    cells = {
        ZERO_CELL: {"kind": "formula", "constant": "0",
                    "terms": [{"coef": "1", "cell_id": ZERO_CELL}]},
    }
    relatives = {}
    for root in roots:
        if root in relatives:
            raise OrganError(f"重复基准管 {root}")
        relatives[root] = Fraction(0)
        cells[root] = {"kind": "formula", "constant": "0",
                       "terms": [{"coef": "1", "cell_id": ZERO_CELL}]}
    for target, base, c in differences:
        if target in relatives:
            raise OrganError(f"管 {target} 已定义，差值不可重复定义")
        if base not in relatives:
            raise OrganError(f"差值 {target}-{base}={c} 的基准管 {base} 未定义")
        cf = parse_rational(c, field=f"difference {target}-{base}")
        relatives[target] = relatives[base] + cf
        cells[target] = {"kind": "formula", "constant": str(cf),
                         "terms": [{"coef": "1", "cell_id": base}]}
    sum_cells = []
    for a, b in sums:
        if a not in relatives or b not in relatives:
            raise OrganError(f"和格 {a}+{b} 含未定义管")
        cid = f"sum_{a}_{b}"
        cells[cid] = {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": a},
                                {"coef": "1", "cell_id": b}]}
        sum_cells.append((cid, a, b))
    iv_cells = []
    for a, b in queries:
        if a not in relatives or b not in relatives:
            raise OrganError(f"音程查询 {b}-{a} 含未定义管")
        cid = f"iv_{a}_{b}"
        cells[cid] = {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": b},
                                {"coef": "-1", "cell_id": a}]}
        iv_cells.append((cid, a, b))
    outputs = list(relatives) + [cid for cid, _, _ in sum_cells] \
        + [cid for cid, _, _ in iv_cells]
    return {
        "model_id": model_id,
        "name": name,
        "cells": cells,
        "outputs": outputs,
        "_zero_cell": ZERO_CELL,
        "_relatives": relatives,
        "_sums": sum_cells,
        "_intervals": iv_cells,
    }


def resolve_card(service, payload):
    """在真实快照上求解测音卡；返回每根管状态、每个音程/和的确定值与相对零位。

    只解一次（一个 ``solve_id``），所有查询共用同一份解；并做独立验证。于是
    “前后查同一台架不会各挑一次误差”。
    """
    model_id = payload["model_id"]
    body = {k: v for k, v in payload.items() if not k.startswith("_")}
    service.put_model(body)
    snap = service.snapshot(model_id)
    artifact, _ = service.compile(snap["snap_id"])
    report = service.solve(artifact)
    solve_id = report["solve_id"]
    pipes = {}
    for cell in payload["_relatives"]:
        q = service.query(solve_id, f"{model_id}::{cell}")
        pipes[cell] = {"status": q["status"], "value": q.get("value"),
                       "expression": q.get("expression")}
    intervals = {}
    for cid, a, b in payload["_intervals"]:
        q = service.query(solve_id, f"{model_id}::{cid}")
        intervals[(a, b)] = {"status": q["status"], "value": q.get("value")}
    sums = {}
    for cid, a, b in payload["_sums"]:
        q = service.query(solve_id, f"{model_id}::{cid}")
        sums[(a, b)] = {"status": q["status"], "value": q.get("value")}
    verdict = service.verify_solve(solve_id)
    return {
        "solve_id": solve_id,
        "artifact_hash": artifact["artifact_hash"],
        "pipes": pipes,
        "intervals": intervals,
        "sums": sums,
        "relatives": dict(payload["_relatives"]),
        "verify_ok": verdict["ok"],
        "report": report,
    }


# ===========================================================================
# 装配层
# ===========================================================================
class Rig:
    """一台小管风琴组合。

    ``pipes`` : ``{name: {"zero": 有理数串, "rod": rod_or_None}}``
        ``zero`` 是已确定的相对零位常数（共同自由 ``t`` 在音程/和弦里抵消）。
    ``rods``  : ``{rod: {"knobs": {knob: {"coef": 有理数, "detents": [有理数...]}}}}``
    ``regions``: ``{region: {"pipes": [...], "seal_after": int|None}}``
    ``chords`` : ``[{"name":..., "pairs": [[from,to,allowed],...], "deadline": int}]``
        音程定义为 ``pitch(to) - pitch(from)``。
    ``fixed_pipes``: 已与外部固定音准合格、绝对音高不得再动的管（offset 必须
        恒为 0）。任何会**实际带动**它们的拨栓/微调都被禁止；仍可装/脱接头。
    """

    def __init__(self, pipes, rods=None, chords=None, regions=None,
                 fixed_pipes=None, max_pipes=6, max_knobs=4, max_shifts=5):
        if len(pipes) > max_pipes:
            raise OrganError(f"管数 {len(pipes)} 超过上限 {max_pipes}")
        rods = rods or {}
        knob_count = sum(len(r.get("knobs", {})) for r in rods.values())
        if knob_count > max_knobs:
            raise OrganError(f"调音栓数 {knob_count} 超过上限 {max_knobs}")
        if max_shifts > 5:
            raise OrganError(f"班次数 {max_shifts} 超过上限 5")
        self.max_shifts = max_shifts
        self.fixed_pipes = set(fixed_pipes or [])

        self.pipes = {}
        for name, spec in pipes.items():
            zero = spec.get("zero", "0")
            parse_rational(zero, field=f"pipe {name}.zero")
            rod = spec.get("rod")
            if rod is not None and rod not in rods:
                raise OrganError(f"管 {name} 引用未知杆 {rod}")
            self.pipes[name] = {"zero": Fraction(zero), "rod": rod}

        self.rods = {}
        for rod, spec in rods.items():
            knobs = {}
            for knob, kspec in spec.get("knobs", {}).items():
                coef = parse_rational(kspec.get("coef", "1"), field=f"{rod}.{knob}.coef")
                detents = [parse_rational(d, field=f"{rod}.{knob}.detent")
                           for d in kspec.get("detents", ["-1", "1"])]
                knobs[knob] = {"coef": coef, "detents": detents}
            self.rods[rod] = {"knobs": knobs}

        self.regions = {}
        for region, spec in (regions or {}).items():
            for p in spec.get("pipes", []):
                if p not in self.pipes:
                    raise OrganError(f"封区 {region} 含未知管 {p}")
            self.regions[region] = {
                "pipes": list(spec.get("pipes", [])),
                "seal_after": spec.get("seal_after"),
            }

        self.chords = []
        for ch in chords or []:
            pairs = [(a, b, parse_rational(v, field="chord.allowed"))
                     for a, b, v in ch["pairs"]]
            for a, b, _ in pairs:
                for p in (a, b):
                    if p not in self.pipes:
                        raise OrganError(f"和弦 {ch['name']} 含未知管 {p}")
            self.chords.append({"name": ch["name"], "pairs": pairs,
                                "deadline": int(ch["deadline"])})

    # -------------------------------------------------------------- 结构查询
    def pipes_on_rod(self, rod):
        return [p for p, s in self.pipes.items() if s["rod"] == rod]

    def knob_rod(self, knob):
        for rod, spec in self.rods.items():
            if knob in spec["knobs"]:
                return rod
        return None

    def sealed_regions(self, shift):
        return {r for r, spec in self.regions.items()
                if spec["seal_after"] is not None and spec["seal_after"] < shift}

    def sealed_pipes(self, shift):
        out = set()
        for r in self.sealed_regions(shift):
            out.update(self.regions[r]["pipes"])
        return out

    def min_deadline(self):
        return min((ch["deadline"] for ch in self.chords), default=self.max_shifts)


class Assembly:
    """精确装配状态：音高 = zero + offset。"""

    def __init__(self, rig):
        self.rig = rig
        self.offset = {p: Fraction(0) for p in rig.pipes}
        self.detached = set()
        self.connectors = set()
        self.micro = set()

    def clone(self):
        new = Assembly.__new__(Assembly)
        new.rig = self.rig
        new.offset = dict(self.offset)
        new.detached = set(self.detached)
        new.connectors = set(self.connectors)
        new.micro = set(self.micro)
        return new

    def key(self):
        return (tuple(sorted((p, o) for p, o in self.offset.items() if o != 0)),
                frozenset(self.detached), frozenset(self.connectors),
                frozenset(self.micro))

    def pitch(self, pipe):
        return self.rig.pipes[pipe]["zero"] + self.offset[pipe]

    def interval(self, a, b):
        return self.pitch(b) - self.pitch(a)

    def follows(self, pipe):
        """当前装配下该管是否仍随原杆联动。只有“脱开的接头”才会让它停下；
        独立微调栓只是额外给该管一个 ±1 音分的独立偏置，并不脱离原杆。"""
        return pipe not in self.detached

    def moved_by_knob(self, knob, detent):
        rod = self.rig.knob_rod(knob)
        if rod is None:
            raise OrganError(f"未知调音栓 {knob}")
        coef = self.rig.rods[rod]["knobs"][knob]["coef"]
        delta = coef * detent
        return {p: delta for p in self.rig.pipes_on_rod(rod) if self.follows(p)}

    def apply_knob(self, knob, detent):
        moved = self.moved_by_knob(knob, detent)
        for p, d in moved.items():
            self.offset[p] += d
        return moved

    def detach(self, pipe):
        if pipe not in self.connectors:
            raise OrganError(f"管 {pipe} 未装可脱接头")
        self.detached.add(pipe)

    def apply_micro(self, pipe, cents):
        if pipe not in self.micro:
            raise OrganError(f"管 {pipe} 未装微调栓")
        if cents not in (Fraction(-1), Fraction(0), Fraction(1)):
            raise OrganError("微调栓档位必须在 ±1 音分内")
        self.offset[pipe] += cents
        return {pipe: cents}

    def chord_holds(self, chord):
        return all(self.interval(a, b) == allowed for a, b, allowed in chord["pairs"])


# ===========================================================================
# 调律师的两个单点查询（在给定装配/测量下）
# ===========================================================================
def query_natural_interval(card_result, a, b):
    """单查某对管“天然”相差多少（``b - a``，精确分数串或 None）。

    ``card_result`` 是 :func:`resolve_card` 的返回值；只用其真实快照解，不另解。
    """
    key = (a, b)
    if key in card_result["intervals"]:
        info = card_result["intervals"][key]
        return info["value"] if info["status"] == "determined" else None
    # 反向查询可由已知正向取负
    if (b, a) in card_result["intervals"]:
        info = card_result["intervals"][(b, a)]
        if info["status"] == "determined":
            return str(-Fraction(info["value"]))
    return None


def query_detent_effect(rig, knob, detent=1, assembly=None):
    """一枚栓进一格时，真实联动下哪些管一同走（含各自位移）。

    默认在当前装配（尚无脱开/微调）下计算；也可传入 :class:`Assembly` 反映
    已经脱开某些管之后的真实联动。
    """
    asm = assembly or Assembly(rig)
    moved = asm.moved_by_knob(knob, Fraction(detent))
    return {p: str(d) for p, d in moved.items()}


# ===========================================================================
# 规划：按班次 BFS；封区由 region.seal_after 决定。
# ===========================================================================
def _legal_actions(rig, asm, shift, allow_connectors, allow_micro, installed_any,
                   install_earliest):
    sealed = rig.sealed_pipes(shift)
    open_pipes = [p for p in rig.pipes if p not in sealed]
    actions = []

    if not installed_any and shift >= install_earliest:  # 窗内至多装一次工装
        if allow_connectors:
            for p in open_pipes:
                if rig.pipes[p]["rod"] is not None:
                    actions.append((INSTALL_CONNECTOR, {"pipe": p}, [p]))
        if allow_micro:
            for p in open_pipes:
                actions.append((INSTALL_MICRO, {"pipe": p}, [p]))

    if allow_connectors:
        for p in sorted(asm.connectors):
            if p not in asm.detached and p not in sealed:
                actions.append((DETACH, {"pipe": p}, [p]))

    for rod, spec in rig.rods.items():
        for knob, kspec in spec["knobs"].items():
            for det in kspec["detents"]:
                if det == 0:
                    continue
                moved = asm.moved_by_knob(knob, det)
                if not moved:
                    continue
                if any(p in sealed for p in moved):
                    continue
                if any(p in rig.fixed_pipes for p in moved):
                    continue  # 不得实际带动已达固定音准的管
                actions.append((KNOB, {"knob": knob, "detent": str(det)},
                                sorted(moved)))

    for p in sorted(asm.micro):
        if p in sealed or p in rig.fixed_pipes:
            continue
        for cents in (Fraction(-1), Fraction(1)):
            actions.append((MICRO_MOVE, {"pipe": p, "cents": str(cents)}, [p]))

    actions.append((NOOP, {}, []))
    return actions


def _apply(asm, kind, info):
    if kind == INSTALL_CONNECTOR:
        asm.connectors.add(info["pipe"])
    elif kind == INSTALL_MICRO:
        asm.micro.add(info["pipe"])
    elif kind == DETACH:
        asm.detach(info["pipe"])
    elif kind == KNOB:
        asm.apply_knob(info["knob"], Fraction(info["detent"]))
    elif kind == MICRO_MOVE:
        asm.apply_micro(info["pipe"], Fraction(info["cents"]))


def _all_hold(rig, asm):
    return all(asm.chord_holds(ch) for ch in rig.chords)


def _due_hold(rig, asm, shift):
    """凡截止班 <= shift 的和弦，此刻都必须成立。"""
    return all(asm.chord_holds(ch) for ch in rig.chords if ch["deadline"] <= shift)


def plan(rig, allow_connectors=True, allow_micro=False, max_shifts=None,
         install_earliest=1):
    """在班次上限内搜索一份完整工单。返回 ``{"ok", "shifts", "reason", ...}``。

    交付规则：在最早一个“两套和弦同时成立、且所有已到期截止都已满足”的班次
    交付。BFS 逐班推进，故找到即最短；每班边界都要求已到期和弦成立。

    ``install_earliest``：工装（接头/微调栓）最早可在第几班安装。把来料班往后
    移，使某些安装落在封区之后，用来演示“哪张工单失效”。
    """
    max_shifts = min(max_shifts or rig.max_shifts, rig.max_shifts)
    start = Assembly(rig)
    frontier = [(start, [], False)]  # (asm, history, installed_any)
    seen = set()

    for shift in range(1, max_shifts + 1):
        next_frontier = []
        for asm, history, installed_any in frontier:
            if not _due_hold(rig, asm, shift - 1):
                continue
            for kind, info, listen in _legal_actions(
                    rig, asm, shift, allow_connectors, allow_micro, installed_any,
                    install_earliest):
                child = asm.clone()
                _apply(child, kind, info)
                if not _due_hold(rig, child, shift):
                    continue
                new_installed = installed_any or kind in (INSTALL_CONNECTOR,
                                                          INSTALL_MICRO)
                record = {
                    "shift": shift,
                    "action": kind,
                    "detail": info,
                    "listen": listen,
                    "sealed": sorted(rig.sealed_regions(shift)),
                    "pitch": {p: str(child.pitch(p)) for p in sorted(rig.pipes)},
                }
                hist = history + [record]
                if _all_hold(rig, child):
                    return {"ok": True, "shifts": hist,
                            "implementation": _impl(hist)}
                state_key = (shift, child.key(), new_installed)
                if state_key in seen:
                    continue
                seen.add(state_key)
                next_frontier.append((child, hist, new_installed))
        frontier = next_frontier
        if not frontier:
            break
    return {"ok": False, "shifts": None,
            "reason": _diagnose(rig, allow_connectors, allow_micro, install_earliest)}


def _impl(history):
    if any(r["action"] == INSTALL_MICRO for r in history):
        return "micro_knob"
    if any(r["action"] == INSTALL_CONNECTOR for r in history):
        return "detachable_connector"
    return "existing_rig"


def _diagnose(rig, allow_connectors, allow_micro, install_earliest=1):
    base = Assembly(rig)
    msgs = []

    # 0) 安装窗被推到封区之后：唯一可行手段（接头/微调）无法及时装上。
    late_pipes_blocked = _seal_blocks_install(rig, install_earliest)
    if late_pipes_blocked:
        for p, region, seal_after in late_pipes_blocked:
            msgs.append(
                f"来料/安装最早班 {install_earliest} 已晚于管 {p} 所属封区 "
                f"{region} 的封区时刻（第 {seal_after} 班末），"
                f"接头装不上，该管的联动脱开方案失效")

    # 1) 首班前即封锁、却又必须调整的管
    sealed1 = rig.sealed_pipes(1)
    for ch in rig.chords:
        for a, b, allowed in ch["pairs"]:
            if base.interval(a, b) == allowed:
                continue
            for p in (a, b):
                if p in sealed1:
                    msgs.append(
                        f"和弦 {ch['name']} 的管对 ({a},{b}) 需调到 {allowed}，"
                        f"但管 {p} 在首班前已封区；任何会实际带动它的操作都被禁止")
                    break
            for p in (a, b):
                if p in rig.fixed_pipes:
                    msgs.append(
                        f"和弦 {ch['name']} 的管对 ({a},{b}) 需调到 {allowed}，"
                        f"但管 {p} 已达固定音准，不得被实际带动")
                    break

    # 2) 同杆耦合：差恒定，拨杆无法改变该音程
    for ch in rig.chords:
        for a, b, allowed in ch["pairs"]:
            ra, rb = rig.pipes[a]["rod"], rig.pipes[b]["rod"]
            if ra is not None and ra == rb and a not in rig.fixed_pipes \
                    and b not in rig.fixed_pipes:
                if base.interval(a, b) != allowed:
                    msgs.append(
                        f"和弦 {ch['name']} 的管对 ({a},{b}) 同挂杆 {ra}，"
                        f"两管永远同步、差恒为 {base.interval(a, b)}，"
                        f"单拨该杆无法变成 {allowed}")

    # 3) 同一杆抬管会带坏已成立的另一和弦
    breaks = rod_break_analysis(rig)
    for hit in breaks:
        msgs.append(hit["message"])

    # 4) 微调栓只能动一根管
    if allow_micro and not allow_connectors:
        involved = set()
        for ch in rig.chords:
            for a, b, allowed in ch["pairs"]:
                if base.interval(a, b) != allowed:
                    involved.update((a, b))
        if len(involved) >= 2:
            msgs.append(
                f"所选微调栓实现中只有一枚可用微栓，只能动一根管，"
                f"无法同时抬 {sorted(involved)} 中的多根（不能假装有第二枚微栓）")

    if not msgs:
        # 单档不足：某管目标位移需要多格，但每次只能走一个准许档。
        shortfalls = _single_detent_shortfalls(rig)
        msgs.extend(shortfalls)
    if not msgs:
        msgs.append("在班次上限内未找到满足全部和弦、封区与联动约束的工单")
    seen = set()
    uniq = []
    for m in msgs:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return "；".join(uniq)


def _seal_blocks_install(rig, install_earliest):
    """若安装最早班落在某封区之后，返回那时已封、且本可用于脱开联动的管清单。"""
    blocked = []
    for region, spec in rig.regions.items():
        seal_after = spec["seal_after"]
        if seal_after is None:
            continue
        # 第 seal_after 班末封；安装最早可发生在第 install_earliest 班。
        if install_earliest > seal_after:
            for p in spec["pipes"]:
                if rig.pipes[p]["rod"] is not None:
                    blocked.append((p, region, seal_after))
    return blocked


def _single_detent_shortfalls(rig):
    """诊断：某对管要改变的音程需要某管位移超出单班准许档，且只拨一档不够。"""
    base = Assembly(rig)
    msgs = []
    for ch in rig.chords:
        for a, b, allowed in ch["pairs"]:
            gap = allowed - base.interval(a, b)
            if gap == 0:
                continue
            # 看是否存在某栓单档即可独立达成该 gap（无封区/固定约束时）
            achievable = False
            for rod, spec in rig.rods.items():
                for knob, kspec in spec["knobs"].items():
                    for det in kspec["detents"]:
                        trial = base.clone()
                        trial.apply_knob(knob, det)
                        if trial.interval(a, b) == allowed:
                            achievable = True
            if not achievable:
                # 找出最小可达成该音程所需的单档是否不存在（差得不止一档）
                msgs.append(
                    f"和弦 {ch['name']} 的管对 ({a},{b}) 需从 {base.interval(a, b)} "
                    f"变到 {allowed}，但没有任何单班准许档能一次达成，"
                    f"一次不够")
    return msgs


def rod_break_analysis(rig):
    """找出“为满足某未成立和弦而必须动的杆，会带坏另一已成立和弦”的耦合。

    对每个栓的单步（真实联动），比较动作前后哪些和弦由成立变为不成立，且该
    动作确实朝某个未成立和弦靠拢。用于解释“同一杆会带坏上一和弦”。
    """
    base = Assembly(rig)
    hits = []
    unmet = [ch for ch in rig.chords if not base.chord_holds(ch)]
    held = [ch for ch in rig.chords if base.chord_holds(ch)]
    for rod, spec in rig.rods.items():
        for knob, kspec in spec["knobs"].items():
            for det in kspec["detents"]:
                if det == 0:
                    continue
                trial = base.clone()
                moved = trial.apply_knob(knob, det)
                if not moved:
                    continue
                broken = [ch["name"] for ch in held if not trial.chord_holds(ch)]
                fixed = [ch["name"] for ch in unmet if trial.chord_holds(ch)]
                if broken and fixed:
                    hits.append({
                        "rod": rod, "knob": knob, "detent": str(det),
                        "moved": sorted(moved),
                        "fixes": fixed, "breaks": broken,
                        "message": (
                            f"拨动杆 {rod} 的栓 {knob} 走 {det} 格可修好 "
                            f"{fixed}，但会实际带动 {sorted(moved)}，"
                            f"带坏已成立的和弦 {broken}"),
                    })
    return hits


# ===========================================================================
# 沿真实装配复核工单
# ===========================================================================
def validate_work_order(rig, planned):
    """只信工单列出的动作，按真实联动重演，复核两套和弦、封区与余班。"""
    if not planned or not planned.get("ok"):
        return {"ok": False, "violations": [{"reason": "无有效工单"}],
                "final": {}, "trace": [], "seals": {}}
    asm = Assembly(rig)
    violations = []
    trace = []
    prev_sealed = set()
    seals = {}
    for rec in planned["shifts"]:
        shift = rec["shift"]
        sealed = rig.sealed_pipes(shift)
        for r in sealed - prev_sealed:
            seals[r] = shift
        prev_sealed = sealed
        kind, info = rec["action"], rec["detail"]
        moved = {}
        if kind == INSTALL_CONNECTOR:
            if info["pipe"] in sealed:
                violations.append({"shift": shift, "reason": "在封区装接头"})
            asm.connectors.add(info["pipe"])
        elif kind == INSTALL_MICRO:
            if info["pipe"] in sealed:
                violations.append({"shift": shift, "reason": "在封区装微调栓"})
            asm.micro.add(info["pipe"])
        elif kind == DETACH:
            if info["pipe"] in sealed:
                violations.append({"shift": shift, "reason": "在封区转接脱开"})
            asm.detach(info["pipe"])
        elif kind == KNOB:
            moved = asm.apply_knob(info["knob"], Fraction(info["detent"]))
        elif kind == MICRO_MOVE:
            moved = asm.apply_micro(info["pipe"], Fraction(info["cents"]))
        for p in moved:
            if p in sealed:
                violations.append({"shift": shift, "pipe": p,
                                   "reason": "操作实际带动了已封区的管"})
        trace.append({
            "shift": shift, "action": kind, "detail": info,
            "moved": {p: str(d) for p, d in moved.items()},
            "listen": rec.get("listen", []),
            "sealed": sorted(sealed),
            "pitch": {p: str(asm.pitch(p)) for p in sorted(rig.pipes)},
        })
    final = {}
    ok_all = True
    for ch in rig.chords:
        pairs = []
        ok = True
        for a, b, allowed in ch["pairs"]:
            got = asm.interval(a, b)
            good = got == allowed
            pairs.append({"pair": [a, b], "target": str(allowed),
                          "actual": str(got), "ok": good})
            ok = ok and good
        final[ch["name"]] = {"ok": ok, "pairs": pairs, "deadline": ch["deadline"]}
        ok_all = ok_all and ok
    used = planned["shifts"][-1]["shift"] if planned["shifts"] else 0
    fixed_violations = [
        p for p, o in asm.offset.items() if p in rig.fixed_pipes and o != 0
    ]
    for p in fixed_violations:
        violations.append({"pipe": p, "reason": "已达固定音准的管被实际移动"})
    return {
        "ok": ok_all and not violations,
        "final": final,
        "violations": violations,
        "trace": trace,
        "seals": seals,
        "shifts_used": used,
        "shifts_left": rig.max_shifts - used,
    }


def render_work_order(rig, planned):
    """把工单渲染成可读文本：每班听哪管、动作、封区。"""
    if not planned or not planned.get("ok"):
        return "无可行工单：" + (planned.get("reason", "") if planned else "")
    lines = [f"实现：{planned['implementation']}"]
    for rec in planned["shifts"]:
        act = rec["action"]
        if act == KNOB:
            d = rec["detail"]
            desc = f"拨栓 {d['knob']} 走 {d['detent']} 格"
        elif act == INSTALL_CONNECTOR:
            desc = f"在管 {rec['detail']['pipe']} 的联杆接点装可脱接头"
        elif act == INSTALL_MICRO:
            desc = f"在管 {rec['detail']['pipe']} 装独立微调栓"
        elif act == DETACH:
            desc = f"转接脱开管 {rec['detail']['pipe']}"
        elif act == MICRO_MOVE:
            desc = f"拨微调栓 {rec['detail']['pipe']} {rec['detail']['cents']} 音分"
        else:
            desc = "空班"
        sealed = ("；封区 " + ",".join(rec["sealed"])) if rec["sealed"] else ""
        listen = ("；听 " + ",".join(rec["listen"])) if rec["listen"] else ""
        lines.append(f"第 {rec['shift']} 班：{desc}{listen}{sealed}")
    return "\n".join(lines)
