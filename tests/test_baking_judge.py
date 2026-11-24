"""烘焙展盲评/颁章/复尝系统测试：规定例 + 通用机制 + 交付办法。

运行：python3 -m tests.test_baking_judge
"""

from fractions import Fraction

from baking_judge import (
    CardSystem,
    decide_award,
    plan_retaste,
    resolve_delayed_recusal,
    round_supplier_means,
    trimmed_mean,
    weighted_totals,
)
from baking_judge.award import supporting_cards
from baking_judge.cards import CardError
from baking_judge.delivery import (
    CENTRAL_DISPATCH,
    PRE_DISTRIBUTED,
    deliver,
)
from baking_judge.scheduling import Sample, Slot
from baking_judge.scenarios import (
    c1_free_integer_card,
    c1_outcomes,
    c2_recuse_third_seat,
    delayed_recusal_two_timestamps,
    two_rounds_shared_base,
)
from baking_judge.appeal import Booking, Session, viability, who_may_consume, PENDING_POLICY

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


# ============================================================ 记分规则
def test_trimmed_mean_rules():
    # 三席：去一高一低
    check(trimmed_mean([0, 3, 4]) == Fraction(3), "三席去一高一低取中位 3")
    check(trimmed_mean([1, 2, 9]) == Fraction(2), "三席 1,2,9 → 2")
    # 恰两位直接均值
    check(trimmed_mean([2, 4]) == Fraction(3), "恰两位直接取均值 3")
    # 一位以下必须重尝
    check(trimmed_mean([7]) is None, "一位及格必须重尝（None）")
    check(trimmed_mean([]) is None, "零位必须重尝（None）")
    # 四席：仍去一高一低（容量上限 4 席）
    check(trimmed_mean([1, 2, 3, 100]) == Fraction(5, 2), "四席去一高一低取剩两席均值 5/2")


def test_recusal_removes_whole_seat():
    scores = {
        ("J1", "甲"): 10, ("J2", "甲"): 8, ("J3", "甲"): 6,
        ("J1", "乙"): 5, ("J2", "乙"): 5, ("J3", "乙"): 5,
    }
    without = round_supplier_means(["J1", "J2", "J3"], scores, ["甲", "乙"])
    check(without["甲"]["mean"] == Fraction(8), "整席前甲(8,10,6)去高低后 8")
    with_rec = round_supplier_means(["J1", "J2", "J3"], scores, ["甲", "乙"],
                                   recusals=["J3"])
    # J3 对甲、对乙都被整席删除
    check(with_rec["甲"]["qualified_scores"] == [Fraction(10), Fraction(8)],
          "回避 J3 后甲只剩 J1,J2")
    check(with_rec["乙"]["qualified_scores"] == [Fraction(5), Fraction(5)],
          "回避 J3 对乙同样是整席删除（不是只删一家）")
    check(with_rec["甲"]["mean"] == Fraction(9), "两位合格直接均值 9")
    check(with_rec["乙"]["mean"] == Fraction(5), "乙两位均值 5")


def test_retaste_when_one_or_fewer():
    scores = {("J1", "甲"): 8, ("J1", "乙"): 3, ("J2", "乙"): 4}
    r = round_supplier_means(["J1", "J2"], scores, ["甲", "乙"])
    check(r["甲"]["needs_retaste"], "甲仅一位 → 该轮必须重尝")
    check(r["甲"]["mean"] is None, "甲无有效均值")
    check(not r["乙"]["needs_retaste"] and r["乙"]["mean"] == Fraction(7, 2),
          "乙两位直接均值 7/2")

    totals = weighted_totals([r], [1])
    check(totals["totals"]["甲"] is None, "有重尝轮时甲总分不可定（不给残值）")
    check(totals["retaste_rounds"]["甲"] == [0], "标明甲需在第 0 轮复尝")


def test_weighted_rounds():
    r1 = round_supplier_means(
        ["J1", "J2", "J3"],
        {("J1", "甲"): 4, ("J2", "甲"): 6, ("J3", "甲"): 8,
         ("J1", "乙"): 2, ("J2", "乙"): 3, ("J3", "乙"): 4},
        ["甲", "乙"])
    r2 = round_supplier_means(
        ["K1", "K2"],
        {("K1", "甲"): 10, ("K2", "甲"): 12,
         ("K1", "乙"): 1, ("K2", "乙"): 1},
        ["甲", "乙"])
    agg = weighted_totals([r1, r2], [2, 1])
    # 甲: r1 中位6 *2 + r2 均值11 *1 = 12+11 = 23 ; 乙: 3*2 + 1 = 7
    check(agg["totals"]["甲"] == Fraction(23), "甲加权 6*2 + 11*1 = 23")
    check(agg["totals"]["乙"] == Fraction(7), "乙加权 3*2 + 1*1 = 7")


