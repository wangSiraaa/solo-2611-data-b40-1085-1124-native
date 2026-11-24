import unittest

from wrsource.steno import (BACK, COMMIT, DELETE, INSERT, MAX_GESTURES,
                            MAX_STROKES, Card, Command, Manual, StenoError,
                            apply_semantics, induce_semantics, proofread)


def ins(char):
    return Command(INSERT, char)


def manual_uv():
    # u=delete, v=insert 丙 are installed as semantics below.
    return Manual({"u": [("u",)], "v": [("v",)]})


class CommandTokenTest(unittest.TestCase):
    def test_roundtrip(self):
        for cmd in (Command(DELETE), Command(BACK), Command(COMMIT),
                    ins("丙")):
            self.assertEqual(Command.parse(cmd.token()), cmd)

    def test_bad_token_rejected(self):
        with self.assertRaises(StenoError):
            Command.parse("insert")
        with self.assertRaises(StenoError):
            Command.parse("insert:abc")
        with self.assertRaises(StenoError):
            Command.parse("rewrite")

    def test_insert_token_requires_one_char(self):
        with self.assertRaises(StenoError):
            Command(INSERT, None).token()


class ManualCutTest(unittest.TestCase):
    def setUp(self):
        self.manual = Manual({
            "u": [("u",)],
            "v": [("v",)],
            "w": [("w", "x")],
            "z": [("u", "v"), ("v", "u")],
        })

    def test_complete_partitions_only(self):
        # "uv" must be cut as two single-gesture ligatures; no suffix-only
        # reading of a longer string is ever returned.
        self.assertEqual(self.manual.partitions("uv"), [("u", "v")])
        self.assertEqual(
            sorted(self.manual.partitions("z")),
            [("u", "v"), ("v", "u")])

    def test_unknown_suffix_is_stuck(self):
        stuck = self.manual.first_stuck("uq")
        self.assertEqual(stuck, {"index": 1, "suffix": "q"})
        self.assertEqual(self.manual.cut_partitions("uq"), [])

    def test_multi_char_ligature_advance(self):
        self.assertEqual(self.manual.partitions("w"), [("w", "x")])
        self.assertIsNone(self.manual.first_stuck("wv"))

    def test_stroke_limit_enforced(self):
        with self.assertRaises(StenoError):
            Manual({str(i): [(str(i),)] for i in range(MAX_STROKES + 1)})

    def test_gesture_limit_enforced(self):
        with self.assertRaises(StenoError):
            self.manual.cut_partitions("u" * (MAX_GESTURES + 1))

    def test_one_meaning_per_stroke_needs_replace(self):
        self.manual.define("u", Command(DELETE))
        with self.assertRaises(StenoError):
            self.manual.define("u", Command(BACK))
        self.manual.define("u", Command(BACK), replace=True)
        self.assertEqual(self.manual.meaning("u"), Command(BACK))


class InductionTest(unittest.TestCase):
    def setUp(self):
        self.manual = manual_uv()
        self.cards = [
            Card("c1", "uv", (Command(DELETE), ins("丙"))),
            Card("c2", "vu", (ins("丙"), Command(DELETE))),
        ]

    def test_induce_determined_meanings(self):
        report = induce_semantics(self.cards, self.manual)
        self.assertEqual(report.conflicts, {})
        self.assertEqual(sorted(report.semantics), ["u", "v"])
        self.assertEqual(report.semantics["u"], Command(DELETE))
        self.assertEqual(report.semantics["v"], ins("丙"))
        self.assertEqual(report.unsupported_cards, [])
        apply_semantics(self.manual, report)
        self.assertEqual(self.manual.meaning("u"), Command(DELETE))

    def test_repeated_stroke_same_meaning_is_supported(self):
        cards = [Card("c", "vv", (ins("丙"), ins("丙")))]
        report = induce_semantics(cards, self.manual)
        self.assertEqual(report.semantics["v"], ins("丙"))
        self.assertEqual(report.unsupported_cards, [])

    def test_conflicting_evidence_is_reported_not_guessed(self):
        cards = self.cards + [Card("c3", "uv", (Command(BACK), ins("丙")))]
        report = induce_semantics(cards, self.manual)
        self.assertIn("u", report.conflicts)
        self.assertEqual(set(report.conflicts["u"]["tokens"]),
                         {"back", "delete"})
        # Every card witnessing either meaning is cited as evidence.
        self.assertEqual(set(report.conflicts["u"]["card_ids"]),
                         {"c1", "c2", "c3"})
        self.assertNotIn("u", report.semantics)

    def test_wrong_length_printed_proof_is_unsupported(self):
        # The only complete cut of "uv" has two strokes, never three.
        bad = Card("bad", "uv",
                   (Command(DELETE), ins("丙"), ins("丁")))
        report = induce_semantics([bad], self.manual)
        self.assertEqual(report.unsupported_cards, ["bad"])


class ProofreadTest(unittest.TestCase):
    def setUp(self):
        self.manual = manual_uv()
        self.manual.define("u", Command(DELETE))
        self.manual.define("v", ins("丙"))

    def test_proofread_lists_reading_with_support(self):
        result = proofread(self.manual, "uv")
        self.assertIsNone(result.stuck)
        self.assertEqual(len(result.readings), 1)
        reading = result.readings[0]
        self.assertEqual(reading.tokens(), ["delete", "insert:丙"])
        self.assertEqual(reading.cuts[0], [("u", ("u",)), ("v", ("v",))])

    def test_unknown_stroke_reading_dropped_but_reported(self):
        manual = Manual({"u": [("u",)], "q": [("q",)]})
        manual.define("u", Command(DELETE))
        result = proofread(manual, "uq")
        self.assertEqual(result.readings, [])
        self.assertEqual(result.unknown_strokes, ["q"])

    def test_grouping_merges_cuts_reaching_same_commands(self):
        manual = Manual({"z": [("u", "v"), ("u", "v",)],
                         "u": [("u",)], "v": [("v",)]})
        manual.define("u", Command(DELETE))
        manual.define("v", ins("丙"))
        result = proofread(manual, "z")
        self.assertEqual(len(result.readings), 1)
        # Two distinct cut witnesses back the one command string.
        self.assertEqual(len(result.readings[0].cuts), 2)

    def test_first_stuck_returned_when_no_cut(self):
        result = proofread(self.manual, "uvq")
        self.assertEqual(result.stuck, {"index": 2, "suffix": "q"})
        self.assertEqual(result.readings, [])


class CardBodyTest(unittest.TestCase):
    def test_card_body_roundtrip(self):
        card = Card("c", "uv", (Command(DELETE), ins("丙")))
        row = {"row_id": "c", "body": card.body_json()}
        got = Card.from_row(row)
        self.assertEqual(got, card)

    def test_non_card_body_rejected(self):
        with self.assertRaises(StenoError):
            Card.from_row({"row_id": "x", "body": '{"kind": "other"}'})


if __name__ == "__main__":
    unittest.main()
