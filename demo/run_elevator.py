#!/usr/bin/env python3
"""桌面电梯演练端到端入口（可重复开班）。

从仓库根目录运行::

    python3 demo/run_elevator.py

它在 ``demo/elevator/_work`` 下重建全部工作产物，每次运行逐字节可复现：

1. 把每个场景门口计数员的计数行写进加权源（真实 JSON 正文 + 权重 + 分片）。
2. 用固定 256 位 seed 跑加权采样，生成数据集；账房 *只* 取回真正入样的
   计数行，并从这些行还原所有相容搭乘历史。
3. 对每个调度单用两种规程（ticket 提前发票 / hail 临时招呼）各实办一次
   接替，给出可保证送达的最多预约组与完整可执行班单。
4. 在每个匿名交班世界上回放班单，打印司机当场读数流水与首个失约窗口。
5. 覆盖题面给定的全部已确认窗口变体（改下客计数、改容量、调宽窗口）。

只用合成人数与积木，不接任何外部服务。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ELEV_DIR = os.path.join(HERE, "elevator")
sys.path.insert(0, ROOT)

from wrsource.dataset import build_dataset, load_dataset
from wrsource.elevator import (HAIL, TICKET, certain_onboard,
                               enumerate_histories, load_dispatch,
                               load_scenario, plan_handover,
                               replay_all_worlds, rows_from_dataset,
                               scenario_ingest_ops, possible_onboard)
from wrsource.source import SourceService

SEED = bytes((i * 7 + 3) & 0xFF for i in range(32))
SAMPLE_GROUP = "elevator-counts"
QUOTA = 3

# (场景文件, 调度文件列表, 某站关门确定性查询楼层)
SCENARIOS = [
    ("scenario_ex1_base.json", ["dispatch_ex1_base.json"], 1),
    ("scenario_ex1_downs0.json", ["dispatch_ex1_downs0.json"], 1),
    ("scenario_ex1_cap1.json", ["dispatch_ex1_base.json"], 1),
    ("scenario_blind.json", ["dispatch_blind.json"], 2),
    ("scenario_ex2_base.json", ["dispatch_ex2.json"], 1),
    ("scenario_ex2_alight1.json", ["dispatch_ex2.json"], 1),
    ("scenario_hail.json",
     ["dispatch_hail_narrow.json", "dispatch_hail_wide.json"], 1),
    ("scenario_named.json", [], 1),
]


def banner(text: str) -> None:
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def summarize_plan(plan: dict) -> dict:
    return {
        "protocol": plan["protocol"],
        "tickets": plan.get("tickets", []),
        "guaranteed_score": plan["score"],
        "handover_worlds": plan["worlds"],
        "planner_nodes": plan["planner_stats"]["nodes"],
    }


def main() -> None:
    work = os.path.join(ELEV_DIR, "_work")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)

    summary = []
    for scenario_file, dispatch_files, query_floor in SCENARIOS:
        scn_path = os.path.join(ELEV_DIR, scenario_file)
        scn = load_scenario(scn_path)
        banner(f"场景 {scn.name}（容量 {scn.capacity}，{len(scn.groups)} 组，"
               f"{len(scn.rows)} 条计数行）")

        # 每个场景独立源库：不同场景的计数行 row_id 允许重名。
        svc = SourceService(os.path.join(work, f"{scn.name}_source.sqlite"))
        svc.init()
        # 1) 计数行入加权源并采样（固定 seed => 可重复开班）。
        version = svc.apply_batch(
            scenario_ingest_ops(scn, SAMPLE_GROUP)).version
        out_dir = os.path.join(work, scn.name + "_dataset")
        manifest, records = build_dataset(
            svc, version, SEED, {SAMPLE_GROUP: QUOTA}, out_dir)
        sampled_ids = [r["row_id"] for r in records]
        print(f"入样计数行（seed 固定，可复跑）: {sampled_ids}")

        # 2) 账房只从真正入样的计数行还原可行历史。
        manifest, records = load_dataset(out_dir)
        sampled_rows = rows_from_dataset(scn, manifest, records)
        histories = enumerate_histories(scn, sampled_rows)
        print(f"相容搭乘历史数: {len(histories)}")
        for h in histories:
            rides = {g: d for g, d in h.choice.items() if d is not None}
            print("   历史:", rides or "（无组搭乘）")
        if histories:
            print(f"楼层 {query_floor} 关门时确定在厢: "
                  f"{list(certain_onboard(histories, query_floor))}")
            print(f"楼层 {query_floor} 关门时可能在厢: "
                  f"{list(possible_onboard(histories, query_floor))}")
        write_json(os.path.join(work, scn.name + "_histories.json"),
                   {"sampled_rows": sampled_ids,
                    "history_count": len(histories),
                    "histories": [
                        {g: d for g, d in h.choice.items() if d is not None}
                        for h in histories]})

        # 3)+4) 每张预约班单两种规程各办一次，并逐世界回放。
        for dispatch_file in dispatch_files:
            dsp = load_dispatch(os.path.join(ELEV_DIR, dispatch_file))
            print(f"\n  调度单 {dispatch_file}：{len(dsp.reservations)} 个预约组，"
                  f"交班层 {dsp.handover_floor}，当场下客读数 "
                  f"{dsp.observed_alight}")
            for protocol in (TICKET, HAIL):
                plan = plan_handover(scn, dsp, histories, protocol)
                write_json(os.path.join(
                    work, f"{scn.name}_{dispatch_file.removesuffix('.json')}"
                          f"_{protocol}_plan.json"), plan)
                print(f"    [{protocol:6s}] 可保证送达 {plan['score']} 组"
                      + (f"，发票: {plan['tickets']}"
                         if protocol == TICKET else "（临时招呼）"))
                reports = replay_all_worlds(scn, dsp, plan)
                for rep in reports:
                    reading = " | ".join(
                        f"F{x['floor']}@t{x['beat']}"
                        f"(下{x['alight_anonymous']}/称{x['weighed_load']}"
                        f"{'+招呼拍' if x['hail_extra_beat'] else ''})"
                        for x in rep["log"])
                    print(f"        世界 {rep['world']}: {reading}")
                    if rep["missed_window"]:
                        print(f"          首个失约窗口: {rep['missed_window']}")
                summary.append({"scenario": scn.name,
                                "dispatch": dispatch_file,
                                **summarize_plan(plan)})

    banner("汇总：各场景 × 规程可保证送达的最多预约组")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    write_json(os.path.join(work, "summary.json"), summary)
    print(f"\n全部产物已写入 {work}")


if __name__ == "__main__":
    main()
