"""针织班端到端演示：版师 + 缝台，复用借还台账的真实取件回执。

运行：python3.11 knit_demo.py
退出码 0 且全部断言通过，即说明题面各案例全部兑现。
原 library/demo.py 入口保持不变，本文件只演示新增的可复用功能。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from knitroom import KnitRoom, scenario
from knitroom.scenario import BODY_CHART, SLEEVE_CHART

EASE_INSIDE = ["AXL->SHD"]
EASE_OUTSIDE = ["SHD->SHD"]


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def build_room() -> KnitRoom:
    room = KnitRoom.open(":memory:")
    scenario.load_library_material(room.library)
    room.library.catalog.activate_revision("v2")
    j5 = scenario.fulfill_j5(room.library)
    assert j5["copy"] == "C3" and j5["reader"] == "RC"
    return room


def register_charts(room: KnitRoom) -> None:
    p = room.pattern
    # 固定肩宽原单：一次增加（3 段）与连续两次增加（4 段）两版图解
    p.register_edition(scenario.sleeve_chart("e1", 1), "K-sleeve-e1")
    p.register_edition(scenario.sleeve_chart("e2", 2), "K-sleeve-e2")
    p.register_edition(scenario.body_chart("b1"), "K-body-b1")


def main() -> None:
    # ------------------------------------------------------------ 台账与 J5
    banner("J5：RC 实际取到 C3 的回执，版师据此出缝边记录")
    room = build_room()
    register_charts(room)
    p, s = room.pattern, room.sewing

    sleeve = p.create_seam_record("SLEEVE-1", "H3", SLEEVE_CHART, "sleeve", "K-seam-s1")
    sleeve2 = p.create_seam_record(
        "SLEEVE-2", "H3", SLEEVE_CHART, "sleeve", "K-seam-s2", edition="e2"
    )
    body = p.create_seam_record("BODY-1", "H3", BODY_CHART, "body", "K-seam-b1")
    print("原单袖记录:", json.dumps({k: sleeve[k] for k in
          ("receipt_id", "revision", "edition", "copy_id", "ticket_id", "reader_id")},
          ensure_ascii=False))
    assert sleeve["revision"] == "v1" and sleeve["edition"] == "e1"
    assert sleeve["copy_id"] == "C3" and sleeve["ticket_id"] == "TC"

    # ------------------------------------------------ 边圈谱与短行查询
    banner("版师边圈谱：具名线圈谱系、边弧、标记（可单独查）")
    spec_s = p.load_spectrum("SLEEVE-1")
    spec_b = p.load_spectrum("BODY-1")
    print("袖 AXL->SHD 边弧数:", spec_s.count_region("AXL->SHD"))
    print("身 AXL->SHD 边弧数:", spec_b.count_region("AXL->SHD"))
    assert spec_s.count_region("AXL->SHD") == 3
    assert spec_b.count_region("AXL->SHD") == 2
    inc_loop = next(
        l for l in p.edge_loop_ledger("SLEEVE-1") if l["loop_id"] == "M12A"
    )
    print("加针子圈 M12A 的父圈:", inc_loop["parents"])
    assert inc_loop["parents"] == ["S03"]
    print("两标记之间剩余弧:", [a["arc_id"] for a in p.arcs_between("SLEEVE-1", "AXL", "SHD")])
    print("标记 SHD 是否仍在:", p.marker_alive("SLEEVE-1", "SHD"))

    # --------------------------------------------- 固定肩宽原单的四组针序
    banner("固定肩宽：2对1吃势 + 1对1 可合；双增/无吃势/吃势移外均合不上")
    base = s.plan_needles("SLEEVE-1", "BODY-1", EASE_INSIDE)
    assert base["status"] == "ok"
    print("原单针序:", [(n["region"], n["ease"], n["sleeve_arcs"], n["body_arcs"])
          for n in base["needles"]])
    assert [n["ease"] for n in base["needles"]] == [0, 1, 0, 0]

    double_inc = s.plan_needles("SLEEVE-2", "BODY-1", EASE_INSIDE)
    no_ease = s.plan_needles("SLEEVE-1", "BODY-1", [])
    ease_outside = s.plan_needles("SLEEVE-1", "BODY-1", EASE_OUTSIDE)
    for tag, r in (("连续两次增加", double_inc), ("取消吃势", no_ease),
                   ("吃势移到肩标外", ease_outside)):
        assert r["status"] == "rejected" and r["error"]["reason"] == "cannot_align"
        print(f"{tag} -> 拒绝 {r['error']['reason']}：",
              f"袖剩{r['error']['sleeve_left']} 身剩{r['error']['body_left']}")

    # --------------------------------------------- 同名再版改变收针端
    banner("同名再版改变收针端：旧袖按原册查，标记随收针消失")
    p.register_edition(scenario.sleeve_reissue_chart("e3", "right"), "K-sleeve-e3")
    p.activate_edition(SLEEVE_CHART, "e3", "K-activate-e3")
    reissue = p.create_seam_record("SLEEVE-3", "H3", SLEEVE_CHART, "sleeve", "K-seam-s3")
    assert reissue["edition"] == "e3"
    print("再版记录册版:", reissue["edition"],
          "| 旧记录册版仍为:", p.get_seam_record("SLEEVE-1")["edition"])
    print("再版标记存活:", {m["marker_id"]: m["alive"] for m in p.markers("SLEEVE-3")})
    assert p.marker_alive("SLEEVE-3", "AXL") is True
    assert p.marker_alive("SLEEVE-3", "SHD") is False
    gone = s.plan_needles  # 短行查询直接抛 marker_gone
    try:
        p.arcs_between("SLEEVE-3", "AXL", "SHD")
        raise AssertionError("已消失标记不应能查区间")
    except Exception as exc:
        print("消失标记查询 ->", exc.reason)

    # --------------------------------------------- 线轴与两种接单方式
    banner("缝台：临缝专用线、装饰标线、定针锁定与改肩宽承诺")
    s.register_spool("BASTE", "baste", 4, "K-spool-baste")
    s.register_spool("PERM", "permanent", 16, "K-spool-perm")
    s.register_spool("DECOR", "decorative", 1, "K-spool-decor")

    # 两只袖各自临缝都够线；同轴线同时只够一只
    baste_l = s.baste_sleeve("FIT", "SL", "SLEEVE-1", "BODY-1", "BASTE", EASE_INSIDE, "K-baste-l")
    baste_r = s.baste_sleeve("FIT", "SR", "SLEEVE-1", "BODY-1", "BASTE", EASE_INSIDE, "K-baste-r")
    assert baste_l["status"] == "ok"
    assert baste_r["status"] == "rejected"
    assert baste_r["error"]["reason"] == "baste_thread_reserved_until_delivery"
    print("左袖临缝:", baste_l["needles"], "针；右袖同时临缝 ->",
          baste_r["error"]["reason"])
    # 拆临缝后右袖才能借同一轴线
    assert s.release_allocation("alloc-K-baste-l", "K-release-baste")["status"] == "ok"
    baste_r2 = s.baste_sleeve("FIT", "SR", "SLEEVE-1", "BODY-1", "BASTE", EASE_INSIDE, "K-baste-r2")
    assert baste_r2["status"] == "ok"
    print("拆临缝后右袖临缝:", baste_r2["needles"], "针")

    # 装饰标线只够一只
    assert s.allocate_decorative("FIT", "SL", "DECOR", "K-decor-l")["status"] == "ok"
    decor_r = s.allocate_decorative("FIT", "SR", "DECOR", "K-decor-r")
    assert decor_r["error"]["reason"] == "decorative_thread_one_sleeve_only"
    print("装饰标线给右袖 ->", decor_r["error"]["reason"])

    # 改肩：左袖定针只落在末弧之前（留活口），右袖一落到底
    perm_l = s.place_permanent(
        "ALTER", "SL", "SLEEVE-1", "BODY-1", "PERM", EASE_INSIDE, "K-perm-l", stop_after=1
    )
    perm_r = s.place_permanent(
        "ALTER", "SR", "SLEEVE-1", "BODY-1", "PERM", EASE_INSIDE, "K-perm-r"
    )
    assert perm_l["open_tail"] is True
    change_l = s.evaluate_shoulder_change(
        "ALTER", "SL", "SLEEVE-1", EASE_INSIDE, "SHD", "K-change-l"
    )
    change_r = s.evaluate_shoulder_change(
        "ALTER", "SR", "SLEEVE-1", EASE_INSIDE, "SHD", "K-change-r"
    )
    print("左袖改肩:", change_l["status"], change_l.get("absorb_region"))
    print("右袖改肩:", change_r["status"], change_r.get("reason"),
          "挡针", change_r.get("blocking_needle_seq"))
    assert change_l["status"] == "ok"
    assert change_r["reason"] == "permanent_needle_blocks_change"

    # 交货报告：能交的袖对、占用的线、不能交时的断口
    report = s.delivery_report("ALTER")
    print("交货报告:", json.dumps(report, ensure_ascii=False, indent=2))
    print("实针记录可单独查:", len(s.needle_log("ALTER")), "针；线轴:", s.thread_status())

    # ------------------------------------------------------------ 持久化重开
    banner("持久化重开：J5 旧袖按原册版找回那一针与实际余弧")
    dbp = tempfile.mktemp(suffix=".db")
    file_room = KnitRoom.open(dbp)
    scenario.load_library_material(file_room.library)
    file_room.library.catalog.activate_revision("v2")
    scenario.fulfill_j5(file_room.library)
    fp, fs = file_room.pattern, file_room.sewing
    fp.register_edition(scenario.sleeve_chart("e1", 1), "F-sleeve-e1")
    fp.register_edition(scenario.body_chart("b1"), "F-body-b1")
    fp.create_seam_record("OLD-S", "H3", SLEEVE_CHART, "sleeve", "F-seam-s")
    fp.create_seam_record("OLD-B", "H3", BODY_CHART, "body", "F-seam-b")
    fs.register_spool("PERM", "permanent", 8, "F-spool")
    fs.place_permanent("GRP", "SL", "OLD-S", "OLD-B", "PERM", EASE_INSIDE, "F-needle", stop_after=2)
    file_room.close()

    reopened = KnitRoom.reopen(dbp)
    assert reopened.pattern.get_seam_record("OLD-S")["edition"] == "e1"
    remaining = reopened.sewing.plan_needles(
        "OLD-S", "OLD-B", EASE_INSIDE, sleeve_id="SL", seam_group="GRP"
    )
    print("重开后余下针序的袖弧:", [n["sleeve_arcs"] for n in remaining["needles"]])
    assert [n["sleeve_arcs"] for n in remaining["needles"]] == [["A4"], ["A5"]]
    reopened.close()
    for suffix in ("", "-wal", "-shm"):
        path = Path(dbp + suffix)
        if path.exists():
            path.unlink()

    banner("全部断言通过")


if __name__ == "__main__":
    main()
