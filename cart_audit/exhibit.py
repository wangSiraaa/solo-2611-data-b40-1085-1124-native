"""展览地面保洁交接：色粉互沾推演、吸附巾调度与观展廊交接判定。

本模块在既有“离线动作回放”的严格合同上增加一层地面保洁语义，全部时间/
坐标仍用 :class:`fractions.Fraction`，不做采样、不用浮点：

- 小厅地面分成固定的轴对齐矩形区（:class:`Zone`），刷头是已有轮廓合同下
  的矩形；刷头与某区的接触区间直接由 :class:`ProfileOverlapService` 求出，
  因此 **只贴边不沾** 与车对核查是同一条严格正面积合同；
- 一把普通滚刷与一块地的同一次连续接触累计达到该区规定时长时，在第一次
  达到的时刻互沾双方现有的全部颜色；同次接触只办一次，离开（接触区间
  断开）后重新进入另计；跨轨迹断点但未分开的接触由 ``merge_union`` 接着算；
- 同一时刻到期的数笔互沾成组，全部从该组之前（截点登记动作处理之后）的
  颜色快照结算，杜绝按循环顺序在一拍内传过两站；
- 指定时刻更换刷套：清刷（颜色清空、套号加一）并把既有接触在该点截断；
- 吸附巾有实物号，同一块全局只能用一次；擦净是地面时间线上的真实事件；
- 展务台按场单的开放时点、禁色、邻区通路与等候区交接时长，用“整个时间窗
  内逐段合格”的精确判定决定路线能否交给领队，而不是逐拍看点。

本模块不依赖 SQLite；持久化与旧版轨迹复核在 :mod:`cart_audit.exhibit_store`。
"""

from __future__ import annotations

import itertools
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .intervals import TimeInterval, merge_union
from .models import MotionSegment, Profile, Trajectory
from .rational import fraction, qstr
from .service import ProfileOverlapService

__all__ = [
    "ExhibitError",
    "Zone",
    "Hall",
    "BrushPlan",
    "Wipe",
    "WipeSlot",
    "SessionSheet",
    "SimResult",
    "simulate",
    "Desk",
    "earliest_switch_start",
    "judge_switch",
    "naive_tick_routes",
]


class ExhibitError(ValueError):
    """展览保洁资料或交接计划非法。"""


# --------------------------------------------------------------------------- #
# 领域资料
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Zone:
    """固定矩形地面区；``required`` 是该区规定的连续接触时长。"""

    zone_id: str
    cx: Fraction
    cy: Fraction
    half_width: Fraction
    half_height: Fraction
    required: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id:
            raise ExhibitError("zone_id 必须是非空字符串")
        for name in ("cx", "cy", "half_width", "half_height", "required"):
            object.__setattr__(self, name, fraction(getattr(self, name), field=f"zone {self.zone_id}.{name}"))
        if self.half_width <= 0 or self.half_height <= 0:
            raise ExhibitError(f"区 {self.zone_id!r} 的半宽/半高必须为正")
        if self.required <= 0:
            raise ExhibitError(f"区 {self.zone_id!r} 的规定接触时长必须为正")

    @property
    def x0(self) -> Fraction:
        return self.cx - self.half_width

    @property
    def x1(self) -> Fraction:
        return self.cx + self.half_width

    @property
    def y0(self) -> Fraction:
        return self.cy - self.half_height

    @property
    def y1(self) -> Fraction:
        return self.cy + self.half_height

    def profile(self) -> Profile:
        # 把固定区当成静止轮廓喂给既有严格求交服务。
        return Profile(self.zone_id, self.half_width, self.half_height)

    def static_segment(self, u: Fraction, v: Fraction) -> MotionSegment:
        return MotionSegment(0, u, v, self.cx, self.cy, self.cx, self.cy)

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "cx": qstr(self.cx),
            "cy": qstr(self.cy),
            "half_width": qstr(self.half_width),
            "half_height": qstr(self.half_height),
            "required": qstr(self.required),
        }

    @classmethod
    def from_data(cls, data: dict) -> "Zone":
        try:
            zone_id = str(data["zone_id"])
        except KeyError as exc:
            raise ExhibitError(f"地面区缺少字段 {exc.args[0]!r}") from exc
        # 两种定位写法：中心+半尺寸（与 Profile 一致），或四角边界 x0/y0/x1/y1。
        if all(key in data for key in ("x0", "y0", "x1", "y1")):
            x0, y0, x1, y1 = (fraction(data[k], field=f"zone {zone_id}.{k}")
                               for k in ("x0", "y0", "x1", "y1"))
            if not x1 > x0 or not y1 > y0:
                raise ExhibitError(f"区 {zone_id!r} 的边界必须满足 x1>x0、y1>y0")
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        else:
            try:
                cx, cy = data["cx"], data["cy"]
                hw, hh = data["half_width"], data["half_height"]
            except KeyError as exc:
                raise ExhibitError(f"地面区缺少字段 {exc.args[0]!r}") from exc
        return cls(
            zone_id=zone_id,
            cx=cx,
            cy=cy,
            half_width=hw,
            half_height=hh,
            required=data.get("required", data.get("required_duration", 1)),
        )