# ============================================================ C1 自由整数卡
def test_c1_free_integer_card():
    sol, rows = c1_outcomes()
    check(sol.status == "parametric", "C1 卡 x+y=4 且 y 自由 ⇒ 参数解")
    check(sol.free_vars == ["y"], "自由变量是 y")
    check(len(sol.integer_cards) == 3, "y∈{1,2,3} ⇒ 3 份整桌补卡")
    check(sorted(c["y"] for c in sol.integer_cards) == [1, 2, 3], "y 取遍 1,2,3")
    check(sorted(c["jia_x"] for c in sol.integer_cards) == [1, 2, 3],
          "x=4-y 取遍 1,2,3")
    verdicts = {r["y"]: r["verdict"] for r in rows}
    check(verdicts[1] == "甲赢", "y=1,x=3 甲中分 3 赢")
    check(verdicts[2] == "平", "y=2,x=2 甲 2 与乙 2 平")
    check(verdicts[3] == "乙赢", "y=3,x=1 甲 1 输给乙 2")
    # 单查某格
    qx = c1_free_integer_card().query_cell("jia_x")
    check(qx["status"] == "free" and qx["possible_values"] == [1, 2, 3],
          "单查 x 得全部可行值 1,2,3")
    # 不能直接颁章；两家都须复尝
    cards_with_means = [{"card": r["card"], "means": {"甲": r["jia"], "乙": r["yi"]}}
                        for r in rows]
    d = decide_award(cards_with_means)
    check(d["decision"] == "no_award", "C1 三卡胜负不同 ⇒ 不能直接颁章")
    check(set(d["contesting"]) == {"甲", "乙"}, "C1 两家都可能夺章，都须复尝")
    sup = supporting_cards(d, cards_with_means)
    check(set(sup) == {"甲", "乙"}, "C1 两家都有真正支持的补卡")
    # 严格支持：甲只在 y=1 卡严格赢；乙只在 y=3 卡严格赢；y=2 并列不算严格支持
    check(sup["甲"] == [{"jia_x": 3, "y": 1}], "甲严格被支持于 y=1（x=3）补卡")
    check(sup["乙"] == [{"jia_x": 1, "y": 3}], "乙严格被支持于 y=3（x=1）补卡")


# ============================================================ C2 第三席回避
def test_c2_recuse_third_seat():
    r = c2_recuse_third_seat()
    check(r["甲"]["mean"] == Fraction(3, 2), "回避第三席后甲均值 1.5")
    check(r["乙"]["mean"] == Fraction(2), "乙均值 2")
    check(r["乙"]["mean"] > r["甲"]["mean"], "乙严格高 ⇒ 乙领章")


# ==================================== 两轮各自领先但共同底分不能同时成立
def test_two_rounds_shared_base():
    tr = two_rounds_shared_base()
    check(tr["solo_hi"]["solution"].status == "determined", "第一轮单独可解")
    check(tr["solo_hi"]["leader"] == "甲", "第一轮单独甲领先（v=3 时甲中分 3）")
    check(tr["solo_hi"]["means"]["甲"]["mean"] == Fraction(3),
          "第一轮 v=3：甲中分 3")
    check(tr["solo_lo"]["solution"].status == "determined", "第二轮单独可解")
    check(tr["solo_lo"]["leader"] == "乙", "第二轮单独乙领先（v=1 时乙均值 5/2）")
    check(tr["solo_lo"]["means"]["乙"]["mean"] == Fraction(5, 2),
          "第二轮 v=1：乙均值 5/2")
    joint = tr["joint"]
    check(joint.status == "inconsistent", "共同底分 v 同时为 3 与 1 ⇒ 矛盾")
    cert = joint.contradictions[0]
    check(cert["residual_lhs"] == "0" and cert["residual_rhs"] != "0",
          "矛盾证书形如 0=非零")
    check(set(cert["conflicting_records"]) == {"round_1", "round_2"},
          "证书指出这两张记录不能同时使用")
    check(len(cert["conflicting_sources"]) == 2, "并给出冲突来源卡")


