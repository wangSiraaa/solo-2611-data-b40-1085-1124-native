"""展览地面保洁交接端到端演示（合成轨迹，不控制任何实体设备）。

回答题面问题“刷头已带进一粒展览色粉，开放前哪条观展廊还能交给领队”，并
逐样复现：

1. 四区（甲乙丙丁）两刷三场、只用红/蓝本地标签；展务台按场单次序派实物巾、
   交路线、领队签收；同块巾竞争与“后场还剩哪条路”；
2. 同一等候区接东西支廊的换道悖论：逐拍看都有干净路线，连续一拍交接却不
   成立；两份冻结实轨（西廊实际擦净时点 3 vs 进场前 2）+ 领队签收还原差别；
3. 单通道过乙、甲有红粉：原轨 + 开场前巾可交；补正为擦后回乙再够一拍则
   开场又红；原轨但巾提前借别厅也交不成；
4. 一例实轨触边未沾；一例擦去源区改变远处沾色；
5. 同时到期互沾按同一快照结算（粉不能一拍传两站）；旧版轨迹首次来源/套号
   查询与冻结报告独立复核。

运行：``python3 demo_exhibit.py``
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction as Q
from pathlib import Path

from cart_audit.models import Knot, Profile, Trajectory
from cart_audit.exhibit import (
    BrushPlan,
    Desk,
    SessionSheet,
    Wipe,
    WipeSlot,
    Zone,
    Hall,
    brush_zone_intervals,
    earliest_switch_start,
    judge_switch,
    naive_tick_routes,
    simulate,
)
from cart_audit.exhibit_store import ExhibitLibrary, build_payload, verify_exhibit


def K(t, x, y):
    return Knot(Q(t), Q(x), Q(y))


def line(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def show_decision(d: dict) -> None:
    if d["kind"] == "fixed":
        route = "→".join(d["route"]) if d.get("route") else "（无）"
        print(f"  [{d['session_id']}] {'可交' if d['ok'] else '交不成'} "
              f"禁{d['forbidden']} 窗 {d['window'][0]}~{d['window'][1]}  路线 {route}")
        for w in d.get("wipes_used", []):
            print(f"      派巾 {w['wipe_id']} 擦 {w['zone']}@{w['t']}")
        if not d["ok"]:
            print(f"      原因：{d.get('reason')}")
    else:
        if d["ok"]:
            print(f"  [{d['session_id']}] 可换道 禁{d['forbidden']}  "
                  f"旧廊 {'→'.join(d['old_route'])} / 新廊 {'→'.join(d['new_route'])}  "
                  f"交接窗 {d['handover_window'][0]}~{d['handover_window'][1]}")
            for w in d.get("wipes_used", []):
                print(f"      派巾 {w['wipe_id']} 擦 {w['zone']}@{w['t']}")
        else:
            print(f"  [{d['session_id']}] 换道不成立：{d.get('reason')}")
    print(f"      剩余实物巾：{d['remaining_wipe_ids']}")


# --------------------------------------------------------------------------- #
# 四区两刷三场
# --------------------------------------------------------------------------- #
def capstone(lib: ExhibitLibrary | None = None) -> None:
    line("一、四区·两刷·三场：刷头带进一粒红粉，开放前哪条廊还能交？")
    zones = {z: Zone(z, x, y, 1, 1, 1) for z, (x, y) in
             {"甲": (0, 0), "乙": (2, 0), "丙": (0, 2), "丁": (2, 2)}.items()}
    hall = Hall.from_data({
        "hall_id": "H4", "labels": ["红", "蓝"],
        "zones": [z.to_dict() for z in zones.values()],
        "entrance": "甲", "exhibit": "丁",
    })
    H = (Q(0), Q(12))
    p = Profile("P", Q(1, 2), Q(1, 2))
    r1 = Trajectory("刷1", "P", [K(0, 0, 0), K(2, 0, 0), K(4, 2, 0), K(6, 2, 0),
                                 K(8, 6, 0), K(12, 6, 0)], H)
    r2 = Trajectory("刷2", "P", [K(0, 0, 6), K(2, 0, 2), K(4, 0, 2), K(6, 2, 2),
                                 K(8, 2, 2), K(10, 2, 6), K(12, 2, 6)], H)
    plans = [BrushPlan("刷1", frozenset({"红"})), BrushPlan("刷2")]
    init = {"丙": ["蓝"]}

    raw = simulate(hall, H, {"刷1": r1, "刷2": r2}, {"刷1": p, "刷2": p}, plans, init)
    print("  精确推演的互沾时刻（左闭右开，达到规定时长的那一拍上色）：")
    for e in raw.events:
        if e["kind"] == "exchange_group":
            for m in e["members"]:
                print(f"    t={e['t']:>3}  {m['brush']} 与 {m['zone']} 互沾（套{m['sleeve']}）")
        elif e["kind"] == "wipe":
            print(f"    t={e['t']:>3}  巾 {e['wipe_id']} 擦 {e['zone']}")
    print("  各区首次受色来源（清扫岗旧轨查询口径）：")
    for z in "甲乙丙丁":
        for color, rec in raw.provenance[z].items():
            who = rec["source"] if rec["kind"] == "exchange" else rec["source"]
            print(f"    {z} 的{color}：t={rec['t']} 来源={who}")

    s1 = SessionSheet("场1", "红", 4, 6, slots=(WipeSlot("甲", 3), WipeSlot("乙", 4)))
    s2 = SessionSheet("场2", "蓝", 6, 9, slots=(WipeSlot("丁", 6),))
    s3 = SessionSheet("场3", "蓝", 9, 12)

    print("\n  [开放前的原始回放] 场1 窗 [4,6) 禁红，两条廊逐区整窗检查：")
    for route in (("甲", "丙", "丁"), ("甲", "乙", "丁")):
        dirty = [z for z in route if not raw.is_clean(z, "红", 4, 6)]
        print(f"    {'→'.join(route)}：{'不合格，脏区 ' + '/'.join(dirty) if dirty else '合格'}")
    print("  => 入口甲 t=1 已带红，原始轨在开放前没有任何一条廊能直接交。")

    desk = Desk(hall, H, {"刷1": r1, "刷2": r2}, {"刷1": p, "刷2": p},
                plans, init, ["巾01", "巾02"])
    print("\n  [展务台按场单次序派巾]")
    for s in (s1, s2, s3):
        show_decision(desk.run_sheet(s))
    print("  [领队签收]")
    for sid, leader in (("场1", "林领队"), ("场2", "周领队"), ("场3", "林领队")):
        rec = desk.sign_off(sid, leader)
        print(f"    {sid} {leader}：{rec['verdict']}")
    back = desk.available_routes("蓝", Q(9), Q(12))
    print("  [给后场留下的路] 场3 窗 [9,12) 禁蓝，可走：",
          ["→".join(r["route"]) for r in back] or "无")
    print("  说明：巾01 占住 甲@3 这个开放前擦净时点（同次接触不重办，甲保持干净），")
    print("        场1 以一块巾保住 甲→丙→丁；巾02 占住 丁@6，场2 走 甲→乙→丁；")
    print("        丁的洁净持续到离场，场3 不再耗巾，沿用 甲→乙→丁。")

    print("\n  [多个场次争同一块巾] 若全台只有 巾01：")
    desk_one = Desk(hall, H, {"刷1": r1, "刷2": r2}, {"刷1": p, "刷2": p},
                    plans, init, ["巾01"])
    for s in (s1, s2, s3):
        d = desk_one.run_sheet(s)
        print(f"    {s.session_id}: {'可交 ' + '→'.join(d['route']) if d['ok'] else '交不成（巾已被前场领走/无巾可派）'}")

    if lib is not None:
        payload = build_payload(
            case_id="H4", hall=hall, horizon=H,
            trajectories={"刷1": r1, "刷2": r2},
            brush_profiles={"刷1": p, "刷2": p}, brush_plans=plans,
            initial_zone_colors=init, inventory=["巾01", "巾02"], sheets=[s1, s2, s3],
            signoffs=[{"session_id": "场1", "leader": "林领队"},
                      {"session_id": "场2", "leader": "周领队"},
                      {"session_id": "场3", "leader": "林领队"}],
        )
        result = lib.publish("H4-三场", payload)
        assert result["ok"]
        assert verify_exhibit(lib, "H4-三场")["verified"]


# --------------------------------------------------------------------------- #
# 东西廊换道悖论 + 两份实轨签收
# --------------------------------------------------------------------------- #
def east_west(lib: ExhibitLibrary) -> None:
    line("二、同一等候区接东西支廊：逐拍有干净路 ≠ 能连续交接")
    zones = {
        "I": Zone("I", -2, 0, 1, 1, 1),
        "W": Zone("W", 0, 0, 1, 3, 1),
        "E": Zone("E", 2, 2, 1, 1, 1),
        "X": Zone("X", 2, -2, 1, 1, 1),
        "V": Zone("V", 4, 0, 1, 3, 1),
    }
    hall = Hall.from_data({"hall_id": "EW", "labels": ["红", "蓝"],
                           "zones": [z.to_dict() for z in zones.values()],
                           "entrance": "I", "exhibit": "V"})
    H = (Q(0), Q(6))
    r = Trajectory("RE", "P", [K(0, 2, 7), K(2, 2, Q(7, 2)), K(4, 2, 2), K(6, 2, 2)], H)
    p = Profile("P", Q(1, 2), Q(1, 2))
    old, new = ("I", "W", "E", "V"), ("I", "W", "X", "V")
    sheet = SessionSheet("EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2, handover=1)

    print("  实轨：东廊在 t=3 首次沾红（干净到第 3 拍前）；西廊原有红粉。")
    for tag, xt in (("甲轨", 3), ("乙轨", 2)):
        sim = simulate(hall, H, {"RE": r}, {"RE": p}, [BrushPlan("RE", frozenset({"红"}))],
                        {"X": ["红"]}, [Wipe("WX", "X", xt)])
        naive = naive_tick_routes(sim, hall, "红", [Q(2), Q(3)])
        verdict = judge_switch(sim, sheet, old, new, [Wipe("WX", "X", xt)])
        early = earliest_switch_start(sim, sheet, old, new, [Wipe("WX", "X", xt)])
        print(f"\n  [{tag}] 西廊实际擦净时点 t={xt}")
        for row in naive:
            routes = ["→".join(x) for x in row["routes"]]
            print(f"    朴素逐拍 t={row['t']} 瞬时干净路线：{routes or '无'}")
        print(f"    整窗交接判定：{'成立' if verdict['ok'] else '不成立'}；"
              f"最早可开始交接 s*={early}")
        if not verdict["ok"]:
            for prob in verdict["problems"]:
                print(f"      - {prob}")

    # 两份冻结实轨 + 领队签收，入报告库后可独立复核/还原差别。
    for report_id, xt, expect in (
        ("EW-甲轨", 3, "failed"),
        ("EW-乙轨", 2, "switched"),
    ):
        full = SessionSheet("EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2,
                            handover=1, old_route=old, new_route=("W", "X", "V"))
        payload = build_payload(
            case_id=report_id, hall=hall, horizon=H,
            trajectories={"RE": r}, brush_profiles={"RE": p},
            brush_plans=[BrushPlan("RE", frozenset({"红"}))],
            initial_zone_colors={"X": ["红"]}, inventory=["WX"], sheets=[full],
            actual_wipes=[Wipe("WX", "X", xt)],
            signoffs=[{"session_id": "EW", "leader": "何领队"}],
        )
        res = lib.publish(report_id, payload)
        assert res["signoffs"][0]["verdict"] == expect
        assert verify_exhibit(lib, report_id)["verified"]

    a = lib.read("EW-甲轨")
    b = lib.read("EW-乙轨")
    print("\n  [两份实轨 + 签收还原差别]")
    print(f"    甲轨 X 区时间线 {a['zone_timeline']['X']}  签收 {a['signoffs'][0]['verdict']}")
    print(f"    乙轨 X 区时间线 {b['zone_timeline']['X']}  签收 {b['signoffs'][0]['verdict']}")
    print("  => 差别只有一个截点：西廊真实擦净是第 3 拍（交接开始后才干净）还是")
    print("     第 2 拍的进场前动作（截点先擦后算 [2,3) 窗）。前者失败，后者在")
    print("     第 2 拍到第 3 拍前完成换道；交接窗内部 (2,3) 两侧均未擦巾。")


# --------------------------------------------------------------------------- #
# 单通道过乙三变体
# --------------------------------------------------------------------------- #
def jia_yi(lib: ExhibitLibrary) -> None:
    line("三、单通道经过乙，甲有红粉：原轨可交 / 回乙再染 / 巾借别厅")
    A = Zone("甲", 0, 0, 1, 1, 1)
    B = Zone("乙", 2, 0, 1, 1, 1)
    bp = Profile("P", 1, 1)

    def hall():
        return Hall("JY", ("红", "蓝"), {"甲": A, "乙": B},
                    frozenset({("甲", "乙")}), "乙", "乙")  # 交的通道只经过乙

    orig = Trajectory("R", "P", [K(0, 0, 0), K(1, 0, 0), K(2, 2, 0), K(3, 2, 0),
                                 K(5, 6, 0), K(6, 6, 0)], (Q(0), Q(6)))
    sheet = SessionSheet("JY", "红", 4, 6, slots=(WipeSlot("乙", 3),))

    print("  [变体1 原轨 + 开场前巾] 刷在甲 t=1 够拍取红，在乙 t=2 够拍传红。")
    desk = Desk(hall(), (Q(0), Q(6)), {"R": orig}, {"R": bp}, [BrushPlan("R")],
                {"甲": ["红"]}, ["巾乙"])
    d = desk.run_sheet(sheet)
    show_decision(d)
    rec = desk.sign_off("JY", "赵领队")
    print(f"      签收：{rec['verdict']}")
    assert d["ok"] and rec["signed"]

    print("  [变体2 巾提前借给别厅] 同一块实物号已被外厅消费，本台无巾可派。")
    desk2 = Desk(hall(), (Q(0), Q(6)), {"R": orig}, {"R": bp}, [BrushPlan("R")],
                 {"甲": ["红"]}, ["巾乙"],
                 external_consumed=[Wipe("巾乙", "别厅", 2)])
    d2 = desk2.run_sheet(sheet)
    rec2 = desk2.sign_off("JY", "赵领队")
    print(f"      判定：{'可交' if d2['ok'] else '交不成'}；签收：{rec2['verdict']}")
    assert not d2["ok"] and not rec2["signed"]

    print("  [变体3 补正轨迹：擦后该刷再回乙且够 1 拍]")
    corr = Trajectory("R", "P", [
        K(0, 0, 0), K(1, 0, 0), K(2, 2, 0), K(3, 2, 0), K(5, 6, 0), K(6, 6, 0),
        K(7, 2, 0), K(8, 2, 0), K(9, 6, 0), K(10, 6, 0)], (Q(0), Q(10)))
    sim = simulate(hall(), (Q(0), Q(10)), {"R": corr}, {"R": bp}, [BrushPlan("R")],
                    {"甲": ["红"]}, [Wipe("巾乙", "乙", 3)])
    re_events = [e["t"] for e in sim.events if e["kind"] == "exchange_group"]
    print(f"      互沾时刻 {re_events}：第一次乙@2，擦@3，重进后的新 episode 在 15/2 再沾。")
    print(f"      开场 t=8 时乙带红：{'红' in sim.colors_at('乙', 8)}")
    sheet3 = SessionSheet("JY3", "红", 8, 10, slots=(WipeSlot("乙", 3),))
    desk3 = Desk(hall(), (Q(0), Q(10)), {"R": corr}, {"R": bp}, [BrushPlan("R")],
                 {"甲": ["红"]}, ["巾乙"])
    d3 = desk3.run_sheet(sheet3)
    rec3 = desk3.sign_off("JY3", "赵领队")
    print(f"      判定：{'可交' if d3['ok'] else '交不成（开场又红）'}；签收：{rec3['verdict']}")
    assert not d3["ok"] and not rec3["signed"]

    payload = build_payload(
        case_id="JY-OK", hall=hall(), horizon=(Q(0), Q(6)),
        trajectories={"R": orig}, brush_profiles={"R": bp},
        brush_plans=[BrushPlan("R")], initial_zone_colors={"甲": ["红"]},
        inventory=["巾乙"], sheets=[sheet],
        signoffs=[{"session_id": "JY", "leader": "赵领队"}],
    )
    assert lib.publish("JY-原轨可交", payload)["ok"]


# --------------------------------------------------------------------------- #
# 触边未沾 + 擦源改远 + 同时互沾原子性
# --------------------------------------------------------------------------- #
def edge_and_source() -> None:
    line("四、两个点名实轨例：触边未沾 / 擦去源区改变远处沾色")
    z = Zone("Z", 0, 0, 1, 1, 1)
    hall = Hall("EDGE", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
    H = (Q(0), Q(10))
    e = Trajectory("E", "P", [K(0, 6, 0), K(5, 2, 0), K(10, 6, 0)], H)
    bp = Profile("P", 1, 1)  # 半宽和=2；t=5 中心距恰为 2
    iv = brush_zone_intervals(e, bp, z)
    sim = simulate(hall, H, {"E": e}, {"E": bp}, [BrushPlan("E")], {"Z": ["红"]})
    print(f"  [触边未沾] 实轨在 t=5 恰好贴边，正面积接触区间={iv}（空），"
          f"互沾事件数={len([x for x in sim.events if x['kind'] == 'exchange_group'])}，"
          f"刷始终未取色。")
    assert iv == []

    A, B, C = Zone("甲", 0, 0, 1, 1, 1), Zone("乙", 2, 0, 1, 1, 1), Zone("丙", 4, 0, 1, 1, 1)
    hd = Hall("FAR", ("红", "蓝"), {"甲": A, "乙": B, "丙": C},
              frozenset({("甲", "乙"), ("乙", "丙")}), "甲", "丙")
    Hd = (Q(0), Q(8))
    r = Trajectory("R", "P", [K(0, 0, 0), K(1, 0, 0), K(3, 4, 0), K(4, 4, 0),
                              K(6, 8, 0), K(8, 8, 0)], Hd)
    raw = simulate(hd, Hd, {"R": r}, {"R": bp}, [BrushPlan("R")], {"甲": ["红"]})
    wiped = simulate(hd, Hd, {"R": r}, {"R": bp}, [BrushPlan("R")], {"甲": ["红"]},
                     [Wipe("WSRC", "甲", Q(1, 2))])
    print("  [擦源改远] 不擦：互沾时刻 甲@1 → 乙@2 → 丙@3，丙 t=4 带红 = "
          f"{'红' in raw.colors_at('丙', 4)}")
    print(f"             在甲@1/2（到期前）擦源：刷全程色袋="
          f"{[set(c) for _, c, _ in wiped.brush_timeline['R']]}，"
          f"丙 t=4 带红 = {'红' in wiped.colors_at('丙', 4)}")
    assert "红" in raw.colors_at("丙", 4) and "红" not in wiped.colors_at("丙", 4)

    line("五、同时数笔互沾：全部从同一快照结算")
    a, b = Zone("A", -1, 0, 1, 1, 1), Zone("B", 1, 0, 1, 1, 1)
    h2 = Hall("ATOM", ("红", "蓝"), {"A": a, "B": b}, frozenset({("A", "B")}), "A", "B")
    H2 = (Q(0), Q(4))
    rr = Trajectory("R", "P", [K(0, 0, 0), K(4, 0, 0)], H2)
    p2 = Profile("P", Q(1, 2), Q(1, 2))
    atom = simulate(h2, H2, {"R": rr}, {"R": p2}, [BrushPlan("R")],
                     {"A": ["红"], "B": ["蓝"]})
    print("  t=1 刷同时与 A、B 到期：A 仍只有", set(atom.colors_at("A", 2)),
          "，B 仍只有", set(atom.colors_at("B", 2)),
          "；刷一次拿到", [set(c) for _, c, _ in atom.brush_timeline["R"] if c][-1])
    print("  （若按循环顺序逐笔即时结算，B 会在同一拍收到 A 的红——本实现禁止。）")
    assert set(atom.colors_at("A", 2)) == {"红"}
    assert set(atom.colors_at("B", 2)) == {"蓝"}


def main() -> None:
    line("展览地面保洁交接演示（全部精确有理数，无采样、无浮点）")
    db_path = Path(__file__).parent / "cart_audit_exhibit_demo.sqlite3"
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(db_path))
    lib = ExhibitLibrary(conn=conn)
    try:
        capstone(lib)
        east_west(lib)
        jia_yi(lib)
        edge_and_source()
        line("六、冻结报告与旧版轨迹来源查询")
        for rid in lib.list_reports():
            v = verify_exhibit(lib, rid)
            print(f"  报告 {rid}：verified={v['verified']} ok={v['ok']}")
        prov = lib.provenance("H4-三场")
        print("  清扫岗读 H4-三场 旧轨：乙的红首次来源 =", prov["乙"]["红"]["source"],
              "；丁的蓝 =", prov["丁"]["蓝"]["source"])
        print(f"\n  冻结库：{db_path}")
        print("  全部演示完成，断言通过。")
    finally:
        lib.close()
        conn.close()


if __name__ == "__main__":
    main()
