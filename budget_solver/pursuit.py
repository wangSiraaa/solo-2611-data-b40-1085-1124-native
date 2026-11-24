"""训练馆团体追逐赛执裁模型。

把过线单、连拍位差和有限格点校准档编译成既有线性模型：

* 永久身份是 ``model_id + cell_id``；迟交材料只产生模型修订，旧快照/报告保留；
* 位置、速度、位差都是有理线性格，相邻测时点间速度恒定，速度格另作非负校验；
* 线性解算只负责“确定 / 参数化 / 矛盾”，有限格点校准档再把参数化结果筛成
  一份份可核证的完整车迹；
* 队位取当时有效车手中里程第三高者，第三人换位由分段线性车迹的精确交点给出；
* 本馆只开放实时执裁：所有校准车迹都已连续追足约定时长才允许鸣铃。
"""

import os
from fractions import Fraction

from .compiler import var_name
from .errors import BudgetError, ValidationError
from .rationals import parse_rational, qstr
from .util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic
from .service import BudgetService

REAL_TIME = "real_time"
STAGE_ORDER = {"initial": 0, "late": 1}


def F(value, field):
    return parse_rational(value, field=field)


def _require(obj, keys, context):
    if not isinstance(obj, dict):
        raise ValidationError(f"{context} 必须是对象")
    missing = [key for key in keys if key not in obj]
    if missing:
        raise ValidationError(f"{context} 缺少字段 {missing}")


def _rider_team(event):
    mapping = {}
    for team, riders in event["riders"].items():
        for rider in riders:
            if rider in mapping:
                raise ValidationError(f"车手 {rider} 不能同时属于两队")
            mapping[rider] = team
    return mapping


def pos_cell(rider, index):
    return f"p_{rider}_t{index}"


def vel_cell(rider, index):
    return f"v_{rider}_i{index}"


def gap_cell(evidence_id):
    return f"gap_{evidence_id}"


def constraint_cell(evidence_id):
    return f"e_{evidence_id}"