# ====================================== 迟来回避：两个时刻不同复尝去向
def test_delayed_recusal_two_timestamps():
    dr = delayed_recusal_two_timestamps()
    opened = dr["case_opened"]
    booked = dr["case_booked"]

    # 同样的旧名次与预约数
    check(dr["old_ranking"] == {"甲": Fraction(5), "乙": Fraction(2)},
          "旧名次：甲 5 领先乙 2（两时刻相同）")

    # 两时刻都先重判原始有效票：甲只剩一条 → 必须复尝
    for case, tag in ((opened, "已开"), (booked, "未开")):
        check(case["rejudged_means"]["甲"] is None, f"[{tag}] 重判后甲需复尝")
        check(case["rejudged_means"]["乙"] == Fraction(2), f"[{tag}] 重判后乙均值 2")
        check(case["needs_retaste"] == ["甲"], f"[{tag}] 只有甲需复尝")

    # 时刻①：唯一留样已开且与回避席同场 → 作废、无法补尝
    check(len(opened["void_retastes"]) == 1, "[已开] 与回避席同场的复尝作废")
    check(opened["new_retaste_plan"]["ok"] is False, "[已开] 无法再排有效复尝")
    check("甲" in opened["uncompensable"], "[已开] 甲确已无样，标明无法补尝")
    check("无法收回" in opened["uncompensable"]["甲"], "[已开] 说明留样已吃掉无法收回")

    # 时刻②：只订座未开（同样 1 个预约）→ 可改派到干净机位 S2
    check(booked["void_retastes"] == [], "[未开] 没有已开结果需作废")
    check(booked["new_retaste_plan"]["ok"] is True, "[未开] 甲可改派有效复尝")
    slots = {b["slot_id"] for b in booked["new_retaste_plan"]["bookings"]}
    check(slots == {"S2"}, "[未开] 甲的实际复尝去向是干净机位 S2")
    check(booked["uncompensable"] == {}, "[未开] 无无法补尝项")

    # 同样的旧名次和预约数，去向却不同
    check(opened["new_retaste_plan"]["bookings"] != booked["new_retaste_plan"]["bookings"],
          "两时刻实际复尝去向不同（空 vs S2）")


# ==================================== 一格双席、两样同标签且均在场，一格接齐
def test_single_slot_two_seats_same_label():
    slot = Slot("R1", session=2, time=20, capacity=2, label="X")
    jia = Sample("s_jia", "甲", label="X", window=(0, 50), last_session=2, count=1)
    yi = Sample("s_yi", "乙", label="X", window=(0, 50), last_session=2, count=1)
    plan = plan_retaste(["甲", "乙"], [slot], [jia, yi])
    check(plan["ok"] and plan["slot_count"] == 1, "一格双席同标签可接齐两家")
    check(len(plan["bookings"]) == 2, "同一格放下两份试样")

    # 标签不同 → 不能同桌
    yi_bad = Sample("s_yi", "乙", label="Y", window=(0, 50), last_session=2, count=1)
    plan2 = plan_retaste(["甲", "乙"], [slot], [jia, yi_bad])
    check(plan2["ok"] is False, "隔离标签不同的两家不能同桌")
    check("乙" in plan2["missing"], "缺因指出乙（标签不符）")

    # 一份缺样
    plan3 = plan_retaste(["甲", "乙"], [slot], [jia])
    check(plan3["ok"] is False and plan3["missing"] == ["乙"],
          "缺乙留样时排不下并指出缺乙")


def test_sample_window_and_last_session():
    # 试样只在试吃时窗到场，且受最后可开班次限制
    slot_late = Slot("S_late", session=5, time=60, capacity=1, label="X")
    slot_early = Slot("S_early", session=1, time=5, capacity=1, label="X")
    s = Sample("s", "甲", label="X", window=(0, 30), last_session=2, count=1)
    check(not s.available_at(slot_late), "超最后可开班次/时窗 ⇒ 不可用")
    check(s.available_at(slot_early), "时窗内且班次允许 ⇒ 可用")
    plan = plan_retaste(["甲"], [slot_late, slot_early], [s])
    check(plan["ok"] and plan["bookings"][0]["slot_id"] == "S_early",
          "只能在符合时窗与班次的机位复尝")

    # 份数为 0 视为无样
    s0 = Sample("s0", "甲", label="X", window=(0, 30), last_session=2, count=0)
    check(not s0.available_at(slot_early), "份数 0 ⇒ 不可用")


