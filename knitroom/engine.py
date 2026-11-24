"""针织机：把图解逐行织成带具名线圈、谱系、标记与边弧的边谱。

图解中每行只由三种动作组成：
- knit 平织：父圈一对一延续；
- dec  二并一：两个父圈合并成一个子圈，两个父圈都追得回去；
- inc  在一针上增一针：一个父圈分出两个子圈，两个子圈都追得回去。

边针何时从两端收掉由本册勘误决定：ChartSpec.edge_dec_end 取
'left' / 'right' / None，落在边端的 dec 才是收边针，该端边圈被收掉，
锚在它上面的缝线标记随之消失。再版只改这个值即可改变收针端。

本模块全部是纯数据、纯函数，不碰数据库；持久化由 pattern.PatternService 负责。
规模上限：最多 8 行，每行不超过 12 针（超过由 knit 拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from library.errors import Rejection

MAX_ROWS = 8
MAX_STITCHES = 12

# 三种动作
KNIT, DEC, INC = "knit", "dec", "inc"
ACTIONS = (KNIT, DEC, INC)

# 边端
LEFT, RIGHT = "left", "right"


@dataclass(frozen=True)
class StitchOp:
    """一行里的一针动作。

    - knit/dec：产出一个子圈，名字在 name；
    - inc（在一针上增一针）：一个父圈分出两个子圈，名字在 names，
      两个子圈与父圈在谱系里互相追得回去；
    - dec（二并一）：sources 是相邻两针，两个父圈合并成一个子圈。
    """

    action: str
    sources: tuple[int, ...]
    name: str | None = None
    names: tuple[str, ...] = ()
    on_edge: bool = False  # 该针是否落在图解指定的边圈上（边增/边并标记位）


@dataclass(frozen=True)
class ChartRow:
    row_no: int
    ops: tuple[StitchOp, ...]


@dataclass(frozen=True)
class ChartSpec:
    """一份完整图解（某册版下的不可变快照内容）。"""

    chart_id: str
    edition: str
    cast_on: tuple[str, ...]          # 起针行的具名线圈
    rows: tuple[ChartRow, ...]
    edge_dec_end: str | None = None   # 本册勘误决定的收针端
    markers: tuple[dict[str, str], ...] = ()  # 图解自带标记 {id, anchor, orientation}


@dataclass
class Loop:
    loop_id: str
    row_no: int
    position: int
    alive: bool = True
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "row_no": self.row_no,
            "position": self.position,
            "alive": self.alive,
            "parents": list(self.parents),
            "children": list(self.children),
        }


@dataclass
class Marker:
    marker_id: str
    anchor_loop: str
    orientation: str
    alive: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "marker_id": self.marker_id,
            "anchor_loop": self.anchor_loop,
            "orientation": self.orientation,
            "alive": self.alive,
        }


@dataclass
class Arc:
    """一段边弧：缝边上两标记之间（或端点到首/末标记之间）的一段。"""

    arc_id: str
    seq: int
    owner_loop: str
    region: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "arc_id": self.arc_id,
            "seq": self.seq,
            "owner_loop": self.owner_loop,
            "region": self.region,
        }


@dataclass
class EdgeSpectrum:
    """织成后的边谱：全部线圈（含已收掉的）、标记、当前边圈与边弧。"""

    chart_id: str
    edition: str
    loops: dict[str, Loop]
    markers: dict[str, Marker]
    edge_order: list[str]               # 当前边圈，按从左到右（腋下 -> 肩点）
    arcs: list[Arc]
    edge_dec_end: str | None

    def loop(self, loop_id: str) -> Loop:
        if loop_id not in self.loops:
            raise Rejection("unknown_loop", loop=loop_id)
        return self.loops[loop_id]

    def marker(self, marker_id: str) -> Marker:
        if marker_id not in self.markers:
            raise Rejection("unknown_marker", marker=marker_id)
        return self.markers[marker_id]

    # -------------------------------------------------------- 查询（本地短行）

    def marker_alive(self, marker_id: str) -> bool:
        """某标记是否已随收针消失。"""
        return self.marker(marker_id).alive

    def arcs_between(self, start_marker: str, end_marker: str) -> list[dict[str, Any]]:
        """两条缝线标记之间还剩几段边弧（短行查询，纯读）。

        任一标记已随收针消失即抛拒绝；两端锚圈自持的弧不计入“之间”，
        只统计严格夹在两标记当前锚圈之间的边圈所持弧。
        """
        m1, m2 = self.marker(start_marker), self.marker(end_marker)
        if not (m1.alive and m2.alive):
            raise Rejection(
                "marker_gone",
                start=start_marker,
                end=end_marker,
                start_alive=m1.alive,
                end_alive=m2.alive,
            )
        live = set(self.edge_order)
        idx1 = self._edge_index(m1.anchor_loop, live)
        idx2 = self._edge_index(m2.anchor_loop, live)
        lo, hi = sorted((idx1, idx2))
        return [
            a.as_dict()
            for a in self.arcs
            if a.owner_loop in live and lo < self.edge_order.index(a.owner_loop) < hi
        ]

    def count_region(self, region: str) -> int:
        """某缝线标记区间内当前还剩几段边弧（含切点锚圈自持弧）。"""
        return sum(1 for a in self.arcs if a.region == region and a.owner_loop in set(self.edge_order))

    def _edge_index(self, loop_id: str, live: set[str]) -> int:
        if loop_id not in live:
            # 标记仍 alive 而锚圈不在边圈上，属于数据损坏而非业务拒绝
            raise Rejection("anchor_not_on_edge", loop=loop_id)
        return self.edge_order.index(loop_id)

    def snapshot(self) -> dict[str, Any]:
        """完整快照：重开后据此重建边谱，不依赖当时的册版表是否仍在。"""
        return {
            "chart_id": self.chart_id,
            "edition": self.edition,
            "edge_dec_end": self.edge_dec_end,
            "loops": [l.as_dict() for l in self.loops.values()],
            "markers": [m.as_dict() for m in self.markers.values()],
            "edge_order": list(self.edge_order),
            "arcs": [a.as_dict() for a in self.arcs],
        }


def build_spec(payload: dict[str, Any]) -> ChartSpec:
    """把登记册版用的 JSON 字典装配成 ChartSpec，并做形状校验。"""
    try:
        chart_id = payload["chart_id"]
        edition = payload["edition"]
        cast_on = tuple(payload["cast_on"])
        edge_dec_end = payload.get("edge_dec_end")
        if edge_dec_end is not None and edge_dec_end not in (LEFT, RIGHT):
            raise Rejection("bad_edge_dec_end", value=edge_dec_end)
        rows = tuple(
            ChartRow(
                row_no=int(r["row_no"]),
                ops=tuple(
                    StitchOp(
                        action=op["action"],
                        sources=tuple(op["sources"]),
                        name=op.get("name"),
                        names=tuple(op.get("names", ())),
                        on_edge=bool(op.get("on_edge", False)),
                    )
                    for op in r["ops"]
                ),
            )
            for r in payload["rows"]
        )
        markers = tuple(dict(m) for m in payload.get("markers", ()))
    except KeyError as exc:
        raise Rejection("chart_field_missing", field=str(exc)) from exc
    spec = ChartSpec(chart_id, edition, cast_on, rows, edge_dec_end, markers)
    validate_spec(spec)
    return spec


def validate_spec(spec: ChartSpec) -> None:
    """织之前的静态校验：行数/针数上限、动作合法性、来源相邻且不重叠。"""
    if not spec.cast_on:
        raise Rejection("empty_cast_on", chart=spec.chart_id)
    if len(spec.cast_on) > MAX_STITCHES:
        raise Rejection("too_many_stitches", count=len(spec.cast_on), limit=MAX_STITCHES)
    if len(spec.rows) > MAX_ROWS:
        raise Rejection("too_many_rows", count=len(spec.rows), limit=MAX_ROWS)
    if len(set(spec.cast_on)) != len(spec.cast_on):
        raise Rejection("duplicate_loop_name", chart=spec.chart_id)
    prev_count = len(spec.cast_on)
    seen_names: set[str] = set(spec.cast_on)
    for row in spec.rows:
        if not row.ops:
            raise Rejection("empty_row", row=row.row_no)
        out_count = sum(2 if op.action == INC else 1 for op in row.ops)
        if out_count > MAX_STITCHES:
            raise Rejection("too_many_stitches", row=row.row_no, limit=MAX_STITCHES)
        covered: list[int] = []
        for op in row.ops:
            if op.action not in ACTIONS:
                raise Rejection("bad_action", action=op.action, row=row.row_no)
            if not op.sources or any(s < 0 or s >= prev_count for s in op.sources):
                raise Rejection("source_out_of_range", row=row.row_no)
            if op.action == DEC:
                if len(op.sources) != 2 or op.sources[1] != op.sources[0] + 1:
                    raise Rejection("dec_must_be_adjacent_pair", name=op.name)
            elif len(op.sources) != 1:
                raise Rejection("single_source_required", action=op.action)
            if op.action == INC:
                if len(op.names) != 2 or len(set(op.names)) != 2:
                    raise Rejection("inc_needs_two_distinct_names", row=row.row_no)
                new_names = list(op.names)
            else:
                if not op.name:
                    raise Rejection("op_needs_name", action=op.action, row=row.row_no)
                new_names = [op.name]
            for nm in new_names:
                if nm in seen_names:
                    raise Rejection("duplicate_loop_name", name=nm)
                seen_names.add(nm)
            covered.extend(op.sources)
        if sorted(covered) != list(range(prev_count)):
            # 每个父圈恰好被消费一次：不许多吃、漏吃、重叠吃
            raise Rejection("row_sources_not_partition", row=row.row_no)
        prev_count = out_count
        if prev_count > MAX_STITCHES:
            raise Rejection("too_many_stitches", row=row.row_no, limit=MAX_STITCHES)
    marker_ids: set[str] = set()
    for m in spec.markers:
        if m["id"] in marker_ids:
            raise Rejection("duplicate_marker", marker=m["id"])
        marker_ids.add(m["id"])
        if m.get("orientation") not in ("up", "down"):
            raise Rejection("bad_marker_orientation", marker=m["id"])
        if m["anchor"] not in seen_names:
            raise Rejection("marker_anchor_missing", marker=m["id"], anchor=m["anchor"])


def knit(spec: ChartSpec, extra_markers: list[dict[str, str]] | None = None) -> EdgeSpectrum:
    """逐行织出边谱。extra_markers 是本册已画记号（挂在具名线圈上）。"""
    loops: dict[str, Loop] = {}
    # 被二并一消费过的父圈：锚在它或它祖先上的标记随收针消失
    dec_consumed: set[str] = set()
    current: list[str] = []
    for pos, name in enumerate(spec.cast_on):
        loops[name] = Loop(name, row_no=0, position=pos)
        current.append(name)

    for row in spec.rows:
        nxt: list[str] = []
        out_pos = 0
        edge_ops_by_pos: dict[int, StitchOp] = {}
        for op in row.ops:
            parents = [current[s] for s in op.sources]
            if op.action == DEC:
                dec_consumed.update(parents)
            if op.action == INC:
                # 一个父圈分出两个具名子圈，谱系两侧都挂得上
                for nm in op.names:
                    child = Loop(nm, row_no=row.row_no, position=out_pos)
                    child.parents = list(parents)
                    loops[nm] = child
                    for src in op.sources:
                        loops[current[src]].children.append(nm)
                    nxt.append(nm)
                    edge_ops_by_pos[out_pos] = op
                    out_pos += 1
            else:
                child = Loop(op.name, row_no=row.row_no, position=out_pos)
                child.parents = parents
                loops[op.name] = child
                for src in op.sources:
                    loops[current[src]].children.append(op.name)
                nxt.append(op.name)
                edge_ops_by_pos[out_pos] = op
                out_pos += 1
        # 收边针：仅当二并一发生在本册勘误指定的边端
        if spec.edge_dec_end is not None:
            edge_idx = 0 if spec.edge_dec_end == LEFT else len(nxt) - 1
            edge_op = edge_ops_by_pos[edge_idx]
            if edge_op.action == DEC and edge_op.on_edge:
                # 被并掉的外侧父圈从边上收掉：left 收 sources 对里的左父，
                # right 收右父；合并子圈留在针上
                gone_pos = (
                    edge_op.sources[0]
                    if spec.edge_dec_end == LEFT
                    else edge_op.sources[1]
                )
                loops[current[gone_pos]].alive = False
        for name in current:
            if not loops[name].children:  # 没有子圈延续的父圈已不在针上
                loops[name].alive = False
        current = nxt

    edge_order = [name for name in current if loops[name].alive]

    # 标记：图解自带 + 本册记号；同名以本册记号为准（“本册已画记号”）
    markers: dict[str, Marker] = {}
    for m in spec.markers:
        markers[m["id"]] = Marker(m["id"], m["anchor"], m["orientation"])
    for m in extra_markers or []:
        if m["anchor"] not in loops:
            raise Rejection("copy_mark_anchor_missing", anchor=m["anchor"])
        markers[m["id"]] = Marker(m["id"], m["anchor"], m["orientation"])
    # 标记随锚圈（沿谱系）被收针而消失：延续链上任一圈被二并一消费即消失
    for marker in markers.values():
        chain = _lineage_chain(marker.anchor_loop, loops)
        if any(name in dec_consumed for name in chain):
            marker.alive = False
        else:
            marker.anchor_loop = _lineage_tip(marker.anchor_loop, loops)
            marker.alive = loops[marker.anchor_loop].alive

    arcs = _build_arcs(markers, edge_order, loops)
    return EdgeSpectrum(
        chart_id=spec.chart_id,
        edition=spec.edition,
        loops=loops,
        markers=markers,
        edge_order=edge_order,
        arcs=arcs,
        edge_dec_end=spec.edge_dec_end,
    )


def _lineage_chain(loop_id: str, loops: dict[str, Loop]) -> list[str]:
    """从锚圈沿唯一活子圈走到针尖的整条延续链（含锚圈自身）。

    加针分出两个活子圈处停止——标记锚的是加针前那个具名圈，分叉后不唯一。
    """
    chain = [loop_id]
    cur = loop_id
    while True:
        children = [c for c in loops[cur].children if loops[c].alive]
        if len(children) != 1:
            return chain
        cur = children[0]
        chain.append(cur)


def _lineage_tip(loop_id: str, loops: dict[str, Loop]) -> str:
    """沿唯一活子圈走到当前仍在针上的后代（平织/二并一的延续链）。

    遇到加针分出的两个活子圈即停止——标记锚的是加针前的那个具名圈，
    分叉后不再唯一代表它，标记停在分叉前一圈。
    """
    return _lineage_chain(loop_id, loops)[-1]


def _build_arcs(
    markers: dict[str, Marker], edge_order: list[str], loops: dict[str, Loop]
) -> list[Arc]:
    """按标记把边切成弧：每个当前边圈恰好持一段弧，弧序从腋下向肩点。

    每个边圈的区间由“最近的、锚位不晚于它的标记”和“最近的、锚位不早于它的
    标记”夹出；锚圈自身两边都是同一个标记，于是两端锚圈自持弧分别是
    AXL->AXL、SHD->SHD，严格夹在两标记之间的边圈才属于 AXL->SHD。
    没有标记时整条边只有一个 AXL->SHD 区间。
    """
    live = [m for m in markers.values() if m.alive and m.anchor_loop in edge_order]
    cuts = sorted((edge_order.index(m.anchor_loop), m.marker_id) for m in live)
    arcs: list[Arc] = []
    for seq, owner in enumerate(edge_order, start=1):
        pos = edge_order.index(owner)
        at_or_before = [name for idx, name in cuts if idx <= pos]
        at_or_after = [name for idx, name in cuts if idx >= pos]
        left = at_or_before[-1] if at_or_before else "AXL"   # 腋下起点
        right = at_or_after[0] if at_or_after else "SHD"     # 肩点
        arcs.append(Arc(f"A{seq}", seq, owner, f"{left}->{right}"))
    return arcs


def spectrum_from_snapshot(snapshot: dict[str, Any]) -> EdgeSpectrum:
    """重开/复算时从缝边记录里的快照重建边谱（不查册版表）。"""
    loops = {
        d["loop_id"]: Loop(
            d["loop_id"], d["row_no"], d["position"], d["alive"],
            list(d["parents"]), list(d["children"]),
        )
        for d in snapshot["loops"]
    }
    markers = {
        d["marker_id"]: Marker(
            d["marker_id"], d["anchor_loop"], d["orientation"], d["alive"]
        )
        for d in snapshot["markers"]
    }
    arcs = [
        Arc(d["arc_id"], d["seq"], d["owner_loop"], d["region"])
        for d in snapshot["arcs"]
    ]
    arcs.sort(key=lambda a: a.seq)
    return EdgeSpectrum(
        chart_id=snapshot["chart_id"],
        edition=snapshot["edition"],
        loops=loops,
        markers=markers,
        edge_order=list(snapshot["edge_order"]),
        arcs=arcs,
        edge_dec_end=snapshot.get("edge_dec_end"),
    )
