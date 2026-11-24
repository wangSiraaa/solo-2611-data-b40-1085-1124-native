"""展览保洁交接的契约测试。

覆盖题目点名的每条规则：只贴边不沾、同次接触只办一次、重进重计、跨断点
接着算、同时互沾按同一快照结算、换套清刷并截断、巾号全局一次、左闭右开
整窗合格、东西廊换道悖论、巾资源竞争、签收失败留痕，以及冻结报告幂等/
异文拒绝/独立复核与旧版轨迹首次来源查询。
"""

from __future__ import annotations

import sqlite3
import unittest
from fractions import Fraction

from cart_audit.models import Knot, Profile, Trajectory
from cart_audit.exhibit import (
    BrushPlan,
    Desk,
    Hall,
    SessionSheet,
    Wipe,
    WipeSlot,
    Zone,
    brush_zone_intervals,
    earliest_switch_start,
    judge_switch,
    naive_tick_routes,
    simulate,
)
from cart_audit.exhibit_store import (
    ExhibitLibrary,
    ExhibitRejected,
    build_payload,
    verify_exhibit,
)

Q = Fraction
H10 = (Q(0), Q(10))


def k(t, x, y):
    return Knot(Q(t), Q(x), Q(y))


def traj(cid, points, horizon=H10, pid="P"):
    return Trajectory(cid, pid, [k(*p) for p in points], horizon)


def zone(zid, x=0, y=0, w=1, h=1, req=1):
    return Zone(zid, Q(x), Q(y), Q(w), Q(h), Q(req))


def prof(pid="P", w=1, h=1):
    return Profile(pid, Q(w), Q(h))


def two_zone_hall(a=None, b=None, entrance="A", exhibit="B"):
    a = a or zone("A", -1, 0)
    b = b or zone("B", 1, 0)
    return Hall("h", ("红", "蓝"), {"A": a, "B": b}, frozenset({("A", "B")}), entrance, exhibit)