# ==================================== 直接颁章 vs 候选/支持补卡
def test_award_only_if_same_strict_winner():
    # 两份补卡都严格选出甲 → 直接颁章
    cards = [
        {"card": {"a": 1}, "means": {"甲": Fraction(9), "乙": Fraction(4)}},
        {"card": {"a": 2}, "means": {"甲": Fraction(7), "乙": Fraction(3)}},
    ]
    d = decide_award(cards)
    check(d["decision"] == "award" and d["winner"] == "甲",
          "每份补卡都严格选出同一最高者 → 直接颁章")
    check(d["retaste_required"] is False, "直接颁章无需复尝")

    # 一份甲赢、一份乙赢 → 不能颁章，列出夺章者与各自支持补卡
    cards2 = [
        {"card": {"a": 1}, "means": {"甲": Fraction(9), "乙": Fraction(4)}},
        {"card": {"a": 2}, "means": {"甲": Fraction(3), "乙": Fraction(8)}},
    ]
    d2 = decide_award(cards2)
    check(d2["decision"] == "no_award", "不同补卡不同赢家 → 不能直接颁章")
    check(set(d2["contesting"]) == {"甲", "乙"}, "列出两家夺章者")
    sup = supporting_cards(d2, cards2)
    check(sup["甲"] == [{"a": 1}], "甲真正支持的补卡是 a=1")
    check(sup["乙"] == [{"a": 2}], "乙真正支持的补卡是 a=2")

    # 并列最高 → 不能颁章，两家都是候选
    cards3 = [
        {"card": {"a": 1}, "means": {"甲": Fraction(5), "乙": Fraction(5)}},
    ]
    d3 = decide_award(cards3)
    check(d3["decision"] == "no_award" and set(d3["contesting"]) == {"甲", "乙"},
          "并列最高不能颁章，双方均为候选")
    # 并列卡不算对任何一家的“严格支持”
    check(supporting_cards(d3, cards3) == {"甲": [], "乙": []},
          "并列补卡不构成任何一家的严格支持")

    # 有补卡缺有效均值（需重尝）→ 不能直接颁章
    cards4 = [
        {"card": {"a": 1}, "means": {"甲": Fraction(9), "乙": Fraction(4)}},
        {"card": {"a": 2}, "means": {"甲": None, "乙": Fraction(4)}},
    ]
    d4 = decide_award(cards4)
    check(d4["decision"] == "no_award", "某补卡存在需重尝家 ⇒ 不能直接颁章")
    check("甲" in d4["contesting"], "需求重尝的甲进入候选")


# ==================================== 卡系统：矛盾定位 / 同表核对 / 整数盒
def test_card_contradiction_and_same_table():
    sys = CardSystem()
    sys.add_var("s", 0, 10)
    sys.add_record("row_sum", [("s", 1)], 7, source=["card#7 行合计"])
    sys.add_record("other_row", [("s", 1)], 2, source=["card#9 换桌互证"])
    sol = sys.solve()
    check(sol.status == "inconsistent", "同一格两条不同记录 ⇒ 矛盾")
    cert = sol.contradictions[0]
    check(set(cert["conflicting_records"]) == {"row_sum", "other_row"},
          "证书指出哪两条记录不能同时使用")
    check(set(cert["conflicting_sources"]) == {"card#7 行合计", "card#9 换桌互证"},
          "证书回溯到原始卡来源")

    # 同表核对：两份补卡能否出自同一桌
    sys2 = CardSystem()
    sys2.add_var("a", 0, 5)
    sys2.add_var("b", 0, 5)
    sys2.add_record("sum", [("a", 1), ("b", 1)], 6)
    ok, reason, witness = sys2.same_table({"a": 2}, {"b": 4})
    check(ok, "a=2,b=4 合计 6 ⇒ 可同出一桌")
    check(witness == {"a": 2, "b": 4}, "给出可代回的联合补卡见证")
    ok2, reason2, w2 = sys2.same_table({"a": 2}, {"b": 5})
    check(not ok2, "a=2,b=5 合计 7 ≠ 6 ⇒ 不能同出一桌")
    ok3, reason3, w3 = sys2.same_table({"a": 2}, {"a": 3})
    check(not ok3, "同一格两值冲突 ⇒ 不能同出一桌")


