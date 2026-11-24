"""桌面电梯演练：从入样计数行还原搭乘历史，并在中途交班后实办接替。

本模块是加权采样仓库的演练入口，分两个角色，严格对应题面：

1. **账房（历史还原）**：门口计数员的每条计数行只给出一个停靠区间上的
   总上人数 / 总下人数 / 总上下人数 / 净变动，可能再附一个楼层的实名进出
   观察。账房知道每个具名同行组的人数、候梯层、允许目的层集合，各组只能
   *整进整出*。本模块枚举出与全部入样计数、运动方向、最大载量、初终空厢
   共同相容的 *所有* 搭乘历史，并可单独查询某站关门时哪些组确定（或可能）
   留在厢内。每个区间必须来自同一趟真实历史（``trip_id`` 不一致直接拒绝）。

2. **管理员（接替调度）**：在指定站开门、原乘客已下完而新客尚未上的时点
   交班。司机当场只看得到读数：本拍楼层、本拍下客人数、称重后的厢内总人数；
   看不到没下厢者的身份。规划因此在 *信念态*（所有与读数相容的匿名世界之
   集合）上做与或搜索，同一条口令对所有世界生效，绝不按不可见身份发不同
   口令。每移动一层、每开门各耗一拍；只有停站开门才知谁下、称重后才知人数。

两种接客规程：

* ``ticket``（A，提前发票）：具名整组的登厢票预先送到楼层，票带有效开门
  窗口；窗口内开门时必须为已发票整组留位并整组上车，别的楼层先有空也不能
  挪。规划器从大到小枚举票组，返回可保证兑现的最多预约组。
* ``hail``（B，手中留名额）：不预先承诺，到门称重后由楼层员在当次开门
  招呼整组上车；当次开门只要有招呼就多耗一拍。规划器最大化可保证送达的
  预约组数。

已发票、已招呼上车的实客都随车交班；``dispatch.pre_aboard`` 表达交班时
已在厢内的具名新约客。

规模刻意保持桌面级：最多四层、五组、十个动作，因此直接做穷举/信念态搜索，
不引入任何启发式近似，结论可证明。仅使用 Python 3.11 标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 场景数据模型
# ---------------------------------------------------------------------------

UP = "up"
DOWN = "down"

OBS_TYPES = ("ups", "downs", "gross", "net")


@dataclass(frozen=True)
class Group:
    """一个具名同行组：整进整出，只在候梯层上、在允许目的层中的一层下。"""

    gid: str
    size: int
    origin: int
    destinations: Tuple[int, ...]


@dataclass(frozen=True)
class CountRow:
    """门口计数员的一行记录（入样后即成为硬等式约束）。"""

    row_id: str
    trip_id: str
    interval: Tuple[int, int]      # 闭区间 [lo, hi]，按楼层号
    kind: str                      # ups / downs / gross / net
    value: int
    named_floor: Optional[int] = None
    named_in: Tuple[str, ...] = ()
    named_out: Tuple[str, ...] = ()
    # 仅用于接入加权采样源：
    weight: int = 1
    shard: str = "s0"


@dataclass(frozen=True)
class Reservation:
    """一张预约班单上的新约整组。"""

    rid: str
    size: int
    origin: int
    destinations: Tuple[int, ...]
    window: Tuple[int, int]        # 只在 origin 开门且拍点落入窗口时可上
    deliver_by: Optional[int] = None  # 目的层开门拍点的硬截止


@dataclass(frozen=True)
class Scenario:
    name: str
    floors: Tuple[int, ...]
    direction: str
    capacity: int
    groups: Tuple[Group, ...]
    rows: Tuple[CountRow, ...]

    def group(self, gid: str) -> Group:
        for g in self.groups:
            if g.gid == gid:
                return g
        raise KeyError(gid)

    @property
    def visit_order(self) -> Tuple[int, ...]:
        """电梯实际停靠顺序：上行按楼层升序，下行按楼层降序。"""

        if self.direction == DOWN:
            return tuple(sorted(self.floors, reverse=True))
        return tuple(self.floors)

    def floor_index(self, floor: int) -> int:
        return self.visit_order.index(floor)


@dataclass(frozen=True)
class Dispatch:
    """管理员独立收到的接替调度单。"""

    handover_floor: int
    observed_alight: int           # 交班开门当场看到的下客人数
    reservations: Tuple[Reservation, ...]
    pre_aboard: Tuple[Tuple[str, int, int, Optional[int]], ...] = ()
    # pre_aboard 每项 = (rid, size, dest, deliver_by)：交班时已在厢的具名客
    max_beats: int = 10

    def reservation(self, rid: str) -> Reservation:
        for r in self.reservations:
            if r.rid == rid:
                return r
        raise KeyError(rid)


# ---------------------------------------------------------------------------
# 场景解析（JSON -> 数据类），并做结构自检
# ---------------------------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def parse_scenario(obj: dict) -> Scenario:
    _require(isinstance(obj.get("floors"), list) and obj["floors"],
             "scenario.floors 必须是非空楼层表")
    floors = tuple(int(x) for x in obj["floors"])
    _require(all(floors[i] < floors[i + 1] for i in range(len(floors) - 1)),
             "楼层表必须严格递增（单方向运动）")
    direction = obj.get("direction", UP)
    _require(direction in (UP, DOWN), "direction 只能是 up/down")
    capacity = int(obj["capacity"])
    _require(capacity >= 1, "capacity 必须 >= 1")

    groups: List[Group] = []
    seen_gids: set = set()
    for raw in sorted(obj.get("groups", []), key=lambda x: x["id"]):
        _require(raw["id"] not in seen_gids,
                 f"重复的组 id: {raw['id']}")
        seen_gids.add(raw["id"])
        dests = tuple(sorted(int(d) for d in raw["destinations"]))
        _require(dests and all(d != raw["origin"] for d in dests),
                 f"组 {raw['id']} 的目的层必须非空且不同于候梯层")
        _require(all(raw["origin"] in floors and d in floors for d in dests),
                 f"组 {raw['id']} 引用了不存在的楼层")
        _require(int(raw["size"]) >= 1, f"组 {raw['id']} 人数必须 >= 1")
        groups.append(Group(raw["id"], int(raw["size"]), int(raw["origin"]),
                            dests))
    gids = {g.gid for g in groups}

    rows: List[CountRow] = []
    seen_rids: set = set()
    for raw in sorted(obj.get("observations", []),
                      key=lambda x: x["row_id"]):
        _require(raw["row_id"] not in seen_rids,
                 f"重复的计数行 id: {raw['row_id']}")
        seen_rids.add(raw["row_id"])
        lo, hi = (int(x) for x in raw["interval"])
        _require(lo in floors and hi in floors and lo <= hi,
                 f"计数行 {raw['row_id']} 区间非法")
        kind = raw["type"]
        _require(kind in OBS_TYPES, f"计数行 {raw['row_id']} 类型非法")
        _require(int(raw["value"]) >= 0,
                 f"计数行 {raw['row_id']} 计数不能为负")
        nf = raw.get("named_floor")
        nin = tuple(raw.get("named_in", []))
        nout = tuple(raw.get("named_out", []))
        if nf is not None:
            _require(int(nf) in floors, f"计数行 {raw['row_id']} 实名层非法")
            _require(set(nin) <= gids and set(nout) <= gids,
                     f"计数行 {raw['row_id']} 实名观察引用了未知组")
        rows.append(CountRow(
            raw["row_id"], str(raw.get("trip_id", "trip-0")), (lo, hi), kind,
            int(raw["value"]), None if nf is None else int(nf), nin, nout,
            int(raw.get("weight", 1)), str(raw.get("shard", "s0"))))

    # 同一趟演练：每个区间必须取自同一趟真实历史。
    trips = {r.trip_id for r in rows}
    _require(len(trips) <= 1,
             f"计数行来自多个 trip_id，禁止混趟还原: {sorted(trips)}")
    return Scenario(obj.get("name", "scenario"), floors, direction, capacity,
                    tuple(groups), tuple(rows))


def parse_dispatch(obj: dict) -> Dispatch:
    reservations: List[Reservation] = []
    seen_rids: set = set()
    for raw in sorted(obj.get("reservations", []), key=lambda x: x["id"]):
        _require(raw["id"] not in seen_rids,
                 f"重复的预约 id: {raw['id']}")
        seen_rids.add(raw["id"])
        dests = tuple(sorted(int(d) for d in raw["destinations"]))
        wlo, whi = (int(x) for x in raw["window"])
        _require(wlo <= whi, f"预约 {raw['id']} 窗口非法")
        _require(dests and int(raw["size"]) >= 1,
                 f"预约 {raw['id']} 非法")
        db = raw.get("deliver_by")
        _require(db is None or int(db) >= 0,
                 f"预约 {raw['id']} deliver_by 非法")
        reservations.append(Reservation(
            raw["id"], int(raw["size"]), int(raw["origin"]), dests,
            (wlo, whi), None if db is None else int(db)))
    pre_ids = {p["id"] for p in obj.get("pre_aboard", [])}
    _require(not (pre_ids & seen_rids),
             "pre_aboard 与新预约存在重复 id")
    pre = tuple(
        (p["id"], int(p["size"]), int(p["dest"]),
         None if p.get("deliver_by") is None else int(p["deliver_by"]))
        for p in sorted(obj.get("pre_aboard", []), key=lambda x: x["id"]))
    _require(int(obj["max_beats"]) >= 1, "max_beats 必须 >= 1")
    return Dispatch(int(obj["handover_floor"]),
                    int(obj["observed_alight"]), tuple(reservations), pre,
                    int(obj.get("max_beats", 10)))


def load_scenario(path: str) -> Scenario:
    with open(path, encoding="utf-8") as fh:
        return parse_scenario(json.load(fh))


def load_dispatch(path: str) -> Dispatch:
    with open(path, encoding="utf-8") as fh:
        return parse_dispatch(json.load(fh))


# ---------------------------------------------------------------------------
# 账房：搭乘历史的枚举
#
# 一条历史 = 每个组的选择：不搭乘，或在 destinations 中选一层下。
# 模拟每层“先下后上”，逐楼层校验载量与实名观察，逐区间校验聚合计数。
# 组按 gid 排序、选择按 [不搭乘, 目的层升序] 排序，输出确定可复跑。
# ---------------------------------------------------------------------------

@dataclass
class History:
    """一条完整搭乘历史：gid -> 下厢层（未出现即未搭乘）。"""

    choice: Dict[str, int]
    # 每楼层的事件与各时点厢内组（按楼层顺序）
    events: Dict[int, Tuple[Tuple[str, ...], Tuple[str, ...]]]
    onboard_after: Dict[int, FrozenSet[str]]   # 关门后留在厢内的组
    loads: Dict[int, int]                      # 关门后厢内人数


def _simulate(scn: Scenario, choice: Dict[str, int]
             ) -> Optional[History]:
    """按楼层顺序模拟；载量越界或实名观察不符返回 None。"""

    onboard: List[str] = []                      # 当前厢内组 id
    events: Dict[int, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}
    onboard_after: Dict[int, FrozenSet[str]] = {}
    loads: Dict[int, int] = {}
    by_id = {g.gid: g for g in scn.groups}

    for floor in scn.visit_order:
        alighters = tuple(sorted(
            gid for gid in onboard if choice[gid] == floor))
        for gid in alighters:
            onboard.remove(gid)
        boarders = tuple(sorted(
            gid for gid, dest in choice.items()
            if dest is not None and by_id[gid].origin == floor))
        # 整组只能沿运动方向走：候梯层必须在目的层之前被访问。
        for gid in boarders:
            if scn.floor_index(by_id[gid].origin) >= \
                    scn.floor_index(choice[gid]):
                return None
        # 实名进出观察（若该层有）：整组名单必须完全一致。
        for row in scn.rows:
            if row.named_floor == floor:
                if set(row.named_in) != set(boarders):
                    return None
                if set(row.named_out) != set(alighters):
                    return None
        for gid in boarders:
            if gid in onboard:  # 防御：整组只上一次
                return None
            onboard.append(gid)
        load_people = sum(by_id[gid].size for gid in onboard)
        if load_people > scn.capacity:
            return None
        events[floor] = (boarders, alighters)
        onboard_after[floor] = frozenset(onboard)
        loads[floor] = load_people

    # 终点已知为空。
    if onboard:
        return None
    return History(dict(choice), events, onboard_after, loads)


def _row_value(row: CountRow, scn: Scenario, hist: History) -> int:
    lo, hi = row.interval
    board_people = alight_people = 0
    by_id = {g.gid: g for g in scn.groups}
    for floor in scn.floors:
        if lo <= floor <= hi:
            boarders, alighters = hist.events[floor]
            board_people += sum(by_id[g].size for g in boarders)
            alight_people += sum(by_id[g].size for g in alighters)
    if row.kind == "ups":
        return board_people
    if row.kind == "downs":
        return alight_people
    if row.kind == "gross":
        return board_people + alight_people
    return board_people - alight_people


def enumerate_histories(scn: Scenario,
                        rows: Optional[Sequence[CountRow]] = None
                        ) -> List[History]:
    """枚举与全部（入样）计数行相容的所有搭乘历史。

    ``rows`` 给定时只以这些行作为约束（账房只拿到真正入样的行）；否则用
    场景内全部计数行。
    """

    rows = tuple(scn.rows if rows is None else rows)
    trips = {r.trip_id for r in rows}
    _require(len(trips) <= 1,
             f"入样行来自多个 trip_id，禁止混趟: {sorted(trips)}")
    ordered = sorted(scn.groups, key=lambda g: g.gid)
    results: List[History] = []

    def dfs(i: int, choice: Dict[str, int]) -> None:
        if i == len(ordered):
            hist = _simulate(scn, choice)
            if hist is None:
                return
            if all(_row_value(row, scn, hist) == row.value for row in rows):
                results.append(hist)
            return
        g = ordered[i]
        # 选项顺序固定：不搭乘 -> 目的层升序，保证输出可重复。
        dfs(i + 1, {**choice, g.gid: None})
        for dest in g.destinations:
            dfs(i + 1, {**choice, g.gid: dest})

    dfs(0, {})
    def sort_key(h: History) -> Tuple[str, ...]:
        return tuple(
            ("" if h.choice[g.gid] is None else f"{h.choice[g.gid]}")
            for g in ordered)
    results.sort(key=sort_key)
    return results


def certain_onboard(histories: Sequence[History], floor: int
                    ) -> Tuple[str, ...]:
    """某站关门时，在 *所有* 相容历史中都留在厢内的组（确定在厢）。"""

    _require(histories, "没有任何相容历史，无法判定")
    inter = set(histories[0].onboard_after[floor])
    for h in histories[1:]:
        inter &= set(h.onboard_after[floor])
    return tuple(sorted(inter))


def possible_onboard(histories: Sequence[History], floor: int
                     ) -> Tuple[str, ...]:
    """某站关门时，在 *至少一条* 相容历史中留在厢内的组。"""

    _require(histories, "没有任何相容历史，无法判定")
    union: set = set()
    for h in histories:
        union |= set(h.onboard_after[floor])
    return tuple(sorted(union))


def history_to_dict(hist: History) -> dict:
    return {
        "rides": {gid: dest for gid, dest in sorted(hist.choice.items())
                  if dest is not None},
        "absent": sorted(gid for gid, dest in hist.choice.items()
                         if dest is None),
        "events": [
            {"floor": f,
             "board": list(hist.events[f][0]),
             "alight": list(hist.events[f][1]),
             "load_after_close": hist.loads[f]}
            # floors 顺序由调用方场景决定；events 已覆盖每个楼层
            for f in sorted(hist.events)],
    }


# ---------------------------------------------------------------------------
# 交班：把历史折叠成司机不可读身份的匿名世界
# ---------------------------------------------------------------------------

def handover_worlds(scn: Scenario, histories: Sequence[History],
                    handover_floor: int, observed_alight: int
                    ) -> List[Tuple[Tuple[Tuple[int, int], ...], ...]]:
    """从相容历史构造交班开门（下客已完成）后的匿名世界列表。

    每个世界是一个匿名在途者多重集 ``(目的层, 人数)`` 的有序元组；身份被
    擦除——司机只可能在称重后读到总人数。同一匿名构型的多条历史折叠为一个
    世界（司机的读数无法区分它们）。
    """

    by_id = {g.gid: g for g in scn.groups}
    worlds: List[Tuple[Tuple[int, int], ...]] = []
    seen = set()
    for hist in histories:
        boarders, alighters = hist.events[handover_floor]
        if boarders:
            # 交班时点是“原乘客已下完、新客尚未上”；若历史在该层还有原始
            # 组登厢，说明场景把登厢和交班放进了同一开门，语义无法切分。
            raise ValueError(
                f"历史在交班楼层 {handover_floor} 仍有原始组登厢 "
                f"{list(boarders)}，请把候梯层与交班层错开")
        alight_people = sum(by_id[g].size for g in alighters)
        if alight_people != observed_alight:
            continue
        riders = tuple(sorted(
            (hist.choice[gid], by_id[gid].size)
            for gid in hist.onboard_after[handover_floor]))
        if riders not in seen:
            seen.add(riders)
            worlds.append(riders)
    if not worlds:
        raise ValueError(_handover_empty_error(handover_floor,
                                               observed_alight))
    return worlds


def _handover_empty_error(floor: int, alight: int) -> str:
    return (f"交班读数无相容世界：楼层 {floor} 下客 {alight} 人与所有还原历史"
            "矛盾")


# ---------------------------------------------------------------------------
# 管理员：信念态与或搜索
#
# 状态 = (当前开门楼层 f, 当前拍点 t, 信念（匿名世界多重集）, 具名在途者,
#         未处理预约, 已送达预约)。开门时按 (下客人数, 称重总人数) 分裂信念，
# 同一节点的上车口令对所有分支相同——策略结构本身保证不偷看身份。
# ----------------------------------------------------------------name

TICKET = "ticket"
HAIL = "hail"
INFEASIBLE = None


class _Planner:
    def __init__(self, scn: Scenario, dispatch: Dispatch,
                 worlds: List[Tuple[Tuple[int, int], ...]], protocol: str,
                 tickets: FrozenSet[str]):
        self.scn = scn
        self.dsp = dispatch
        self.worlds = [tuple(sorted(w)) for w in worlds]
        self.protocol = protocol
        self.tickets = tickets
        self.resv = {r.rid: r for r in dispatch.reservations}
        self.memo: Dict[tuple, object] = {}
        self.stats = {"nodes": 0, "branches": 0, "memo_hits": 0}

    # -- 基础工具 ----------------------------------------------------------

    def _vi(self, floor: int) -> int:
        """楼层在运动方向访问序列中的位置（方向无关的“前后”度量）。"""

        return self.scn.floor_index(floor)

    def _named_load(self, named: Tuple[tuple, ...]) -> int:
        return sum(size for _, size, _, _ in named)

    def _world_load(self, world: Tuple[Tuple[int, int], ...]) -> int:
        return sum(size for _, size in world)

    def _min_dest(self, belief: List[Tuple[Tuple[int, int], ...]],
                  named: Tuple[tuple, ...]) -> Optional[int]:
        """沿运动方向最近的下客楼层（访问序最小），与上行/下行无关。"""

        dests = [d for w in belief for d, _ in w]
        dests += [d for _, _, d, _ in named]
        if not dests:
            return None
        return min(dests, key=self._vi)

    def _expire(self, f: int, pending: Tuple[str, ...]
                ) -> Tuple[Tuple[str, ...], List[str]]:
        """候梯层已经开过（访问序严格早于当前层）的预约失去窗口。

        当前层 f 的预约在本次开门仍可上车，不算失约。
        """

        live, missed = [], []
        here = self._vi(f)
        for rid in pending:
            if self._vi(self.resv[rid].origin) < here:
                missed.append(rid)
            else:
                live.append(rid)
        return tuple(sorted(live)), sorted(missed)

    # -- 开门：下客、上车、按读数分裂 --------------------------------------

    def _open_door(self, f: int, t: int,
                   belief: List[Tuple[Tuple[int, int], ...]],
                   named: Tuple[tuple, ...],
                   pending: Tuple[str, ...],
                   delivered: Tuple[str, ...],
                   board_choice: Tuple[Tuple[str, int], ...]
                   ) -> Optional[dict]:
        """在给定上车决定下执行开门，按读数分裂信念。

        每个读数分支保留各自的匿名世界列表——同一读数下的不同构型必须分别
        保留，未来开门可能再次区分它们，绝不能并成一个“混合世界”。
        """

        boarded_named = tuple(sorted(
            (rid, self.resv[rid].size, dest,
             self.resv[rid].deliver_by)
            for rid, dest in board_choice))
        branches: Dict[Tuple[int, int], dict] = {}
        for world in belief:
            stay = tuple(sorted((d, s) for d, s in world if d != f))
            alight = self._world_load(world) - self._world_load(stay)
            named_after_alight = tuple(sorted(
                x for x in named if x[2] != f))
            named_alight = sorted(x[0] for x in named if x[2] == f)
            # 上车后完整具名构型（下客后的在途者 + 本拍新上车）。
            named_full = tuple(sorted(set(named_after_alight)
                                      | set(boarded_named)))
            post_load = self._world_load(stay) + self._named_load(named_full)
            if post_load > self.scn.capacity:
                return None
            key = (alight, post_load)
            if key not in branches:
                branches[key] = {
                    "worlds": [],
                    "named_full": named_full,
                    "delivered": tuple(sorted(
                        set(delivered) | set(named_alight))),
                }
                self.stats["branches"] += 1
            slot = branches[key]
            if stay not in slot["worlds"]:
                slot["worlds"].append(stay)
            # 具名客对司机是可见身份，同读数下其构型必然一致。
            if slot["named_full"] != named_full:
                raise AssertionError("同读数下具名在途者构型不一致")
        new_pending = tuple(sorted(set(pending) - {rid for rid, _ in
                                                   board_choice}))
        return {"branches": branches,
                "pending": new_pending,
                "hail": self.protocol == HAIL and bool(board_choice)}

    def _board_options(self, f: int, t: int,
                       belief: List[Tuple[Tuple[int, int], ...]],
                       named: Tuple[tuple, ...],
                       pending: Tuple[str, ...]
                       ) -> List[Tuple[Tuple[str, int], ...]]:
        """本拍开门允许的整组上车方案（含目的层指派），按确定顺序枚举。"""

        waiting = [rid for rid in pending
                   if self.resv[rid].origin == f
                   and self.resv[rid].window[0] <= t
                   <= self.resv[rid].window[1]]
        min_free = min(self.scn.capacity
                       - (self._world_load(w) - sum(
                           s for d, s in w if d == f)
                          + self._named_load(tuple(
                              x for x in named if x[2] != f)))
                       for w in belief)
        if min_free <= 0:
            return [()]
        if self.protocol == TICKET:
            forced = tuple(sorted(rid for rid in waiting
                                  if rid in self.tickets))
            need = sum(self.resv[rid].size for rid in forced)
            if need > min_free:
                return []                       # 票无法兑现 -> 此路不通
            waiting = forced                    # 规程 A 只许已发票组上车
        # 枚举 waiting 的整组子集（B 规程），再枚举目的层指派。
        options: List[Tuple[Tuple[str, int], ...]] = []
        n = len(waiting)
        for mask in range(1 << n):
            picks = [waiting[i] for i in range(n) if mask & (1 << i)]
            total = sum(self.resv[rid].size for rid in picks)
            if total > min_free:
                continue
            assignments: List[List[Tuple[str, int]]] = [[]]
            here = self._vi(f)
            for rid in picks:
                assignments = [acc + [(rid, d)]
                               for acc in assignments
                               for d in self.resv[rid].destinations
                               if self._vi(d) > here]
            for acc in assignments:
                options.append(tuple(sorted(acc)))
        # 去重并确定排序：先按上车人数（少->多，搜索更易早命中），再按名字。
        options = sorted(set(options),
                         key=lambda o: (sum(self.resv[r].size for r, _ in o),
                                        o))
        if () not in options:
            options.insert(0, ())
        return options

    # -- 递归与或搜索 ------------------------------------------------------

    def dfs(self, f: int, t: int,
            belief: List[Tuple[Tuple[int, int], ...]],
            named: Tuple[tuple, ...],
            pending: Tuple[str, ...],
            delivered: Tuple[str, ...]) -> Optional[dict]:
        belief = sorted(set(tuple(sorted(w)) for w in belief))
        named = tuple(sorted(named))
        pending = tuple(sorted(pending))
        delivered = tuple(sorted(delivered))
        key = (f, t, tuple(belief), named, pending, delivered)
        if key in self.memo:
            self.stats["memo_hits"] += 1
            return self.memo[key]
        self.stats["nodes"] += 1

        live, just_missed = self._expire(f, pending)
        if self.protocol == TICKET and any(rid in self.tickets
                                           for rid in just_missed):
            self.memo[key] = INFEASIBLE
            return INFEASIBLE
        # 当前层候梯的预约：本次开门若不选它上车，窗口即作废。
        # 规程 A 只认票，无票预约直接放弃；规程 B 由本节点的上车枚举决定
        # 是否招呼，因此这里只把“确定不会招呼”的无票/无空位预约剔除。
        if self.protocol == TICKET:
            drop_here = tuple(sorted(
                rid for rid in live
                if self.resv[rid].origin == f and rid not in self.tickets))
            live = tuple(x for x in live if x not in drop_here)
        else:
            live = tuple(sorted(set(live) - set(just_missed)))
        pending = live

        # 截止期校验：从当前开门拍 t 直达目的层 d，开门拍点为 t + 访问序距离
        # （每移动一层耗一拍，到达即开门；方向由访问序吸收）。
        here = self._vi(f)
        for _, _, d, deadline in named:
            if deadline is not None and t + (self._vi(d) - here) > deadline:
                self.memo[key] = INFEASIBLE
                return INFEASIBLE

        best: Optional[dict] = None
        for board in self._board_options(f, t, belief, named, pending):
            opened = self._open_door(f, t, belief, named, pending,
                                     delivered, board)
            if opened is None:
                continue
            hail_cost = 1 if opened["hail"] else 0
            # 本次开门后仍未上车、且候梯层就是当前层的预约，窗口已用尽。
            boarded_ids = {rid for rid, _ in board}
            new_pending = tuple(sorted(
                rid for rid in opened["pending"]
                if not (self.resv[rid].origin == f
                        and rid not in boarded_ids)))
            cont = self._continue(f, t + hail_cost, opened,
                                  new_pending, board, hail_cost)
            if cont is None:
                continue
            cand = {"floor": f, "beat": t,
                    "board": [{"id": rid, "dest": d} for rid, d in board],
                    "hail": bool(hail_cost), **cont}
            if best is None or cand["score"] > best["score"]:
                best = cand
        self.memo[key] = best
        return best

    def _continue(self, f: int, t_after: int, opened: dict,
                  new_pending: Tuple[str, ...],
                  board: Tuple[Tuple[str, int], ...], hail_cost: int
                  ) -> Optional[dict]:
        """开门之后：要么收班，要么选下一站；所有读数分支必须同时可行。

        每个读数分支自带“下客+上车后”的具名构型 ``named_full``，收班判断、
        最近下客点、下层递归都用分支自己的构型，绝不跨分支混用。
        """

        branch_material = opened["branches"]

        def all_clear(b: dict) -> bool:
            # 注意 worlds 是“世界列表”，空世界本身也是一个元素，必须按长度判。
            return (len(b["worlds"]) == 0 or all(len(w) == 0
                                                  for w in b["worlds"])) \
                and len(b["named_full"]) == 0 and len(new_pending) == 0

        if all(all_clear(b) for b in branch_material.values()):
            return self._branch_policy(branch_material, closed=True,
                                       next_node=None)
        all_worlds = [w for b in branch_material.values()
                      for w in b["worlds"]]
        all_named = tuple(sorted({x for b in branch_material.values()
                                  for x in b["named_full"]}))
        min_dest = self._min_dest(all_worlds, all_named)
        candidates = self._next_stops(f, t_after, new_pending, min_dest)
        here = self._vi(f)
        for nf in candidates:
            move = self._vi(nf) - here          # 方向无关的层数差
            t_next = t_after + move + 1
            if t_next > self.dsp.max_beats:
                continue
            child_node = self._branch_dfs(branch_material, nf, t_next,
                                          new_pending)
            if child_node is not None:
                return self._branch_policy(branch_material, closed=False,
                                           next_node=child_node)
        return None

    def _next_stops(self, f: int, t_after: int,
                    pending: Tuple[str, ...],
                    min_dest: Optional[int]) -> List[int]:
        """候选下一站：必须有人要下或有预约候梯，且不得越过最近下客点。

        一切“前后”比较都走运动方向访问序索引，因此上行、下行同构。
        越过最近下客点意味着有人到层下不了厢，硬失败；无在途者时没有该
        约束，只奔预约候梯层。
        """

        here = self._vi(f)
        origins = {self.resv[rid].origin for rid in pending
                   if self._vi(self.resv[rid].origin) > here}
        useful: List[int] = []
        for x in self.scn.visit_order:
            xi = self._vi(x)
            if xi <= here:
                continue
            if min_dest is not None and xi > self._vi(min_dest):
                continue
            if x in origins or x == min_dest:
                useful.append(x)
        return useful

    def _branch_dfs(self, branch_material: dict, nf: int, t_next: int,
                    pending: Tuple[str, ...]) -> Optional[object]:
        """同一口令前进到 nf：对每个读数分支递归，任一不通则整体不通。"""

        sub_nodes = {}
        for key, b in sorted(branch_material.items()):
            sub = self.dfs(nf, t_next, b["worlds"], b["named_full"],
                           pending, b["delivered"])
            if sub is None:
                return None
            sub_nodes[key] = sub
        # 不同分支的子策略可以不同（基于各自读到的读数），但节点内的口令
        # 仍只依赖读数，不依赖匿名身份。
        return ("branch", sub_nodes)

    def _branch_policy(self, branch_material: dict, closed: bool,
                       next_node) -> Optional[dict]:
        if closed:
            # 收班时所有分支都已清空；保证分数取所有读数分支中的最小送达数，
            # 各世界的实际送达由 replay 逐世界登记。
            counts = [len(b["delivered"]) for b in branch_material.values()]
            return {"branches": {self._key(k): {"closed": True,
                                                  "delivered": b["delivered"]}
                                  for k, b in branch_material.items()},
                    "score": min(counts)}
        _, sub_nodes = next_node
        return {"branches": {self._key(k): node
                              for k, node in sub_nodes.items()},
                "score": min(node["score"] for node in sub_nodes.values())}

    @staticmethod
    def _key(key: Tuple[int, int]) -> str:
        return f"alight={key[0]};load={key[1]}"

    # -- 规程 A：从大到小枚举票组 ------------------------------------------

    def plan_ticket(self) -> Optional[dict]:
        rids = sorted(self.resv)
        for size in range(len(rids), -1, -1):
            for combo in _combinations(rids, size):
                # 票的窗口/候梯层基本可达性快速过滤。
                if not self._ticket_basics(combo):
                    continue
                self.tickets = frozenset(combo)
                self.memo = {}
                root = self._root()
                if root is None:
                    continue
                # 每张票都是硬承诺：必须在 *每个* 交班世界里都完成送达，
                # 否则该组合不能承诺，换下一个更小的组合。
                if set(combo) <= self._guaranteed_delivered(root):
                    root["protocol"] = TICKET
                    root["tickets"] = list(combo)
                    return root
        return None

    def _guaranteed_delivered(self, root: dict) -> FrozenSet[str]:
        """沿策略树收集在所有读数分支叶子上都送达的预约。"""

        leaves: List[set] = []

        def walk(node: dict) -> None:
            if node.get("closed"):
                leaves.append(set(node["delivered"]))
                return
            for child in node["branches"].values():
                walk(child)

        walk(root)
        if not leaves:
            return frozenset()
        guaranteed = set(leaves[0])
        for leaf in leaves[1:]:
            guaranteed &= leaf
        return frozenset(guaranteed)

    def _ticket_basics(self, combo: Sequence[str]) -> bool:
        h = self.dsp.handover_floor
        for rid in combo:
            r = self.resv[rid]
            if r.origin < h:
                return False
        return True

    def plan_hail(self) -> Optional[dict]:
        self.protocol = HAIL
        self.tickets = frozenset()
        self.memo = {}
        root = self._root()
        if root is not None:
            root["protocol"] = HAIL
            root["tickets"] = []
        return root

    def _root(self) -> Optional[dict]:
        d = self.dsp
        named0 = tuple(sorted(
            (rid, size, dest, deadline)
            for rid, size, dest, deadline in d.pre_aboard))
        pending0 = tuple(sorted(self.resv))
        return self.dfs(d.handover_floor, 0, self.worlds, named0,
                        pending0, ())


def _combinations(items: Sequence[str], size: int
                  ) -> List[Tuple[str, ...]]:
    if size == 0:
        return [()]
    out = []
    for i in range(len(items) - size + 1):
        out += [(items[i],) + rest
                for rest in _combinations(items[i + 1:], size - 1)]
    return out


def plan_handover(scn: Scenario, dsp: Dispatch,
                  histories: Sequence[History], protocol: str) -> dict:
    """完整接替规划：历史 -> 匿名世界 -> 信念态策略。"""

    _require(protocol in (TICKET, HAIL), f"未知规程 {protocol}")
    _require(dsp.handover_floor in scn.floors,
             f"交班楼层 {dsp.handover_floor} 不在场景楼层表内")
    for r in dsp.reservations:
        _require(r.origin in scn.floors
                 and all(d in scn.floors for d in r.destinations),
                 f"预约 {r.rid} 引用了场景外楼层")
    for _, _, dest, _ in dsp.pre_aboard:
        _require(dest in scn.floors,
                 f"pre_aboard 目的层 {dest} 不在场景楼层表内")
    worlds = handover_worlds(scn, histories, dsp.handover_floor,
                             dsp.observed_alight)
    planner = _Planner(scn, dsp, worlds, protocol, frozenset())
    if protocol == TICKET:
        plan = planner.plan_ticket()
    else:
        plan = planner.plan_hail()
    if plan is None:
        raise RuntimeError("不存在可保证兑现的接替方案（连空票/空招呼都失败，"
                           "通常说明在途者无法在拍点预算内正常下厢）")
    plan["scenario"] = scn.name
    plan["handover_floor"] = dsp.handover_floor
    plan["observed_alight"] = dsp.observed_alight
    plan["max_beats"] = dsp.max_beats
    plan["worlds"] = [list(w) for w in worlds]
    plan["planner_stats"] = planner.stats
    return plan


# ---------------------------------------------------------------------------
# 按策略回放：司机只读读数，登记首个失约窗口
# ---------------------------------------------------------------------------

def replay_plan(scn: Scenario, dsp: Dispatch, plan: dict,
                world: Optional[Sequence[Tuple[int, int]]] = None,
                world_index: Optional[int] = None) -> dict:
    """沿一份班单模拟一个具体匿名世界，输出司机视角的完整读数流水。"""

    worlds = [tuple(tuple(x) for x in w) for w in plan["worlds"]]
    if world is not None:
        chosen = tuple(sorted(tuple(x) for x in world))
    elif world_index is not None:
        chosen = worlds[world_index]
    else:
        chosen = worlds[0]
    if chosen not in worlds:
        raise ValueError("给定世界不在交班信念集合内")
    resv = {r.rid: r for r in dsp.reservations}
    anon = list(chosen)                                   # (dest, size)
    named = {rid: (size, dest) for rid, size, dest, _ in dsp.pre_aboard}
    delivered: List[str] = []
    log: List[dict] = []
    node = plan
    f = dsp.handover_floor
    missed_window: Optional[str] = None

    while True:
        t = node["beat"]
        # 失约登记：候梯层已过却没上车的预约。
        for rid in sorted(resv):
            r = resv[rid]
            if r.origin <= f and rid not in named and rid not in delivered \
                    and not any(b["id"] == rid for b in node.get("board", [])):
                missed_window = (f"missed:window:{rid}:floor{r.origin}"
                                 f":beat[{r.window[0]},{r.window[1]}]")
                break
        # 下客（匿名在途者先下；具名客下厢可被司机记名）。
        alight_anon = sum(s for d, s in anon if d == f)
        anon = [x for x in anon if x[0] != f]
        named_alight = sorted(rid for rid, (_, d) in named.items() if d == f)
        for rid in named_alight:
            delivered.append(rid)
            del named[rid]
        # 上车（节点口令对所有世界相同）。
        boarded = node.get("board", [])
        for b in boarded:
            rid = b["id"]
            r = resv[rid]
            if not (r.window[0] <= t <= r.window[1]):
                missed_window = missed_window or (
                    f"missed:window:{rid}:beat{t} outside "
                    f"[{r.window[0]},{r.window[1]}]")
            named[rid] = (r.size, b["dest"])
        load = sum(s for _, s in anon) + sum(s for s, _ in named.values())
        if load > scn.capacity:
            raise RuntimeError(f"回放超载：楼层 {f} 人数 {load} > "
                               f"容量 {scn.capacity}")
        reading = {"floor": f, "beat": t, "alight_anonymous": alight_anon,
                   "alight_named": named_alight,
                   "boarded": [b["id"] for b in boarded],
                   "hail_extra_beat": node.get("hail", False),
                   "weighed_load": load}
        log.append(reading)
        key = f"alight={alight_anon};load={load}"
        branches = node["branches"]
        if key not in branches:
            raise RuntimeError(f"策略没有为读数 {key} 准备分支（偷看身份？）")
        child = branches[key]
        if child.get("closed"):
            return {"status": "closed", "world": list(chosen),
                    "delivered": sorted(delivered),
                    "missed_window": missed_window, "log": log}
        node = child
        f = node["floor"]


def replay_all_worlds(scn: Scenario, dsp: Dispatch, plan: dict) -> List[dict]:
    return [replay_plan(scn, dsp, plan, world_index=i)
            for i in range(len(plan["worlds"]))]


# ---------------------------------------------------------------------------
# 与加权采样数据集对接：只认真正入样的计数行
# ---------------------------------------------------------------------------

def rows_from_dataset(scn: Scenario, manifest: dict,
                      records: Sequence[dict]) -> Tuple[CountRow, ...]:
    """从数据集产物取回入样计数行，并核对其正文就是场景中的计数行。"""

    by_id = {r.row_id: r for r in scn.rows}
    picked: List[CountRow] = []
    for rec in records:
        rid = rec["row_id"]
        if rid not in by_id:
            raise ValueError(f"入样行 {rid} 不属于场景 {scn.name}")
        body = json.loads(rec["body"])
        row = by_id[rid]
        _require(body["interval"] == list(row.interval)
                 and body["type"] == row.kind
                 and int(body["value"]) == row.value,
                 f"入样正文与场景计数行不一致: {rid}")
        picked.append(row)
    if not picked:
        raise ValueError("数据集没有入样任何计数行，无法还原")
    return tuple(sorted(picked, key=lambda r: r.row_id))


def scenario_ingest_ops(scn: Scenario, group: str = "elevator-counts"
                        ) -> List[dict]:
    """把计数行写成加权源的 add 批次（正文为真实 JSON 计数）。"""

    ops = []
    for row in scn.rows:
        body = {
            "trip_id": row.trip_id,
            "interval": list(row.interval),
            "type": row.kind,
            "value": row.value,
        }
        if row.named_floor is not None:
            body.update({"named_floor": row.named_floor,
                         "named_in": list(row.named_in),
                         "named_out": list(row.named_out)})
        ops.append({"op": "add", "row_id": row.row_id, "group": group,
                    "weight": row.weight,
                    "body": json.dumps(body, ensure_ascii=False,
                                       sort_keys=True),
                    "shard": row.shard})
    return ops


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrsource.elevator",
        description="桌面电梯演练：计数行还原历史 + 中途交班接替规划。")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reconstruct", help="从场景（或入样数据集）还原历史")
    p.add_argument("--scenario", required=True)
    p.add_argument("--dataset", help="给定数据集目录时只使用入样计数行")
    p.add_argument("--out")

    p = sub.add_parser("certain", help="问某站关门时确定/可能在厢的组")
    p.add_argument("--scenario", required=True)
    p.add_argument("--floor", type=int, required=True)
    p.add_argument("--dataset")

    p = sub.add_parser("handover", help="按调度单实办一次接替规划")
    p.add_argument("--scenario", required=True)
    p.add_argument("--dispatch", required=True)
    p.add_argument("--protocol", choices=[TICKET, HAIL], required=True)
    p.add_argument("--dataset", help="只基于入样计数行还原的历史交班")
    p.add_argument("--out")

    p = sub.add_parser("replay", help="沿班单在每个匿名世界回放并登记失约")
    p.add_argument("--scenario", required=True)
    p.add_argument("--dispatch", required=True)
    p.add_argument("--plan", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "reconstruct":
        scn = load_scenario(args.scenario)
        rows = None
        if args.dataset:
            from .dataset import load_dataset
            manifest, records = load_dataset(args.dataset)
            rows = rows_from_dataset(scn, manifest, records)
        histories = enumerate_histories(scn, rows)
        out = {"scenario": scn.name, "history_count": len(histories),
               "used_rows": [r.row_id for r in (rows or scn.rows)],
               "histories": [history_to_dict(h) for h in histories]}
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=2,
                          sort_keys=True)
        _print(out)
        return 0

    if args.cmd == "certain":
        scn = load_scenario(args.scenario)
        rows = None
        if args.dataset:
            from .dataset import load_dataset
            manifest, records = load_dataset(args.dataset)
            rows = rows_from_dataset(scn, manifest, records)
        histories = enumerate_histories(scn, rows)
        _print({"scenario": scn.name, "floor": args.floor,
                "history_count": len(histories),
                "certain_onboard": certain_onboard(histories, args.floor),
                "possible_onboard": possible_onboard(histories, args.floor)})
        return 0

    if args.cmd == "handover":
        scn = load_scenario(args.scenario)
        dsp = load_dispatch(args.dispatch)
        rows = None
        if args.dataset:
            from .dataset import load_dataset
            manifest, records = load_dataset(args.dataset)
            rows = rows_from_dataset(scn, manifest, records)
        histories = enumerate_histories(scn, rows)
        plan = plan_handover(scn, dsp, histories, args.protocol)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, ensure_ascii=False, indent=2,
                          sort_keys=True)
        _print(plan)
        return 0

    if args.cmd == "replay":
        scn = load_scenario(args.scenario)
        dsp = load_dispatch(args.dispatch)
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        reports = replay_all_worlds(scn, dsp, plan)
        _print({"plan_protocol": plan["protocol"],
                "world_reports": reports})
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
