"""故事盒带读程序端到端演示。

运行：

    python3 demo_storybox.py

退出码 0 且最后打印“故事盒全部断言通过”，表示题面点名的嗅觉计算、抢牌、
自由页保证、小盘/现场送片差异以及交班续办均已真实跑通。
"""

from __future__ import annotations

import json

from library import LibrarySystem, jsonable
from library.storybox import (
    Page,
    Reader,
    ReadingSession,
    ScentBox,
    ScentCard,
    StoryBoxDesk,
    maximum_guaranteed_seats,
    plan_guaranteed,
    replay_record,
    sign_booking,
    songli_box,
    transfer_demo_guide,
    verify_run,
)


def banner(title: str) -> None:
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def show(value: object) -> None:
    print(json.dumps(jsonable(value), ensure_ascii=False, indent=2))


def receipt_demo() -> StoryBoxDesk:
    banner("1. 预约/取件回执 -> 实盒号 -> 香片与试闻表")
    system = LibrarySystem(":memory:")
    copies = [
        {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
        {"id": "C3", "book": "WB", "branch": "north", "format": "hardcover"},
    ]
    system.catalog.submit_revision("cat-v1", ["WA", "WB"], [], copies, op_id="cat-v1")
    system.catalog.activate_revision("cat-v1")
    system.ledger.register_ticket(
        "T1", "RC", "WB", 1, ["north"], ["hardcover"], op_id="ticket-T1"
    )
    hold = system.ledger.reserve("reserve-C3", "cat-v1", "C3")
    assert hold["status"] == "ok"
    pickup = system.ledger.pickup("PK-DEMO", hold["hold_id"], "RC", "C3")
    assert pickup["status"] == "ok"

    # v2 把 WB 并入 WA；同一规范作品下的 C1/C3 两套实盒附不同卡牌。
    system.catalog.submit_revision(
        "cat-v2", ["WA", "WB"], [{"from": "WB", "to": "WA"}], copies, op_id="cat-v2"
    )
    system.catalog.activate_revision("cat-v2")
    system.storybox.register_box({
        "box_id": "C3",
        "work_id": "WA",
        "cards": {
            "青": {"labels": ["松"], "duration": 4},
            "白": {"labels": ["梨"], "duration": 4},
        },
        "rules": [
            {"id": "qing-masks-li-next", "old": "松", "new": "梨", "lag": 1, "hidden": "梨"}
        ],
    })
    system.storybox.register_box({
        "box_id": "C1",
        "work_id": "WA",
        "cards": {"青": {"labels": ["松"], "duration": 4}},
        "rules": [],
    })

    loaded = system.storybox.load_by_receipt("PK-DEMO")
    assert loaded.box_id == "C3"
    assert set(loaded.cards) == {"青", "白"}
    c1 = system.storybox.load_box("C1")
    assert set(c1.cards) == {"青"}
    print("取件回执 PK-DEMO 上的实盒号：", pickup["copy"])
    print("加载到的卡牌：", sorted(loaded.cards))
    print("归并作品 WA 下 C1 的卡牌：", sorted(c1.cards))
    system.close()

    # 关闭重开由 test_storybox.py 的持久化用例覆盖；演示继续用独立内存柜台。
    desk = StoryBoxDesk()
    desk.register_box(songli_box("SONGLI"))
    desk.register_box(songli_box("SONGLI-LONG", mask_through=2))
    return desk


def scent_demo(desk: StoryBoxDesk) -> None:
    banner("2. 青牌松、白牌梨：逐拍察觉集合与遮蔽证据")
    ok = desk.replay("SONGLI", {0: "青", 2: "白"}, reader="甲")
    print("原试闻表 0青2白：", {k: sorted(v) for k, v in ok["perceived"].items()})
    assert ok["perceived"][0] == {"松"}
    assert ok["perceived"][2] == {"梨"}

    long_mask = desk.replay("SONGLI-LONG", {0: "青", 2: "白"}, reader="甲")
    evidence = long_mask["maskings"][0]
    print("遮蔽延到第二拍：第2拍实际标签 =", sorted(long_mask["turns"][1]["perceived"]))
    print("证据：", evidence["explanation"])
    assert long_mask["turns"][1]["perceived"] == set()
    assert evidence["blocked_by"] == "松" and evidence["age"] == 2

    banner("3. 另一读者锁住第2拍唯一白牌 -> 甲读不完")
    a = Reader("甲", (
        Page("松页", frozenset({"松"}), frozenset({"青"}), (0,)),
        Page("梨页", frozenset({"梨"}), frozenset({"白"}), (2,)),
    ))
    b = Reader("乙", (
        Page("锁白", frozenset({"梨"}), frozenset({"白"}), (2,)),
    ))
    locked = maximum_guaranteed_seats(
        guide={"box": songli_box(), "readers": [a, b]},
        mode="tray", tray_locked=False,
    )
    explicit = verify_run(
        songli_box(),
        actions=[
            {"reader": "甲", "page": "松页", "beat": 0, "card": "青"},
            {"reader": "甲", "page": "梨页", "beat": 2, "card": "白"},
            {"reader": "乙", "page": "锁白", "beat": 2, "card": "白"},
        ],
        readers=[a, b], mode="delivery",
    )
    print("最大可保证席位数：", locked["max_seats"], locked["options"])
    print("实牌冲突：", [c["reason"] for c in explicit["conflicts"] if c["kind"] == "card_busy"])
    assert locked["max_seats"] == 1
    assert any(c["kind"] == "card_busy" for c in explicit["conflicts"])

    banner("4. 两位各自读通，但合桌后唯一白牌来不及转手")
    guide = transfer_demo_guide()
    alone_a = plan_guaranteed(guide=guide, seats=["甲"], mode="tray", tray_locked=False)
    alone_b = plan_guaranteed(guide=guide, seats=["乙"], mode="tray", tray_locked=False)
    together = maximum_guaranteed_seats(guide=guide, mode="tray")
    transfer_check = verify_run(
        guide=guide,
        actions=[
            {"reader": "甲", "page": "松1", "beat": 0, "card": "青甲"},
            {"reader": "甲", "page": "梨1", "beat": 2, "card": "白"},
            {"reader": "乙", "page": "松2", "beat": 1, "card": "青乙"},
            {"reader": "乙", "page": "梨2", "beat": 3, "card": "白"},
        ],
        mode="tray", tray_locked=False, transfer_beats=2,
    )
    print("甲单独：", alone_a["status"], "；乙单独：", alone_b["status"])
    print("合桌最大席位：", together["max_seats"], together["options"])
    print("转手冲突：", [c["reason"] for c in transfer_check["conflicts"]])
    assert alone_a["ok"] and alone_b["ok"]
    assert together["max_seats"] == 1
    assert any(c["kind"] == "transfer_too_fast" for c in transfer_check["conflicts"])


def booking_and_handover_demo() -> None:
    banner("5. 两席预约单：付费自由页 0/1 拍不能只挑有利分支")
    old = {
        "box": _booking_box(),
        "readers": _booking_readers(2),
        "free_pages": [{"reader": "甲", "page": "青页", "beats": [0, 1]}],
    }
    old_a = plan_guaranteed(guide=old, seats=["甲"], mode="tray")
    old_max = maximum_guaranteed_seats(guide=old, mode="tray")
    print("白页仍在第2拍时，甲的保证状态：", old_a["status"], old_a["reason"])
    print("全单最大具名席位：", old_max["seats"])
    assert not old_a["ok"]
    assert old_max["seats"] == ["乙"]

    banner("6. 白页改第3拍：小盘业务可签两席，实片各有其盘")
    guide = {
        "box": _booking_box(),
        "readers": _booking_readers(3),
        "free_pages": [{"reader": "甲", "page": "青页", "beats": [0, 1]}],
    }
    tray_sheet = sign_booking(guide, sheet_id="TRAY-2-SEATS", mode="tray")
    print("签约状态：", tray_sheet["status"], tray_sheet["signed_seats"])
    print("各座小盘实片：", tray_sheet["promised_cards"])
    assert tray_sheet["status"] == "signed"
    assert tray_sheet["signed_seats"] == ["甲", "乙"]

    banner("7. 现场送片：同拍两座只能保一席，但按该席实际举牌时点服务")
    delivery = sign_booking(guide, sheet_id="DELIVERY-1-SEAT", mode="delivery")
    print("签约状态：", delivery["status"], "可保：", delivery["guaranteed_seats"],
          "可选具名席：", delivery["alternative_seat_options"])
    assert delivery["status"] == "partial"
    assert delivery["guaranteed_seats"] == ["甲"]

    both = verify_run(
        guide=guide,
        actions=[
            {"reader": "甲", "page": "青页", "beat": 0, "card": "青"},
            {"reader": "甲", "page": "白页", "beat": 3, "card": "白"},
            {"reader": "乙", "page": "乙揭牌", "beat": 3, "card": "赭"},
        ],
        mode="delivery",
    )
    print("若硬排两座第3拍：", [c["reason"] for c in both["conflicts"]])
    assert any(c["kind"] == "courier_busy" for c in both["conflicts"])

    banner("8. 同一已签送片单，分别从甲第0拍/第1拍揭青的现场记录续办")
    for actual_beat in (0, 1):
        session = ReadingSession(delivery, guide["box"])
        session.deliver(f"Q-{actual_beat}", actual_beat, "甲", "青", "青页")
        handover = session.handover_brief(actual_beat)
        cont = session.continue_remaining("甲", actual_beat)
        decay = session.decay_report(2 if actual_beat == 0 else 2, "甲")
        print(f"实际第{actual_beat}拍揭青 -> 交班已知选择 {handover['possible_choices']}，"
              f"余页 {[(x['page'], x['beat'], x['card']) for x in cont['remaining']]}")
        assert cont["remaining"][0]["beat"] == 3
        assert cont["remaining"][0]["card"] == "白"
        assert any(r["source_card"] == "青" for r in decay["active_residues"])

    banner("9. 小盘归还前：只能用馆员早已知情的事实向乙交代")
    tray_session = ReadingSession(tray_sheet, guide["box"])
    brief_b = tray_session.brief_for_seat("乙", at_beat=2)
    robust_b = tray_session.continue_remaining("乙", 2)
    print("乙座可知实片：", brief_b["promised_cards"])
    print("甲揭牌拍的两个可能世界：", brief_b["possible_choices"])
    print("禁止接班人使用：", brief_b["do_not_use"])
    print("乙的共同余页：", [(x["page"], x["beat"], x["card"]) for x in robust_b["common_remaining"]])
    assert brief_b["promised_cards"] == ["赭"]
    assert len(brief_b["possible_choices"]) == 2
    assert robust_b["common_remaining"][0]["card"] == "赭"

    tray_session.return_tray("甲", {"青页": 1})
    after = tray_session.continue_remaining("甲", 2)
    print("小盘归还并确认甲实际第1拍后：",
          [(x["page"], x["beat"], x["card"]) for x in after["remaining"]])
    assert after["choices"] == {"甲.青页": 1}
    assert after["remaining"][0]["beat"] == 3


def _booking_box() -> ScentBox:
    from library.storybox import MaskRule
    return ScentBox(
        "BOOK2",
        {
            "青": ScentCard("青", frozenset({"松"}), 3),
            "白": ScentCard("白", frozenset({"梨"}), 3),
            "赭": ScentCard("赭", frozenset({"杏"}), 3),
        },
        (MaskRule(old="松", new="梨", hidden="梨", lags=frozenset({1})),),
    )


def _booking_readers(white_beat: int) -> list[Reader]:
    return [
        Reader("甲", (
            Page("青页", frozenset({"松"}), frozenset({"青"}), (0, 1), free=True),
            Page("白页", frozenset({"梨"}), frozenset({"白"}), (white_beat,)),
        )),
        Reader("乙", (
            Page("乙揭牌", frozenset({"杏"}), frozenset({"赭"}), (white_beat,)),
        )),
    ]


def main() -> None:
    desk = receipt_demo()
    scent_demo(desk)
    booking_and_handover_demo()
    banner("故事盒全部断言通过")


if __name__ == "__main__":
    main()