def test_integer_box():
    sys = CardSystem()
    sys.add_var("z", 0, 2)
    sys.add_record("z_eq", [("z", 1)], 5)  # z=5 超出 [0,2]
    sol = sys.solve()
    check(sol.status == "determined", "方程唯一解")
    check(not sol.integer_ok, "唯一解超出整数盒 ⇒ 无可行整桌补卡")
    check("超出整数盒" in sol.integer_reason, "说明超出题定整数范围")

    try:
        CardSystem().add_var("bad", 3, 1)
        check(False, "下界大于上界必须拒绝")
    except CardError:
        check(True, "下界大于上界被拒绝")


def test_permanent_cell_links_cards():
    # 同一底分 v 被两张卡引用：永久格号连接，不会各存一份
    sys = CardSystem()
    sys.add_var("v", 1, 9)
    sys.add_record("cardA_row", [("v", 1)], 4, source=["cardA"])
    sys.add_record("cardB_row", [("v", 1)], 4, source=["cardB"])
    sol = sys.solve()
    check(sol.status == "determined", "两卡对同一永久格 v 的约束一致")
    check(sol.integer_cards == [{"v": 4}], "永久格号把多卡连接成同一个 v=4")
    # 若两卡不同值 → 矛盾
    sys2 = CardSystem()
    sys2.add_var("v", 1, 9)
    sys2.add_record("cardA_row", [("v", 1)], 4)
    sys2.add_record("cardB_row", [("v", 1)], 5)
    check(sys2.solve().status == "inconsistent",
          "同一永久格在两卡给不同值 ⇒ 矛盾（不能各存一份）")


def test_row_column_seen_crosscheck_records():
    # 读卡岗把行列合计、已看清单格、换桌记录编入同一快照
    sys = CardSystem()
    sys.add_var("a", 0, 9)
    sys.add_var("b", 0, 9)
    sys.add_var("c", 0, 9)
    sys.add_row_sum("row1", [("a", 1), ("b", 1)], 5, source=["card1 行合计"])
    sys.add_column_sum("col1", [("b", 1), ("c", 1)], 7, source=["card1 列合计"])
    sys.add_seen_cell("seen_c", "c", 4, source=["card1 已看清 c"])
    sol = sys.solve()
    check(sol.status == "determined", "行列合计+单格足以唯一确定")
    check(sol.integer_cards == [{"a": 2, "b": 3, "c": 4}],
          "解出 a=2,b=3,c=4（行合计5、列合计7、c=4）")
    check(len(sys.records_by_kind("row_sum")) == 1, "行合计记录带独立标签")
    check(len(sys.records_by_kind("column_sum")) == 1, "列合计记录带独立标签")
    check(len(sys.records_by_kind("seen_cell")) == 1, "已看清单格记录带独立标签")

    # 换桌互证 a - b = -1（即 b = a+1）
    sys2 = CardSystem()
    sys2.add_var("a", 0, 9)
    sys2.add_var("b", 0, 9)
    sys2.add_seen_cell("seen_a", "a", 3)
    sys2.add_cross_check("cross", "b", "a", offset=1, source=["换桌记录"])
    sol2 = sys2.solve()
    check(sol2.integer_cards == [{"a": 3, "b": 4}], "换桌互证 b=a+1 ⇒ b=4")
    check(len(sys2.records_by_kind("cross_check")) == 1, "换桌记录带独立标签")


