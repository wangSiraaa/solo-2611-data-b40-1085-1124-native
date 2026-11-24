import unittest

from wrsource.printshop import (LockViolation, OutOfSorts, PressError, Stick,
                                TypeBox, analyze_safety, plan_reserve_column,
                                plan_shared_column, press_query,
                                run_press_schedule, ManuscriptOrder,
                                simulate)
from wrsource.steno import (BACK, COMMIT, DELETE, INSERT, Command, Manual,
                            proofread)


def ins(char):
    return Command(INSERT, char)


class TypeBoxTest(unittest.TestCase):
    def test_draw_and_return(self):
        box = TypeBox({"甲": 1})
        box.draw("甲")
        self.assertEqual(box.snapshot(), {})
        with self.assertRaises(OutOfSorts):
            box.draw("甲")
        box.return_sort("甲")
        self.assertEqual(box.snapshot(), {"甲": 1})

    def test_draw_needs_shortage_reports_missing(self):
        with self.assertRaises(OutOfSorts):
            TypeBox({"甲": 1}).draw_needs({"甲": 2})

    def test_copies_are_independent(self):
        box = TypeBox({"甲": 2})
        other = box.copy()
        other.draw("甲")
        self.assertEqual(box.available("甲"), 2)


class StickEditorTest(unittest.TestCase):
    def test_insert_moves_cursor_past_new_sort(self):
        stick = Stick("甲乙", 2, 0, TypeBox({"丙": 1}))
        stick.apply(ins("丙"))
        self.assertEqual((stick.text, stick.cursor), ("甲乙丙", 3))

    def test_uv_proofs_jia_bing(self):
        # Empty-locked stick 甲乙, cursor at end; u=delete, v=insert 丙.
        outcome = simulate("甲乙", [Command(DELETE), ins("丙")],
                           TypeBox({"丙": 1}))
        self.assertFalse(outcome["stopped"])
        self.assertEqual(outcome["text"], "甲丙")

    def test_back_then_insert_gives_jia_bing_bing(self):
        outcome = simulate("甲乙", [Command(BACK), ins("丙")],
                           TypeBox({"丙": 1}))
        self.assertEqual(outcome["text"], "甲丙乙")
        self.assertEqual(outcome["cursor"], 2)

    def test_back_to_boundary_is_a_noop_stop(self):
        stick = Stick("甲", 1, 0, TypeBox())
        stick.apply(Command(BACK))
        self.assertEqual((stick.text, stick.cursor), ("甲", 0))
        ev = stick.apply(Command(BACK))
        self.assertEqual(ev.status, "boundary")
        self.assertEqual(stick.cursor, 0)

    def test_back_can_cross_lock_boundary(self):
        # Retreating onto the locked prefix is allowed; only mutations are not.
        stick = Stick("甲乙", 2, 2, TypeBox())
        stick.apply(Command(BACK))
        self.assertEqual(stick.cursor, 1)
        stick.apply(Command(BACK))
        self.assertEqual(stick.cursor, 0)

    def test_delete_inside_lock_is_refused(self):
        stick = Stick("甲乙", 2, 2, TypeBox({"丙": 1}))
        with self.assertRaises(LockViolation):
            stick.apply(Command(DELETE))
        # The refused step is the last recorded event and nothing changed.
        self.assertEqual(stick.text, "甲乙")
        self.assertEqual(stick.events[-1].status, "refused")

    def test_first_stroke_stops_when_lock_covers_whole_stick(self):
        outcome = simulate("甲乙", [Command(DELETE), ins("丙")],
                           TypeBox({"丙": 1}), locked=2)
        self.assertTrue(outcome["stopped"])
        self.assertEqual(outcome["events"][0]["detail"],
                         "delete would touch locked prefix")
        self.assertEqual(outcome["text"], "甲乙")

    def test_insert_inside_lock_refused(self):
        stick = Stick("甲乙", 1, 2, TypeBox({"丙": 1}))
        with self.assertRaises(LockViolation):
            stick.apply(ins("丙"))

    def test_commit_extends_lock_and_blocks_later_delete(self):
        stick = Stick("甲乙", 1, 0, TypeBox())
        stick.apply(Command(COMMIT))
        self.assertEqual(stick.locked, 1)
        with self.assertRaises(LockViolation):
            stick.apply(Command(DELETE))

    def test_run_stops_at_first_violation(self):
        outcome = simulate("甲乙",
                           [Command(DELETE), ins("丙"), ins("丁")],
                           TypeBox({"丙": 1, "丁": 1}), locked=2)
        self.assertTrue(outcome["stopped"])
        self.assertEqual(len(outcome["events"]), 1)
        self.assertEqual(outcome["text"], "甲乙")

    def test_out_of_sorts_stops_the_run(self):
        outcome = simulate("甲", [ins("乙")], TypeBox())
        self.assertTrue(outcome["stopped"])
        self.assertEqual(outcome["error"], "OutOfSorts")
        self.assertEqual(outcome["text"], "甲")

    def test_delete_returns_sort_to_box(self):
        box = TypeBox()
        stick = Stick("甲", 1, 0, box)
        stick.apply(Command(DELETE))
        self.assertEqual(box.snapshot(), {"甲": 1})