@dataclass(frozen=True)
class Hall:
    """小厅：两区色标签、固定矩形区、邻区通路、入口与展件区。"""

    hall_id: str
    labels: Tuple[str, str]
    zones: Dict[str, Zone]
    passages: frozenset  # frozenset[Tuple[str, str]]，规范序
    entrance: str
    exhibit: str

    def __post_init__(self) -> None:
        if not isinstance(self.hall_id, str) or not self.hall_id:
            raise ExhibitError("hall_id 必须是非空字符串")
        labels = tuple(self.labels)
        if len(labels) != 2 or len(set(labels)) != 2 or not all(isinstance(c, str) and c for c in labels):
            raise ExhibitError("彩粉必须且只能有两个互不相同的非空字符串标签（本地标签）")
        object.__setattr__(self, "labels", labels)
        if self.entrance not in self.zones:
            raise ExhibitError(f"入口区 {self.entrance!r} 不在分区表中")
        if self.exhibit not in self.zones:
            raise ExhibitError(f"展件区 {self.exhibit!r} 不在分区表中")
        for a, b in self.passages:
            if a not in self.zones or b not in self.zones:
                raise ExhibitError(f"通路 {a!r}-{b!r} 引用了未知区")
            if a == b:
                raise ExhibitError("通路不能是区自环")

    # -- 邻区 ------------------------------------------------------------- #
    def neighbors(self, zone_id: str) -> List[str]:
        out = []
        for a, b in self.passages:
            if a == zone_id:
                out.append(b)
            elif b == zone_id:
                out.append(a)
        return sorted(out)

    def simple_routes(self, src: str, dst: str, limit: int = 64) -> List[Tuple[str, ...]]:
        """枚举 src→dst 的全部简单区序列（字典序，确定性）。"""
        if src not in self.zones or dst not in self.zones:
            raise ExhibitError("路线端点引用了未知区")
        if src == dst:
            return [(src,)]
        results: List[Tuple[str, ...]] = []

        def dfs(cur: str, path: List[str], seen: set) -> None:
            if len(results) >= limit:
                return
            for nb in self.neighbors(cur):
                if nb in seen:
                    continue
                path.append(nb)
                if nb == dst:
                    results.append(tuple(path))
                else:
                    seen.add(nb)
                    dfs(nb, path, seen)
                    seen.discard(nb)
                path.pop()

        dfs(src, [src], {src})
        return sorted(results)

    @staticmethod
    def geometric_passage(a: Zone, b: Zone) -> bool:
        """共边且公共线段长度为正才算邻区；只共角不算通路。"""
        overlap_x = min(a.x1, b.x1) - max(a.x0, b.x0)
        overlap_y = min(a.y1, b.y1) - max(a.y0, b.y0)
        vertical_edge = (a.x1 == b.x0 or b.x1 == a.x0) and overlap_y > 0
        horizontal_edge = (a.y1 == b.y0 or b.y1 == a.y0) and overlap_x > 0
        return vertical_edge or horizontal_edge

    @classmethod
    def from_data(cls, data: dict) -> "Hall":
        zones_list = [Zone.from_data(z) for z in data["zones"]]
        zones = {z.zone_id: z for z in zones_list}
        if len(zones) != len(zones_list):
            raise ExhibitError("zone_id 重复")
        if "passages" in data and data["passages"] is not None:
            # 场单登记的邻区通路以登记为准（只校验引用与自环）；几何共边关系
            # 仍可用 Hall.geometric_passage 独立核对。未登记时才按正长度共边推导。
            pairs = set()
            for pair in data["passages"]:
                a, b = str(pair[0]), str(pair[1])
                if a not in zones or b not in zones:
                    raise ExhibitError(f"登记通路 {a!r}-{b!r} 引用了未知区")
                pairs.add(tuple(sorted((a, b))))
            passages = frozenset(pairs)
        else:
            pairs = set()
            ids = sorted(zones)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    if cls.geometric_passage(zones[a], zones[b]):
                        pairs.add(tuple(sorted((a, b))))
            passages = frozenset(pairs)
        return cls(
            hall_id=str(data["hall_id"]),
            labels=(str(data["labels"][0]), str(data["labels"][1])),
            zones=zones,
            passages=passages,
            entrance=str(data["entrance"]),
            exhibit=str(data["exhibit"]),
        )

    def to_dict(self) -> dict:
        return {
            "hall_id": self.hall_id,
            "labels": list(self.labels),
            "zones": [z.to_dict() for z in sorted(self.zones.values(), key=lambda z: z.zone_id)],
            "passages": [list(p) for p in sorted(self.passages)],
            "entrance": self.entrance,
            "exhibit": self.exhibit,
        }


@dataclass(frozen=True)
class BrushPlan:
    """一把刷的保洁资料：初始带色（第 1 套）与指定换套时点。"""

    cart_id: str
    initial_colors: frozenset = frozenset()
    sleeve_changes: Tuple[Fraction, ...] = ()

    def __post_init__(self) -> None:
        changes = tuple(sorted(fraction(t, field=f"brush {self.cart_id}.sleeve_change") for t in self.sleeve_changes))
        if any(b <= a for a, b in zip(changes, changes[1:])):
            raise ExhibitError(f"刷 {self.cart_id!r} 的换套时点必须严格递增")
        object.__setattr__(self, "sleeve_changes", changes)
        object.__setattr__(self, "initial_colors", frozenset(self.initial_colors))


@dataclass(frozen=True)
class Wipe:
    """一次真实擦净：有实物号的同一块吸附巾在某时擦某区一次。"""

    wipe_id: str
    zone_id: str
    t: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", fraction(self.t, field=f"wipe {self.wipe_id}.t"))


@dataclass(frozen=True)
class WipeSlot:
    """场单允许派巾的一个（区域，时点）格子；实物号在调度时分配。"""

    zone_id: str
    t: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", fraction(self.t, field="slot.t"))