def test_bounds_not_a_card_to_build_table():
    # 有自由度时不得把各格上下界随意拼成一张；build_table 给出真正可代回的一致补卡
    sys = CardSystem()
    sys.add_var("p", 0, 5)
    sys.add_var("q", 0, 5)
    sys.add_record("pq", [("p", 1), ("q", 1)], 6)
    bounds = sys.bounds_only()
    check(bounds == {"p": (0, 5), "q": (0, 5)}, "独立上下界仅用于审计，不构成补卡")
    card = sys.build_table()
    check(card["p"] + card["q"] == 6, "build_table 给出一致整桌补卡（p+q=6）")
    check(0 <= card["p"] <= 5 and 0 <= card["q"] <= 5, "补卡落在整数盒内")
    # 钉住 p=2 → 必须给 q=4，而不是各取边界
    card2 = sys.build_table(pinned={"p": 2})
    check(card2 == {"p": 2, "q": 4}, "钉住 p=2 时联立得 q=4")
    # 与记录冲突的钉住 → 拒绝（不能拼凑）
    try:
        sys.build_table(pinned={"p": 1, "q": 1})
        check(False, "p=1,q=1 与 p+q=6 冲突，应拒绝")
    except CardError:
        check(True, "冲突钉住被拒绝，不会拼出假补卡")


def test_c1_single_slot_retaste_both_suppliers():
    # C1 结论：不能直接颁章，两家都须复尝；假设可用一格双席、两样同标签且均在场，一格接齐
    sol, rows = c1_outcomes()
    cards_with_means = [{"card": r["card"], "means": {"甲": r["jia"], "乙": r["yi"]}}
                        for r in rows]
    d = decide_award(cards_with_means)
    check(d["decision"] == "no_award", "C1 不能直接颁章")
    slot = Slot("R1", session=2, time=20, capacity=2, label="X")
    jia = Sample("s_jia", "甲", label="X", window=(0, 50), last_session=2, count=1)
    yi = Sample("s_yi", "乙", label="X", window=(0, 50), last_session=2, count=1)
    plan = plan_retaste(d["contesting"], [slot], [jia, yi])
    check(plan["ok"] and plan["slot_count"] == 1, "C1 两家各一份留样同标签，一格双席接齐")
    check({b["supplier"] for b in plan["bookings"]} == {"甲", "乙"}, "一格同时接甲乙")


# ==================================== 交付办法 A / B 都可运行
def test_delivery_both_schemes():
    supplies = [{
        "supplier": "甲", "sample_id": "s1", "label": "X",
        "window": (0, 50), "last_session": 5, "count": 1,
    }]
    slot_ok = Slot("S", session=2, time=20, capacity=1, label="X")

    # 办法 A：分交班=1，保鲜期=1 场 ⇒ 最晚班次 min(5, 2)=2，S(session2) 可用
    a = deliver(PRE_DISTRIBUTED, supplies, session=1, shelf_life=1)
    plan_a = plan_retaste(["甲"], [slot_ok], a)
    check(plan_a["ok"], "办法 A：保鲜期内可复尝")

    # 办法 A：保鲜期=0 ⇒ 最晚班次 1，S(session2) 不可用
    a0 = deliver(PRE_DISTRIBUTED, supplies, session=1, shelf_life=0)
    plan_a0 = plan_retaste(["甲"], [slot_ok], a0)
    check(not plan_a0["ok"], "办法 A：超保鲜期无法复尝")

    # 办法 B：t0=0, 运输=15 ⇒ 到场不早于 15，S(time20) 可用
    b = deliver(CENTRAL_DISPATCH, supplies, dispatch_time=0, transit=15)
    plan_b = plan_retaste(["甲"], [slot_ok], b)
    check(plan_b["ok"], "办法 B：运输后到时窗内可复尝")

    # 办法 B：运输=25 ⇒ 到场 25 已过时窗(50? 25<50 可用)…改时窗窄
    narrow = [{
        "supplier": "甲", "sample_id": "s1", "label": "X",
        "window": (0, 18), "last_session": 5, "count": 1,
    }]
    b_slow = deliver(CENTRAL_DISPATCH, narrow, dispatch_time=0, transit=25)
    plan_bs = plan_retaste(["甲"], [slot_ok], b_slow)
    check(not plan_bs["ok"], "办法 B：运输过长错过时窗 ⇒ 不可用")