class SameFinalCrossingLockTest(unittest.TestCase):
    """Ligature with two cuts, same free final text, one crossing the lock."""

    def setUp(self):
        self.manual = Manual({
            "u": [("u",)], "a": [("a",)], "x": [("x",)],
            "y": [("u", "a", "x"), ("x",)],
        })
        self.manual.define("u", Command(DELETE))
        self.manual.define("a", ins("甲"))
        self.manual.define("x", ins("丁"))
        self.readings = proofread(self.manual, "y").readings
        self.assertEqual(len(self.readings), 2)

    def test_free_runs_share_one_final_text(self):
        finals = [simulate("甲", r.commands,
                           TypeBox({"甲": 2, "丁": 2}))["text"]
                  for r in self.readings]
        self.assertEqual(finals, ["甲丁", "甲丁"])

    def test_locked_run_breaks_on_first_stroke_with_opposite_safe(self):
        report = analyze_safety("甲", self.readings, "甲丁", locked=1,
                                box=TypeBox({"甲": 2, "丁": 2}))
        self.assertFalse(report.all_safe)
        self.assertFalse(report.all_meet_title)
        self.assertEqual(report.earliest_violation["stopped_step"], 0)
        self.assertEqual(report.earliest_violation["detail"],
                         "delete would touch locked prefix")
        self.assertIsNotNone(report.opposite_reading)
        self.assertEqual(report.opposite_reading["tokens"], ["insert:丁"])


class SharedColumnPlanTest(unittest.TestCase):
    def setUp(self):
        # n = b (insert 乙) or u+a+b (delete 甲, re-cast 甲, insert 乙).
        self.manual = Manual({
            "u": [("u",)], "a": [("a",)], "b": [("b",)],
            "n": [("b",), ("u", "a", "b")],
        })
        self.manual.define("u", Command(DELETE))
        self.manual.define("a", ins("甲"))
        self.manual.define("b", ins("乙"))
        self.readings = proofread(self.manual, "n").readings

    def test_longest_safe_morning_prefix_is_zero(self):
        plan = plan_shared_column("甲", self.readings, "甲乙", 0,
                                  TypeBox({"甲": 2, "乙": 1}))
        self.assertEqual(plan.longest_safe_length, 0)
        self.assertEqual(plan.morning_text, "")
        self.assertEqual(plan.morning_blocker["reason"],
                         "delete would touch locked prefix")
        # With nothing locked up front, the same stick still delivers at night.
        self.assertTrue(plan.evening_possible)
        self.assertEqual([e["final_text"] for e in plan.evening_readings],
                         ["甲乙", "甲乙"])

    def test_box_shortage_blocks_morning_prefix(self):
        plan = plan_shared_column("甲", self.readings, "甲乙", 0,
                                  TypeBox({"乙": 1}))
        # No 甲 sort available for the initial stick's morning print; the only
        # feasible level remains zero and its reason is recorded.
        self.assertEqual(plan.longest_safe_length, 0)
        self.assertIsNotNone(plan.morning_blocker)


class ReserveColumnPlanTest(unittest.TestCase):
    def setUp(self):
        self.manual = Manual({
            "u": [("u",)], "a": [("a",)], "b": [("b",)],
            "n": [("b",), ("u", "a", "b")],
        })
        self.manual.define("u", Command(DELETE))
        self.manual.define("a", ins("甲"))
        self.manual.define("b", ins("乙"))
        self.readings = proofread(self.manual, "n").readings

    def test_two_jia_sorts_cast_both_forms(self):
        plan = plan_reserve_column("甲", self.readings, "甲乙", 0,
                                   TypeBox({"甲": 2, "乙": 1}))
        self.assertTrue(plan.feasible)
        self.assertTrue(plan.all_delivered)
        forms = {e["reading_index"]: e["form"]
                 for e in plan.evening}
        # Keep-reading stays on the printed main stick; delete-reading needs
        # the untouched reserve form.
        self.assertEqual(forms, {0: "main", 1: "reserve"})
        self.assertEqual(plan.reserved_sorts, {"甲": 1})

    def test_one_jia_sort_makes_reserve_impossible(self):
        plan = plan_reserve_column("甲", self.readings, "甲乙", 0,
                                   TypeBox({"甲": 1, "乙": 1}))
        self.assertFalse(plan.feasible)
        self.assertIn("missing", plan.reason)
        self.assertEqual(plan.evening, [])

    def test_press_query_combines_everything(self):
        answer = press_query("甲", self.readings, "甲乙", 0,
                             {"甲": 2, "乙": 1})
        self.assertEqual(answer["cut_index"], 0)
        self.assertEqual(answer["shared"]["longest_safe_length"], 0)
        self.assertTrue(answer["reserve"]["feasible"])
        self.assertFalse(
            answer["safety_against_locked_morning"]["all_safe"])