@dataclass(frozen=True)
class SessionSheet:
    """一场的场单：开放/离场时点、禁色，以及可选的指定等候区一次换道。"""

    session_id: str
    forbidden: str
    open_t: Fraction
    depart_t: Fraction
    entrance: Optional[str] = None
    exhibit: Optional[str] = None
    waiting_zone: Optional[str] = None
    arrive_waiting: Optional[Fraction] = None
    handover: Optional[Fraction] = None
    old_route: Optional[Tuple[str, ...]] = None
    new_route: Optional[Tuple[str, ...]] = None
    slots: Tuple[WipeSlot, ...] = ()

    def __post_init__(self) -> None:
        for name in ("open_t", "depart_t", "arrive_waiting", "handover"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, fraction(value, field=f"session {self.session_id}.{name}"))
        if not self.open_t < self.depart_t:
            raise ExhibitError(f"场 {self.session_id!r} 必须满足 open_t < depart_t")
        if (self.waiting_zone is None) != (self.handover is None):
            raise ExhibitError(f"场 {self.session_id!r} 换道资料必须同时给出等候区与交接时长")
        if self.waiting_zone is not None:
            if self.arrive_waiting is None:
                raise ExhibitError(f"场 {self.session_id!r} 换道必须给出队伍实际到达等候区时刻")
            if not self.handover > 0:
                raise ExhibitError("交接时长必须为正")
            if not self.arrive_waiting >= self.open_t:
                raise ExhibitError("队伍到达等候区不得早于开放时点")
            if self.arrive_waiting + self.handover > self.depart_t:
                raise ExhibitError(f"场 {self.session_id!r} 交接在离场前无法完成")
        object.__setattr__(self, "slots", tuple(self.slots))


# --------------------------------------------------------------------------- #
# 接触区间：复用严格正面积服务（只贴边不沾、跨断点接着算）
# --------------------------------------------------------------------------- #
def brush_zone_intervals(
    traj: Trajectory,
    brush_profile: Profile,
    zone: Zone,
    service: Optional[ProfileOverlapService] = None,
) -> List[TimeInterval]:
    """一把刷与一个区在轨迹核查域内的全部连续正面积接触并集。"""
    service = service or ProfileOverlapService()
    h0, h1 = traj.horizon
    grid = {h0, h1}
    for knot in traj.knots:
        grid.add(knot.t)
    grid = sorted(t for t in grid if h0 <= t <= h1)
    segments = traj.segments()
    starts = [s.t0 for s in segments]
    raw: List[TimeInterval] = []
    for u, v in zip(grid, grid[1:]):
        idx = bisect_right(starts, u) - 1
        if idx < 0 or idx >= len(segments):
            continue
        seg = segments[idx]
        iv = service.segment_overlap(brush_profile, seg, zone.profile(), zone.static_segment(u, v))
        if iv is None:
            continue
        iv = intersect_clip(iv, u, v)
        if iv is not None:
            raw.append(iv)
    return merge_union(raw)


def intersect_clip(iv: TimeInterval, u: Fraction, v: Fraction) -> Optional[TimeInterval]:
    from .intervals import intersect_intervals

    return intersect_intervals(iv, TimeInterval(u, v, True, False))


def split_at_cuts(iv: TimeInterval, cuts: Sequence[Fraction]) -> List[TimeInterval]:
    """把一个连续接触区间在换套时点处截断（截断点时长为零，端点标志不影响累计）。"""
    pieces = [iv]
    for cut in cuts:
        nxt: List[TimeInterval] = []
        for p in pieces:
            if p.left < cut < p.right:
                nxt.append(TimeInterval(p.left, cut, p.left_inclusive, False))
                nxt.append(TimeInterval(cut, p.right, True, p.right_inclusive))
            else:
                nxt.append(p)
        pieces = nxt
    return pieces


# --------------------------------------------------------------------------- #
# 推演结果
# --------------------------------------------------------------------------- #
@dataclass
class SimResult:
    horizon: Tuple[Fraction, Fraction]
    zone_timeline: Dict[str, List[Tuple[Fraction, frozenset]]]
    brush_timeline: Dict[str, List[Tuple[Fraction, frozenset, int]]]
    events: List[dict]
    provenance: Dict[str, Dict[str, dict]]

    def colors_at(self, zone_id: str, t: Fraction) -> frozenset:
        timeline = self.zone_timeline[zone_id]
        idx = bisect_right([p[0] for p in timeline], t) - 1
        if idx < 0:
            return frozenset()
        return timeline[idx][1]

    def is_clean(self, zone_id: str, color: str, a: Fraction, b: Fraction) -> bool:
        """区在整个左闭右开窗 [a, b) 内都不带 ``color``。"""
        if not b > a:
            return True
        timeline = self.zone_timeline[zone_id]
        times = [p[0] for p in timeline]
        idx = bisect_right(times, a) - 1
        if idx < 0:
            return True
        if color in timeline[idx][1]:
            return False
        j = idx + 1
        while j < len(timeline) and timeline[j][0] < b:
            if color in timeline[j][1]:
                return False
            j += 1
        return True

    def dirty_spans(self, zone_id: str, color: str, a: Fraction, b: Fraction) -> List[Tuple[Fraction, Fraction]]:
        """[a, b) 内带禁色的各段（用于失败举证），端点按左闭右开整理。"""
        timeline = self.zone_timeline[zone_id]
        spans: List[Tuple[Fraction, Fraction]] = []
        for i, (t, colors) in enumerate(timeline):
            seg_l = max(t, a)
            seg_r = min(timeline[i + 1][0] if i + 1 < len(timeline) else self.horizon[1], b)
            if seg_r > seg_l and color in colors:
                spans.append((seg_l, seg_r))
        # 与 a 之前开始的状态相交的情形：上面 idx 段的 t<a 时 seg_l=a 已覆盖。
        return spans

    def clean_intervals(self, zone_id: str, color: str) -> List[TimeInterval]:
        h0, h1 = self.horizon
        timeline = self.zone_timeline[zone_id]
        out: List[TimeInterval] = []
        run_start: Optional[Fraction] = None
        for i, (t, colors) in enumerate(timeline):
            seg_r = timeline[i + 1][0] if i + 1 < len(timeline) else h1
            clean = color not in colors
            if clean and run_start is None:
                run_start = max(t, h0)
            if not clean and run_start is not None:
                out.append(TimeInterval(run_start, t, True, False))
                run_start = None
        if run_start is not None:
            out.append(TimeInterval(run_start, h1, True, False))
        return out


