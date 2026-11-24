"""演示台：逐拍推齿位、换挡窗锁合、滑座独占与换挡动作表。

模型约定（整齿 / 整数刻）：

* 演示台 ``ticks_per_rev`` 刻一圈，默认 4；相位只在整数刻上取值并绕回。
* 每根轴每拍前进当段转速所给整数刻：``φ ← (φ + ω) mod ticks_per_rev``。
  **只接受整数刻/拍的速度**——出现分数刻/拍直接拒绝（题面只采用整数刻情形）。
* 犬牙锁合（sleeve 把轴 a、b 接合）需同时满足：
  1. 在指定换挡窗内（``ShiftRequest.window`` 含该拍）；
  2. 两端当段转速相同 ``ω_a = ω_b``；
  3. 两端相位在 ``phase_mod`` 下同余 ``φ_a ≡ φ_b (mod phase_mod)``；
  4. 该套的滑座空闲（已合的套独占滑座，别的套不能同拍抢）。
* 锁合后本段后续各拍必须维持两端同速；若下一速度段不再同速，须**先**在准许窗
  脱开，否则违规。换挡只改离合状态，绝不复位任何轴的相位/标记。

一个“请求”给出一条换挡动作（在给定窗内择一拍执行）。`simulate` 把一组读数
（每段的整数转速表）跑成逐拍轨迹、动作表与违规清单；`common_action_table`
要求同一动作表在所有尚可成立的读数下都兑现。
"""

from dataclasses import dataclass, field

from .kinematics import GaugeError


# --------------------------------------------------------------- 数据模型
@dataclass
class BenchShaft:
    name: str
    phase0: int = 0


@dataclass
class Sleeve:
    sleeve_id: str
    seat: str
    a: str
    b: str
    phase_mod: int = 4
    engaged: bool = False


@dataclass
class SegmentPlan:
    name: str
    speeds: dict  # shaft -> int（刻/拍）
    beats: int


@dataclass
class ShiftRequest:
    request_id: str
    sleeve_id: str
    action: str  # 'engage' | 'disengage'
    window: list  # 绝对拍号（从 0 计）

    def __post_init__(self):
        if self.action not in ("engage", "disengage"):
            raise GaugeError("换挡动作只能是 engage 或 disengage")
        if not self.window:
            raise GaugeError("换挡请求必须给出非空窗口")


@dataclass
class BenchSpec:
    shafts: list
    sleeves: list
    segments: list
    requests: list
    ticks_per_rev: int = 4

    def shaft_names(self):
        return [s.name for s in self.shafts]

    def sleeve(self, sleeve_id):
        for sl in self.sleeves:
            if sl.sleeve_id == sleeve_id:
                return sl
        raise GaugeError(f"没有套 {sleeve_id}")

    def total_beats(self):
        return sum(seg.beats for seg in self.segments)

    def beat_segment_map(self):
        mapping = []
        for seg in self.segments:
            mapping.extend([seg] * seg.beats)
        return mapping


# --------------------------------------------------------------- 结果容器
@dataclass
class SimViolation:
    beat: int
    code: str
    detail: str

    def as_dict(self):
        return {"beat": self.beat, "code": self.code, "detail": self.detail}


@dataclass
class BenchResult:
    action_table: dict  # request_id -> {"beat", "action", "sleeve", "ok"}
    trace: list  # 每拍快照
    violations: list
    final_phases: dict
    final_engaged: dict  # sleeve_id -> bool
    seats: dict  # seat -> 最后占用 sleeve_id 或 None
    readings: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.violations

    def engaged_at_end(self, sleeve_id):
        return self.final_engaged.get(sleeve_id, False)


# --------------------------------------------------------------- 仿真核心
def _validate_speeds(spec, segment):
    for shaft, speed in segment.speeds.items():
        if not isinstance(speed, int) or isinstance(speed, bool):
            raise GaugeError(
                f"段 {segment.name} 的轴 {shaft} 转速不是整数刻/拍：{speed!r}"
                "（只采用每拍整数刻的情形）"
            )
        if shaft not in spec.shaft_names():
            raise GaugeError(f"段 {segment.name} 引用了不存在的轴 {shaft}")


def _phase_ok(phase_a, phase_b, modulus):
    return (phase_a - phase_b) % modulus == 0


