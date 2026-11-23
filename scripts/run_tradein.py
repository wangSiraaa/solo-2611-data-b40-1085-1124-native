"""端到端演示：以旧换新开单 -> 验收台判定 -> 回收工场交付 -> 改单/预约代价。

运行：python3 scripts/run_tradein.py

把题目里的每个要求都落到一次真实运行，并打印结构化结果（可直接对照题目核对）。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradein.acceptance import AcceptanceDesk, ClaimError, InventoryGateError  # noqa: E402
from tradein.audit import audit_jia_machine  # noqa: E402
from tradein.convert import (  # noqa: E402
    ConversionError, ConversionWindow, Quote, Reservation, SlotPool, Station,
    Ticket, compare_policies, convert_via_signed_back, issue_reservation,
    release_reservation, station_guard, try_convert_to_whole,
)
from tradein.diagram import Basket, Diagram, Piece  # noqa: E402
from tradein.workshop import Order, Workshop  # noqa: E402


def _pp(title, obj):
    print(f"\n== {title} ==")
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def build_machines():
    """三台旧机的实机分解图（含相邻杆损伤、整副字篮、可复装）。"""
    jia = Diagram(model="JIA", pieces={
        "N": Piece("N", "nut", cells=1, reversible=True, reassemble_cells=1),
        "R": Piece("R", "retainer", cells=1),
        "X": Piece("X", "bar", key="X", cells=1, damage=("Y",)),
        "Y": Piece("Y", "bar", key="Y", cells=1),
    }, label="jia")
    ue = Diagram(model="UE", pieces={
        "N": Piece("N", "nut", cells=1, reversible=True, reassemble_cells=1),
        "R": Piece("R", "retainer", cells=1),
        "a": Piece("a", "bar", key="a", cells=1),
        "b": Piece("b", "bar", key="b", cells=1),
    }, basket=Basket("BK", ("a", "b"), cells=2), label="ue")
    ol = Diagram(model="OL", pieces={
        "N": Piece("N", "nut", cells=1, reversible=True, reassemble_cells=1),
        "R": Piece("R", "retainer", cells=1),
        "k": Piece("k", "bar", key="k", cells=2),
    }, label="ol")
    return jia, ue, ol


def acceptance_story():
    desk = AcceptanceDesk()
    desk.align_invoice("i1", "P", "S", q=4, T=80, C=40)
    desk.open_external_sale("s-north", outlet="北店", price=120, invoice_id="i1")
    desk.open_external_sale("s-south", outlet="南店", price=100, invoice_id="i1")

    # 北店：两台具名旧机（一台未交钥匙 -> 先存放）
    desk.register_machine("北店", "JIA", "body-jia", key_handed_in=True)
    desk.register_machine("北店", "UE", "body-ue", key_handed_in=False)  # 钥匙未交
    # 两卖场不得同时认领同一机身
    try:
        desk.register_machine("南店", "JIA", "body-jia")
    except ClaimError as exc:
        print("  [拒] 双重认领:", exc)

    t_north = desk.record_ticket(
        "t-north", "北店", "s-north", cash=80,
        trade_values={"body-jia": 25, "body-ue": 15}, old_bodies=["body-jia", "body-ue"],
    )
    # 南店一张单机小票
    desk.register_machine("南店", "OL", "body-ol", key_handed_in=True)
    t_south = desk.record_ticket(
        "t-south", "南店", "s-south", cash=90,
        trade_values={"body-ol": 10}, old_bodies=["body-ol"],
    )
    _pp("① 小票对账（现金+折价 == 对外售价）", {
        "t-north": {"total": t_north.total(), "external": t_north.external_sale_price,
                    "ok": t_north.reconciled()},
        "t-south": {"total": t_south.total(), "external": t_south.external_sale_price,
                    "ok": t_south.reconciled()},
    })

    # 未交付/未入账前入库被拒
    try:
        desk.admit_to_inventory("t-north")
    except InventoryGateError as exc:
        print("  [拒] 未交付/未入账入库:", exc)

    # 完成新机交付 + 约定现金入账 -> 成馆方存货
    desk.deliver_new("t-north")
    desk.book_cash("t-north")
    desk.deliver_new("t-south")
    desk.book_cash("t-south")
    added_north = desk.admit_to_inventory("t-north")
    added_south = desk.admit_to_inventory("t-south")

    # 同一外销单后续凭收款凭证结清
    desk.settle_external_sale("s-north", 120)
    rep = desk.recoverable_bodies()
    _pp("② 逐机关账头+交接件判定可回收 / 已兑现折价", {
        "admittednorth": added_north,
        "admitted_south": added_south,
        "recoverable": rep,
        "north_settlement": {"status": desk.external_sales["s-north"].status},
    })
    return desk


def workshop_story():
    jia, ue, ol = build_machines()
    orders = [
        Order("o-ue-a", "UE", "bar", "a"),
        Order("o-ue-b", "UE", "bar", "b"),
        Order("o-jia-X", "JIA", "bar", "X"),
        Order("o-jia-Y", "JIA", "bar", "Y"),
        Order("o-ol-k", "OL", "bar", "k"),
        Order("o-ol-whole", "OL", "whole_machine"),
    ]
    result = Workshop([jia, ue, ol], orders, budget=8).solve()
    _pp("③ 工场交付：最大兑现数、平手最少工时、领机与拆取次序", result.as_json())
    return jia, ue, ol, orders


def convert_story():
    # 在途测试 A：只松螺帽（可复装）-> 可改
    d = Diagram(model="JIA", pieces={
        "N": Piece("N", "nut", cells=1, reversible=True, reassemble_cells=1),
        "R": Piece("R", "retainer", cells=1, reversible=False),
        "X": Piece("X", "bar", key="X", cells=1, damage=("Y",)),
        "Y": Piece("Y", "bar", key="Y", cells=1),
    }, label="jia")
    st_a = Station("body-jia", d)
    st_a.loosen_nut("N")
    qa = Quote("Q-jia")
    dec_a = try_convert_to_whole(st_a, Ticket("T-jia-x", "JIA", "X"), qa,
                                 [Ticket("T-jia-y", "JIA", "Y")], [], 8)

    # 在途测试 B：固定杆已剪断（不可逆）-> 拒 + 旧报价失效
    st_b = Station("body-jia", d)
    st_b.loosen_nut("N")
    st_b.pry_retainer("R")
    qb = Quote("Q-jia2")
    dec_b = try_convert_to_whole(st_b, Ticket("T-jia-x", "JIA", "X"), qb,
                                 [Ticket("T-jia-y", "JIA", "Y")], [], 8)
    _pp("④ 在途改整机测试", {
        "A_only_nut_loosened": dec_a.as_json(),
        "B_retainer_cut": {**dec_b.as_json(), "old_quote_valid": qb.valid},
    })

    # 预约占位 / 守卫 / 释放 / 签回
    pool = SlotPool(1)
    win = ConversionWindow(open=True)
    res = issue_reservation(pool, Reservation("R1", "T-jia-x", "body-jia", "JIA"), win)
    guard = {}
    try:
        station_guard(win, res, Station("body-jia", d), "R")
    except ConversionError as exc:
        guard = {"blocked": True, "why": str(exc)}
    db = Station("body-jia", d)
    db.loosen_nut("N")
    db.sign_back()
    signed = convert_via_signed_back(db, Ticket("T-jia-x", "JIA", "X"), Quote("Q3"),
                                     [], [], 8)
    release_reservation(pool, res)
    _pp("⑤ 预约何时占位/释放、签回可改交付", {
        "reservation_free_after": pool.free(),
        "window_guard": guard,
        "signed_back_convert": signed.as_json(),
    })

    # 两种做法对第三机可交结果的代价
    policies = compare_policies(reservation_window_end=10,
                                third_machine_deadline=6,
                                customer_convert_deadline=8)
    _pp("⑥ ‘留唯一待单位’ vs ‘先开工’：可交结果与代价", [p.as_json() for p in policies])
    return dec_a, dec_b, policies


def jia_story():
    audit = audit_jia_machine(already_spent=2, budget=8)
    _pp("⑦ 甲机三视图（原头/账头退回现金到账前/压条检验改互损）", audit.as_json())


def main():
    acceptance_story()
    workshop_story()
    convert_story()
    jia_story()
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