# --------------------------------------------------------------------------- #
# 核心推演
# --------------------------------------------------------------------------- #
def simulate(
    hall: Hall,
    horizon: Tuple[Fraction, Fraction],
    trajectories: Dict[str, Trajectory],
    brush_profiles: Dict[str, Profile],
    brush_plans: Sequence[BrushPlan],
    initial_zone_colors: Optional[Dict[str, Iterable[str]]] = None,
    wipes: Sequence[Wipe] = (),
) -> SimResult:
    """按精确时间线推演整场：接触到期互沾、换套、擦净与首次来源。"""
    h0, h1 = horizon
    plans = {p.cart_id: p for p in brush_plans}
    initial_zone_colors = initial_zone_colors or {}

    # 实物巾同号只能用一次（全局合同，提前借别厅由调用方先消费该号）。
    seen_wipes: set = set()
    for w in wipes:
        if w.wipe_id in seen_wipes:
            raise ExhibitError(f"吸附巾 {w.wipe_id!r} 同一块被派发了不止一次")
        seen_wipes.add(w.wipe_id)
        if w.zone_id not in hall.zones:
            raise ExhibitError(f"吸附巾 {w.wipe_id!r} 派往未知区 {w.zone_id!r}")
        if not h0 <= w.t <= h1:
            raise ExhibitError(f"吸附巾 {w.wipe_id!r} 的时点 {w.t} 超出回放域 [{h0}, {h1}]")

    # -- 初始状态 -------------------------------------------------------- #
    zone_colors: Dict[str, set] = {zid: set() for zid in hall.zones}
    provenance: Dict[str, Dict[str, dict]] = {zid: {} for zid in hall.zones}
    for zid, colors in initial_zone_colors.items():
        if zid not in hall.zones:
            raise ExhibitError(f"铺粉资料引用未知区 {zid!r}")
        for c in colors:
            if c not in hall.labels:
                raise ExhibitError(f"区 {zid!r} 的色粉标签 {c!r} 不在本场两色标签内")
            zone_colors[zid].add(c)
            provenance[zid][c] = {"kind": "prelaid", "t": qstr(h0), "source": "展前铺粉", "brush": None, "sleeve": None}

    brush_colors: Dict[str, set] = {}
    sleeve_no: Dict[str, int] = {}
    for cid, traj in trajectories.items():
        plan = plans.get(cid)
        init = set(plan.initial_colors) if plan else set()
        for c in init:
            if c not in hall.labels:
                raise ExhibitError(f"刷 {cid!r} 自带颜色 {c!r} 不在两色标签内")
        brush_colors[cid] = set(init)
        sleeve_no[cid] = 1

    # -- 接触 episode -> 到期互沾事件 ----------------------------------- #
    # exchanges: t -> list[(brush, zone, sleeve_at_expiry 占位在结算时取)]
    exchanges: Dict[Fraction, List[Tuple[str, str]]] = {}
    contact_evidence: List[dict] = []
    service = ProfileOverlapService()
    for cid, traj in trajectories.items():
        prof = brush_profiles.get(cid)
        if prof is None:
            raise ExhibitError(f"刷 {cid!r} 缺少轮廓资料")
        plan = plans.get(cid)
        cuts = plan.sleeve_changes if plan else ()
        for zid, zone in hall.zones.items():
            intervals = brush_zone_intervals(traj, prof, zone, service)
            episodes: List[TimeInterval] = []
            for iv in intervals:
                pieces = split_at_cuts(iv, cuts)
                episodes.extend(pieces)
                for ep in pieces:
                    contact_evidence.append({
                        "brush": cid, "zone": zid,
                        "episode": [qstr(ep.left), qstr(ep.right)],
                    })
            for ep in episodes:
                tau = ep.left + zone.required
                # 累计时长在 tau 第一次达到规定值；tau==ep.right 也算达到
                # （甲足 1 拍后恰好切到乙的合同例）。
                if tau <= ep.right:
                    exchanges.setdefault(tau, []).append((cid, zid))

    # -- 登记动作（换套/擦巾）按输入次序编号，供同截点确定排序 ----------- #
    # 时间 -> {"sleeve": [(seq, brush, t)], "wipe": [(seq, wipe)]}
    sleeve_events: Dict[Fraction, List[Tuple[int, str]]] = {}
    seq = 0
    for cid in sorted(trajectories):
        plan = plans.get(cid)
        if not plan:
            continue
        for t in plan.sleeve_changes:
            sleeve_events.setdefault(t, []).append((seq, cid))
            seq += 1
    wipe_events: Dict[Fraction, List[Wipe]] = {}
    for w in wipes:
        wipe_events.setdefault(w.t, []).append(w)

    all_times = sorted(set(exchanges) | set(sleeve_events) | set(wipe_events))

    zone_timeline: Dict[str, List[Tuple[Fraction, frozenset]]] = {
        zid: [(h0, frozenset(zone_colors[zid]))] for zid in hall.zones
    }
    brush_timeline: Dict[str, List[Tuple[Fraction, frozenset, int]]] = {
        cid: [(h0, frozenset(brush_colors[cid]), 1)] for cid in trajectories
    }
    events: List[dict] = []

    def push_zone(zid: str, t: Fraction) -> None:
        last_t, last_colors = zone_timeline[zid][-1]
        cur = frozenset(zone_colors[zid])
        if cur != last_colors:
            zone_timeline[zid].append((t, cur))

    def push_brush(cid: str, t: Fraction) -> None:
        last_t, last_colors, last_no = brush_timeline[cid][-1]
        cur = frozenset(brush_colors[cid])
        if cur != last_colors or sleeve_no[cid] != last_no:
            brush_timeline[cid].append((t, cur, sleeve_no[cid]))

    for t in all_times:
        # 阶段一：截点登记动作——换套（清刷、套号加一；接触截断已在分 episode 办过）。
        for _, cid in sorted(sleeve_events.get(t, []), key=lambda x: x[0]):
            brush_colors[cid] = set()
            sleeve_no[cid] += 1
            events.append({"t": qstr(t), "kind": "sleeve_change", "brush": cid, "sleeve": sleeve_no[cid]})
            push_brush(cid, t)

        # 阶段二：擦巾（实物巾在此刻把该区颜色清空）。
        for w in wipe_events.get(t, []):
            zone_colors[w.zone_id] = set()
            events.append({"t": qstr(t), "kind": "wipe", "wipe_id": w.wipe_id, "zone": w.zone_id})
            push_zone(w.zone_id, t)

        # 阶段三：本拍全部到期互沾，从同一快照结算，禁止链式传递。
        group = exchanges.get(t, [])
        if group:
            snap_b: Dict[str, frozenset] = {b: frozenset(brush_colors[b]) for b, _ in group}
            snap_z: Dict[str, frozenset] = {z: frozenset(zone_colors[z]) for _, z in group}
            brush_adds: Dict[str, set] = {}
            zone_adds: Dict[str, set] = {}
            # (区,色) -> 本拍实际供出该色的（刷,套），保证首次来源归属正确。
            color_sources: Dict[Tuple[str, str], List[Tuple[str, int]]] = {}
            for b, z in group:
                brush_adds.setdefault(b, set()).update(snap_z[z])
                zone_adds.setdefault(z, set()).update(snap_b[b])
                for c in snap_b[b]:
                    color_sources.setdefault((z, c), []).append((b, sleeve_no[b]))
            for b, adds in brush_adds.items():
                brush_colors[b].update(adds)
            for z, adds in zone_adds.items():
                newly = adds - zone_colors[z]
                zone_colors[z].update(adds)
                for c in newly:
                    if c not in provenance[z]:
                        sources = sorted(set(color_sources.get((z, c), [])))
                        if sources:
                            b0, s0 = sources[0]
                            provenance[z][c] = {
                                "kind": "exchange", "t": qstr(t),
                                "source": f"{b0}#套{s0}",
                                "brush": b0, "sleeve": s0,
                            }
                push_zone(z, t)
            for b in brush_adds:
                push_brush(b, t)
            events.append({
                "t": qstr(t), "kind": "exchange_group",
                "members": sorted([{"brush": b, "zone": z, "sleeve": sleeve_no[b]} for b, z in group],
                                  key=lambda m: (m["brush"], m["zone"])),
            })

    return SimResult(
        horizon=(h0, h1),
        zone_timeline=zone_timeline,
        brush_timeline=brush_timeline,
        events=events,
        provenance=provenance,
    )