class PursuitModelBuilder:
    """把当前预算快照可见的证据编译成线性模型负载。"""

    def __init__(self, event):
        self.event = event
        self.times = [F(t, "times") for t in event["times"]]
        if any(self.times[i] >= self.times[i + 1] for i in range(len(self.times) - 1)):
            raise ValidationError("测时点必须严格递增")
        if len(self.times) > 6:
            raise ValidationError("每场最多六个测时点")
        self.actual_stop = F(event["actual_stop"], "actual_stop")
        self.measurement_times = [t for t in self.times if t <= self.actual_stop]
        self.rider_team = _rider_team(event)

    def _time_index(self, time_value, evidence_id):
        for index, value in enumerate(self.times):
            if value == time_value:
                return index
        raise ValidationError(f"证据 {evidence_id} 的时刻不在准许测时点内")

    def available_evidence(self, stage):
        """按提交阶段和实际收车时刻过滤材料；收车后的材料永不进入模型。"""
        stage_no = STAGE_ORDER[stage]
        superseded = set()
        available = []
        for evidence in self.event["evidence"]:
            if STAGE_ORDER[evidence["submitted"]] > stage_no:
                continue
            recorded = F(evidence["recorded_at"], f"evidence {evidence['evidence_id']}.recorded_at")
            if recorded > self.actual_stop:
                continue
            if evidence.get("supersedes"):
                superseded.add(evidence["supersedes"])
            available.append(evidence)
        return [e for e in available if e["evidence_id"] not in superseded]

    def available_grid(self, stage):
        """按提交阶段过滤有限格点校准档。

        ``recorded_at`` 表示该档所校准的比赛时刻（必须在测时点上），不是材料
        交到计时岗的时刻；迟交因此不会把一份赛中校准档错误排除。
        """
        stage_no = STAGE_ORDER[stage]
        return [
            point
            for point in self.event.get("grid", [])
            if STAGE_ORDER[point["submitted"]] <= stage_no
        ]

    def build(self, stage):
        if self.event.get("mode", REAL_TIME) != REAL_TIME:
            raise ValidationError("本训练馆只实现 real_time 执裁模式")
        evidence_list = self.available_evidence(stage)
        ids = [evidence["evidence_id"] for evidence in evidence_list]
        if len(ids) != len(set(ids)):
            raise ValidationError("证据 ID 必须唯一")
        for evidence in evidence_list:
            if evidence["type"] == "photo":
                raise ValidationError("实时执裁模式不接收额外双队同步底片")
            if evidence["type"] not in ("line", "gap"):
                raise ValidationError(f"未知证据类型 {evidence['type']}")

        cells = {}
        n = len(self.measurement_times)
        all_riders = [rider for riders in self.event["riders"].values() for rider in riders]

        # 位置 x0 与各段速度先作为自由格（自指形成 0=0）；证据方程再把它们钉住
        # 或保留为参数。位置递推是 x_{i+1}=x_i+(t_{i+1}-t_i)v_i。
        for rider in all_riders:
            cells[pos_cell(rider, 0)] = {
                "kind": "formula",
                "constant": "0",
                "terms": [{"coef": "1", "cell_id": pos_cell(rider, 0)}],
            }
            for i in range(n - 1):
                cells[vel_cell(rider, i)] = {
                    "kind": "formula",
                    "constant": "0",
                    "terms": [{"coef": "1", "cell_id": vel_cell(rider, i)}],
                }
            for i in range(n - 1):
                dt = qstr(self.measurement_times[i + 1] - self.measurement_times[i])
                cells[pos_cell(rider, i + 1)] = {
                    "kind": "formula",
                    "constant": "0",
                    "terms": [
                        {"coef": "1", "cell_id": pos_cell(rider, i)},
                        {"coef": dt, "cell_id": vel_cell(rider, i)},
                    ],
                }

        for evidence in evidence_list:
            eid = evidence["evidence_id"]
            recorded = F(evidence["recorded_at"], f"evidence {eid}.recorded_at")
            index = self._time_index(recorded, eid)
            if index >= n:
                raise ValidationError(f"证据 {eid} 产生于实际收车后，不能入模")
            if evidence["type"] == "line":
                rider = evidence["rider"]
                if rider not in self.rider_team:
                    raise ValidationError(f"证据 {eid} 引用未知车手 {rider}")
                distance = F(evidence["distance"], f"evidence {eid}.distance")
                # 观测残差 = 真实位置 - 过线里程。同一真实位置的互斥过线单会
                # 在判定层按证据身份分组识别；正常校准车迹再逐单据代回。
                observed = f"obs_{eid}_{pos_cell(rider, index)}"
                cells[constraint_cell(eid)] = {
                    "kind": "input",
                    "value": qstr(distance),
                }
                cells[observed] = {
                    "kind": "formula",
                    "constant": "0",
                    "terms": [
                        {"coef": "1", "cell_id": pos_cell(rider, index)},
                        {"coef": "-1", "cell_id": constraint_cell(eid)},
                    ],
                }
            else:
                ra, rb = evidence["rider_a"], evidence["rider_b"]
                if ra not in self.rider_team or rb not in self.rider_team:
                    raise ValidationError(f"证据 {eid} 引用未知车手")
                difference = F(evidence["difference"], f"evidence {eid}.difference")
                # 位差格直接钉为证据值，同时保留绝对位置的参数自由度；
                # 位置不定而位差确定会在解算报告中分成两类输出。
                cells[gap_cell(eid)] = {
                    "kind": "input",
                    "value": qstr(difference),
                }
                cells[constraint_cell(eid)] = {
                    "kind": "formula",
                    "constant": "0",
                    "terms": [
                        {"coef": "1", "cell_id": gap_cell(eid)},
                        {"coef": "-1", "cell_id": pos_cell(ra, index)},
                        {"coef": "1", "cell_id": pos_cell(rb, index)},
                    ],
                }

        outputs = [cell_id for cell_id in sorted(cells)]
        return {
            "model_id": self.event["model_id"],
            "name": f"追逐车迹：{self.event['event_id']}",
            "description": "实时执裁证据快照编译出的精确线性车迹模型",
            "cells": cells,
            "outputs": outputs,
        }


