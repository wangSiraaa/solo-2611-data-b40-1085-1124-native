"""复尝机位排位：时段 + 容量 + 隔离标签 + 到场时窗，最少位覆盖。

约束（题面）：

* 每个复尝位（slot）有一个**时段**、一个**容量**与一个**隔离标签**；
* 同桌（同一 slot）只容**隔离标签相同**的试样；
* 试样（sample）只在其**试吃时窗**到场，另有份数与**最后可开班次**；
* 展方可在备赛时分交具名留样，也可临开炉由保管台发实样——本模块只认
  “该试样能否在某 slot 到场”，与交付方式无关。

``plan_retaste`` 用精确最少 slot 覆盖“必须复尝的供应社”集合：只要能给每家
放下一份符合条件的试样即算覆盖。排不下时返回缺哪一位（missing suppliers 与原因）。
"""

from itertools import combinations


class Slot:
    __slots__ = ("slot_id", "session", "time", "capacity", "label")

    def __init__(self, slot_id, session, time, capacity, label):
        if capacity <= 0:
            raise ValueError(f"slot {slot_id} 容量必须为正")
        self.slot_id = slot_id
        self.session = session
        self.time = time
        self.capacity = capacity
        self.label = label

    def to_json(self):
        return {
            "slot_id": self.slot_id,
            "session": self.session,
            "time": self.time,
            "capacity": self.capacity,
            "label": self.label,
        }


class Sample:
    __slots__ = ("sample_id", "supplier", "label", "window", "last_session", "count")

    def __init__(self, sample_id, supplier, label, window, last_session, count=1):
        self.sample_id = sample_id
        self.supplier = supplier
        self.label = label
        self.window = (window[0], window[1])  # inclusive [start, end]
        self.last_session = last_session
        self.count = count
        if count < 0:
            raise ValueError("份数不能为负")

    def available_at(self, slot):
        return (
            self.count > 0
            and self.label == slot.label
            and slot.session <= self.last_session
            and self.window[0] <= slot.time <= self.window[1]
        )

    def to_json(self):
        return {
            "sample_id": self.sample_id,
            "supplier": self.supplier,
            "label": self.label,
            "window": list(self.window),
            "last_session": self.last_session,
            "count": self.count,
        }


def _assignment_exists(chosen_slots, required, samples):
    """在给定 slot 集合内，能否为每个 required 供应社各放一份试样。

    容量与份数都要守恒；同一供应社可有多份试样，择优尝试。返回
    ``(ok, placements, missing)``。
    """
    # 按供应社收集可用 (sample, slot) 组合
    remaining = {sup: None for sup in required}
    capacity_left = {slot.slot_id: slot.capacity for slot in chosen_slots}

    # 回溯：先满足约束最强的供应社（可用位置最少）
    options = {}
    for sup in required:
        opts = []
        for sample in samples:
            if sample.supplier != sup or sample.count <= 0:
                continue
            for slot in chosen_slots:
                if sample.available_at(slot):
                    opts.append((sample.sample_id, slot.slot_id))
        options[sup] = opts

    order = sorted(required, key=lambda s: (len(options[s]), s))

    def backtrack(i, placed):
        if i == len(order):
            return True
        sup = order[i]
        seen = set()
        for sample_id, slot_id in options[sup]:
            key = (sample_id, slot_id)
            if capacity_left[slot_id] <= 0 or key in seen:
                continue
            seen.add(key)
            capacity_left[slot_id] -= 1
            placed.append({"supplier": sup, "sample_id": sample_id, "slot_id": slot_id})
            if backtrack(i + 1, placed):
                return True
            placed.pop()
            capacity_left[slot_id] += 1
        return False

    placed = []
    if backtrack(0, placed):
        return True, placed, []
    missing = sorted(s for s in required if not options[s])
    return False, [], missing


def plan_retaste(required_suppliers, slots, samples):
    """用最少 slot 覆盖全部 required 供应社。

    返回 dict：

    * ``ok``：是否排得下；
    * ``slot_count``：用了几位；
    * ``bookings``：每位放哪几份试样（订位动作）；
    * ``missing``：排不下时缺哪些供应社；
    * ``reason``：补充说明；
    * ``attempts``：最少位数尝试记录，便于审计。
    """
    required = sorted(set(required_suppliers))
    if not required:
        return {"ok": True, "slot_count": 0, "bookings": [], "missing": [],
                "reason": "无需复尝", "attempts": []}

    slots = list(slots)
    attempts = []
    for k in range(1, len(slots) + 1):
        for combo in combinations(slots, k):
            ok, placements, missing = _assignment_exists(combo, required, samples)
            attempts.append({
                "k": k,
                "slot_ids": [s.slot_id for s in combo],
                "ok": ok,
                "missing": missing,
            })
            if ok:
                bookings = []
                for p in placements:
                    slot = next(s for s in combo if s.slot_id == p["slot_id"])
                    bookings.append({
                        "supplier": p["supplier"],
                        "sample_id": p["sample_id"],
                        "slot_id": p["slot_id"],
                        "session": slot.session,
                        "time": slot.time,
                        "label": slot.label,
                        "isolated_with": slot.label,
                        "action": "book_and_open",
                    })
                return {
                    "ok": True,
                    "slot_count": k,
                    "bookings": bookings,
                    "missing": [],
                    "reason": f"最少 {k} 位可覆盖全部 {len(required)} 家",
                    "attempts": attempts,
                }
    # 覆盖不了的逐家缺因：区分“单独也排不了”与“单独可排但凑不齐”
    reasons = {}
    truly_missing = []
    for sup in required:
        has_any = any(s.supplier == sup and s.count > 0 for s in samples)
        capable = [
            s for s in samples
            if s.supplier == sup and s.count > 0
            and any(s.available_at(slot) for slot in slots)
        ]
        if not has_any:
            reasons[sup] = "无留样，无法补尝"
            truly_missing.append(sup)
        elif not capable:
            reasons[sup] = "留样在全部机位的时段/标签/最后开班次下都无法到场"
            truly_missing.append(sup)
        else:
            reasons[sup] = "本家留样可到某机位，但同桌需与其它家并置，其它家缺样或缺位"
    if not truly_missing:
        # 每家单独都能排，只是拼不进现有容量：报容量冲突
        missing_out = sorted(required)
        group_reason = "各留样单独都可排，但现有复尝位的容量/标签无法把它们并置到同一批最少位"
    else:
        missing_out = sorted(truly_missing)
        group_reason = "有供应社缺样，无法与其它家并置"
    return {
        "ok": False,
        "slot_count": None,
        "bookings": [],
        "missing": missing_out,
        "reason": group_reason,
        "missing_detail": reasons,
        "attempts": attempts,
    }