# --------------------------------------------------------------------------- #
# 换道判定：整个时间窗合格，而不是逐拍看点
# --------------------------------------------------------------------------- #
def judge_switch(
    sim: SimResult,
    sheet: SessionSheet,
    old_route: Sequence[str],
    new_route: Sequence[str],
    wipes: Sequence[Wipe] = (),
) -> dict:
    """按场单合同判定一次指定等候区换道能否兑现。

    - 沿用旧廊从开放一直保洁到交接结束 ``[open, t_arr+d)``；
    - 新廊从交接开始就要干净 ``[t_arr, depart)``；
    - 交接窗内部 ``(t_arr, t_arr+d)`` 两侧区一律禁擦（端点的截点动作按场单
      次序先办，故恰好落在两端的擦巾不违规）。
    """
    t0 = sheet.arrive_waiting
    tend = t0 + sheet.handover
    problems: List[str] = []

    old_dirty: List[dict] = []
    for z in old_route:
        if not sim.is_clean(z, sheet.forbidden, sheet.open_t, tend):
            spans = sim.dirty_spans(z, sheet.forbidden, sheet.open_t, tend)
            old_dirty.append({"zone": z, "spans": [[qstr(a), qstr(b)] for a, b in spans]})
            problems.append(f"旧廊区 {z} 在 [{qstr(sheet.open_t)}, {qstr(tend)}) 内带禁色 {sheet.forbidden}")

    new_dirty: List[dict] = []
    for z in new_route:
        if not sim.is_clean(z, sheet.forbidden, t0, sheet.depart_t):
            spans = sim.dirty_spans(z, sheet.forbidden, t0, sheet.depart_t)
            new_dirty.append({"zone": z, "spans": [[qstr(a), qstr(b)] for a, b in spans]})
            problems.append(f"新廊区 {z} 在 [{qstr(t0)}, {qstr(sheet.depart_t)}) 内带禁色 {sheet.forbidden}")

    corridor = set(old_route) | set(new_route)
    forbidden_wipes = [
        {"wipe_id": w.wipe_id, "zone": w.zone_id, "t": qstr(w.t)}
        for w in wipes
        if w.zone_id in corridor and t0 < w.t < tend
    ]
    if forbidden_wipes:
        problems.append(f"交接窗 ({qstr(t0)}, {qstr(tend)}) 内两侧走廊禁擦，却有擦巾: {forbidden_wipes}")

    return {
        "ok": not problems,
        "handover_window": [qstr(t0), qstr(tend)],
        "old_window": [qstr(sheet.open_t), qstr(tend)],
        "new_window": [qstr(t0), qstr(sheet.depart_t)],
        "old_dirty": old_dirty,
        "new_dirty": new_dirty,
        "forbidden_wipes": forbidden_wipes,
        "problems": problems,
    }