class PursuitService:
    """事件、预算快照、校准车迹和实时判定的可重开门面。"""

    def __init__(self, data_root):
        self.data_root = os.path.abspath(data_root)
        self.budget = BudgetService(self.data_root)
        self.pursuit_root = os.path.join(self.data_root, "pursuit")
        self.event_dir = os.path.join(self.pursuit_root, "events")
        self.judgment_dir = os.path.join(self.pursuit_root, "judgments")
        ensure_dir(self.event_dir)
        ensure_dir(self.judgment_dir)
        self._seq_path = os.path.join(self.pursuit_root, "seq.json")
        self._seq = read_json(self._seq_path) if os.path.exists(self._seq_path) else {"next": 1}

    # ------------------------------------------------------------ 事件管理
    def _event_path(self, event_id):
        return os.path.join(self.event_dir, f"{event_id}.json")

    def intake_event(self, payload):
        _require(payload, [
            "event_id", "model_id", "teams", "riders", "times",
            "actual_stop", "required_hold", "evidence",
        ], "追逐事件")
        if payload.get("mode", REAL_TIME) != REAL_TIME:
            raise ValidationError("本训练馆只实现 real_time 执裁模式")
        if set(payload["teams"]) != set(payload["riders"]):
            raise ValidationError("两队都必须给出车手清单")
        if len(payload["teams"]) != 2:
            raise ValidationError("团体追逐恰好两队")
        rider_team = _rider_team(payload)
        times = [F(t, "times") for t in payload["times"]]
        if len(times) > 6 or any(times[i] >= times[i + 1] for i in range(len(times) - 1)):
            raise ValidationError("测时点必须严格递增且不超过六个")
        F(payload["actual_stop"], "actual_stop")
        F(payload["required_hold"], "required_hold")
        seen = set()
        for evidence in payload["evidence"]:
            _require(evidence, ["evidence_id", "type", "recorded_at", "submitted"], "证据")
            if evidence["evidence_id"] in seen:
                raise ValidationError(f"证据 ID 重复：{evidence['evidence_id']}")
            seen.add(evidence["evidence_id"])
            if evidence["submitted"] not in STAGE_ORDER:
                raise ValidationError("证据提交阶段只能是 initial 或 late")
            recorded = F(evidence["recorded_at"], "evidence.recorded_at")
            if recorded not in set(times) or recorded > F(payload["actual_stop"], "actual_stop"):
                raise ValidationError("证据必须对应实际收车前的准许测时点")
            if evidence["type"] == "line":
                _require(evidence, ["rider", "distance"], "过线证据")
                if evidence["rider"] not in rider_team:
                    raise ValidationError("过线证据引用未知车手")
                F(evidence["distance"], "line.distance")
            elif evidence["type"] == "gap":
                _require(evidence, ["rider_a", "rider_b", "difference"], "位差证据")
                F(evidence["difference"], "gap.difference")
            elif evidence["type"] == "photo":
                raise ValidationError("实时执裁模式不接收额外双队同步底片")
            else:
                raise ValidationError(f"未知证据类型 {evidence['type']}")
        measurement_set = set(times)
        for point in payload.get("grid", []):
            _require(point, ["grid_id", "recorded_at", "submitted", "assignments"], "校准格点")
            if point["submitted"] not in STAGE_ORDER:
                raise ValidationError("校准档提交阶段非法")
            grid_time = F(point["recorded_at"], "grid.recorded_at")
            if grid_time not in measurement_set or grid_time > F(payload["actual_stop"], "actual_stop"):
                raise ValidationError("校准档必须对应实际收车前的准许测时点")
            if not isinstance(point["assignments"], dict):
                raise ValidationError("校准格点 assignments 必须是对象")
            for key, value in point["assignments"].items():
                if not isinstance(key, str):
                    raise ValidationError("校准格点键必须是 cell_id")
                F(value, f"grid {point['grid_id']}.{key}")
        path = self._event_path(payload["event_id"])
        if os.path.exists(path):
            raise ValidationError(f"事件 {payload['event_id']} 已存在；请用追加迟交材料接口")
        stored = {**payload, "mode": REAL_TIME, "intaken_at": utc_now_iso()}
        write_json_atomic(path, stored)
        return stored

    def get_event(self, event_id):
        path = self._event_path(event_id)
        if not os.path.exists(path):
            raise BudgetError(f"事件 {event_id} 不存在")
        return read_json(path)

    def append_late_material(self, event_id, evidence=None, grid_points=None):
        """追加赛前/赛中已产生但迟交的材料；事件旧文件留修订历史。"""
        event = self.get_event(event_id)
        evidence = evidence or []
        grid_points = grid_points or []
        measurement_set = {F(t, "times") for t in event["times"]}
        existing_e = {e["evidence_id"] for e in event["evidence"]}
        existing_g = {g["grid_id"] for g in event.get("grid", [])}
        for item in evidence:
            _require(item, ["evidence_id", "type", "recorded_at", "submitted"], "迟交证据")
            if item["evidence_id"] in existing_e and not item.get("supersedes"):
                raise ValidationError("新证据 ID 与既有材料冲突；校正旧证据必须给 supersedes")
            if item.get("supersedes") and item["supersedes"] not in existing_e:
                raise ValidationError("supersedes 指向不存在的旧证据")
            if item["submitted"] != "late":
                raise ValidationError("追加接口只能接收 submitted=late 的材料")
            recorded = F(item["recorded_at"], "late_evidence.recorded_at")
            if recorded not in measurement_set or recorded > F(event["actual_stop"], "actual_stop"):
                raise ValidationError("迟交证据必须对应实际收车前的准许测时点")
        measurement_set = {F(t, "times") for t in event["times"]}
        for item in grid_points:
            if item["grid_id"] in existing_g or item["submitted"] != "late":
                raise ValidationError("迟交校准档非法")
            grid_time = F(item["recorded_at"], "late_grid.recorded_at")
            if grid_time not in measurement_set or grid_time > F(event["actual_stop"], "actual_stop"):
                raise ValidationError("迟交校准档必须对应实际收车前的准许测时点")
        event["evidence"].extend(evidence)
        event.setdefault("grid", []).extend(grid_points)
        event["late_appended_at"] = utc_now_iso()
        write_json_atomic(self._event_path(event_id), event)
        return event

    def _evidence_contradiction(self, builder, stage):
        """同一车手、同一准许测时点出现两个不同过线里程即互斥。"""
        groups = {}
        for evidence in builder.available_evidence(stage):
            if evidence["type"] != "line":
                continue
            key = (evidence["rider"], evidence["recorded_at"])
            groups.setdefault(key, set()).add(evidence["distance"])
        return any(len(distances) > 1 for distances in groups.values())

    # ------------------------------------------------------------ 解算与候选
    def _compile_solve(self, event, stage):
        builder = PursuitModelBuilder(event)
        payload = builder.build(stage)
        self.budget.put_model(payload)  # 同 model_id 的新证据形成修订，永久格名不变
        snap = self.budget.snapshot(payload["model_id"], label=f"{event['event_id']}:{stage}")
        artifact, _ = self.budget.compile(snap["snap_id"], label=f"{event['event_id']}:{stage}")
        report = self.budget.solve(artifact)
        return builder, snap, artifact, report

    def _report_value(self, report, variable):
        return report["outputs"].get(variable)

    def _candidate_traces(self, event, builder, report, stage):
        model_id = event["model_id"]
        times = builder.measurement_times
        riders = [rider for team in event["riders"].values() for rider in team]
        free_cells = [pos_cell(rider, 0) for rider in riders]
        for rider in riders:
            free_cells.extend(vel_cell(rider, i) for i in range(len(times) - 1))

        inconsistent = any(
            info["status"] == "inconsistent" for info in report["outputs"].values()
        )
        if inconsistent:
            return [], "inconsistent"

        grid = builder.available_grid(stage)
        candidates = []
        for point in grid:
            assignment = {}
            complete = True
            for cell in free_cells:
                variable = var_name(model_id, cell)
                info = self._report_value(report, variable)
                if info is None:
                    complete = False
                    break
                if cell in point["assignments"]:
                    assignment[cell] = F(point["assignments"][cell], f"grid.{cell}")
                    if info["status"] == "determined" and assignment[cell] != F(info["value"], "report"):
                        complete = False
                        break
                elif info["status"] == "determined":
                    assignment[cell] = F(info["value"], "report")
                else:
                    complete = False
                    break
            if not complete:
                continue

            positions = {}
            velocities = {}
            valid = True
            for rider in riders:
                x0 = assignment[pos_cell(rider, 0)]
                positions[(rider, 0)] = x0
                for i in range(len(times) - 1):
                    velocity = assignment[vel_cell(rider, i)]
                    if velocity < 0:  # 不许倒车
                        valid = False
                    velocities[(rider, i)] = velocity
                    positions[(rider, i + 1)] = (
                        positions[(rider, i)]
                        + (times[i + 1] - times[i]) * velocity
                    )
            if not valid:
                continue
            # 独立代回当前快照可见的全部线性证据。
            for evidence in builder.available_evidence(stage):
                t = F(evidence["recorded_at"], "evidence.recorded_at")
                index = times.index(t)
                if evidence["type"] == "line":
                    expected = F(evidence["distance"], "evidence.distance")
                    if positions[(evidence["rider"], index)] != expected:
                        valid = False
                        break
                else:
                    expected = F(evidence["difference"], "evidence.difference")
                    actual = (
                        positions[(evidence["rider_a"], index)]
                        - positions[(evidence["rider_b"], index)]
                    )
                    if actual != expected:
                        valid = False
                        break
            if valid:
                candidates.append({
                    "grid_id": point["grid_id"],
                    "positions": {rider: [positions[(rider, i)] for i in range(len(times))]
                                  for rider in riders},
                    "velocities": {rider: [velocities[(rider, i)] for i in range(len(times) - 1)]
                                   for rider in riders},
                })
        return candidates, ("calibrated" if candidates else "no_complete_trace")

    # ------------------------------------------------------------ 队位/追及
    def _active_riders(self, event, team, at_time):
        withdrawals = {
            item["rider"]: F(item["after"], "withdrawal.after")
            for item in event.get("withdrawals", [])
            if self._rider_team_of(event, item["rider"]) == team
        }
        return [rider for rider in event["riders"][team]
                if rider not in withdrawals or at_time <= withdrawals[rider]]

    @staticmethod
    def _rider_team_of(event, rider):
        return _rider_team(event).get(rider)

    @staticmethod
    def _affine(times, values, rider, at_time):
        if at_time <= times[0]:
            i = 0
            left = times[0]
        elif at_time >= times[-1]:
            i = len(times) - 2
            left = times[i]
        else:
            i = max(j for j in range(len(times) - 1) if times[j] <= at_time)
            left = times[i]
        span = times[i + 1] - times[i]
        return values[i] + (values[i + 1] - values[i]) * (at_time - left) / span

    def _team_position_segments(self, event, trace, team, horizon):
        times = [t for t in [F(x, "times") for x in event["times"]] if t <= horizon]
        breakpoints = set(times)
        breakpoints.add(horizon)
        for item in event.get("withdrawals", []):
            at = F(item["after"], "withdrawal.after")
            if times[0] < at < horizon:
                breakpoints.add(at)

        for left, right in zip(times[:-1], times[1:]):
            active = [r for r in self._active_riders(event, team, right)]
            for i, ra in enumerate(active):
                for rb in active[i + 1:]:
                    va = self._affine(times, trace["positions"][ra], ra, left)
                    vb = self._affine(times, trace["positions"][rb], rb, left)
                    wa = self._affine(times, trace["positions"][ra], ra, right)
                    wb = self._affine(times, trace["positions"][rb], rb, right)
                    d0, d1 = va - vb, wa - wb
                    if d0 != d1 and d0 * d1 <= 0:
                        root = left + (-d0) * (right - left) / (d1 - d0)
                        if left < root < right:
                            breakpoints.add(root)
        points = sorted(breakpoints)
        segments = []
        for left, right in zip(points[:-1], points[1:]):
            if right > horizon:
                continue
            mid = (left + right) / 2
            active = self._active_riders(event, team, mid)
            if len(active) < 3:
                return {"status": "short_team", "team": team, "at": qstr(mid)}
            ranked = sorted(
                active,
                key=lambda rider: (
                    self._affine(times, trace["positions"][rider], rider, mid), rider
                ),
                reverse=True,
            )
            third = ranked[2]
            segments.append({
                "start": qstr(left),
                "end": qstr(right),
                "third_rider": third,
            })
        return {"status": "ok", "segments": segments}

    def _catch_intervals(self, event, trace, team_segments, horizon):
        times = [t for t in [F(x, "times") for x in event["times"]] if t <= horizon]
        chase, target = event["teams"][0], event["teams"][1]
        if event["chasing_team"] == event["teams"][1]:
            chase, target = target, chase
        breaks = {times[0], horizon}
        for segment_set in team_segments.values():
            for segment in segment_set["segments"]:
                breaks.add(F(segment["start"], "segment.start"))
                breaks.add(F(segment["end"], "segment.end"))
        points = sorted(breaks)
        elementary = []
        for left, right in zip(points[:-1], points[1:]):
            if right > horizon:
                continue
            mid = (left + right) / 2
            riders = {}
            for team in (chase, target):
                match = next(s for s in team_segments[team]["segments"]
                             if F(s["start"], "segment.start") <= mid <= F(s["end"], "segment.end"))
                riders[team] = match["third_rider"]
            c0 = self._affine(times, trace["positions"][riders[chase]], riders[chase], left)
            c1 = self._affine(times, trace["positions"][riders[chase]], riders[chase], right)
            p0 = self._affine(times, trace["positions"][riders[target]], riders[target], left)
            p1 = self._affine(times, trace["positions"][riders[target]], riders[target], right)
            g0, g1 = c0 - p0, c1 - p1
            root_points = [left]
            if g0 != g1:
                root = left + (-g0) * (right - left) / (g1 - g0)
                # 左端点就是零点时，正长度的“保持”从下一个基本区间开始；
                # 严格内部零点则切开正/负位差，避免相邻区间被错误合并。
                if root == left:
                    root_points = [left, right]
                elif left < root < right:
                    root_points.append(root)
            root_points.append(right)
            for a, b in zip(root_points[:-1], root_points[1:]):
                if b <= a:
                    continue
                ga = c0 + (c1 - c0) * (a - left) / (right - left) - (
                    p0 + (p1 - p0) * (a - left) / (right - left))
                gb = c0 + (c1 - c0) * (b - left) / (right - left) - (
                    p0 + (p1 - p0) * (b - left) / (right - left))
                # “达到或超过”允许位差为 0；正长度区间内取中点，避免只在端点
                # 相切时被误算为持续保持。
                gm = (ga + gb) / 2
                elementary.append((a, b, gm >= 0))

        intervals, current = [], None
        for left, right, valid in elementary:
            if valid:
                if current is None:
                    current = [left, right]
                elif current[1] == left:
                    current[1] = right
                else:
                    intervals.append(tuple(current))
                    current = [left, right]
            else:
                if current is not None:
                    intervals.append(tuple(current))
                    current = None
        if current is not None:
            intervals.append(tuple(current))
        return [(s, e) for s, e in intervals if e > s]

    def _evaluate_trace(self, event, trace, horizon):
        chase, target = event["teams"]
        team_segments = {}
        for team in event["teams"]:
            result = self._team_position_segments(event, trace, team, horizon)
            if result["status"] != "ok":
                return result
            team_segments[team] = result
        intervals = self._catch_intervals(event, trace, team_segments, horizon)
        hold = F(event["required_hold"], "required_hold")
        qualified = [(s, e) for s, e in intervals if e - s >= hold]
        max_hold = max((e - s for s, e in intervals), default=Fraction(0))
        earliest = min((s + hold for s, e in qualified), default=None)
        return {
            "status": "caught" if qualified else "not_caught",
            "grid_id": trace["grid_id"],
            "team_position_segments": {
                team: [
                    {
                        "start": segment["start"],
                        "end": segment["end"],
                        "third_rider": segment["third_rider"],
                    }
                    for segment in team_segments[team]["segments"]
                ]
                for team in event["teams"]
            },
            "nonnegative_gap_intervals": [
                {"start": qstr(s), "end": qstr(e), "length": qstr(e - s)}
                for s, e in intervals
            ],
            "qualified_intervals": [
                {"start": qstr(s), "end": qstr(e), "length": qstr(e - s)}
                for s, e in qualified
            ],
            "maximum_observed_hold": qstr(max_hold),
            "earliest_completion": qstr(earliest) if earliest is not None else None,
        }

    def _determined_gaps(self, report, event, builder, stage):
        out = []
        for evidence in builder.available_evidence(stage):
            if evidence["type"] != "gap":
                continue
            variable = var_name(event["model_id"], gap_cell(evidence["evidence_id"]))
            info = report["outputs"].get(variable)
            if info and info["status"] == "determined":
                out.append({
                    "evidence_id": evidence["evidence_id"],
                    "rider_a": evidence["rider_a"],
                    "rider_b": evidence["rider_b"],
                    "difference": info["value"],
                })
        return out

    def judge(self, event_id, stage):
        event = self.get_event(event_id)
        builder, snap, artifact, report = self._compile_solve(event, stage)
        horizon = min(
            F(event["actual_stop"], "actual_stop"),
            max(builder.measurement_times),
        )
        candidates, candidate_status = self._candidate_traces(event, builder, report, stage)
        solver_summary = {
            "determined": sorted(
                variable for variable, info in report["outputs"].items()
                if info["status"] == "determined"
            ),
            "parametric": sorted(
                variable for variable, info in report["outputs"].items()
                if info["status"] == "parametric"
            ),
            "inconsistent": sorted(
                variable for variable, info in report["outputs"].items()
                if info["status"] == "inconsistent"
            ),
        }
        base = {
            "event_id": event_id,
            "stage": stage,
            "mode": REAL_TIME,
            "actual_stop": event["actual_stop"],
            "evidence_horizon": qstr(horizon),
            "visible_evidence": [e["evidence_id"] for e in builder.available_evidence(stage)],
            "visible_grid": [g["grid_id"] for g in builder.available_grid(stage)],
            "snapshot_id": snap["snap_id"],
            "artifact_hash": artifact["artifact_hash"],
            "solve_id": report["solve_id"],
            "solver": solver_summary,
            "determined_gaps": self._determined_gaps(report, event, builder, stage),
        }
        if candidate_status == "inconsistent" or self._evidence_contradiction(builder, stage):
            verdict = {**base, "verdict": "contradiction",
                       "reason": "线性证据相互矛盾，不存在任何车迹"}
            return self._publish_judgment(verdict)
        if not candidates:
            verdict = {
                **base,
                "verdict": "pending",
                "reason": "位置仍参数化且当前有限格点校准档不足以给出完整车迹",
            }
            return self._publish_judgment(verdict)

        traces = [self._evaluate_trace(event, candidate, horizon) for candidate in candidates]
        if any(item["status"] == "short_team" for item in traces):
            short = next(item for item in traces if item["status"] == "short_team")
            verdict = {**base, "verdict": "short_team", "trace_reviews": traces, "detail": short}
            return self._publish_judgment(verdict)
        statuses = {item["status"] for item in traces}
        if statuses == {"caught"}:
            bell = max(F(item["earliest_completion"], "earliest_completion") for item in traces)
            aggregate = "determined_caught"
        elif statuses == {"not_caught"}:
            bell = None
            aggregate = "determined_not_caught"
        else:
            bell = None
            aggregate = "pending"
        verdict = {
            **base,
            "verdict": aggregate,
            "bell_at": qstr(bell) if bell is not None else None,
            "trace_count": len(traces),
            "trace_reviews": traces,
        }
        return self._publish_judgment(verdict)

    def _publish_judgment(self, verdict):
        seq = self._seq["next"]
        self._seq["next"] += 1
        judgment_id = f"judgment{seq}"
        payload = {
            "judgment_id": judgment_id,
            "created_at": utc_now_iso(),
            **verdict,
        }
        payload["judgment_hash"] = content_hash(
            {k: v for k, v in payload.items() if k != "judgment_hash"}
        )
        write_json_atomic(os.path.join(self.judgment_dir, f"{judgment_id}.json"), payload)
        write_json_atomic(self._seq_path, self._seq)
        return payload

    def get_judgment(self, judgment_id):
        path = os.path.join(self.judgment_dir, f"{judgment_id}.json")
        if not os.path.exists(path):
            raise BudgetError(f"判定 {judgment_id} 不存在")
        return read_json(path)