class BoundaryIndexTest(unittest.TestCase):
    def test_cut_index_uses_ligature_boundary(self):
        manual = Manual({
            "m": [("u",)], "n": [("b",), ("u", "a", "b")],
            "u": [("u",)], "a": [("a",)], "b": [("b",)],
        })
        for stroke, cmd in (("u", Command(DELETE)), ("a", ins("甲")),
                            ("b", ins("乙"))):
            manual.define(stroke, cmd)
        readings = proofread(manual, "mn").readings
        # After the first ligature every reading has consumed one command.
        self.assertEqual({r.boundary_index(1) for r in readings}, {1})

    def test_disagreeing_boundary_is_rejected(self):
        from wrsource.steno import Reading
        r1 = Reading((ins("乙"),), (([("m", ("b",))]),), ("insert:乙",))
        r2 = Reading((Command(DELETE), ins("乙")),
                     (([("m", ("u", "b"))]),),
                     ("delete", "insert:乙"))
        with self.assertRaises(PressError):
            press_query("", [r1, r2], "乙", 1, {"乙": 2})


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.manual = Manual({
            "u": [("u",)], "a": [("a",)], "b": [("b",)],
            "n": [("b",), ("u", "a", "b")],
        })
        self.manual.define("u", Command(DELETE))
        self.manual.define("a", ins("甲"))
        self.manual.define("b", ins("乙"))
        self.readings = proofread(self.manual, "n").readings

    def _order(self):
        first = ManuscriptOrder("headline", "甲", self.readings, "甲乙")
        second = ManuscriptOrder("strap", "甲", self.readings, "甲乙")
        return [first, second]

    def test_shared_schedule_consumes_real_sorts_in_delivery_order(self):
        # Two manuscripts, each title 甲乙 needs one 甲 and one 乙.
        report = run_press_schedule({"甲": 2, "乙": 2}, self._order(),
                                    "shared")
        self.assertTrue(report["all_delivered"])
        self.assertEqual([m["name"] for m in report["manuscripts"]],
                         ["headline", "strap"])
        for entry in report["manuscripts"]:
            self.assertEqual(entry["longest_safe_length"], 0)
            self.assertTrue(entry["delivered"])
        # Both titles printed: the box ends empty.
        self.assertEqual(report["box_final"], {})

    def test_shared_schedule_fails_when_box_runs_out(self):
        report = run_press_schedule({"甲": 1, "乙": 2}, self._order(),
                                    "shared")
        self.assertFalse(report["all_delivered"])
        self.assertTrue(report["manuscripts"][0]["delivered"])
        self.assertFalse(report["manuscripts"][1]["delivered"])

    def test_reserve_schedule_holds_back_real_sorts(self):
        # Each manuscript needs two 甲 sorts while both forms exist.
        report = run_press_schedule({"甲": 4, "乙": 2}, self._order(),
                                    "reserve")
        self.assertTrue(report["all_delivered"])
        for entry in report["manuscripts"]:
            self.assertTrue(entry["feasible"])
            self.assertEqual(entry["reserved_sorts"], {"甲": 1})
        # Two titles 甲乙 keep two 甲 and two 乙 with the readers.
        self.assertEqual(report["box_final"], {"甲": 2})

    def test_reserve_schedule_reports_infeasible_manuscript(self):
        report = run_press_schedule({"甲": 2, "乙": 2}, self._order(),
                                    "reserve")
        # First manuscript casts both forms (needs 2x甲); the second cannot.
        self.assertFalse(report["all_delivered"])
        self.assertTrue(report["manuscripts"][0]["feasible"])
        self.assertFalse(report["manuscripts"][1]["feasible"])
        self.assertIn("missing", report["manuscripts"][1]["reason"])

    def test_unknown_process_rejected(self):
        with self.assertRaises(PressError):
            run_press_schedule({}, self._order(), "telepathy")


if __name__ == "__main__":
    unittest.main()