def earliest_switch_start(
    sim: SimResult,
    sheet: SessionSheet,
    old_route: Sequence[str],
    new_route: Sequence[str],
    wipes: Sequence[Wipe] = (),
) -> Optional[Fraction]:
    """“等队伍和通路实际都满足条件”的判定法：找最早可开始交接的时刻 s。

    队伍已在 ``arrive_waiting`` 到达等候区，故 s ≥ 到达时刻。洁净状态只在
    各区时间线变化点改变，候选 s 取到达时刻与廊区全部变化点；对每个候选
    用 :func:`judge_switch` 同款整窗合同检查（旧廊保到 s+d、新廊从 s 干净
    到离场、交接窗内部禁擦）。不存在返回 None。
    """
    d = sheet.handover
    earliest = sheet.arrive_waiting
    candidates = {earliest}
    for z in set(old_route) | set(new_route):
        for t, _ in sim.zone_timeline[z]:
            if earliest <= t < sheet.depart_t:
                candidates.add(t)
    for w in wipes:
        if w.zone_id in set(old_route) | set(new_route) and earliest <= w.t < sheet.depart_t:
            candidates.add(w.t)
    for s in sorted(candidates):
        trial = SessionSheet(
            session_id=sheet.session_id,
            forbidden=sheet.forbidden,
            open_t=sheet.open_t,
            depart_t=sheet.depart_t,
            waiting_zone=sheet.waiting_zone,
            arrive_waiting=s,
            handover=d,
        )
        verdict = judge_switch(sim, trial, old_route, new_route, wipes)
        if verdict["ok"] and s + d <= sheet.depart_t:
            return s
    return None


