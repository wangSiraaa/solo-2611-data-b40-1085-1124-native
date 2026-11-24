"""Tests for the bouldering route-grading layer.

These encode each claim in the task statement as an executable check, so the
README and the behavior cannot drift apart.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from citation_system import export
from citation_system.catalog import Catalog
from citation_system.repository import CitationRepository

from route_grading.model import (
    LEFT, RIGHT, grade, min_steps, enumerate_min_paths, make_hold as H, Route,
)
from route_grading.examples import (
    build_jia_route, JIA_START, JIA_FINISH,
    build_yi_route, build_yi_route_private_start, YI_START, YI_FINISH,
    YI_PRIVATE_START, build_relay_pair, arm_span_grading_example, build_league,
)
from route_grading.product import (
    HoldGeometry, RouteSpec, build_route, USABLE, SUSPENDED,
)
from route_grading.scheduling import (
    Slot, find_cover_plan, relay_schedule, slots_safe_at_beats,
    round_shared_holds, required_gap, validate_delivery, relay_with_delivery,
)
from route_grading.assignment import (
    Group, RouteEntry, assign_round, assign_by_signup, LaneLock,
)
from route_grading.session import (
    Device, SessionPlan, plan_session, assert_single_device,
)


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------
class CoreModelTests(unittest.TestCase):
    def test_jia_two_steps_either_hand_first(self):
        r = build_jia_route()
        result = grade(r, JIA_START, JIA_FINISH, 2)
        self.assertTrue(result.gradable)
        self.assertEqual(result.steps, 2)
        # Both orders are minimum paths; each moves one hand per step.
        paths = enumerate_min_paths(r, JIA_START, JIA_FINISH, 2)
        first_hands = {p.steps[0].hand for p in paths}
        self.assertEqual(first_hands, {LEFT, RIGHT})

    def test_removing_open_right_top_hold_breaks_jia(self):
        r = build_jia_route()
        result = grade(r, JIA_START, JIA_FINISH, 2, removed=["jia.Rf"])
        self.assertFalse(result.gradable)
        self.assertEqual(result.reason, "missing_hold:jia.Rf")

    def test_points_never_descend(self):
        # Left starts at y=1 but the finish needs it at y=0 (below): an
        # upward-only hand can never get there.
        r = Route.from_holds(
            "down",
            [H("a", 0, 1, hands="L"), H("b", 1, 1, hands="R"),
             H("low", 0, 0, hands="L"), H("top", 1, 2, hands="R")],
        )
        result = grade(r, ("a", "b"), ("low", "b"), 4)
        self.assertFalse(result.gradable)
        self.assertEqual(result.reason, "no_path")
        # The lower hold is never entered from above.
        self.assertNotEqual(result.final_pose[0], "low")

    def test_arm_span_limits_pose(self):
        # Hands 4 apart, span 3: cannot even start.
        r = Route.from_holds(
            "wide",
            [H("a", 0, 0, hands="L"), H("b", 4, 0, hands="R"),
             H("c", 4, 1, hands="R")],
        )
        result = grade(r, ("a", "b"), ("a", "c"), 3)
        self.assertFalse(result.gradable)
        self.assertEqual(result.reason, "start_exceeds_arm_span")

    def test_two_distinct_finish_vertices_per_hand(self):
        r = Route.from_holds(
            "same",
            [H("s", 0, 0, hands="B"), H("f", 0, 1, hands="B")],
        )
        # Same hold cannot be the finish for both hands.
        result = grade(r, ("s", "s"), ("f", "f"), 5)
        self.assertFalse(result.gradable)
        self.assertIn("same_hold", result.reason)


# ---------------------------------------------------------------------------
# Relay observation constraint
# ---------------------------------------------------------------------------
class RelayConstraintTests(unittest.TestCase):
    def setUp(self):
        self.r1, self.r2, self.meta = build_relay_pair()
        self.req = {"L": [self.meta["observation"]]}

    def test_requirement_forces_two_scored_moves(self):
        for route, start, finish in (
            (self.r1, self.meta["start1"], self.meta["finish1"]),
            (self.r2, self.meta["start2"], self.meta["finish2"]),
        ):
            bare = grade(route, start, finish, 3)
            strict = grade(route, start, finish, 3, required=self.req)
            self.assertEqual(bare.steps, 1)      # paper shortcut
            self.assertEqual(strict.steps, 2)    # must touch observation first

    def test_each_group_has_both_observation_choices(self):
        paths = enumerate_min_paths(
            self.r1, self.meta["start1"], self.meta["finish1"], 3, required=self.req
        )
        first_touch = {p.steps[0].to_id for p in paths}
        self.assertEqual(first_touch, {"obs.jia", "obs.yi"})

    def test_perfect_replay_exists_but_is_not_safe(self):
        # "即使存在左取甲、右取乙的完美回放，也不足以保安全": there is a
        # conflict-free simultaneous replay, but neither climber can see the
        # other's not-yet-made pick, so both-take-甲 (or both-take-乙) is also
        # possible and collides.  Safety must quantify over ALL min paths.
        from route_grading.scheduling import paths_conflict
        s1 = Slot("L", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("R", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        p1, p2 = s1.paths(), s2.paths()
        # There exists a conflict-free "left-甲 / right-乙" replay.
        clean = [(a, b) for a in p1 for b in p2 if not paths_conflict(a, 0, b, 0)]
        self.assertTrue(clean)
        # Yet not every pairing is clean -> same-beat is unsafe overall.
        self.assertFalse(slots_safe_at_beats(s1, 0, s2, 0)[0])

    def test_same_beat_is_unsafe(self):
        s1 = Slot("G1", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("G2", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        safe, why = slots_safe_at_beats(s1, 0, s2, 0)
        self.assertFalse(safe)
        self.assertEqual(why, "possible_collision")

    def test_cover_jia_left_yi_right_is_complete(self):
        s1 = Slot("LEFT", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("RIGHT", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        plan = find_cover_plan(
            [s1, s2], forced={"LEFT": ["obs.yi"], "RIGHT": ["obs.jia"]}
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.shared_after, {})
        # Grades preserved.
        self.assertEqual(s1.with_cover("obs.yi").steps(), 2)
        self.assertEqual(s2.with_cover("obs.jia").steps(), 2)

    def test_removing_rights_optional_yi_breaks_the_split(self):
        s1 = Slot("LEFT", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("RIGHT", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        # Remove right wing's optional 乙 as well: right may only use 甲.
        plan = find_cover_plan(
            [s1, s2],
            forced={"LEFT": ["obs.yi"], "RIGHT": ["obs.jia", "obs.yi"]},
        )
        self.assertIsNone(plan)

    def test_consecutive_beats_still_unsafe_but_gap_two_safe(self):
        s1 = Slot("G1", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("G2", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        # Beat 1: one climber releases the shared observation hold while the
        # other grabs it -- under any same-beat ordering that is a collision.
        self.assertFalse(slots_safe_at_beats(s1, 0, s2, 1)[0])
        # A gap of two fully separates the shared observation hold.
        self.assertEqual(required_gap(s1, s2), 2)
        self.assertTrue(slots_safe_at_beats(s1, 0, s2, 2)[0])

    def test_relay_with_two_usable_beats_is_separated(self):
        s1 = Slot("G1", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        s2 = Slot("G2", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)
        # Two usable send beats far enough apart -> conflict-free.
        sched = relay_schedule([s1, s2], [0, 2])
        self.assertTrue(sched.feasible)
        # Only one beat is not enough for two groups.
        self.assertFalse(relay_schedule([s1, s2], [0]).feasible)

    def test_delayed_second_number_cannot_backfill_separation(self):
        # Both groups actually first move at beat 0; a number arriving at beat
        # 5 cannot retroactively claim they were separated.
        check = validate_delivery({"G1": 0, "G2": 5}, {"G1": 0, "G2": 0})
        self.assertFalse(check.ok)
        self.assertIn("too_late", check.reason)

    def test_two_numbers_same_beat_rejected(self):
        check = validate_delivery({"G1": 3, "G2": 3}, {"G1": 4, "G2": 4})
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "two_numbers_same_beat")


# ---------------------------------------------------------------------------
# Arm-span-sensitive grading
# ---------------------------------------------------------------------------
class ArmSpanGradingTests(unittest.TestCase):
    def test_grade_changes_with_arm_span(self):
        demo = arm_span_grading_example()
        route, start, finish = demo["route"], demo["start"], demo["finish"]
        grades = {s: min_steps(route, start, finish, s) for s in demo["spans"]}
        self.assertIsNone(grades[1])          # cannot start
        self.assertNotEqual(grades[2], grades[3])  # reach changes the grade

    def test_assignment_uses_actual_arm_span_for_equal_grade(self):
        demo = arm_span_grading_example()
        route, start, finish = demo["route"], demo["start"], demo["finish"]
        # Two groups, same line, different arm span -> different grades.
        e = RouteEntry("L", route, start, finish)
        g_small = Group("S", 2, 1)
        g_large = Group("L", 3, 2)
        # A second, independent copy of the line for the second group.
        route2 = Route.from_holds(
            "span-demo-2",
            [H("t." + h.hold_id.split(".")[-1], h.x, h.y, color=h.color, hands=h.hands)
             for h in route.holds.values()],
            name="臂展演示线2",
        )
        start2 = ("t.Ls", "t.Rs"); finish2 = ("t.Lf", "t.Rf")
        e2 = RouteEntry("L2", route2, start2, finish2)
        # Equal-grade required, but actual spans give different grades -> fail.
        res = assign_round([g_small, g_large], [e, e2], equal_grade_pairs=[("S", "L")])
        self.assertFalse(res.ok)
        self.assertTrue(res.reason.startswith("grade_mismatch"))


# ---------------------------------------------------------------------------
# Assignment of 甲 / 乙
# ---------------------------------------------------------------------------
class JiaYiAssignmentTests(unittest.TestCase):
    def test_shared_left_start_blocks_same_round(self):
        jia, yi = build_jia_route(), build_yi_route()
        groups = [Group("A", 2, 1), Group("B", 2, 2)]
        routes = [RouteEntry("甲", jia, JIA_START, JIA_FINISH),
                  RouteEntry("乙", yi, YI_START, YI_FINISH)]
        res = assign_round(groups, routes)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "undeclared_shared_hold")
        # Shared hold is exactly the left start.
        self.assertEqual(jia.shares_holds_with(yi), {"jia.Ls"})

    def test_private_start_is_fair(self):
        jia, yi = build_jia_route(), build_yi_route_private_start()
        groups = [Group("A", 2, 1), Group("B", 2, 2)]
        routes = [RouteEntry("甲", jia, JIA_START, JIA_FINISH),
                  RouteEntry("乙", yi, YI_PRIVATE_START, YI_FINISH)]
        res = assign_round(groups, routes)
        self.assertTrue(res.ok)
        self.assertEqual(res.grades["A"], 2)
        self.assertEqual(res.grades["B"], 2)

    def test_declared_warmup_overlap_is_allowed(self):
        jia, yi = build_jia_route(), build_yi_route()
        groups = [Group("A", 2, 1), Group("B", 2, 2)]
        routes = [
            RouteEntry("甲", jia, JIA_START, JIA_FINISH,
                       declared_overlap=frozenset({"jia.Ls"})),
            RouteEntry("乙", yi, YI_START, YI_FINISH,
                       declared_overlap=frozenset({"jia.Ls"})),
        ]
        res = assign_round(groups, routes)
        self.assertTrue(res.ok)

    def test_locked_group_keeps_route_others_can_switch(self):
        jia, yi = build_jia_route(), build_yi_route_private_start()
        jia2 = Route.from_holds(
            "jia2",
            [H("j2." + h.hold_id.split(".")[-1], h.x, h.y, color=h.color, hands=h.hands)
             for h in jia.holds.values()],
        )
        groups = [Group("A", 2, 1), Group("B", 2, 2)]
        # Backup route for B that is reachable in 2 steps and disjoint.
        routes = [RouteEntry("甲", jia, JIA_START, JIA_FINISH),
                  RouteEntry("乙", yi, YI_PRIVATE_START, YI_FINISH),
                  RouteEntry("甲2", jia2, ("j2.Ls", "j2.Rs"), ("j2.Lf", "j2.Rf"))]
        lock = LaneLock()
        lock.start("A", "甲")
        # B must not take 甲 (locked) but may take 乙 or 甲2.
        res = assign_round(groups, routes, locked=lock.locked_map)
        self.assertTrue(res.ok)
        self.assertEqual(res.by_group["A"], "甲")
        self.assertIn(res.by_group["B"], ("乙", "甲2"))


# ---------------------------------------------------------------------------
# Session device exclusivity
# ---------------------------------------------------------------------------
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.r1, self.r2, self.meta = build_relay_pair()
        self.req = {"L": [self.meta["observation"]]}
        self.s1 = Slot("L", self.r1, self.meta["start1"], self.meta["finish1"], 3, self.req)
        self.s2 = Slot("R", self.r2, self.meta["start2"], self.meta["finish2"], 3, self.req)

    def test_covers_plan_feasible(self):
        plan = plan_session([self.s1, self.s2], Device.COVERS)
        self.assertTrue(plan.feasible, plan.reason)
        self.assertIsNotNone(plan.covers)
        self.assertEqual(plan.covers.shared_after, {})

    def test_relay_plan_needs_real_delivery(self):
        # Arrival beats 0 and 2 -> actual first moves at 1 and 3 (a number
        # arrives strictly before the real move).
        plan = plan_session(
            [self.s1, self.s2], Device.RELAY_NUMBERS,
            send_beats=[0, 2], observed_first_move={"L": 1, "R": 3},
        )
        self.assertTrue(plan.feasible, plan.reason)
        # If the observed first moves do not follow the arrival (both moved at
        # beat 0), the claimed separation is a backfill and is invalid.
        bad = plan_session(
            [self.s1, self.s2], Device.RELAY_NUMBERS,
            send_beats=[0, 2], observed_first_move={"L": 0, "R": 0},
        )
        self.assertFalse(bad.feasible)

    def test_no_device_fails_when_shared(self):
        plan = plan_session([self.s1, self.s2], Device.NONE)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "shared_holds_without_device")

    def test_mixed_device_rejected(self):
        with self.assertRaises(ValueError):
            assert_single_device([Device.COVERS, Device.RELAY_NUMBERS])


# ---------------------------------------------------------------------------
# League instance
# ---------------------------------------------------------------------------
class LeagueTests(unittest.TestCase):
    def setUp(self):
        self.lines = build_league()

    def _entries(self):
        return [RouteEntry(k, r, s, f) for k, (r, s, f) in self.lines.items()]

    def test_at_most_ten_holds(self):
        all_ids = set()
        for r, _s, _f in self.lines.values():
            all_ids |= r.hold_ids()
        self.assertLessEqual(len(all_ids), 10)

    def test_each_line_two_steps(self):
        for r, s, f in self.lines.values():
            self.assertEqual(grade(r, s, f, 2).steps, 2)

    def test_disjoint_pair_can_share_round_shared_cells_cannot(self):
        # L1 and L4 are hold-disjoint.
        self.assertEqual(self.lines["L1"][0].shares_holds_with(self.lines["L4"][0]), frozenset())
        # L2 and L3 reuse cells but under different hold numbers -> also
        # disjoint as *holds*.
        self.assertEqual(self.lines["L2"][0].shares_holds_with(self.lines["L3"][0]), frozenset())
        # L1 and L2 share holds a,b (same numbers) -> cannot share a round.
        g = [Group("A", 2, 1), Group("B", 2, 2)]
        res = assign_round(g, [RouteEntry("L1", *self.lines["L1"]),
                               RouteEntry("L2", *self.lines["L2"])])
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "undeclared_shared_hold")

    def test_three_groups_get_three_disjoint_lines(self):
        groups = [Group("A", 2, 1), Group("B", 2, 2), Group("C", 2, 3)]
        # Lines {L1,L4} and {L2,L3} are each internally disjoint; only two can
        # be chosen per round because cells overlap across the pairs.  Search
        # over all four with a third group requires a triple that is pairwise
        # hold-disjoint -- none exists here, so the assignment must fail.
        res = assign_round(groups, self._entries())
        self.assertFalse(res.ok)

    def test_two_groups_fair_round(self):
        groups = [Group("A", 2, 1), Group("B", 2, 2)]
        res = assign_by_signup(groups, self._entries(), equal_grade_pairs=[("A", "B")])
        self.assertTrue(res.ok)
        self.assertEqual(res.grades["A"], res.grades["B"])


# ---------------------------------------------------------------------------
# Bridge from the citation product
# ---------------------------------------------------------------------------
def _write_citation_db(tmp: Path):
    cat = Catalog.create(str(tmp / "cat.db"))
    cat.load_pages_document({
        "format": "volume-pages/1", "revision": 1,
        "volumes": [{
            "volume_id": "R1",
            "pages": [
                {"page_id": "h1", "section_id": "holds", "label": "1", "active": True},
                {"page_id": "h2", "section_id": "holds", "label": "2", "active": True},
                {"page_id": "h3", "section_id": "holds", "label": "3", "active": True},
                {"page_id": "h4", "section_id": "holds", "label": "4", "active": True},
            ],
        }],
    })
    repo = CitationRepository(cat)
    repo.create_range("C_RANGE", "开线", "R1", "h1", "h4")
    repo.create_pinned("C_SIGNS", "起终点签", "R1", ["h1", "h2", "h3", "h4"])
    return cat


class ProductBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = _write_citation_db(self.tmp)
        self.repo = CitationRepository(self.cat)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def _products(self):
        _, usable, attention = export.collect_products(self.cat.conn)
        return {p["citation_id"]: p for p in usable + attention}

    def _geometry(self):
        return {
            ("R1", "h1"): HoldGeometry("R1", "h1", 0, 0, "s", frozenset({LEFT})),
            ("R1", "h2"): HoldGeometry("R1", "h2", 1, 0, "s", frozenset({RIGHT})),
            ("R1", "h3"): HoldGeometry("R1", "h3", 0, 1, "f", frozenset({LEFT})),
            ("R1", "h4"): HoldGeometry("R1", "h4", 1, 1, "f", frozenset({RIGHT})),
        }

    def _spec(self):
        return RouteSpec(
            route_id="r", name="test",
            range_citation_id="C_RANGE", pinned_citation_id="C_SIGNS",
            start=(("R1", "h1"), ("R1", "h2")),
            finish=(("R1", "h3"), ("R1", "h4")),
            arm_span=2,
        )

    def test_usable_route_built_and_graded(self):
        built = build_route(self._spec(), self._products(), self._geometry())
        self.assertEqual(built.status, USABLE)
        result = grade(built.route, built.start, built.finish, 2)
        self.assertEqual(result.steps, 2)

    def test_reversed_range_suspends_route(self):
        # Move end h4 before start h1 -> committed RANGE endpoints reversed.
        self.cat.apply_batch(
            {"operation": "move", "volume_id": "R1", "page_id": "h4", "before_page_id": "h1"}
        )
        built = build_route(self._spec(), self._products(), self._geometry())
        self.assertEqual(built.status, SUSPENDED)
        self.assertEqual(built.reason, "range_endpoints_reversed")

    def test_moving_top_page_out_of_range_breaks_line(self):
        # h3 is the left finish sign. Deactivate it -> PINNED needs attention,
        # and the route is suspended (no finish hold).
        self.cat.apply_batch(
            {"operation": "deactivate", "volume_id": "R1", "page_id": "h3"}
        )
        built = build_route(self._spec(), self._products(), self._geometry())
        self.assertEqual(built.status, SUSPENDED)
        self.assertIn("pinned", built.reason)


if __name__ == "__main__":
    unittest.main()