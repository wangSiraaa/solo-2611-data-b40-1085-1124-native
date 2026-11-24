#!/usr/bin/env python3
"""制带室 + 换卡柜端到端演示（题目三例）。

从仓库根目录运行：

    python3 demo/run_counter_demo.py

演示全程只用标准库，覆盖：

1. 例 1：回条把第 5 包锁成普船，两册普船 + 一张普星 + 一次换权，
   事先可签两册；撤回回条或撤走换权都只能保一册。
2. 例 2：押换柜制下“开星才成交”，星/船两种包况都补齐甲、乙两册；
   仅即换只能保一册；按原卡号 S、换权 R0 重查去向；撤入押许可/改离柜
   时点后重算可签册号。
3. 补例：开包后才该决定下一包的自适应办理（不超过六包、三册），
   并对比固定路线严格更差。
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wrsource.counter import (CounterPlanner, CounterSpec, OrderRunner,
                              build_from_spec)
from wrsource.tape import (Belief, Card, FORWARD, REVERSE, Hypothesis,
                           Receipt, TapePattern, TapeRoom)

星 = Card("星", False)
船 = Card("船", False)
月 = Card("月", False)
TAPE_A = TapePattern("星船月", (星, 船, 月))
TAPE_B = TapePattern("星月船", (星, 月, 船))


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def show(planner, slips, policy, belief, order_id):
    print(f"可事先签下的册号：{list(slips)}（共 {len(slips)} 本）")
    print("办理树：")
    for line in policy.walk_lines(1):
        print(line)
    print("\n每个可行世界（合法包况）下的台账复核：")
    for h in belief.hypotheses:
        runner = OrderRunner(planner, order_id, slips)
        st = runner.run(policy, planner.initial_state(slips), h.key)
        assert all(not slots for slots in st.remaining), "保证单未兑现"
        mounts = [(e["card_id"], e["album_id"])
                  for e in runner.ledger.events if e["event"] == "mount"]
        print(f"  世界 {h.as_dict()}: 装册 {mounts}")


def example_one():
    banner("例 1：回条锁定同相位包，两册普船可同时签下")
    room = TapeRoom((TAPE_A, TAPE_B),
                    (Receipt("第一圈第2包", 2, 0, design="船"),))
    belief = Belief(room.feasible_hypotheses(
        start_cells=[0], directions=[FORWARD]))
    print("制带室核出的可行解释：",
          [h.as_dict() for h in belief.hypotheses])
    print("第 5 包的全部可能包况：",
          [[c.label() for c in cs] for cs in belief.pack_outcomes(5)])
    print("第 6 包的全部可能包况：",
          [[c.label() for c in cs] for cs in belief.pack_outcomes(6)])

    counter = dict(
        albums=[{"album_id": "甲",
                 "slots": [{"design": "船", "foil": False}]},
                {"album_id": "乙",
                 "slots": [{"design": "船", "foil": False}]}],
        holdings=[{"card_id": "S",
                   "card": {"design": "星", "foil": False}}],
        rights=1,
        swap_rules=[{"rule_id": "星换船",
                     "from": {"design": "星", "foil": False},
                     "to": {"design": "船", "foil": False}}],
        customers=[], packs=[5, 6], max_opens=1)
    spec = CounterSpec.from_dict(counter)
    planner = CounterPlanner(spec, belief)
    slips, policy = planner.plan_max()
    show(planner, slips, policy, belief, "ORD-例1")

    # 反事实一：只撤回回条且本组无替补。
    wide = Belief(TapeRoom((TAPE_A, TAPE_B), ()).feasible_hypotheses(
        start_cells=[0], directions=[FORWARD]))
    s1, _ = CounterPlanner(spec, wide).plan_max()
    # 反事实二：恢复回条，仅撤走换权。
    s2, _ = CounterPlanner(
        CounterSpec.from_dict({**counter, "rights": 0}), belief).plan_max()
    print(f"\n反事实：撤回回条 -> 可签 {list(s1)}；"
          f"恢复回条但撤换权 -> 可签 {list(s2)}")


def example_two():
    banner("例 2：押换“开星才成交” vs 仅即换，同一签单两种开包实况")
    # 第 5 包可能星也可能船，两种都与开单时的回条相容。
    h_star = Hypothesis(TAPE_A, (TAPE_A.cells.index(星) - 5) % 3, FORWARD)
    h_boat = Hypothesis(TAPE_A, (TAPE_A.cells.index(船) - 5) % 3, FORWARD)
    belief = Belief((h_star, h_boat))
    counter = dict(
        albums=[{"album_id": "甲",
                 "slots": [{"design": "船", "foil": False}]},
                {"album_id": "乙",
                 "slots": [{"design": "星", "foil": False}]}],
        holdings=[{"card_id": "S",
                   "card": {"design": "星", "foil": False}}],
        rights=1,
        customers=[{"customer_id": "换卡户",
                    "card": {"card_id": "C",
                             "card": {"design": "船", "foil": False}},
                    "wants": {"design": "星"},
                    "departs_before_pack": 5}],
        packs=[5], max_opens=1)

    print("【柜制 A：允许押换】")
    spec = CounterSpec.from_dict(counter)
    planner = CounterPlanner(spec, belief)
    slips, policy = planner.plan_max()
    show(planner, slips, policy, belief, "ORD-例2押换")

    # 按原卡号/换权号在“开船落空”世界重查。
    boat_world = next(h for h in belief.hypotheses
                      if h.pack_content(5, 1) == (船,))
    runner = OrderRunner(planner, "ORD-例2押换", slips)
    runner.run(policy, planner.initial_state(slips), boat_world.key)
    print("\n开船落空世界，按原卡号 S 重查：")
    for e in runner.ledger.card_trace("S"):
        print("  ", e["seq"], e["event"], e.get("album_id", ""),
              e.get("dealt", ""))
    print("按换权 R0 重查：",
          [(e["seq"], e["event"]) for e in runner.ledger.right_trace("R0")])

    print("\n【柜制 B：只许即换】")
    spec_instant = CounterSpec.from_dict(
        {**counter, "escrow_allowed": False})
    planner_instant = CounterPlanner(spec_instant, belief)
    slips_i, policy_i = planner_instant.plan_max()
    show(planner_instant, slips_i, policy_i, belief, "ORD-例2即换")

    print("\n【反事实：撤掉 S 的入押许可】")
    blocked = CounterSpec.from_dict(
        {**counter, "escrow_blocked_cards": ["S"]})
    print("  可签：", list(CounterPlanner(blocked, belief).plan_max()[0]))
    print("【反事实：户在签单时已离柜（离柜时点改为第 0 包前）】")
    early = CounterSpec.from_dict({**counter, "customers": [
        {**counter["customers"][0], "departs_before_pack": 0}]})
    print("  可签：", list(CounterPlanner(early, belief).plan_max()[0]))


def example_three():
    banner("补例：开包后才该决定下一包（六包以内、三册）")
    # 正向与反向两个世界；包 4 正向月/反向船，包 5 正向船/反向月。
    h_f = Hypothesis(TAPE_A, 0, FORWARD)
    h_r = Hypothesis(TAPE_A, 0, REVERSE)
    belief = Belief((h_f, h_r))
    counter = dict(
        albums=[{"album_id": "甲",
                 "slots": [{"design": "月", "foil": False}]},
                {"album_id": "乙",
                 "slots": [{"design": "船", "foil": False}]},
                {"album_id": "丙",
                 "slots": [{"design": "月", "foil": False}]}],
        holdings=[{"card_id": "H",
                   "card": {"design": "月", "foil": False}}],
        rights=0, customers=[], packs=[4, 5], max_opens=2)
    spec = CounterSpec.from_dict(counter)
    planner = CounterPlanner(spec, belief)
    slips, policy = planner.plan_max()
    show(planner, slips, policy, belief, "ORD-例3")

    # 固定路线对照：只许开一包时两册同签不再可能。
    one = CounterSpec.from_dict({**counter, "max_opens": 1})
    print("\n固定只开一包时可签：",
          list(CounterPlanner(one, belief).plan_max()[0]))


def example_json_cli(tmp_path):
    banner("附：制带室与柜台共用一份 JSON 规格（python -m wrsource.counter）")
    spec = {
        "pack_size": 1,
        "patterns": [TAPE_A.as_dict(), TAPE_B.as_dict()],
        "receipts": [Receipt("第一圈第2包", 2, 0,
                             design="船").as_dict()],
        "constraints": {"start_cells": [0], "directions": ["forward"]},
        "counter": {
            "albums": [{"album_id": "甲",
                        "slots": [{"design": "船", "foil": False}]},
                       {"album_id": "乙",
                        "slots": [{"design": "船", "foil": False}]}],
            "holdings": [{"card_id": "S",
                          "card": {"design": "星", "foil": False}}],
            "rights": 1,
            "swap_rules": [{"rule_id": "星换船",
                            "from": {"design": "星", "foil": False},
                            "to": {"design": "船", "foil": False}}],
            "customers": [], "packs": [5, 6], "max_opens": 1}}
    path = os.path.join(tmp_path, "counter_spec.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False)
    _, cs, belief, planner = build_from_spec(spec)
    slips, policy = planner.plan_max()
    print(f"共用规格下可签：{list(slips)}；规格文件：{path}")
    print("CLI 复跑：")
    print(f"  python3 -m wrsource.counter plan --spec {path}")


def main():
    example_one()
    example_two()
    example_three()
    work = os.path.join(HERE, "_work")
    os.makedirs(work, exist_ok=True)
    example_json_cli(work)
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