# --------------------------------------------------------------------------- #
# 展务台：按场单次序分配实物巾、交路线、领队签收
# --------------------------------------------------------------------------- #
class Desk:
    """三场调度台。

    用法：按场单登记次序逐个 :meth:`run_sheet`。已领走的实物巾与已交出的
    路线窗口不可撤销；规划只做“加巾”，而擦巾对颜色集合单调（只减不增），
    所以先前场次的巾不会破坏后场的洁净。
    """

    def __init__(
        self,
        hall: Hall,
        horizon: Tuple[Fraction, Fraction],
        trajectories: Dict[str, Trajectory],
        brush_profiles: Dict[str, Profile],
        brush_plans: Sequence[BrushPlan],
        initial_zone_colors: Dict[str, Iterable[str]],
        inventory: Sequence[str],
        external_consumed: Optional[Sequence[Wipe]] = None,
        actual_wipes: Optional[Sequence[Wipe]] = None,
    ):
        self.hall = hall
        self.horizon = horizon
        self.trajectories = trajectories
        self.brush_profiles = brush_profiles
        self.brush_plans = list(brush_plans)
        self.initial_zone_colors = initial_zone_colors
        self.inventory = sorted(set(inventory))
        if len(self.inventory) != len(set(inventory)):
            raise ExhibitError("实物巾号清单有重复")
        # 借去别厅等场外已消费的巾号：占号但不进本厅推演。
        self.external_consumed = list(external_consumed or [])
        for w in self.external_consumed:
            if w.wipe_id not in self.inventory:
                raise ExhibitError(f"场外消费的巾号 {w.wipe_id!r} 不在本台实物册")
        # 实轨里已经发生的场内擦巾：既成事实，占号、不可撤销、始终在时间线上。
        self.actual_wipes = list(actual_wipes or [])
        actual_ids = [w.wipe_id for w in self.actual_wipes]
        if len(set(actual_ids)) != len(actual_ids):
            raise ExhibitError("同一块实物巾在实轨中被擦了不止一次")
        self.used_ids = {w.wipe_id for w in self.external_consumed}
        for w in self.actual_wipes:
            if w.wipe_id not in self.inventory:
                raise ExhibitError(f"实轨擦巾 {w.wipe_id!r} 不在本台实物册")
            if w.wipe_id in self.used_ids:
                raise ExhibitError(f"实物巾 {w.wipe_id!r} 已被场外/前一笔占用，不能再擦")
            self.used_ids.add(w.wipe_id)
        # 已承诺派发的巾（含实轨既成擦巾）；后续场次不能撤销。
        self.committed_wipes: List[Wipe] = list(self.actual_wipes)
        self.decisions: List[dict] = []
        self.signoffs: List[dict] = []

    # -- 推演 ------------------------------------------------------------ #
    def simulation(self, extra_wipes: Sequence[Wipe] = ()) -> SimResult:
        return simulate(
            self.hall, self.horizon, self.trajectories, self.brush_profiles,
            self.brush_plans, self.initial_zone_colors,
            list(self.committed_wipes) + list(extra_wipes),
        )

    def free_ids(self) -> List[str]:
        return [w for w in self.inventory if w not in self.used_ids]

    def _assign_ids(self, count: int) -> List[str]:
        free = self.free_ids()
        if len(free) < count:
            return []
        return free[:count]

    # -- 固定路线场 ------------------------------------------------------ #
    def run_fixed(self, sheet: SessionSheet) -> dict:
        src = sheet.entrance or self.hall.entrance
        dst = sheet.exhibit or self.hall.exhibit
        routes = self.hall.simple_routes(src, dst)
        if sheet.old_route is not None:  # 也允许场单直接钉死路线
            routes = [tuple(sheet.old_route)]
        base_sim = self.simulation()
        route_reports = []
        for route in routes:
            route_set = set(route)
            slots = [s for s in sheet.slots if s.zone_id in route_set and s.t < sheet.depart_t]
            chosen = self._search_wipes(sheet, route, slots, mode="fixed")
            report = {
                "route": list(route),
                "clean_without_wipe": all(
                    base_sim.is_clean(z, sheet.forbidden, sheet.open_t, sheet.depart_t) for z in route
                ),
            }
            if chosen is not None:
                wipes_used, sim = chosen
                report.update({
                    "feasible": True,
                    "wipes": [{"wipe_id": w.wipe_id, "zone": w.zone_id, "t": qstr(w.t)} for w in wipes_used],
                })
                route_reports.append(report)
                decision = self._commit_fixed(sheet, route, wipes_used, sim)
                return decision
            report.update({"feasible": False, "wipes": []})
            route_reports.append(report)
        decision = {
            "session_id": sheet.session_id,
            "kind": "fixed",
            "ok": False,
            "strategy": None,
            "forbidden": sheet.forbidden,
            "window": [qstr(sheet.open_t), qstr(sheet.depart_t)],
            "route": None,
            "wipes_used": [],
            "candidates": route_reports,
            "reason": "所有入口→展件路线都无法在现有实物巾与允许时点内保净整场",
            "remaining_wipe_ids": self.free_ids(),
        }
        self.decisions.append(decision)
        return decision

    def _commit_fixed(self, sheet, route, wipes_used, sim) -> dict:
        for w in wipes_used:
            self.used_ids.add(w.wipe_id)
            self.committed_wipes.append(w)
        decision = {
            "session_id": sheet.session_id,
            "kind": "fixed",
            "ok": True,
            "strategy": "secure_whole_route",
            "forbidden": sheet.forbidden,
            "window": [qstr(sheet.open_t), qstr(sheet.depart_t)],
            "route": list(route),
            "wipes_used": [{"wipe_id": w.wipe_id, "zone": w.zone_id, "t": qstr(w.t)} for w in wipes_used],
            "occupied_cleaning": [
                f"{w.wipe_id} 占住 {w.zone_id}@{qstr(w.t)} 这一擦净时点，"
                f"保 [{qstr(sheet.open_t)}, {qstr(sheet.depart_t)}) 无 {sheet.forbidden}"
                for w in wipes_used
            ],
            "remaining_wipe_ids": self.free_ids(),
        }
        self.decisions.append(decision)
        return decision

    # -- 换道场 ---------------------------------------------------------- #
    def run_switch(self, sheet: SessionSheet) -> dict:
        if sheet.waiting_zone not in self.hall.zones:
            raise ExhibitError(f"场 {sheet.session_id!r} 等候区未知")
        src = sheet.entrance or self.hall.entrance
        dst = sheet.exhibit or self.hall.exhibit
        if sheet.old_route is not None and sheet.new_route is not None:
            pairs = [(tuple(sheet.old_route), tuple(sheet.new_route))]
        else:
            # 旧廊 = 入口到展件、途经等候区的完整在用路线（含旧支廊，它要一直
            # 保洁到交接结束）；新廊 = 等候区到展件的替代支廊。
            full_routes = [
                r for r in self.hall.simple_routes(src, dst)
                if sheet.waiting_zone in r
            ]
            suffixes = self.hall.simple_routes(sheet.waiting_zone, dst)
            pairs = [(old, new) for old in full_routes for new in suffixes]
            pairs.sort(key=lambda p: (p[0], p[1]))
        for old_route, new_route in pairs:
            corridor = set(old_route) | set(new_route)
            # 已承诺的巾若落在交接窗内部，该副廊直接不可能（两侧禁擦）。
            pre_verdict = judge_switch(self.simulation(), sheet, old_route, new_route, self.committed_wipes)
            if pre_verdict["forbidden_wipes"]:
                continue
            slots = [
                s for s in sheet.slots
                if s.zone_id in corridor and not (sheet.arrive_waiting < s.t < sheet.arrive_waiting + sheet.handover)
            ]
            found = self._search_wipes(sheet, (old_route, new_route), slots, mode="switch")
            if found is None:
                continue
            wipes_used, sim, verdict = found
            for w in wipes_used:
                self.used_ids.add(w.wipe_id)
                self.committed_wipes.append(w)
            start = earliest_switch_start(sim, sheet, old_route, new_route, self.committed_wipes)
            decision = {
                "session_id": sheet.session_id,
                "kind": "switch",
                "ok": True,
                "strategy": "switch_when_actual",
                "forbidden": sheet.forbidden,
                "old_route": list(old_route),
                "new_route": list(new_route),
                "handover_window": verdict["handover_window"],
                "old_window": verdict["old_window"],
                "new_window": verdict["new_window"],
                "earliest_switch_start": qstr(start) if start is not None else None,
                "wipes_used": [{"wipe_id": w.wipe_id, "zone": w.zone_id, "t": qstr(w.t)} for w in wipes_used],
                "occupied_cleaning": [
                    f"{w.wipe_id} 占住 {w.zone_id}@{qstr(w.t)}（交接开始前的进场前动作）"
                    for w in wipes_used
                ],
                "remaining_wipe_ids": self.free_ids(),
            }
            self.decisions.append(decision)
            return decision
        # 所有廊对都失败：保留最后一次判定作为举证。
        verdict = judge_switch(self.simulation(), sheet,
                               pairs[0][0], pairs[0][1], self.committed_wipes) if pairs else {"problems": ["无可达路线"]}
        decision = {
            "session_id": sheet.session_id,
            "kind": "switch",
            "ok": False,
            "strategy": None,
            "forbidden": sheet.forbidden,
            "handover_window": [qstr(sheet.arrive_waiting), qstr(sheet.arrive_waiting + sheet.handover)],
            "reason": "没有任何东西支廊组合能满足整窗交接合同（逐拍看似有干净路不等于可换道）",
            "last_verdict": verdict,
            "remaining_wipe_ids": self.free_ids(),
        }
        self.decisions.append(decision)
        return decision

    # -- 巾子集搜索（规模小、确定性） ------------------------------------ #
    def _search_wipes(self, sheet: SessionSheet, route_key, slots: Sequence[WipeSlot], mode: str):
        """按巾数从少到多、子集字典序找第一个可行派发；实物号按号序分配。"""
        # 同一 (zone,t) 只保留一个候选格；不同格按 (t, zone) 排序。
        uniq = sorted({(s.zone_id, s.t) for s in slots}, key=lambda zt: (zt[1], zt[0]))
        free = self.free_ids()
        for count in range(0, min(len(uniq), len(free)) + 1):
            for subset in itertools.combinations(uniq, count):
                ids = free[:count]
                extra = [Wipe(wid, z, t) for wid, (z, t) in zip(ids, subset)]
                # 同一块巾不能在同一批里被分到两个时点（上面每个 id 只用一次，OK）。
                sim = self.simulation(extra)
                if mode == "fixed":
                    route = route_key
                    if all(sim.is_clean(z, sheet.forbidden, sheet.open_t, sheet.depart_t) for z in route):
                        return extra, sim
                else:
                    old_route, new_route = route_key
                    verdict = judge_switch(sim, sheet, old_route, new_route,
                                           list(self.committed_wipes) + extra)
                    if verdict["ok"]:
                        return extra, sim, verdict
        return None

    def run_sheet(self, sheet: SessionSheet) -> dict:
        h0, h1 = self.horizon
        if sheet.forbidden not in self.hall.labels:
            raise ExhibitError(
                f"场 {sheet.session_id!r} 的禁色 {sheet.forbidden!r} 不在本厅两色标签 {self.hall.labels} 内"
            )
        if not (h0 <= sheet.open_t and sheet.depart_t <= h1):
            raise ExhibitError(
                f"场 {sheet.session_id!r} 的开放/离场窗 [{sheet.open_t}, {sheet.depart_t}) "
                f"超出回放域 [{h0}, {h1}]"
            )
        for s in sheet.slots:
            if not h0 <= s.t <= h1:
                raise ExhibitError(
                    f"场 {sheet.session_id!r} 的允许擦点 {s.t} 超出回放域 [{h0}, {h1}]"
                )
        if sheet.waiting_zone is not None:
            return self.run_switch(sheet)
        return self.run_fixed(sheet)

    def run_all(self, sheets: Sequence[SessionSheet]) -> List[dict]:
        return [self.run_sheet(s) for s in sheets]

    # -- 领队签收 -------------------------------------------------------- #
    def sign_off(self, session_id: str, leader: str, at: Optional[Fraction] = None) -> dict:
        """领队签收：始终用当前已承诺巾的全新推演复核，不照抄规划结论。

        若规划失败或复核时窗内仍有禁色（“只在门口交新图、仍有人处在脏走廊”），
        签收结论为 failed。
        """
        decision = next((d for d in self.decisions if d["session_id"] == session_id), None)
        if decision is None:
            raise ExhibitError(f"场 {session_id!r} 尚未经展务台处理，无路线图可签收")
        at = self.horizon[1] if at is None else fraction(at, field="signoff.at")
        sim = self.simulation()
        record: dict = {
            "session_id": session_id,
            "leader": leader,
            "at": qstr(at),
            "decision_ok": decision["ok"],
        }
        if not decision["ok"]:
            record.update({
                "signed": False,
                "verdict": "failed",
                "reason": decision.get("reason", "交接不可兑现；不能只在门口交出新路线图"),
            })
        elif decision["kind"] == "fixed":
            route = decision["route"]
            a, b = Fraction(decision["window"][0]), Fraction(decision["window"][1])
            dirty = [z for z in route if not sim.is_clean(z, decision["forbidden"], a, b)]
            ok = not dirty
            record.update({
                "signed": ok,
                "verdict": "handed_over" if ok else "failed",
                "route": route,
                "window": decision["window"],
                "dirty_at_signoff": dirty,
                "reason": None if ok else f"签收复核发现 {dirty} 仍处脏走廊，按失败办",
            })
        else:
            # 用决策中冻结的窗口做独立复核。
            t0, tend = Fraction(decision["handover_window"][0]), Fraction(decision["handover_window"][1])
            open_t = Fraction(decision["old_window"][0])
            depart_t = Fraction(decision["new_window"][1])
            old_route, new_route = decision["old_route"], decision["new_route"]
            bad_old = [z for z in old_route if not sim.is_clean(z, decision["forbidden"], open_t, tend)]
            bad_new = [z for z in new_route if not sim.is_clean(z, decision["forbidden"], t0, depart_t)]
            ok = not bad_old and not bad_new
            record.update({
                "signed": ok,
                "verdict": "switched" if ok else "failed",
                "old_route": old_route,
                "new_route": new_route,
                "handover_window": decision["handover_window"],
                "dirty_old": bad_old,
                "dirty_new": bad_new,
                "reason": None if ok else "签收复核发现仍有人处在脏走廊，换道按失败办",
            })
        self.signoffs.append(record)
        return record

    # -- 后场还剩哪条可走的路 -------------------------------------------- #
    def available_routes(self, forbidden: str, a: Fraction, b: Fraction,
                         src: Optional[str] = None, dst: Optional[str] = None) -> List[dict]:
        """在当前已承诺巾的时间线上，枚举 [a,b) 全程无禁色的入口→展件路线。"""
        sim = self.simulation()
        src = src or self.hall.entrance
        dst = dst or self.hall.exhibit
        out = []
        for route in self.hall.simple_routes(src, dst):
            bad = [z for z in route if not sim.is_clean(z, forbidden, a, b)]
            if not bad:
                out.append({"route": list(route), "window": [qstr(a), qstr(b)]})
        return out


# --------------------------------------------------------------------------- #
# 逐拍“看似有干净路线”的朴素判定（专门用来对照连续窗合同）
# --------------------------------------------------------------------------- #
def naive_tick_routes(sim: SimResult, hall: Hall, forbidden: str, ticks: Iterable[Fraction]) -> List[dict]:
    """每个拍点只看该时刻各区颜色，列出当时瞬时干净的路线。

    这正是东西廊反例里“任一拍看起来都有干净路线”的错误办法，保留它用于
    对照演示与测试。
    """
    rows = []
    for t in ticks:
        clean_zones = {z for z in hall.zones if forbidden not in sim.colors_at(z, t)}
        routes = [list(r) for r in hall.simple_routes(hall.entrance, hall.exhibit)
                  if all(z in clean_zones for z in r)]
        rows.append({"t": qstr(t), "clean_zones": sorted(clean_zones), "routes": routes})
    return rows
