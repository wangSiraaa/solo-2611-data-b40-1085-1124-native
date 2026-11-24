import json
import os
import shutil
import tempfile
import unittest

from wrsource.dataset import build_dataset, compare_datasets, load_dataset
from wrsource.proofdesk import (cards_from_dataset, induce_from_dataset,
                                ingest_cards, load_manual, proofread_dataset,
                                retract_and_refresh, revise_card_body,
                                save_manual)
from wrsource.source import SourceService
from wrsource.steno import (BACK, DELETE, INSERT, Card, Command, Manual,
                            apply_semantics, proofread)

SEED = bytes(range(32))


def ins(char):
    return Command(INSERT, char)


def manual():
    m = Manual({
        "u": [("u",)], "v": [("v",)], "w": [("w",)], "x": [("x",)],
        "a": [("a",)], "b": [("b",)],
        "n": [("b",), ("u", "a", "b")],
    })
    for stroke, cmd in (("u", Command(DELETE)), ("v", ins("丙")),
                        ("w", Command(BACK)), ("x", ins("丁")),
                        ("a", ins("甲")), ("b", ins("乙"))):
        m.define(stroke, cmd)
    return m


def cards():
    return [
        Card("card-001", "uv", (Command(DELETE), ins("丙"))),
        Card("card-002", "vw", (ins("丙"), Command(BACK))),
        Card("card-003", "vx", (ins("丙"), ins("丁"))),
        Card("card-004", "wx", (Command(BACK), ins("丁"))),
        Card("card-005", "xv", (ins("丁"), ins("丙"))),
        Card("card-006", "au", (ins("甲"), Command(DELETE))),
        Card("card-007", "bx", (ins("乙"), ins("丁"))),
    ]


class ProofDeskBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "src.sqlite")
        self.svc = SourceService(self.db)
        self.svc.init()
        self.version = ingest_cards(self.svc, cards())
        self.manual_path = os.path.join(self.dir, "manual.json")
        save_manual(Manual({
            "u": [("u",)], "v": [("v",)], "w": [("w",)], "x": [("x",)],
            "a": [("a",)], "b": [("b",)],
            "n": [("b",), ("u", "a", "b")],
        }), self.manual_path)
        self.ds = os.path.join(self.dir, "ds")
        build_dataset(self.svc, self.version, SEED,
                      {"practice": 5}, self.ds)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ProofDeskIntegrationTest(ProofDeskBase):
    def test_cards_read_from_sampled_rows_only(self):
        got = cards_from_dataset(self.ds)
        _, records = load_dataset(self.ds)
        self.assertEqual({c.card_id for c in got},
                         {r["row_id"] for r in records})
        # Bodies are the exact versioned sampler output.
        for card, rec in zip(got, records):
            self.assertEqual(card.body_json(),
                             json.dumps(json.loads(rec["body"]),
                                        ensure_ascii=False, sort_keys=True))

    def test_induce_and_proofread_new_manuscript(self):
        m = load_manual(self.manual_path)
        report = induce_from_dataset(self.ds, m)
        self.assertEqual(report.conflicts, {})
        apply_semantics(m, report)
        result = proofread(m, "uv")
        self.assertEqual(result.readings[0].tokens(),
                         ["delete", "insert:丙"])

    def test_proofread_dataset_helper(self):
        m = load_manual(self.manual_path)
        result = proofread_dataset(self.ds, m, "n")
        self.assertEqual(len(result.readings), 2)
        tokens = sorted(tuple(r.tokens()) for r in result.readings)
        self.assertEqual(tokens, [
            ("delete", "insert:甲", "insert:乙"),
            ("insert:乙",),
        ])

    def test_revise_card_publishes_new_version_and_changes_meaning(self):
        before = load_manual(self.manual_path)
        report = induce_from_dataset(self.ds, before)
        apply_semantics(before, report)
        self.assertEqual(before.meaning("u"), Command(DELETE))

        new_version = revise_card_body(
            self.svc, "card-001", "uv",
            (Command(BACK), ins("丙")))
        self.assertGreater(new_version, self.version)
        ds2 = os.path.join(self.dir, "ds2")
        from wrsource.dataset import refresh_dataset
        refresh_dataset(self.svc, self.ds, new_version=new_version,
                        out_dir=ds2)
        m2 = load_manual(self.manual_path)
        report2 = induce_from_dataset(ds2, m2)
        # Other cards still witness delete, so induction reports a conflict.
        self.assertIn("u", report2.conflicts)
        # The non-conflicted meanings still install normally; u is replaced
        # explicitly with the revised card's evidence.
        apply_semantics(m2, report2)
        m2.define("u", Command(BACK), replace=True)
        outcome = proofread(m2, "uv").readings[0]
        self.assertEqual(outcome.tokens(), ["back", "insert:丙"])

    def test_retract_card_then_refresh_reissues_true_next_row(self):
        _, records = load_dataset(self.ds)
        victim = records[0]["row_id"]
        outcome = retract_and_refresh(
            self.svc, self.ds, victim,
            out_dir=os.path.join(self.dir, "ds_refreshed"))
        refreshed = cards_from_dataset(os.path.join(self.dir, "ds_refreshed"))
        self.assertNotIn(victim, [c.card_id for c in refreshed])
        self.assertEqual(len(refreshed), 5)
        full = os.path.join(self.dir, "ds_full")
        build_dataset(self.svc, outcome["new_version"], SEED,
                      {"practice": 5}, full)
        self.assertTrue(compare_datasets(
            os.path.join(self.dir, "ds_refreshed"), full)["equal"])

    def test_old_version_still_readable_after_revision(self):
        revise_card_body(self.svc, "card-001", "uv",
                         (Command(BACK), ins("丙")))
        row = self.svc.get_row(self.version, "card-001")
        self.assertEqual(json.loads(row["body"])["printed"],
                         ["delete", "insert:丙"])


class ManualFileTest(unittest.TestCase):
    def test_roundtrip(self):
        m = manual()
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "manual.json")
            save_manual(m, path)
            got = load_manual(path)
            self.assertEqual(got.to_dict(), m.to_dict())
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
