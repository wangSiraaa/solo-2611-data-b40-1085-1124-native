"""Part C 规定例：卡系统、颁章、复尝、迟来回避两时刻。

全部数值可手算复核，且不出现真实人名/食品数据。
"""

from fractions import Fraction

from .appeal import Booking, Session, resolve_delayed_recusal
from .cards import CardSystem
from .scores import round_supplier_means


# ============================================================ C1：自由整数 y
def c1_free_integer_card():
    """甲三席 0、x、4；乙三席均 2。原卡 x + y = 4 且 y = 1（解出 x=3，甲中分 3 赢）。

    仅让 y 变为允许 {1,2,3} 的自由整数（去掉 y=1 这条钉死记录，改由整数盒约束），
    x = 4 - y 随之为 {3,2,1}。于是：

    * y=1,x=3 → 甲中分 3 赢；
    * y=2,x=2 → 甲中分 2，与乙 2 平；
    * y=3,x=1 → 甲中分 1 输。

    三份可行整桌补卡给出不同胜负 ⇒ 不能直接颁章，两家都须复尝。
    """
    sys = CardSystem()
    sys.add_var("jia_x", 0, 4)   # 甲第二席底分 x（永久格号 jia_x）
    sys.add_var("y", 1, 3)       # 自由整数 y
    sys.add_record("card_xy", [("jia_x", 1), ("y", 1)], 4,
                   source=["card#1: x+y=4"])
    return sys


def c1_outcomes():
    """返回每份整桌补卡下 (x, 甲均值, 乙均值, 胜负)。"""
    sys = c1_free_integer_card()
    sol = sys.solve()
    rows = []
    for card in sol.integer_cards:
        x = card["jia_x"]
        y = card["y"]
        jia = round_supplier_means(
            ["A1", "A2", "A3"],
            {("A1", "甲"): Fraction(0), ("A2", "甲"): Fraction(x), ("A3", "甲"): Fraction(4),
             ("A1", "乙"): Fraction(2), ("A2", "乙"): Fraction(2), ("A3", "乙"): Fraction(2)},
            ["甲", "乙"],
        )
        jm, ym = jia["甲"]["mean"], jia["乙"]["mean"]
        verdict = "甲赢" if jm > ym else ("平" if jm == ym else "乙赢")
        rows.append({"x": x, "y": y, "jia": jm, "yi": ym, "verdict": verdict, "card": card})
    return sol, rows


# ============================================================ C2：第三席回避
def c2_recuse_third_seat():
    """保持原卡（y=1 ⇒ x=3），仅让第三席回避：甲平均 1.5、乙 2，乙领章。

    原分：甲 (0,3,4)；乙 (2,2,2)。第三席整席删除后甲剩 (0,3) → 均值 3/2；
    乙剩 (2,2) → 均值 2。乙严格高 ⇒ 领章。
    """
    scores = {
        ("A1", "甲"): Fraction(0), ("A2", "甲"): Fraction(3), ("A3", "甲"): Fraction(4),
        ("A1", "乙"): Fraction(2), ("A2", "乙"): Fraction(2), ("A3", "乙"): Fraction(2),
    }
    return round_supplier_means(["A1", "A2", "A3"], scores, ["甲", "乙"],
                               recusals=["A3"])


