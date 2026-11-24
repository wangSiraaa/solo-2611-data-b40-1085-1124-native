"""可复跑演示：把 Part C 规定例逐条算给看。

运行：python3 -m tests.demo_baking_judge
"""

from fractions import Fraction

from baking_judge.award import decide_award, supporting_cards
from baking_judge.delivery import CENTRAL_DISPATCH, PRE_DISTRIBUTED, deliver
from baking_judge.scheduling import Sample, Slot
from baking_judge.scenarios import (
    c1_free_integer_card,
    c1_outcomes,
    c2_recuse_third_seat,
    delayed_recusal_two_timestamps,
    two_rounds_shared_base,
)


def _fmt(frac):
    if frac is None:
        return "需复尝"
    if isinstance(frac, Fraction) and frac.denominator == 1:
        return str(frac.numerator)
    return str(frac)


def main():
    print("=" * 66)
    print("C1  甲三席 0、x、4；乙三席均 2。卡 x+y=4，y 自由取 {1,2,3}")
    print("=" * 66)
    sys = c1_free_integer_card()
    sol = sys.solve()
    print(f"卡状态：{sol.status}；{sol.integer_reason}")
    print(f"单查 x（jia_x）：{sys.query_cell('jia_x')}")
    print(f"单查 y        ：{sys.query_cell('y')}")
    _, rows = c1_outcomes()
    for r in rows:
        print(f"  y={r['y']} x={r['x']}: 甲中分={_fmt(r['jia'])} 乙={_fmt(r['yi'])}"
              f"  → {r['verdict']}")
    cards = [{"card": r["card"], "means": {"甲": r["jia"], "乙": r["yi"]}} for r in rows]
    d = decide_award(cards)
    print(f"颁章判定：{d['decision']}（{d['reason']}）")
    print(f"夺章/需复尝候选：{d['contesting']}")
    sup = supporting_cards(d, cards)
    for s, cs in sup.items():
        print(f"  真正支持 {s} 的整桌补卡：{cs}")
    slot = Slot("R1", session=2, time=20, capacity=2, label="X")
    from baking_judge import plan_retaste
    jia = Sample("s_jia", "甲", label="X", window=(0, 50), last_session=2, count=1)
    yi = Sample("s_yi", "乙", label="X", window=(0, 50), last_session=2, count=1)
    plan = plan_retaste(d["contesting"], [slot], [jia, yi])
    print(f"复尝排位：一格双席、两样同标签均在场 → {plan['slot_count']} 格接齐"
          f"（{plan['reason']}）")

    print()
    print("=" * 66)
    print("C2  保持原卡（y=1 ⇒ x=3），仅让第三席回避")
    print("=" * 66)
    r = c2_recuse_third_seat()
    for s, e in r.items():
        print(f"  {s}: 有效分 {list(map(_fmt, e['qualified_scores']))}"
              f" → 均值 {_fmt(e['mean'])}")
    print("  结论：甲平均 1.5、乙 2，乙严格高 ⇒ 乙领章")

    print()
    print("=" * 66)
    print("两轮：各场单独可能领先，但共用同一永久底分 v 时不能同时成立")
    print("=" * 66)
    tr = two_rounds_shared_base()
    r1 = {s: _fmt(e["mean"]) for s, e in tr["solo_hi"]["means"].items()}
    r2 = {s: _fmt(e["mean"]) for s, e in tr["solo_lo"]["means"].items()}
    print(f"  第一轮单独：{tr['solo_hi']['solution'].status}，"
          f"补卡 {tr['solo_hi']['solution'].integer_cards}，均值 {r1}，"
          f"领先 {tr['solo_hi']['leader']}")
    print(f"  第二轮单独：{tr['solo_lo']['solution'].status}，"
          f"补卡 {tr['solo_lo']['solution'].integer_cards}，均值 {r2}，"
          f"领先 {tr['solo_lo']['leader']}")
    j = tr["joint"]
    print(f"  合并同一底分 v：{j.status}")
    for c in j.contradictions:
        print(f"    证书 0 = {c['residual_rhs']}；不能同时使用的记录 "
              f"{c['conflicting_records']}；来源 {c['conflicting_sources']}")

    print()
    print("=" * 66)
    print("迟来回避：同一席，落在两个时刻，同样的旧名次与预约数")
    print("=" * 66)
    dr = delayed_recusal_two_timestamps()
    print(f"  旧名次：{ {s: _fmt(v) for s, v in dr['old_ranking'].items()} }")
    for key, title in (("case_opened", "时刻① 甲唯一留样已开（与回避席同场）"),
                       ("case_booked", "时刻② 甲只订座仍未开")):
        c = dr[key]
        print(f"  {title}")
        print(f"    先重判原始有效票：甲={_fmt(c['rejudged_means']['甲'])}，"
              f"乙={_fmt(c['rejudged_means']['乙'])}；需复尝 {c['needs_retaste']}")
        print(f"    作废的同场复尝：{len(c['void_retastes'])} 条")
        plan = c["new_retaste_plan"]
        if plan and plan["ok"]:
            slots = [b["slot_id"] for b in plan["bookings"]]
            print(f"    实际复尝去向：{slots}（有效）")
        else:
            print(f"    实际复尝去向：无；无法补尝 {list(c['uncompensable'])}")
            for s, why in c["uncompensable"].items():
                print(f"      - {s}：{why}")
        print(f"    可履行性：{c['viability']}")

    print()
    print("=" * 66)
    print("留样交付：两套任均可运行（备赛分交 / 保管台临发）")
    print("=" * 66)
    supplies = [{"supplier": "甲", "sample_id": "s1", "label": "X",
                 "window": (0, 50), "last_session": 5, "count": 1}]
    slot = Slot("S", session=2, time=20, capacity=1, label="X")
    a = deliver(PRE_DISTRIBUTED, supplies, session=1, shelf_life=1)
    print(f"  办法 A 备赛分交（保鲜期1）：可排={plan_retaste(['甲'], [slot], a)['ok']}")
    b = deliver(CENTRAL_DISPATCH, supplies, dispatch_time=0, transit=15)
    print(f"  办法 B 保管台临发（运输1班）：可排={plan_retaste(['甲'], [slot], b)['ok']}")

    print("\n演示结束。")


if __name__ == "__main__":
    main()
