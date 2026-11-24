"""管风琴调律工单：把题目里点名的台架都建成可精确复跑的案例。

每个工厂函数返回 ``(rig, kwargs, expected)``：
* ``rig``      —— :class:`budget_solver.organtune.Rig`；
* ``kwargs``   —— 传给 ``organtune.plan`` 的开关（是否允许接头/微调、安装最早班…）；
* ``expected`` —— 该案例的预期结论（``ok``、工期、结构性原因关键词…）。

数值全部手工可核对的精确整数/分数；音程统一为 ``pitch(to) - pitch(from)``。
"""

from budget_solver.organtune import Rig


# --------------------------------------------------------------------------
# D1：演示甲乙原先同杆；甲已与固定音准合格即将封区；另一和弦需乙再抬 1。
#     deadline 3；留出安装（1 班）+ 转接（1 班）+ 拨栓（1 班）= 3 班可交。
# --------------------------------------------------------------------------
def d1_connector():
    rig = Rig(
        pipes={"甲": {"zero": "0", "rod": "R"},
               "乙": {"zero": "3", "rod": "R"}},
        rods={"R": {"knobs": {"k": {"coef": "1", "detents": ["-1", "1"]}}}},
        chords=[
            {"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 3},
            {"name": "乙甲", "pairs": [["乙", "甲", "-4"]], "deadline": 3},
        ],
        regions={"甲区": {"pipes": ["甲"], "seal_after": 2}},
        fixed_pipes=["甲"],
        max_shifts=5,
    )
    return rig, {}, {
        "ok": True,
        "implementation": "detachable_connector",
        "shifts_used": 3,
        "actions": ["install_connector", "detach", "knob"],
    }


# --------------------------------------------------------------------------
# D2：把来料班（安装最早班）移到甲乙两区都已封盖之后 —— 同一张工单失效。
# --------------------------------------------------------------------------
def d2_materials_after_seal():
    rig = Rig(
        pipes={"甲": {"zero": "0", "rod": "R"},
               "乙": {"zero": "3", "rod": "R"}},
        rods={"R": {"knobs": {"k": {"coef": "1", "detents": ["-1", "1"]}}}},
        chords=[{"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 5}],
        regions={"甲区": {"pipes": ["甲"], "seal_after": 2},
                 "乙区": {"pipes": ["乙"], "seal_after": 3}},
        fixed_pipes=["甲"],
        max_shifts=5,
    )
    return rig, {"install_earliest": 4}, {
        "ok": False,
        "reason_contains": "来料/安装最早班 4 已晚于",
    }


# --------------------------------------------------------------------------
# D3（带丙）：同一条联杆带甲、乙、丙。甲要封在原音，乙、丙都须抬 1。
#     窗内只许装一次；另给足一次转接与一次拨栓的拍数。
#     开盖时可脱开甲；封甲后旧杆可一起抬乙丙。
# --------------------------------------------------------------------------
def d3_abc_connector():
    rig = Rig(
        pipes={"甲": {"zero": "0", "rod": "R"},
               "乙": {"zero": "0", "rod": "R"},
               "丙": {"zero": "0", "rod": "R"}},
        rods={"R": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
        chords=[
            {"name": "甲乙", "pairs": [["甲", "乙", "1"]], "deadline": 5},
            {"name": "甲丙", "pairs": [["甲", "丙", "1"]], "deadline": 5},
        ],
        regions={"甲区": {"pipes": ["甲"], "seal_after": 2}},
        fixed_pipes=["甲"],
        max_shifts=5,
    )
    return rig, {}, {
        "ok": True,
        "implementation": "detachable_connector",
        "shifts_used": 3,
        # 拨栓那一班同时带动乙、丙（听两根）
        "last_moved": ["丙", "乙"],
    }


# --------------------------------------------------------------------------
# D3′：只有一枚微调栓的台架 —— 不能同时抬两根，不能假装有第二枚。
# --------------------------------------------------------------------------
def d3_abc_micro_only():
    rig, _, _ = d3_abc_connector()
    return rig, {"allow_connectors": False, "allow_micro": True}, {
        "ok": False,
        "reason_contains": "只有一枚可用微栓",
    }


# --------------------------------------------------------------------------
# C1：固定原机和旧工装（不新增接头/微栓）。甲偏差自由 t，乙 t+3，目标差 4；
#     唯一可用栓只抬乙 1 音分，首班可调，随后封 —— 能交。
#     另一套核 乙 至 甲为 -4（反向同一音程）。
# --------------------------------------------------------------------------
def c1_fixed_rig():
    rig = Rig(
        pipes={"甲": {"zero": "0"},
               "乙": {"zero": "3", "rod": "RE"}},
        rods={"RE": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
        chords=[
            {"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 5},
            {"name": "乙甲", "pairs": [["乙", "甲", "-4"]], "deadline": 5},
        ],
        regions={"乙区": {"pipes": ["乙"], "seal_after": 1}},
        max_shifts=5,
    )
    return rig, {"allow_connectors": False, "allow_micro": False}, {
        "ok": True,
        "implementation": "existing_rig",
        "shifts_used": 1,
    }


# --------------------------------------------------------------------------
# C1′：在 C1 台架上仅把测音卡换为 乙 = t+2（目标仍 4）—— 需要 +2，但首班唯一
#     一个准许档只 +1，随后乙即封区，故“一次不够”。
# --------------------------------------------------------------------------
def c1_prime_insufficient():
    rig = Rig(
        pipes={"甲": {"zero": "0"},
               "乙": {"zero": "2", "rod": "RE"}},
        rods={"RE": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
        chords=[
            {"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 5},
            {"name": "乙甲", "pairs": [["乙", "甲", "-4"]], "deadline": 5},
        ],
        regions={"乙区": {"pipes": ["乙"], "seal_after": 1}},
        max_shifts=5,
    )
    return rig, {"allow_connectors": False, "allow_micro": False}, {
        "ok": False,
        "reason_contains": "一次不够",
    }


# --------------------------------------------------------------------------
# C2：恢复原测量（乙 = t+3），但让乙在首班开始前就封住 —— 也交不了。
# --------------------------------------------------------------------------
def c2_sealed_before_first():
    rig = Rig(
        pipes={"甲": {"zero": "0"},
               "乙": {"zero": "3", "rod": "RE"}},
        rods={"RE": {"knobs": {"k": {"coef": "1", "detents": ["1"]}}}},
        chords=[
            {"name": "甲乙", "pairs": [["甲", "乙", "4"]], "deadline": 5},
            {"name": "乙甲", "pairs": [["乙", "甲", "-4"]], "deadline": 5},
        ],
        regions={"乙区": {"pipes": ["乙"], "seal_after": 0}},
        max_shifts=5,
    )
    return rig, {"allow_connectors": False, "allow_micro": False}, {
        "ok": False,
        "reason_contains": "首班前已封区",
    }


# --------------------------------------------------------------------------
# C3：同一杆会带坏上一和弦的场次。
#     前一和弦 丙-甲=0 当前成立；后一和弦 乙-丙=1 需要通过 S 杆抬丙，
#     但抬丙会破坏 丙-甲=0；抬 R 杆又同时抬甲、乙，同样带坏前一和弦。
# --------------------------------------------------------------------------
def c3_rod_breaks_previous_chord():
    rig = Rig(
        pipes={"甲": {"zero": "0", "rod": "R"},
               "乙": {"zero": "0", "rod": "R"},
               "丙": {"zero": "0", "rod": "S"}},
        rods={
            "R": {"knobs": {"r": {"coef": "1", "detents": ["1"]}}},
            "S": {"knobs": {"s": {"coef": "1", "detents": ["1"]}}},
        },
        chords=[
            {"name": "前一和弦", "pairs": [["丙", "甲", "0"]], "deadline": 5},
            {"name": "后一和弦", "pairs": [["乙", "丙", "1"]], "deadline": 5},
        ],
        max_shifts=5,
    )
    return rig, {"allow_connectors": False, "allow_micro": False}, {
        "ok": False,
        "reason_contains": "带坏已成立的和弦",
    }


ALL_CASES = [
    ("D1", d1_connector),
    ("D2", d2_materials_after_seal),
    ("D3", d3_abc_connector),
    ("D3_micro", d3_abc_micro_only),
    ("C1", c1_fixed_rig),
    ("C1p", c1_prime_insufficient),
    ("C2", c2_sealed_before_first),
    ("C3", c3_rod_breaks_previous_chord),
]