class ContactRuleTests(unittest.TestCase):
    def test_edge_touch_has_no_interval_and_no_transfer(self):
        # 刷轮廓半宽 1 + 区半宽 1 = 2；t=5 中心距恰为 2，只碰一条边。
        z = zone("Z", 0, 0)
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        t = traj("E", [(0, 6, 0), (5, 2, 0), (10, 6, 0)])
        self.assertEqual(brush_zone_intervals(t, prof(w=1, h=1), z), [])
        sim = simulate(hall, H10, {"E": t}, {"E": prof(w=1, h=1)},
                        [BrushPlan("E")], {"Z": ["红"]})
        # 区里的红粉还在，但刷一次互沾都没发生，刷始终空色。
        self.assertIn("红", sim.colors_at("Z", Q(5)))
        self.assertTrue(all(c == frozenset() for _, c, _ in sim.brush_timeline["E"]))
        self.assertEqual([e for e in sim.events if e["kind"] == "exchange_group"], [])

    def test_threshold_fires_once_even_if_contact_lasts(self):
        z = zone("Z", 0, 0, req=1)
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        t = traj("R", [(0, 0, 0), (10, 0, 0)])  # 全程停在区内
        sim = simulate(hall, H10, {"R": t}, {"R": prof(w=Q(1, 2), h=Q(1, 2))},
                        [BrushPlan("R", frozenset({"红"}))])
        groups = [e for e in sim.events if e["kind"] == "exchange_group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["t"], "1")
        # 来源只记第一次。
        self.assertEqual(sim.provenance["Z"]["红"]["source"], "R#套1")

    def test_exact_required_duration_at_episode_end_still_counts(self):
        # 静止在区内整个 [0,2)，规定时长恰为 2：tau==right 也算达到。
        H = (Q(0), Q(2))
        z = zone("Z", 0, 0, req=2)
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        t = traj("R", [(0, 0, 0), (2, 0, 0)], horizon=H)
        sim = simulate(hall, H, {"R": t}, {"R": prof(w=Q(1, 2), h=Q(1, 2))},
                        [BrushPlan("R")], {"Z": ["红"]})
        groups = [e for e in sim.events if e["kind"] == "exchange_group"]
        self.assertEqual([g["t"] for g in groups], ["2"])

    def test_reentry_restarts_count_and_retransfers_after_wipe(self):
        # 两次进入同一区，中间离开：两个 episode 各办一次。
        z = zone("Z", 0, 0, req=1)
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        # 0..2 停中心，退到 x=6，再回中心停 7..9。
        t = traj("R", [(0, 0, 0), (2, 0, 0), (4, 6, 0), (6, 6, 0),
                       (7, 0, 0), (9, 0, 0), (10, 6, 0)])
        p = prof(w=Q(1, 2), h=Q(1, 2))
        sim = simulate(hall, H10, {"R": t}, {"R": p}, [BrushPlan("R", frozenset({"红"}))])
        ts = [e["t"] for e in sim.events if e["kind"] == "exchange_group"]
        self.assertEqual(len(ts), 2, ts)
        # 第一次在 t=1；第二次进入后的到期时刻必须晚于第一次且确实再次上色。
        self.assertEqual(ts[0], "1")
        self.assertGreater(Fraction(ts[1]), Q(7))
        # 在第二次到期前擦净，到期后又变红——“擦后再回乙且够 1 拍，开场又红”。
        wipe_t = Fraction(ts[1]) - Q(1, 2)
        sim2 = simulate(hall, H10, {"R": t}, {"R": p}, [BrushPlan("R", frozenset({"红"}))],
                        wipes=[Wipe("W1", "Z", wipe_t)])
        self.assertNotIn("红", sim2.colors_at("Z", wipe_t + Q(1, 4)))
        self.assertIn("红", sim2.colors_at("Z", Q(10) - Q(1, 4)))

    def test_knot_inside_unbroken_contact_does_not_reset(self):
        z = zone("A", 0, 0)
        hall = Hall("h", ("红", "蓝"), {"A": z}, frozenset(), "A", "A")
        p = prof()
        straight = traj("R", [(0, 0, 0), (2, 0, 0), (4, 2, 0)], horizon=(Q(0), Q(4)))
        # 同一条几何轨迹，只在运动段中加一个不改变速度的断点 (3,1)。
        broken = traj("R", [(0, 0, 0), (2, 0, 0), (3, 1, 0), (4, 2, 0)], horizon=(Q(0), Q(4)))
        times = []
        for tt in (straight, broken):
            sim = simulate(hall, (Q(0), Q(4)), {"R": tt}, {"R": p}, [BrushPlan("R")], {"A": ["红"]})
            times.append([e["t"] for e in sim.events if e["kind"] == "exchange_group"])
        self.assertEqual(times[0], times[1])
        self.assertEqual(times[0], ["1"])

    def test_simultaneous_exchanges_settle_from_one_snapshot(self):
        hall = two_zone_hall()
        p = prof(w=Q(1, 2), h=Q(1, 2))
        t = traj("R", [(0, 0, 0), (4, 0, 0)], horizon=(Q(0), Q(4)))
        sim = simulate(hall, (Q(0), Q(4)), {"R": t}, {"R": p}, [BrushPlan("R")],
                        {"A": ["红"], "B": ["蓝"]})
        groups = [e for e in sim.events if e["kind"] == "exchange_group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["members"]), 2)
        # 同一拍内不许把 A 的红经刷顺序传给 B：两区都只有本色。
        self.assertEqual(set(sim.colors_at("A", Q(2))), {"红"})
        self.assertEqual(set(sim.colors_at("B", Q(2))), {"蓝"})
        brush_colors = [c for _, c, _ in sim.brush_timeline["R"] if c]
        self.assertIn(frozenset({"红", "蓝"}), brush_colors)

    def test_sleeve_change_clears_brush_and_truncates_contact(self):
        z = zone("Z", 0, 0, req=2)
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        p = prof(w=Q(1, 2), h=Q(1, 2))
        t = traj("S", [(0, 0, 0), (4, 0, 0)], horizon=(Q(0), Q(4)))
        sim = simulate(hall, (Q(0), Q(4)), {"S": t}, {"S": p},
                        [BrushPlan("S", frozenset({"红"}), [Q(3, 2)])], {"Z": ["蓝"]})
        groups = [e for e in sim.events if e["kind"] == "exchange_group"]
        # 换套把 [0,4) 截成两段：前段 1.5 < 2 不够；后段从 1.5 再积 2 => 3.5。
        self.assertEqual([g["t"] for g in groups], ["7/2"])
        self.assertEqual(groups[0]["members"][0]["sleeve"], 2)
        # 蓝是展前铺粉，首次来源保持 prelaid，不被后续互沾改写。
        self.assertEqual(sim.provenance["Z"]["蓝"]["kind"], "prelaid")
        # 换套后、到期前刷是空色新套。
        self.assertEqual(set(sim.colors_at("Z", Q(2))), {"蓝"})

    def test_second_sleeve_picks_and_relays_provenance(self):
        # 换套清空套1的红；新套在甲取蓝（t=3），再把蓝传到乙（t=4），
        # 乙的首次来源必须记成 R#套2。
        a, b = zone("甲", 0, 0, req=2), zone("乙", 2, 0, req=1)
        hall = Hall("h", ("红", "蓝"), {"甲": a, "乙": b}, frozenset({("甲", "乙")}), "甲", "乙")
        H = (Q(0), Q(8))
        r = traj("R", [(0, 0, 0), (3, 0, 0), (4, 2, 0), (6, 2, 0), (7, 6, 0), (8, 6, 0)], H)
        sim = simulate(hall, H, {"R": r}, {"R": prof(w=1, h=1)},
                        [BrushPlan("R", frozenset({"红"}), [1])], {"甲": ["蓝"]})
        groups = [(e["t"], [(m["zone"], m["sleeve"]) for m in e["members"]])
                  for e in sim.events if e["kind"] == "exchange_group"]
        self.assertIn(("3", [("甲", 2)]), groups)
        self.assertIn(("4", [("乙", 2)]), groups)
        self.assertNotIn("红", sim.colors_at("甲", Q(8)))  # 套1的红在传出前被清
        self.assertEqual(sim.provenance["乙"]["蓝"]["source"], "R#套2")


class WipeAndWindowTests(unittest.TestCase):
    def test_duplicate_wipe_id_rejected(self):
        z = zone("Z")
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        t = traj("R", [(0, 0, 0), (10, 0, 0)])
        with self.assertRaises(Exception):
            simulate(hall, H10, {"R": t}, {"R": prof(w=Q(1, 2), h=Q(1, 2))},
                      [BrushPlan("R")], wipes=[Wipe("W1", "Z", 2), Wipe("W1", "Z", 5)])

    def test_clean_window_is_left_closed_right_open(self):
        z = zone("Z")
        hall = Hall("h", ("红", "蓝"), {"Z": z}, frozenset(), "Z", "Z")
        p = prof(w=Q(1, 2), h=Q(1, 2))
        t = traj("R", [(0, 0, 0), (10, 0, 0)])  # 红刷 t=1 把 Z 染红
        sim = simulate(hall, H10, {"R": t}, {"R": p}, [BrushPlan("R", frozenset({"红"}))])
        self.assertTrue(sim.is_clean("Z", "红", Q(0), Q(1)))   # 右端点 1 排除
        self.assertFalse(sim.is_clean("Z", "红", Q(1), Q(2)))  # 左端点 1 闭
        self.assertFalse(sim.is_clean("Z", "红", Q(0), Q(2)))

    def test_wipe_source_before_exchange_changes_distant_zone(self):
        a, b, c = zone("A", 0, 0), zone("B", 2, 0), zone("C", 4, 0)
        hall = Hall("D", ("红", "蓝"), {"A": a, "B": b, "C": c},
                    frozenset({("A", "B"), ("B", "C")}), "A", "C")
        t = traj("R", [(0, 0, 0), (1, 0, 0), (3, 4, 0), (4, 4, 0), (6, 8, 0), (8, 8, 0)],
                 horizon=(Q(0), Q(8)))
        p = prof(w=1, h=1)
        raw = simulate(hall, (Q(0), Q(8)), {"R": t}, {"R": p}, [BrushPlan("R")], {"A": ["红"]})
        self.assertIn("红", raw.colors_at("C", Q(4)))
        wiped = simulate(hall, (Q(0), Q(8)), {"R": t}, {"R": p}, [BrushPlan("R")],
                          {"A": ["红"]}, [Wipe("WSRC", "A", Q(1, 2))])
        # 在源区到期互沾前擦净源区：刷取不到红，远处 C 不再受色。
        self.assertNotIn("红", wiped.colors_at("C", Q(4)))
        self.assertNotIn("红", wiped.colors_at("B", Q(2)))


# --------------------------------------------------------------------------- #
# 四区两刷三场夹具
# --------------------------------------------------------------------------- #
def capstone_fixture(inventory=("巾01", "巾02")):
    zones = {z: zone(z, x, y) for z, (x, y) in
             {"甲": (0, 0), "乙": (2, 0), "丙": (0, 2), "丁": (2, 2)}.items()}
    hall = Hall.from_data({
        "hall_id": "H4", "labels": ["红", "蓝"],
        "zones": [z.to_dict() for z in zones.values()],
        "entrance": "甲", "exhibit": "丁",
    })
    H = (Q(0), Q(12))
    p = prof("P", Q(1, 2), Q(1, 2))
    r1 = traj("刷1", [(0, 0, 0), (2, 0, 0), (4, 2, 0), (6, 2, 0), (8, 6, 0), (12, 6, 0)], H)
    r2 = traj("刷2", [(0, 0, 6), (2, 0, 2), (4, 0, 2), (6, 2, 2),
                      (8, 2, 2), (10, 2, 6), (12, 2, 6)], H)
    plans = [BrushPlan("刷1", frozenset({"红"})), BrushPlan("刷2")]
    init = {"丙": ["蓝"]}
    sheets = [
        SessionSheet("场1", "红", 4, 6, slots=(WipeSlot("甲", 3), WipeSlot("乙", 4))),
        SessionSheet("场2", "蓝", 6, 9, slots=(WipeSlot("丁", 6),)),
        SessionSheet("场3", "蓝", 9, 12),
    ]
    desk = Desk(hall, H, {"刷1": r1, "刷2": r2}, {"刷1": p, "刷2": p},
                plans, init, list(inventory))
    return desk, sheets, dict(hall=hall, H=H, r1=r1, r2=r2, p=p, plans=plans, init=init)


class DeskSessionTests(unittest.TestCase):
    def test_capstone_three_sessions_and_remaining_route(self):
        desk, sheets, fx = capstone_fixture()
        d1, d2, d3 = desk.run_all(sheets)
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["route"], ["甲", "丙", "丁"])
        self.assertEqual([w["wipe_id"] for w in d1["wipes_used"]], ["巾01"])
        self.assertTrue(d2["ok"])
        self.assertEqual(d2["route"], ["甲", "乙", "丁"])
        self.assertEqual([w["wipe_id"] for w in d2["wipes_used"]], ["巾02"])
        # 场3 继承场2 对丁的持久擦净，无需新巾。
        self.assertTrue(d3["ok"])
        self.assertEqual(d3["wipes_used"], [])
        self.assertEqual(desk.free_ids(), [])
        avail = desk.available_routes("蓝", Q(9), Q(12))
        self.assertEqual([r["route"] for r in avail], [["甲", "乙", "丁"]])
        for sid, leader in (("场1", "林"), ("场2", "周"), ("场3", "林")):
            self.assertTrue(desk.sign_off(sid, leader)["signed"])

    def test_one_wipe_creates_competition(self):
        desk, sheets, _ = capstone_fixture(inventory=("巾01",))
        d1 = desk.run_sheet(sheets[0])
        d2 = desk.run_sheet(sheets[1])
        d3 = desk.run_sheet(sheets[2])
        self.assertTrue(d1["ok"])       # 场1 按场单次序先领走巾01
        self.assertFalse(d2["ok"])      # 场2 争不到第二块
        self.assertFalse(d3["ok"])

    def test_external_hall_consumption_makes_wipe_unavailable(self):
        desk, sheets, fx = capstone_fixture(inventory=("巾乙",))
        a, b = zone("甲", 0, 0), zone("乙", 2, 0)
        hall = Hall("JY", ("红", "蓝"), {"甲": a, "乙": b}, frozenset({("甲", "乙")}), "乙", "乙")
        H = (Q(0), Q(6))
        r = traj("R", [(0, 0, 0), (1, 0, 0), (2, 2, 0), (3, 2, 0), (5, 6, 0), (6, 6, 0)], H)
        sheet = SessionSheet("JY", "红", 4, 6, slots=(WipeSlot("乙", 3),))
        desk = Desk(hall, H, {"R": r}, {"R": prof(w=1, h=1)}, [BrushPlan("R")],
                    {"甲": ["红"]}, ["巾乙"], external_consumed=[Wipe("巾乙", "别厅", 2)])
        decision = desk.run_sheet(sheet)
        self.assertFalse(decision["ok"])
        signoff = desk.sign_off("JY", "赵")
        self.assertFalse(signoff["signed"])
        self.assertEqual(signoff["verdict"], "failed")

    def test_failed_decision_signoff_is_failure_evidence(self):
        desk, sheets, _ = capstone_fixture(inventory=())
        d1 = desk.run_sheet(sheets[0])
        self.assertFalse(d1["ok"])
        rec = desk.sign_off("场1", "林")
        self.assertFalse(rec["signed"])
        self.assertEqual(rec["verdict"], "failed")


def east_west_fixture(west_wipe_t=3):
    zones = {
        "I": zone("I", -2, 0),
        "W": zone("W", 0, 0, w=1, h=3),
        "E": zone("E", 2, 2),
        "X": zone("X", 2, -2),
        "V": zone("V", 4, 0, w=1, h=3),
    }
    hall = Hall.from_data({"hall_id": "EW", "labels": ["红", "蓝"],
                           "zones": [z.to_dict() for z in zones.values()],
                           "entrance": "I", "exhibit": "V"})
    H = (Q(0), Q(6))
    r = traj("RE", [(0, 2, 7), (2, 2, Q(7, 2)), (4, 2, 2), (6, 2, 2)], H)
    p = prof("P", Q(1, 2), Q(1, 2))
    wipes = [Wipe("WX", "X", west_wipe_t)]
    sim = simulate(hall, H, {"RE": r}, {"RE": p}, [BrushPlan("RE", frozenset({"红"}))],
                    {"X": ["红"]}, wipes)
    sheet = SessionSheet("EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2, handover=1)
    old, new = ("I", "W", "E", "V"), ("I", "W", "X", "V")
    return hall, sim, sheet, old, new, wipes


class SwitchTests(unittest.TestCase):
    def test_naive_ticks_always_find_a_route_but_switch_fails(self):
        hall, sim, sheet, old, new, wipes = east_west_fixture(3)
        naive = naive_tick_routes(sim, hall, "红", [Q(2), Q(3)])
        # 第 2 拍只有东廊，第 3 拍只有西廊：逐拍看都有干净路线。
        self.assertTrue(any(r["routes"] for r in naive))
        at2 = next(r for r in naive if r["t"] == "2")
        at3 = next(r for r in naive if r["t"] == "3")
        self.assertIn(["I", "W", "E", "V"], at2["routes"])
        self.assertIn(["I", "W", "X", "V"], at3["routes"])
        # 但连续一拍 [2,3) 换道不成立，且不存在更晚的可行起点。
        verdict = judge_switch(sim, sheet, old, new, wipes)
        self.assertFalse(verdict["ok"])
        self.assertIsNone(earliest_switch_start(sim, sheet, old, new, wipes))

    def test_pre_entry_wipe_at_t2_makes_switch_fulfillable(self):
        hall, sim, sheet, old, new, wipes = east_west_fixture(2)
        verdict = judge_switch(sim, sheet, old, new, wipes)
        self.assertTrue(verdict["ok"], verdict["problems"])
        self.assertEqual(earliest_switch_start(sim, sheet, old, new, wipes), Q(2))

    def test_wipe_inside_handover_window_forbidden(self):
        hall, sim, sheet, old, new, _ = east_west_fixture(3)
        bad_wipe = [Wipe("WX", "X", Q(5, 2))]  # 落在 (2,3) 内部
        verdict = judge_switch(sim, sheet, old, new, bad_wipe)
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["forbidden_wipes"]), 1)
        self.assertTrue(any("禁擦" in p for p in verdict["problems"]))

    def test_old_branch_must_stay_clean_until_handover_end(self):
        # 东廊规定时长只有 1/2：t=2 开始接触 => t=5/2 就沾红，早于交接结束 t=3。
        base, sim0, _, _, _, _ = east_west_fixture(2)
        zones = dict(base.zones)
        zones["E"] = zone("E", 2, 2, req=Q(1, 2))
        hall = Hall.from_data({"hall_id": "EW2", "labels": ["红", "蓝"],
                               "zones": [z.to_dict() for z in zones.values()],
                               "entrance": "I", "exhibit": "V"})
        H = (Q(0), Q(6))
        r = traj("RE", [(0, 2, 7), (2, 2, Q(7, 2)), (4, 2, 2), (6, 2, 2)], H)
        p = prof("P", Q(1, 2), Q(1, 2))
        sheet = SessionSheet("EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2, handover=1)
        # 西廊 t=2 已实际擦净；自动枚举廊对时，旧东廊在 [0,3) 内 t=2.5 脏 => 失败。
        desk = Desk(hall, H, {"RE": r}, {"RE": p}, [BrushPlan("RE", frozenset({"红"}))],
                    {"X": ["红"]}, ["WX"], actual_wipes=[Wipe("WX", "X", 2)])
        decision = desk.run_switch(sheet)
        self.assertFalse(decision["ok"])

    def test_desk_switch_planner_and_signoff(self):
        # 西廊允许的进场前擦净时点只有 t=2：规划成功并签收换道。
        fx_hall, _, _, _, _, _ = east_west_fixture(3)
        zones = fx_hall.zones
        H = (Q(0), Q(6))
        r = traj("RE", [(0, 2, 7), (2, 2, Q(7, 2)), (4, 2, 2), (6, 2, 2)], H)
        p = prof("P", Q(1, 2), Q(1, 2))
        sheet = SessionSheet("EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2, handover=1,
                             slots=(WipeSlot("X", 2),))
        desk = Desk(fx_hall, H, {"RE": r}, {"RE": p},
                    [BrushPlan("RE", frozenset({"红"}))], {"X": ["红"]}, ["WX"])
        d = desk.run_switch(sheet)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["handover_window"], ["2", "3"])
        rec = desk.sign_off("EW", "领队·何")
        self.assertEqual(rec["verdict"], "switched")
        # 若只允许 t=3 擦西廊，新台、新巾仍规划失败，签收按失败办。
        sheet_bad = SessionSheet("EW2", "红", 0, 5, waiting_zone="W", arrive_waiting=2,
                                 handover=1, slots=(WipeSlot("X", 3),))
        desk_bad = Desk(fx_hall, H, {"RE": r}, {"RE": p},
                        [BrushPlan("RE", frozenset({"红"}))], {"X": ["红"]}, ["WX"])
        d2 = desk_bad.run_switch(sheet_bad)
        self.assertFalse(d2["ok"])
        self.assertEqual(desk_bad.sign_off("EW2", "何")["verdict"], "failed")


