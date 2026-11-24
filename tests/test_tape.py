"""制带室（连印带推断）单元测试。"""

import unittest

from wrsource.tape import (Belief, Card, FORWARD, REVERSE, Hypothesis,
                           Receipt, TapeError, TapePattern, TapeRoom)

星 = Card("星", False)
船 = Card("船", False)
月 = Card("月", False)
亮星 = Card("星", True)


def room(receipts=(), patterns=None, pack_size=1, sampled=None):
    return TapeRoom(
        patterns or (TapePattern("星船月", (星, 船, 月)),
                     TapePattern("星月船", (星, 月, 船))),
        tuple(receipts), pack_size=pack_size,
        sampled_pack_indices=None if sampled is None else frozenset(sampled))


class TapeInferenceTest(unittest.TestCase):
    def test_receipt_rules_out_tape_patterns(self):
        # 第 2 包见船：正向、起格 0 时只剩“星月船”这条带
        # （星月船：格 0 星 1 月 2 船，包 2 落在格 2）。
        r = Receipt("r2", 2, 0, design="船")
        hyps = room([r]).feasible_hypotheses(start_cells=[0],
                                             directions=[FORWARD])
        self.assertEqual([(h.pattern.name, h.start_cell, h.direction)
                          for h in hyps], [("星月船", 0, FORWARD)])

    def test_one_hypothesis_must_explain_every_receipt(self):
        # 两张回条必须由同一个（带、起格、方向）共同解释。
        receipts = [Receipt("a", 0, 0, design="星"),
                    Receipt("b", 1, 0, design="船")]
        hyps = room(receipts).feasible_hypotheses()
        self.assertTrue(hyps)
        for h in hyps:
            self.assertTrue(all(
                rc.matches(h.cell_at(rc.pack_index, rc.slot_in_pack))
                for rc in receipts))

    def test_no_hypothesis_survives_contradictory_receipts(self):
        receipts = [Receipt("a", 0, 0, design="船"),
                    Receipt("b", 0, 0, design="月")]
        self.assertEqual(room(receipts).feasible_hypotheses(), ())
        self.assertFalse(room(receipts).build_belief().possible)

    def test_foil_only_receipt(self):
        pats = (TapePattern("普亮", (星, 亮星)),)
        r = Receipt("f", 1, 0, foil=True)
        hyps = TapeRoom(pats, (r,)).feasible_hypotheses()
        self.assertTrue(hyps)
        self.assertTrue(all(h.cell_at(1, 0).foil for h in hyps))

    def test_pack_outcomes_under_belief(self):
        # 无回条时同一未开包在不同解释下有多种整组内容。
        bel = room().build_belief()
        outcomes5 = {c[0] for c in bel.pack_outcomes(5)}
        self.assertIn(船, outcomes5)
        self.assertTrue(len(outcomes5) > 1)
        # 加上回条后，第 5 包被锁成唯一包况。
        r = Receipt("r2", 2, 0, design="船")
        locked = Belief(room([r]).feasible_hypotheses(
            start_cells=[0], directions=[FORWARD]))
        self.assertEqual(locked.pack_outcomes(5), ((船,),))
        # 同一包在确定解释下内容唯一（星月船正向起格 0：包 5 落在格 2=船）。
        h = Hypothesis(TapePattern("星月船", (星, 月, 船)), 0, FORWARD)
        self.assertEqual(h.pack_content(5, 1), (船,))

    def test_receipt_retraction_reopens_belief(self):
        r = Receipt("r2", 2, 0, design="船")
        rm = room([r])
        narrow = rm.build_belief()
        widened = rm.retract_receipt("r2").build_belief()
        self.assertLess(len(narrow.hypotheses), len(widened.hypotheses))
        with self.assertRaises(TapeError):
            rm.retract_receipt("missing")

    def test_observe_filters_belief_like_a_receipt(self):
        rm = room()
        bel = rm.build_belief()
        observed = (船,)
        filtered = rm.filter_belief(bel, 2, observed)
        self.assertTrue(filtered.hypotheses)
        for h in filtered.hypotheses:
            self.assertEqual(h.pack_content(2, 1), observed)

    def test_sampled_directory_gate(self):
        r = Receipt("rx", 9, 0, design="船")
        with self.assertRaises(TapeError):
            room([r], sampled={1, 2})
        ok = room([r], sampled={9})
        self.assertTrue(ok.build_belief().possible)

    def test_validation(self):
        with self.assertRaises(TapeError):
            Receipt("x", 0, 0)                       # 没有任何观察
        with self.assertRaises(TapeError):
            TapePattern("", (星,))                   # 无名带型
        with self.assertRaises(TapeError):
            TapeRoom(())                             # 无候选带
        with self.assertRaises(TapeError):
            TapeRoom((TapePattern("a", (星, 船)),
                      TapePattern("b", (星, 船, 月))))  # 环长不一致
        with self.assertRaises(TapeError):
            room([Receipt("x", 0, 1, design="星")],
                 pack_size=1)                        # 包内格位越界

    def test_round_trip_dict(self):
        rm = room([Receipt("r", 0, 0, foil=False)])
        again = TapeRoom.from_dict({
            "pack_size": 1,
            "patterns": [p.as_dict() for p in rm.patterns],
            "receipts": [r.as_dict() for r in rm.receipts]})
        self.assertEqual(rm.feasible_hypotheses(),
                         again.feasible_hypotheses())

    def test_multi_card_pack(self):
        # 每包两张：分包按连续格位切，包 1 从第 2 格开始。
        pats = (TapePattern("四格", (星, 月, 船, 亮星)),)
        rm = TapeRoom(pats, (), pack_size=2)
        h = Hypothesis(pats[0], 0, FORWARD)
        self.assertEqual(h.pack_content(0, 2), (星, 月))
        self.assertEqual(h.pack_content(1, 2), (船, 亮星))
        # 包 1 的第 2 张是亮星，只观察亮面的回条应能被满足。
        r = Receipt("r", 1, 1, foil=True)
        self.assertTrue(TapeRoom(pats, (r,), pack_size=2)
                        .feasible_hypotheses())

    def test_reverse_direction(self):
        pats = (TapePattern("星船月", (星, 船, 月)),)
        h = Hypothesis(pats[0], 0, REVERSE)
        # 反向：包 0=星, 包 1=月, 包 2=船
        self.assertEqual([h.cell_at(i, 0) for i in range(3)],
                         [星, 月, 船])


if __name__ == "__main__":
    unittest.main()