# ==================================== 申诉：先重判再排复尝、同场作废
def test_appeal_rejudge_before_retaste():
    # 三席，甲只得两个分，第三席也给了甲一个高分；该席迟来被确认回避
    scores = {
        ("J1", "甲"): 4, ("J3", "甲"): 10,
        ("J1", "乙"): 3, ("J2", "乙"): 3, ("J3", "乙"): 3,
    }
    session = Session("s1", ["J1", "J2", "J3"], ["甲", "乙"], scores)
    # 回避 J3：甲只剩 J1=4 一条 → 必须复尝；乙剩 J1,J2 两条 → 均值 3
    clean_slot = Slot("S2", session=2, time=20, capacity=1, label="X")
    sample = Sample("甲1", "甲", label="X", window=(0, 100), last_session=3, count=1)
    booking = Booking("甲", "甲1", "booked", slot_session=1, slot_time=10,
                      slot_label="X", with_recused_judge=True)
    res = resolve_delayed_recusal(
        session, "J3", existing_bookings=[booking],
        available_slots=[clean_slot], samples_by_supplier={"甲": [sample]},
    )
    check(res["rejudged_means"]["甲"] is None, "先重判：甲需复尝")
    check(res["rejudged_means"]["乙"] == Fraction(3), "先重判：乙均值 3")
    check(res["new_retaste_plan"]["ok"], "再为仍有资格的甲排有效复尝")
    # 若已开且同场 → 作废
    opened = Booking("甲", "甲1", "opened", slot_session=1, slot_time=10,
                     slot_label="X", with_recused_judge=True)
    res2 = resolve_delayed_recusal(
        session, "J3", existing_bookings=[opened],
        available_slots=[clean_slot], samples_by_supplier={"甲": [sample]},
    )
    check(res2["void_retastes"], "同场开出的复尝作废")
    check("甲" in res2["uncompensable"], "留下已吃掉的样无法收回")


# ============================================================ 场次上限
def test_pending_appeal_consumption_policy():
    # 申诉未决时只有保管台能真正耗掉留样
    ok, reason = who_may_consume(pending=True, actor="custodian")
    check(ok and "保管台" in reason, "未决期保管台可开封耗样（留痕）")
    ok2, reason2 = who_may_consume(pending=True, actor="judge")
    check(not ok2, "未决期评委不能开封耗样")
    ok3, reason3 = who_may_consume(pending=True, actor="supplier")
    check(not ok3, "未决期供应社不能开封耗样")
    ok4, _ = who_may_consume(pending=False, actor="judge")
    check(ok4, "申诉已决后按常规开样")
    check(PENDING_POLICY["may_consume_while_pending"] == {"custodian"},
          "政策记录唯一可耗样角色")

    # 缺样时判定可履行性
    slot = Slot("S", session=2, time=20, capacity=1, label="X")
    s = Sample("s1", "甲", label="X", window=(0, 50), last_session=3, count=1)
    check(viability("甲", [s], [slot])["status"] == "viable", "有样可排 ⇒ viable")
    check(viability("乙", [s], [slot])["status"] == "no_sample", "无样 ⇒ no_sample")
    # 唯一留样已耗（burned）→ no_sample，无法收回
    v = viability("甲", [s], [slot], burned_ids={"s1"})
    check(v["status"] == "no_sample" and "无法收回" in v["detail"],
          "唯一留样已耗 ⇒ no_sample 且说明无法收回")
    # 有样但标签不符 ⇒ unusable
    s_bad = Sample("s2", "甲", label="Y", window=(0, 50), last_session=3, count=1)
    check(viability("甲", [s_bad], [slot])["status"] == "unusable",
          "标签/时窗不符 ⇒ unusable")


def test_delayed_recusal_viability_reported():
    dr = delayed_recusal_two_timestamps()
    check(dr["case_opened"]["viability"]["甲"]["status"] == "no_sample",
          "已开时刻：甲的可履行性为 no_sample")
    check(dr["case_booked"]["viability"]["甲"]["status"] == "viable",
          "未开时刻：甲仍 viable，可改派 S2")


def test_session_limits():
    from baking_judge.scores import validate_session
    check(validate_session(["A", "B", "C", "D"], ["甲", "乙", "丙"]) is True,
          "四席三家合法")
    for judges, sups, label in [
        (["A", "B", "C", "D", "E"], ["甲"], "超过四席"),
        (["A"], ["甲", "乙", "丙", "丁"], "超过三家"),
        (["A", "A"], ["甲"], "重复席位"),
    ]:
        try:
            validate_session(judges, sups)
            check(False, f"{label} 应被拒绝")
        except ValueError:
            check(True, f"{label} 被拒绝")