class PersistenceTests(unittest.TestCase):
    def _payload(self):
        desk, sheets, fx = capstone_fixture()
        return build_payload(
            case_id="C4", hall=fx["hall"], horizon=fx["H"],
            trajectories={"刷1": fx["r1"], "刷2": fx["r2"]},
            brush_profiles={"刷1": fx["p"], "刷2": fx["p"]},
            brush_plans=fx["plans"], initial_zone_colors=fx["init"],
            inventory=["巾01", "巾02"], sheets=sheets,
            signoffs=[{"session_id": "场1", "leader": "林"},
                      {"session_id": "场2", "leader": "周"},
                      {"session_id": "场3", "leader": "林"}],
        )

    def test_publish_idempotent_verify_and_provenance(self):
        lib = ExhibitLibrary(conn=sqlite3.connect(":memory:"))
        payload = self._payload()
        res = lib.publish("R4", payload)
        self.assertTrue(res["ok"])
        again = lib.publish("R4", payload)
        self.assertEqual(again, res)  # 同内容复读
        verdict = verify_exhibit(lib, "R4")
        self.assertTrue(verdict["verified"], verdict)
        prov = lib.provenance("R4")
        self.assertEqual(prov["乙"]["红"]["source"], "刷1#套1")
        self.assertEqual(prov["丁"]["蓝"]["brush"], "刷2")
        self.assertEqual(prov["丙"]["蓝"]["kind"], "prelaid")
        # 异文拒绝。
        mutated = {**payload, "inventory": ["巾09"]}
        with self.assertRaises(ExhibitRejected):
            lib.publish("R4", mutated)

    def test_two_frozen_tracks_reconstruct_east_west_difference(self):
        lib = ExhibitLibrary(conn=sqlite3.connect(":memory:"))
        reports = {}
        old = ("I", "W", "E", "V")
        new = ("W", "X", "V")
        for tag, wipe_t, leader_verdict in (("ALPHA", 3, "failed"), ("BETA", 2, "switched")):
            hall, _, _, _, _, _ = east_west_fixture(wipe_t)
            H = (Q(0), Q(6))
            r = traj("RE", [(0, 2, 7), (2, 2, Q(7, 2)), (4, 2, 2), (6, 2, 2)], H)
            p = prof("P", Q(1, 2), Q(1, 2))
            # 两份“实轨”都含一次真实擦巾，差别只在实际时点；路线由场单钉明。
            full_sheet = SessionSheet(
                "EW", "红", 0, 5, waiting_zone="W", arrive_waiting=2, handover=1,
                old_route=old, new_route=new)
            payload = build_payload(
                case_id=f"EW-{tag}", hall=hall, horizon=H,
                trajectories={"RE": r}, brush_profiles={"RE": p},
                brush_plans=[BrushPlan("RE", frozenset({"红"}))],
                initial_zone_colors={"X": ["红"]}, inventory=["WX"],
                sheets=[full_sheet], actual_wipes=[Wipe("WX", "X", wipe_t)],
                signoffs=[{"session_id": "EW", "leader": "何"}],
            )
            result = lib.publish(f"EW-{tag}", payload)
            reports[tag] = result
            self.assertEqual(result["signoffs"][0]["verdict"], leader_verdict)
        # 甲轨的真实擦净 t=3 必须留在冻结时间线上（西廊从第 3 拍才干净）。
        self.assertIn(["3", []], reports["ALPHA"]["zone_timeline"]["X"])
        self.assertIn(["2", []], reports["BETA"]["zone_timeline"]["X"])
        # 两份冻结实轨的唯一差别：西廊擦净时点 3 vs 2，签收结论相反。
        self.assertFalse(reports["ALPHA"]["ok"])
        self.assertTrue(reports["BETA"]["ok"])
        self.assertTrue(verify_exhibit(lib, "EW-ALPHA")["verified"])
        self.assertTrue(verify_exhibit(lib, "EW-BETA")["verified"])


if __name__ == "__main__":
    unittest.main()