def simulate(spec, reading=None):
    """按一组读数（段名 -> 整数转速表）跑完整演示。

    ``reading`` 缺省用 ``BenchSpec`` 自带的各段转速。返回 :class:`BenchResult`。
    """
    speeds_by_segment = {}
    for seg in spec.segments:
        effective = dict(seg.speeds)
        if reading is not None and seg.name in reading:
            effective.update(reading[seg.name])
        speeds_by_segment[seg.name] = effective
        _validate_speeds(
            spec, SegmentPlan(name=seg.name, speeds=effective, beats=seg.beats)
        )

    phases = {s.name: s.phase0 % spec.ticks_per_rev for s in spec.shafts}
    engaged = {sl.sleeve_id: sl.engaged for sl in spec.sleeves}
    seat_owner = {}
    for sl in spec.sleeves:
        if sl.engaged:
            if seat_owner.get(sl.seat) not in (None, sl.sleeve_id):
                raise GaugeError(f"初始状态滑座 {sl.seat} 上已有两套锁合")
            seat_owner[sl.seat] = sl.sleeve_id
    sleeve_by_id = {sl.sleeve_id: sl for sl in spec.sleeves}

    requests_by_beat = {}
    for req in spec.requests:
        for beat in req.window:
            requests_by_beat.setdefault(beat, []).append(req)
    # 记录每个请求是否已兑现
    fulfilled = {}
    action_table = {}
    violations = []
    trace = []
    beat_map = spec.beat_segment_map()

    for beat, segment in enumerate(beat_map):
        # 1) 先按当段转速推齿位（整数刻，绕回）
        for shaft in phases:
            phases[shaft] = (
                phases[shaft] + speeds_by_segment[segment.name].get(shaft, 0)
            ) % spec.ticks_per_rev

        # 2) 稳定性：已合套在本段必须两端同速，否则应先脱开（本拍尚未处理请求前判断）
        for sleeve_id, is_engaged in engaged.items():
            if not is_engaged:
                continue
            sl = sleeve_by_id[sleeve_id]
            va = speeds_by_segment[segment.name].get(sl.a, 0)
            vb = speeds_by_segment[segment.name].get(sl.b, 0)
            if va != vb:
                violations.append(
                    SimViolation(
                        beat,
                        "engaged_speed_mismatch",
                        f"套 {sleeve_id} 已合但本段 ω_{sl.a}={va}≠ω_{sl.b}={vb}，"
                        "应先脱开",
                    )
                )
                # 物理上会崩开：视为自动脱开以继续记录
                engaged[sleeve_id] = False
                if seat_owner.get(sl.seat) == sleeve_id:
                    seat_owner[sl.seat] = None

        # 3) 处理本拍到期请求：先脱开后锁合
        due = requests_by_beat.get(beat, [])
        for req in sorted(due, key=lambda r: 0 if r.action == "disengage" else 1):
            if fulfilled.get(req.request_id):
                continue
            sl = sleeve_by_id[req.sleeve_id]
            if req.action == "disengage":
                if engaged[req.sleeve_id]:
                    engaged[req.sleeve_id] = False
                    if seat_owner.get(sl.seat) == req.sleeve_id:
                        seat_owner[sl.seat] = None
                fulfilled[req.request_id] = True
                action_table[req.request_id] = {
                    "beat": beat,
                    "action": "disengage",
                    "sleeve": req.sleeve_id,
                    "ok": True,
                }
                continue
            # engage：检查座位、同速、同余
            owner = seat_owner.get(sl.seat)
            if owner is not None and owner != sl.sleeve_id:
                # 座位被别的套占据，本拍不能合；窗口中后续拍再试
                continue
            va = speeds_by_segment[segment.name].get(sl.a, 0)
            vb = speeds_by_segment[segment.name].get(sl.b, 0)
            if va != vb:
                continue  # 转速不同，本拍不能合（留待后续窗口拍）
            if not _phase_ok(phases[sl.a], phases[sl.b], sl.phase_mod):
                continue  # 相位不同余，留待后续
            engaged[req.sleeve_id] = True
            seat_owner[sl.seat] = req.sleeve_id
            fulfilled[req.request_id] = True
            action_table[req.request_id] = {
                "beat": beat,
                "action": "engage",
                "sleeve": req.sleeve_id,
                "ok": True,
                "phases": {sl.a: phases[sl.a], sl.b: phases[sl.b]},
            }

        trace.append(
            {
                "beat": beat,
                "segment": segment.name,
                "phases": dict(phases),
                "engaged": {k: v for k, v in engaged.items() if v},
                "seat_owner": {k: v for k, v in seat_owner.items() if v},
            }
        )

    # 窗口结束后仍未兑现的请求 → 违规（错过窗口）
    for req in spec.requests:
        if not fulfilled.get(req.request_id):
            action_table[req.request_id] = {
                "beat": None,
                "action": req.action,
                "sleeve": req.sleeve_id,
                "ok": False,
            }
            violations.append(
                SimViolation(
                    spec.total_beats() - 1,
                    "missed_window",
                    f"请求 {req.request_id}（{req.action} 套 {req.sleeve_id}）"
                    f"在窗口 {req.window} 内无法兑现",
                )
            )

    return BenchResult(
        action_table=action_table,
        trace=trace,
        violations=violations,
        final_phases=dict(phases),
        final_engaged={k: v for k, v in engaged.items()},
        seats=dict(seat_owner),
    )