def test_cli_subcommands():
    import json
    import subprocess
    import sys

    def run(cmd, payload):
        proc = subprocess.run(
            [sys.executable, "-m", "baking_judge.cli", cmd, "-"],
            input=json.dumps(payload), capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"{cmd} 失败：{proc.stderr}")
        return json.loads(proc.stdout)

    cs = run("card-solve", {
        "vars": [{"name": "jia_x", "lo": 0, "hi": 4},
                 {"name": "y", "lo": 1, "hi": 3}],
        "records": [{"record_id": "card_xy",
                     "terms": [["jia_x", 1], ["y", 1]], "rhs": 4,
                     "source": ["card#1: x+y=4"]}],
        "query_cell": "jia_x",
    })
    check(cs["status"] == "parametric" and cs["cell_query"]["possible_values"] == [1, 2, 3],
          "CLI card-solve 给出 C1 自由格与 3 份补卡")

    sc = run("score", {
        "suppliers": ["甲", "乙"], "weights": [1],
        "rounds": [{"judges": ["A1", "A2", "A3"], "recusals": ["A3"],
                    "scores": {"A1|甲": 0, "A2|甲": 3, "A3|甲": 4,
                               "A1|乙": 2, "A2|乙": 2, "A3|乙": 2}}],
    })
    check(sc["totals"]["甲"] == "3/2" and sc["totals"]["乙"] == "2",
          "CLI score 复算 C2：甲 3/2、乙 2")

    aw = run("award", {"cards": [
        {"card": {"a": 1}, "means": {"甲": "9", "乙": "4"}},
        {"card": {"a": 2}, "means": {"甲": "3", "乙": "8"}},
    ]})
    check(aw["decision"] == "no_award" and set(aw["contesting"]) == {"甲", "乙"},
          "CLI award 判不同补卡不同赢家 ⇒ 不颁章")

    pl = run("plan", {
        "required_suppliers": ["甲", "乙"],
        "slots": [{"slot_id": "R1", "session": 2, "time": 20,
                   "capacity": 2, "label": "X"}],
        "samples": [
            {"sample_id": "s_jia", "supplier": "甲", "label": "X",
             "window": [0, 50], "last_session": 2, "count": 1},
            {"sample_id": "s_yi", "supplier": "乙", "label": "X",
             "window": [0, 50], "last_session": 2, "count": 1}],
    })
    check(pl["ok"] and pl["slot_count"] == 1, "CLI plan 一格双席接齐")

    ap = run("appeal", {
        "session_id": "s1", "judges": ["A1", "A2", "A3"],
        "suppliers": ["甲", "乙"], "weight": 1,
        "scores": {"A1|甲": 1, "A3|甲": 9, "A1|乙": 2, "A2|乙": 2, "A3|乙": 2},
        "recused_judge": "A3",
        "bookings": [{"supplier": "甲", "sample_id": "jia_only", "state": "booked",
                      "slot_session": 1, "slot_time": 10, "slot_label": "X",
                      "with_recused_judge": True}],
        "slots": [{"slot_id": "S2", "session": 2, "time": 20,
                   "capacity": 1, "label": "X"}],
        "samples": [{"sample_id": "jia_only", "supplier": "甲", "label": "X",
                     "window": [0, 100], "last_session": 3, "count": 1}],
    })
    check(ap["rejudged_means"]["甲"] is None, "CLI appeal 先重判：甲需复尝")
    check(ap["new_retaste_plan"]["bookings"][0]["slot_id"] == "S2",
          "CLI appeal 未开时刻改派 S2")


# ============================================================ 运行
def run_all():
    tests = [
        test_trimmed_mean_rules,
        test_recusal_removes_whole_seat,
        test_retaste_when_one_or_fewer,
        test_weighted_rounds,
        test_c1_free_integer_card,
        test_c2_recuse_third_seat,
        test_two_rounds_shared_base,
        test_delayed_recusal_two_timestamps,
        test_single_slot_two_seats_same_label,
        test_sample_window_and_last_session,
        test_award_only_if_same_strict_winner,
        test_card_contradiction_and_same_table,
        test_integer_box,
        test_permanent_cell_links_cards,
        test_row_column_seen_crosscheck_records,
        test_bounds_not_a_card_to_build_table,
        test_c1_single_slot_retaste_both_suppliers,
        test_delivery_both_schemes,
        test_appeal_rejudge_before_retaste,
        test_pending_appeal_consumption_policy,
        test_delayed_recusal_viability_reported,
        test_session_limits,
        test_cli_subcommands,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