# ================================================ 两轮：各自可领先、共同底分不能同时
def two_rounds_shared_base():
    """两轮各自单独可让不同供应社领先，但共用同一永久底分 v 时不能同时成立。

    永久格号 ``v`` 代表同一底分，被两张卡共同引用：

    * 第一轮卡声称 v = 3，且第一轮里甲的某一席就取这个 v：
      甲 = (0, v, 4)，乙 = (2,2,2)。v=3 时甲中分 3 > 2 ⇒ 甲领先。
    * 第二轮卡声称 v = 1，且第二轮里乙的分数由 v 决定：
      乙 = (v, 6-2v)，甲 = (2,2)。v=1 时乙均值 2.5 > 2 ⇒ 乙领先。

    分开看每场都自洽（各有整数解、各自能选出不同领先者）；但两卡把**同一个**
    永久格 v 钉成 3 与 1 ⇒ 矛盾，证书指出这两张记录不能同时使用，因此“甲在第一
    轮领先、乙在第二轮领先”在同一底分下不能同时成立。
    """
    from .scores import round_supplier_means

    def solo_system(pin):
        sys = CardSystem()
        sys.add_var("v", 1, 3)
        sys.add_record(f"round_card", [("v", 1)], pin, source=[f"卡: v={pin}"])
        return sys

    def round1_means(v):
        return round_supplier_means(
            ["R1a", "R1b", "R1c"],
            {("R1a", "甲"): 0, ("R1b", "甲"): v, ("R1c", "甲"): 4,
             ("R1a", "乙"): 2, ("R1b", "乙"): 2, ("R1c", "乙"): 2},
            ["甲", "乙"])

    def round2_means(v):
        return round_supplier_means(
            ["R2a", "R2b"],
            {("R2a", "甲"): 2, ("R2b", "甲"): 2,
             ("R2a", "乙"): v, ("R2b", "乙"): 6 - 2 * v},
            ["甲", "乙"])

    def leader(means):
        m = {s: e["mean"] for s, e in means.items()}
        top = max(m.values())
        tops = sorted(s for s, val in m.items() if val == top)
        return tops[0] if len(tops) == 1 else None

    r1 = round1_means(3)
    r2 = round2_means(1)

    joint = CardSystem()
    joint.add_var("v", 1, 3)
    joint.add_record("round_1", [("v", 1)], 3, source=["第一轮卡: v=3"])
    joint.add_record("round_2", [("v", 1)], 1, source=["第二轮卡: v=1"])

    return {
        "solo_hi": {"solution": solo_system(3).solve(),
                    "means": r1, "leader": leader(r1)},
        "solo_lo": {"solution": solo_system(1).solve(),
                    "means": r2, "leader": leader(r2)},
        "joint": joint.solve(),
    }


# ============================================ 迟来回避：两个时刻不同复尝去向
def delayed_recusal_two_timestamps():
    """同一次迟来回避，分别落在“甲唯一留样已开”和“甲只订座仍未开”两个时刻。

    场景：三席 A1,A2,A3；甲只在 A1、A3 有分（A2 未给甲打分），乙三席齐全。
    旧分：甲 {A1=1, A3=9}、乙 {2,2,2}。旧名次：甲均值 5 领先乙 2。
    迟来申诉确认 A3 应回避（整席删除）。

    重判原始有效票后：甲只剩 A1=1 一条 ⇒ 必须复尝；乙剩 (2,2) ⇒ 均值 2。

    两时刻“同样的旧名次与预约数（各 1 个预约/留样）”：

    * 时刻① A3 已开样，甲的**唯一留样已被吃掉**且那次复尝与 A3 同场 → 结果作废、
      留样无法收回 ⇒ 甲**无法补尝**，复尝去向为空。
    * 时刻② 甲**只订座未开封** → 同一留样可改派到干净机位 S2 ⇒ 甲的实际复尝去向
      是 S2（session 2, time 20）。
    """
    from .scheduling import Sample, Slot

    scores = {
        ("A1", "甲"): Fraction(1), ("A3", "甲"): Fraction(9),
        ("A1", "乙"): Fraction(2), ("A2", "乙"): Fraction(2), ("A3", "乙"): Fraction(2),
    }
    session = Session("round1", ["A1", "A2", "A3"], ["甲", "乙"], scores, weight=1)

    def sample():
        return Sample("jia_only", "甲", label="X", window=(0, 100),
                      last_session=3, count=1)

    clean_slot = Slot("S2", session=2, time=20, capacity=1, label="X")
    # 与回避席 A3 同场的那次复尝机位（不再复用）
    conflicted_slot = Slot("S1", session=1, time=10, capacity=1, label="X")

    def resolve(booking):
        return resolve_delayed_recusal(
            session, "A3",
            existing_bookings=[booking],
            available_slots=[clean_slot],
            samples_by_supplier={"甲": [sample()]},
        )

    # 时刻①：唯一留样已开，且与 A3 同场 → 作废且无法补尝
    opened = Booking("甲", "jia_only", "opened", slot_session=1, slot_time=10,
                     slot_label="X", with_recused_judge=True)
    case_opened = resolve(opened)

    # 时刻②：只订座未开（同样 1 个预约）→ 可改派到 S2
    booked = Booking("甲", "jia_only", "booked", slot_session=1, slot_time=10,
                     slot_label="X", with_recused_judge=True)
    case_booked = resolve(booked)

    return {
        "session": session,
        "conflicted_slot": conflicted_slot,
        "clean_slot": clean_slot,
        "old_ranking": {"甲": Fraction(5), "乙": Fraction(2)},
        "case_opened": case_opened,
        "case_booked": case_booked,
    }