# --------------------------------------------------------------- 辅助查询
def find_lockable_beats(spec, sleeve_id, window, reading=None):
    """窗口内哪些拍满足“两端同速 + 相位同余 + 座位空”。仅看候选拍，不改状态。"""
    sl = spec.sleeve(sleeve_id)
    speeds_by_segment = {}
    for seg in spec.segments:
        effective = dict(seg.speeds)
        if reading is not None and seg.name in reading:
            effective.update(reading[seg.name])
        speeds_by_segment[seg.name] = effective
    phases = {s.name: s.phase0 % spec.ticks_per_rev for s in spec.shafts}
    beat_map = spec.beat_segment_map()
    # 座位初始占用
    seat_owner = {}
    for other in spec.sleeves:
        if other.engaged and other.seat == sl.seat and other.sleeve_id != sleeve_id:
            seat_owner[sl.seat] = other.sleeve_id
    result = []
    for beat, segment in enumerate(beat_map):
        for shaft in phases:
            phases[shaft] = (
                phases[shaft] + speeds_by_segment[segment.name].get(shaft, 0)
            ) % spec.ticks_per_rev
        if beat not in window:
            continue
        if seat_owner.get(sl.seat) not in (None, sl.sleeve_id):
            continue
        va = speeds_by_segment[segment.name].get(sl.a, 0)
        vb = speeds_by_segment[segment.name].get(sl.b, 0)
        if va == vb and _phase_ok(phases[sl.a], phases[sl.b], sl.phase_mod):
            result.append(beat)
    return result


def common_action_table(spec, readings):
    """要求同一动作表在所有尚可成立的读数下都兑现。

    返回 ``{"ok", "table", "per_reading", "violations"}``。``table`` 只有在每个
    读数都给出完全相同的动作表时才被视为“共同动作表”。
    """
    per_reading = []
    tables = []
    all_ok = True
    for index, reading in enumerate(readings):
        result = simulate(spec, reading=reading)
        per_reading.append(
            {
                "reading_index": index,
                "ok": result.ok,
                "action_table": result.action_table,
                "violations": [v.as_dict() for v in result.violations],
            }
        )
        tables.append(result.action_table)
        if not result.ok:
            all_ok = False
    identical = all(table == tables[0] for table in tables) if tables else True
    # 共同动作表：逐请求要求每个读数都在同一拍完成同一动作
    common = {}
    violations = []
    if tables:
        for request_id in tables[0]:
            beats = {table[request_id]["beat"] for table in tables}
            oks = {table[request_id]["ok"] for table in tables}
            if len(beats) == 1 and oks == {True}:
                common[request_id] = tables[0][request_id]
            else:
                violations.append(
                    {
                        "request_id": request_id,
                        "reason": "读数之间无法共用同一动作拍",
                        "beats": sorted(b for b in beats if b is not None),
                        "all_ok": oks == {True},
                    }
                )
    return {
        "ok": all_ok and identical and not violations,
        "table": common,
        "per_reading": per_reading,
        "violations": violations,
    }
