"""故事盒带读层测试。

这些测试直接对应题面里的核定事实：
- 0 青 2 白读通；遮蔽延到第二拍后梨被遮；
- 另一读者锁住第 2 拍唯一白牌后甲读不完；
- 两座各自可行但合桌转手不及；
- 付费自由页 0/1 两拍必须同时保证；
- 小盘可签两席，现场送片在同拍争员时只能保一席；
- 交班不得偷看未归还小盘的实际揭牌拍，现场记录只能追加。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library import (
    IdempotencyConflict,
    LibrarySystem,
)
from library.storybox import (
    MaskRule,
    Page,
    Reader,
    ReadingSession,
    ScentBox,
    ScentCard,
    StoryBoxDesk,
    StoryBoxError,
    active_residues,
    booking_demo_guide,
    local_schedules,
    maximum_guaranteed_seats,
    plan_guaranteed,
    replay_record,
    sign_booking,
    songli_box,
    transfer_demo_guide,
    verify_run,
)


class ScentPhysicsTest(unittest.TestCase):
    def test_0_qing_2_bai_reads_through(self) -> None:
        box = songli_box()
        result = replay_record(box, {0: "青", 2: "白"}, reader="甲")
        self.assertEqual(result["perceived"][0], {"松"})
        self.assertEqual(result["perceived"][2], {"梨"})
        self.assertEqual(result["maskings"], [])

    def test_mask_can_reach_second_beat(self) -> None:
        box = songli_box(mask_through=2)
        result = replay_record(box, {0: "青", 2: "白"}, reader="甲")
        self.assertEqual(result["perceived"][2], set())
        masking = result["maskings"][0]
        self.assertEqual(masking["label"], "梨")
        self.assertEqual(masking["blocked_by"], "松")
        self.assertEqual(masking["source_beat"], 0)
        self.assertEqual(masking["age"], 2)
        self.assertIn("第0拍", masking["explanation"])

    def test_multiple_un_dispersed_residues_computed_together(self) -> None:
        box = ScentBox(
            "MULTI",
            {
                "A": ScentCard("A", frozenset({"旧一"}), linger=3),
                "B": ScentCard("B", frozenset({"旧二"}), linger=3),
                "N": ScentCard("N", frozenset({"新提示"}), linger=1),
            },
            (
                MaskRule(old="旧一", new="新提示", hidden="新提示", lags=frozenset({2})),
                MaskRule(old="旧二", new="新提示", hidden="新提示", lags=frozenset({1})),
            ),
        )
        # 第 2 拍：A 中旧一的时差是 2，单独即可形成遮蔽。
        r2 = replay_record(box, {0: "A", 2: "N"}, reader="R")
        self.assertEqual(r2["perceived"][2], set())
        self.assertEqual(len(r2["maskings"]), 1)
        self.assertEqual(r2["maskings"][0]["blocked_by"], "旧一")

        # 0=A、1=B、2=N：两个未散残留分别在 lag2/lag1 同时匹配。
        r_multi = replay_record(box, {0: "A", 1: "B", 2: "N"}, reader="R")
        blockers = {(m["blocked_by"], m["age"]) for m in r_multi["maskings"]}
        self.assertEqual(blockers, {("旧一", 2), ("旧二", 1)})
        self.assertEqual(r_multi["perceived"][2], set())

    def test_interruption_does_not_reset_decay(self) -> None:
        box = ScentBox(
            "DECAY",
            {"Q": ScentCard("Q", frozenset({"松"}), linger=1)},
            (),
        )
        at1 = active_residues(box, 1, [{"beat": 0, "card": "Q"}], "R")
        self.assertEqual(len(at1), 1)
        self.assertEqual(at1[0]["remaining_beats"] if "remaining_beats" in at1[0]
                         else at1[0]["linger"] - at1[0]["age"], 0)
        at2 = active_residues(box, 2, [{"beat": 0, "card": "Q"}], "R")
        self.assertEqual(at2, [])


class NarrativeSchedulingTest(unittest.TestCase):
    def test_basic_a_guaranteed(self) -> None:
        plan = plan_guaranteed(
            guide={
                "box": songli_box(),
                "readers": [
                    Reader("甲", (
                        Page("松页", need=frozenset({"松"}), cards=frozenset({"青"}), beats=(0,)),
                        Page("梨页", need=frozenset({"梨"}), cards=frozenset({"白"}), beats=(2,)),
                    ))
                ],
            },
            mode="tray",
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["seats"], ["甲"])

    def test_other_reader_locks_unique_white_at_2(self) -> None:
        box = songli_box()
        a = Reader("甲", (
            Page("松页", frozenset({"松"}), frozenset({"青"}), (0,)),
            Page("梨页", frozenset({"梨"}), frozenset({"白"}), (0, 2)),
        ))
        b = Reader("乙", (Page("锁白", frozenset({"梨"}), frozenset({"白"}), (2,)),))
        full = maximum_guaranteed_seats(
            guide={"box": box, "readers": [a, b]}, mode="tray", tray_locked=False
        )
        self.assertLess(full["max_seats"], 2)
        # 甲不能把第 2 拍改成别的拍；题面规定只可用 0/2，而 0 拍先松后梨不能同拍。
        a_plan = plan_guaranteed(
            guide={"box": box, "readers": [a, b]}, seats=["甲", "乙"],
            mode="delivery", tray_locked=False,
        )
        self.assertFalse(a_plan["ok"])

        checked = verify_run(
            box,
            actions=[
                {"reader": "甲", "page": "松页", "beat": 0, "card": "青"},
                {"reader": "甲", "page": "梨页", "beat": 2, "card": "白"},
                {"reader": "乙", "page": "锁白", "beat": 2, "card": "白"},
            ],
            readers=[a, b],
            mode="delivery",
        )
        self.assertFalse(checked["ok"])
        self.assertTrue(any(c["kind"] == "card_busy" for c in checked["conflicts"]))

    def test_each_reader_can_finish_but_card_cannot_transfer(self) -> None:
        guide = transfer_demo_guide()
        box = guide["box"]
        readers = guide["readers"]
        for r in readers:
            self.assertEqual(len(local_schedules(box, r, {})), 1)

        separate = maximum_guaranteed_seats(guide=guide, mode="tray")
        self.assertEqual(separate["max_seats"], 1)
        self.assertEqual(separate["options"], [["甲"], ["乙"]])

        # 明确核对转手时差：白牌 2 拍在甲座、3 拍在乙座，要求差至少 2 拍。
        check = verify_run(
            guide=guide,
            actions=[
                {"reader": "甲", "beat": 0, "card": "青甲", "page": "松1"},
                {"reader": "甲", "beat": 2, "card": "白", "page": "梨1"},
                {"reader": "乙", "beat": 1, "card": "青乙", "page": "松2"},
                {"reader": "乙", "beat": 3, "card": "白", "page": "梨2"},
            ],
            mode="tray",
            tray_locked=False,
            transfer_beats=2,
        )
        kinds = {c["kind"] for c in check["conflicts"]}
        self.assertIn("transfer_too_fast", kinds)

    def test_surprise_ending_cannot_be_smelled_early(self) -> None:
        box = ScentBox(
            "SURPRISE",
            {
                "剧透片": ScentCard("剧透片", frozenset({"结局", "松"}), 1),
                "松片": ScentCard("松片", frozenset({"松"}), 1),
                "结局片": ScentCard("结局片", frozenset({"结局"}), 1),
            },
            (),
        )
        reader = Reader("甲", (
            Page("第一页", frozenset({"松"}), frozenset({"剧透片", "松片"}), (0,)),
            Page("结局页", frozenset({"结局"}), frozenset({"结局片", "剧透片"}), (2,)),
        ))
        schedules = local_schedules(box, reader, {})
        # 求解器必须选松片；若第一页选剧透片，结局标签会被提前闻到。
        self.assertTrue(schedules)
        self.assertEqual(schedules[0]["actions"][0]["card"], "松片")


class PaidFreePageAndHandoverTest(unittest.TestCase):
    def test_white_at_2_cannot_guarantee_a(self) -> None:
        guide = booking_demo_guide(white_beat=2)
        a = plan_guaranteed(guide=guide, seats=["甲"], mode="tray")
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "some_reader_choice_breaks_plan")

        # 直接固定甲选第 1 拍青：白页在第 2 拍必被遮。
        bad = local_schedules(
            guide["box"], guide["readers"][0], {( "甲", "青页"): 1}
        )
        self.assertEqual(bad, [])

        max_result = maximum_guaranteed_seats(guide=guide, mode="tray")
        self.assertEqual(max_result["max_seats"], 1)
        self.assertEqual(max_result["seats"], ["乙"])

    def test_tray_mode_signs_two_seats_at_white_3(self) -> None:
        guide = booking_demo_guide(white_beat=3)
        signed = sign_booking(guide, sheet_id="TWO-TRAY", mode="tray")
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["signed_seats"], ["甲", "乙"])
        self.assertEqual(set(signed["promised_cards"]["甲"]), {"青", "白"})
        self.assertEqual(signed["promised_cards"]["乙"], ["赭"])
        self.assertEqual(signed["guarantee"]["plan"]["free_choice_count"], 2)

        # 两个自由页分支都能由同一组许诺实片执行。
        for branch in signed["guarantee"]["plan"]["branches"]:
            check = verify_run(
                guide=guide,
                schedules=branch["schedules"],
                mode="tray",
            )
            self.assertTrue(check["ok"], check["conflicts"] + check["errors"])

    def test_delivery_mode_only_one_seat_guaranteed(self) -> None:
        guide = booking_demo_guide(white_beat=3)
        result = maximum_guaranteed_seats(guide=guide, mode="delivery")
        self.assertEqual(result["max_seats"], 1)
        self.assertEqual(result["options"], [["甲"], ["乙"]])

        both_at_3 = verify_run(
            guide=guide,
            actions=[
                {"reader": "甲", "page": "青页", "beat": 0, "card": "青"},
                {"reader": "甲", "page": "白页", "beat": 3, "card": "白"},
                {"reader": "乙", "page": "乙揭牌", "beat": 3, "card": "赭"},
            ],
            mode="delivery",
        )
        self.assertFalse(both_at_3["ok"])
        self.assertTrue(any(c["kind"] == "courier_busy" for c in both_at_3["conflicts"]))

    def test_same_sheet_continues_from_actual_0_or_1(self) -> None:
        guide = booking_demo_guide(white_beat=3)
        signed = sign_booking(guide, sheet_id="DELIVERY-SAME", mode="delivery")

        sessions = []
        for actual_beat in (0, 1):
            session = ReadingSession(signed, guide["box"])
            replay = session.deliver(
                f"green-{actual_beat}", actual_beat, "甲", "青", "青页"
            )
            self.assertEqual(replay["beat"], actual_beat)
            cont = session.continue_remaining("甲", actual_beat)
            self.assertEqual(cont["status"], "ok")
            self.assertEqual(len(cont["remaining"]), 1)
            self.assertEqual(cont["remaining"][0]["card"], "白")
            self.assertEqual(cont["remaining"][0]["beat"], 3)
            sessions.append(session)

        # 同 op_id 同文重放；异文不能覆盖旧现场。
        sessions[1].deliver("green-1", 1, "甲", "青", "青页")
        with self.assertRaises(IdempotencyConflict):
            sessions[1].deliver("green-1", 0, "甲", "青", "青页")
        with self.assertRaises(StoryBoxError):
            sessions[1].deliver("new-op", 0, "甲", "青", "青页")
        with self.assertRaises(StoryBoxError):
            sessions[1].erase_event()
        with self.assertRaises(StoryBoxError):
            sessions[1].backdate_event()

    def test_tray_handover_keeps_unknown_beat_private_until_return(self) -> None:
        guide = booking_demo_guide(white_beat=3)
        signed = sign_booking(guide, sheet_id="TRAY-HANDOVER", mode="tray")
        session = ReadingSession(signed, guide["box"])

        brief_b = session.brief_for_seat("乙", at_beat=2)
        self.assertEqual(brief_b["promised_cards"], ["赭"])
        self.assertEqual(brief_b["possible_choices"], [
            {"甲.青页": 0}, {"甲.青页": 1}
        ])
        self.assertIn("other_seat_hidden_reveal_beat", brief_b["do_not_use"])
        robust = session.continue_remaining("乙", 2)
        self.assertEqual(robust["status"], "robust_until_tray_return")
        self.assertEqual(robust["possible_choice_count"], 2)
        self.assertEqual(robust["common_remaining"][0]["card"], "赭")

        # 归还后实际为 1 拍，接班人才能把可能世界缩成一个。
        session.return_tray("甲", {"青页": 1})
        self.assertEqual(session.possible_choices(), [{"甲.青页": 1}])
        after = session.continue_remaining("甲", 2)
        self.assertEqual(after["remaining"][0]["beat"], 3)

    def test_delivery_cannot_pre_place_card(self) -> None:
        guide = booking_demo_guide(3)
        signed = sign_booking(guide, mode="delivery")
        session = ReadingSession(signed, guide["box"])
        with self.assertRaises(StoryBoxError):
            session.preposition_card()
        with self.assertRaises(StoryBoxError):
            session.deliver_at_reveal("op", 2, "甲", "白", reveal_beat=3)


class ReceiptAndPersistenceTest(unittest.TestCase):
    def _system_with_pickup(self, path: str = ":memory:") -> LibrarySystem:
        system = LibrarySystem(path)
        copies = [
            {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
            {"id": "C3", "book": "WB", "branch": "north", "format": "hardcover"},
        ]
        system.catalog.submit_revision(
            "v1", ["WA", "WB"], [], copies, op_id="cat-v1"
        )
        system.catalog.activate_revision("v1")
        system.ledger.register_ticket(
            "T1", "R1", "WB", 1, ["north"], ["hardcover"], op_id="tk1"
        )
        hold = system.ledger.reserve("rsv1", "v1", "C3")
        self.assertEqual(hold["status"], "ok")
        pickup = system.ledger.pickup("PK1", hold["hold_id"], "R1", "C3")
        self.assertEqual(pickup["status"], "ok")

        # 归并到同一规范作品后，C1/C3 两套实盒仍附不同卡牌。
        system.catalog.submit_revision(
            "v2", ["WA", "WB"], [{"from": "WB", "to": "WA"}], copies, op_id="cat-v2"
        )
        system.catalog.activate_revision("v2")
        system.storybox.register_box({
            "box_id": "C3",
            "cards": {"青": ["松"], "白": ["梨"]},
            "rules": [{"old": "松", "new": "梨", "lag": 1, "hidden": "梨"}],
        })
        system.storybox.register_box({
            "box_id": "C1",
            "cards": {"青": ["松"]},
            "rules": [],
        })
        return system

    def test_load_box_using_pickup_receipt_op(self) -> None:
        system = self._system_with_pickup()
        box = system.storybox.load_by_receipt("PK1")
        self.assertEqual(box.box_id, "C3")
        self.assertIn("白", box.cards)
        again = system.storybox.load_by_receipt({"status": "ok", "copy": "C3"})
        self.assertEqual(again.box_id, "C3")
        system.close()

    def test_merged_copies_have_different_cards(self) -> None:
        system = self._system_with_pickup()
        c3 = system.storybox.load_box("C3")
        c1 = system.storybox.load_box("C1")
        self.assertEqual(set(c3.cards), {"青", "白"})
        self.assertEqual(set(c1.cards), {"青"})
        canon = system.catalog.canonical_map("v2")
        self.assertEqual(canon["WB"], "WA")
        system.close()

    def test_box_material_reopens_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "lib.db")
            system = self._system_with_pickup(path)
            guide = booking_demo_guide(3)
            sheet = system.storybox.sign_booking(guide, sheet_id="S1", mode="tray")
            self.assertEqual(sheet["status"], "signed")
            system.storybox.save_sheet(sheet)
            system.close()
            reopened = LibrarySystem.reopen(path)
            box = reopened.storybox.load_box("C3")
            self.assertIn("白", box.cards)
            loaded_sheet = reopened.storybox.load_sheet("S1")
            session = reopened.storybox.open_session(loaded_sheet)
            self.assertEqual(len(session.possible_choices()), 2)
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
