"""Tests for the wind-notebook inference and the docking-script planner.

These pin the behaviour to the four worked examples in the task description and
to the catalog/repository integration that feeds the wind notebook.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from citation_system.catalog import Catalog
from citation_system.docking import (
    Stage,
    plan_dual,
    plan_rail,
    plan_rod,
    rail_feasible,
    rail_schedule,
    rod_feasible,
    rod_plan,
)
from citation_system.repository import CitationRepository
from citation_system.wind import (
    Flight,
    flights_from_catalog,
    infer,
    minimal_conflict,
)


def _stage(name, winds, stations, **kw):
    """Convenience: one candidate if winds is a tuple, several if a tuple of tuples."""

    layers = len(winds) if isinstance(winds, tuple) and winds and isinstance(winds[0], int) else len(winds[0])
    candidates = (winds,) if (winds and isinstance(winds[0], int)) else tuple(winds)
    return Stage(name=name, layers=layers, candidates=candidates,
                 start_layer=kw.pop("start_layer", 0), start_x=kw.pop("start_x", 0),
                 stations=stations, **kw)


class WindNotebookTests(unittest.TestCase):
    # ---- Abstract-flight examples (the "calibration sheets") ----
    def test_calibration_two_sheets_pin_one_wind_set(self):
        # Sheet 1: low*1 + high*1 = 3 ; Sheet 2: low*1 = 1  -> (low, high) = (1, 2)
        sheet1 = Flight("sheet1", ((0, 1), (1, 1)), exact=3)
        sheet2 = Flight("sheet2", ((0, 1),), exact=1)
        result = infer([sheet1, sheet2], bands=2)
        self.assertTrue(result.unique)
        self.assertEqual(result.solutions, [{0: 1, 1: 2}])

    def test_interior_insert_adds_dwell_and_moves_winds(self):
        # Insert one more low page into sheet1's range: low*2 + high*1 = 3.
        sheet1 = Flight("sheet1", ((0, 2), (1, 1)), exact=3)
        sheet2 = Flight("sheet2", ((0, 1),), exact=1)
        result = infer([sheet1, sheet2], bands=2)
        self.assertEqual(result.solutions, [{0: 1, 1: 1}])
        # The high band now drifts only 1, so the old docking script (needs 2) dies.
        self.assertEqual(result.solutions[0][1], 1)

    def test_interval_displacement_is_closed(self):
        # low in [0, 2] and low+high in [2, 4]
        f1 = Flight("f1", ((0, 1),), minimum=0, maximum=2)
        f2 = Flight("f2", ((0, 1), (1, 1)), minimum=2, maximum=4)
        result = infer([f1, f2], bands=2)
        self.assertIn({0: 0, 1: 2}, result.solutions)
        self.assertIn({0: 2, 1: 2}, result.solutions)
        self.assertTrue(all(0 <= s[0] <= 2 for s in result.solutions))

    def test_same_band_reentered_accumulates(self):
        # Two visits to band 0: dwell 1 then dwell 2 => 3*w0.
        f = Flight("f", ((0, 1), (0, 2)), exact=6)
        result = infer([f], bands=2)
        self.assertEqual({s[0] for s in result.solutions}, {2})

    def test_conflict_reports_minimal_set(self):
        f1 = Flight("f1", ((0, 1),), exact=2)   # low = 2
        f2 = Flight("f2", ((0, 1),), exact=1)   # low = 1
        f3 = Flight("f3", ((1, 1),), exact=0)   # irrelevant
        result = infer([f1, f2, f3], bands=2)
        self.assertFalse(result.consistent)
        self.assertEqual(set(result.conflicting), {"f1", "f2"})
        # Drop either conflicting flight and a solution reappears.
        self.assertTrue(infer([f1, f3], bands=2).consistent)
        self.assertTrue(infer([f2, f3], bands=2).consistent)

    def test_withheld_when_required_sheet_missing(self):
        f1 = Flight("f1", ((0, 1),), exact=1)
        result = infer([f1], bands=1, required=["f1", "f2_missing"])
        self.assertTrue(result.withheld)
        self.assertIn("f2_missing", result.withheld_reason)

    def test_unusable_flight_is_ignored_but_named(self):
        good = Flight("good", ((0, 1),), exact=1)
        broken = Flight("broken", ((0, 1),), exact=2, usable=False)
        result = infer([good, broken], bands=1)
        self.assertEqual(result.solutions, [{0: 1}])
        self.assertEqual(result.unusable_flights, ["broken"])

    def test_wind_values_bounded_to_minus2_2(self):
        f = Flight("f", ((0, 1),), exact=5)  # impossible: max drift 2
        self.assertFalse(infer([f], bands=1).consistent)

    # ---- Catalog / repository integration ----
    def _catalog(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        cat = Catalog.create(str(tmp / "c.db"))
        cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {
                        "volume_id": "W",
                        "pages": [
                            {"page_id": "a", "section_id": "body", "label": "1", "active": True},
                            {"page_id": "b", "section_id": "body", "label": "2", "active": True},
                            {"page_id": "c", "section_id": "body", "label": "3", "active": True},
                        ],
                    }
                ],
            }
        )
        repo = CitationRepository(cat)
        repo.create_range("CAL", "flight", "W", "a", "c",
                          origin={"displacement": {"exact": 0}})
        return cat, repo

    def test_range_flight_grows_with_interior_insert(self):
        cat, repo = self._catalog()
        bands = {"W/a": (0, 1), "W/b": (1, 1), "W/c": (0, 0)}
        before = flights_from_catalog(cat, repo, bands)
        self.assertEqual(before[0].visits, ((0, 1), (1, 1), (0, 0)))
        cat.apply_batch(
            {"operation": "insert", "volume_id": "W", "before_page_id": "b",
             "page": {"page_id": "x", "section_id": "body", "label": "1a", "active": True}}
        )
        bands["W/x"] = (0, 1)  # inserted low page
        after = flights_from_catalog(cat, repo, bands)
        self.assertEqual(after[0].visits, ((0, 1), (0, 1), (1, 1), (0, 0)))

    def test_suspended_endpoint_makes_flight_unusable(self):
        cat, repo = self._catalog()
        bands = {"W/a": (0, 1), "W/b": (1, 1), "W/c": (0, 0)}
        cat.apply_batch({"operation": "deactivate", "volume_id": "W", "page_id": "c"})
        flights = flights_from_catalog(cat, repo, bands)
        self.assertFalse(flights[0].usable)
        result = infer(flights, bands=2, required=["CAL"])
        self.assertTrue(result.withheld)


class DockingTests(unittest.TestCase):
    LOW, HIGH = 1, -1  # known winds from the first two-stand example

    def test_known_wind_guest_rail_succeeds(self):
        # Both balls start at low x=0; first agreed station is -1.
        # Guest rides the pre-installed rail: rise (low->high) at tick 0 costs 0.
        guest = _stage("guest", (self.LOW, self.HIGH), stations=((-1,),), tokens=0)
        self.assertTrue(rail_feasible(guest))
        self.assertEqual(rail_schedule(guest), (1,))

    def test_without_rail_guest_cannot_reach(self):
        # Forced to stay in low band: x = +1, never -1.
        guest = _stage("guest", (self.LOW, self.HIGH), stations=((-1,),),
                       tokens=0, move_domains=((0,),))
        self.assertFalse(rail_feasible(guest))

    def test_measure_rod_costs_token(self):
        measure = _stage("measure", (self.LOW, self.HIGH), stations=((-1,),), tokens=1)
        plan = rod_plan(measure)
        self.assertTrue(plan.feasible)
        self.assertIn(0, plan.move_ticks)      # rod rises at tick 0
        self.assertEqual(plan.max_tokens_used, 1)
        # No token => cannot rise => cannot reach -1.
        broke = _stage("measure", (self.LOW, self.HIGH), stations=((-1,),), tokens=0)
        self.assertFalse(rod_feasible(broke))

    def test_two_candidate_winds_defeat_any_fixed_rail(self):
        # Sets A=(0,+2), B=(-1,+1). Guest starts low x=0, travels low in tick 0
        # (score line there), and must stop at x=0 in tick 1.
        A, B = (0, 2), (-1, 1)
        guest = Stage("guest", layers=2, candidates=(A, B), start_layer=0, start_x=0,
                      stations=(None, (0,)), score_lines=frozenset({0}),
                      tokens=1, move_domains=((0,), (-1, 0, 1)))
        # Any single fixed schedule fits at most one candidate.
        self.assertFalse(rail_feasible(guest))
        # A reactive rod (delivered by tick 1) fits both by branching on x.
        plan = rod_plan(guest)
        self.assertTrue(plan.feasible)
        self.assertLessEqual(plan.max_tokens_used, 1)

    def test_each_candidate_alone_is_rail_feasible(self):
        A, B = (0, 2), (-1, 1)
        for winds in (A, B):
            st = Stage("s", 2, (winds,), 0, 0, stations=(None, (0,)),
                       score_lines=frozenset({0}), tokens=1,
                       move_domains=((0,), (-1, 0, 1)))
            self.assertTrue(rail_feasible(st))

    def test_rod_handoff_deadline_decides_dual_plan(self):
        # Both stages need to branch at tick 1, so a single rod that must spend
        # one tick in handoff cannot serve the second stage in time.
        A, B = (0, 2), (-1, 1)
        def branched(name):
            return Stage(name, 2, (A, B), 0, 0, stations=(None, (0,)),
                         score_lines=frozenset({0}), tokens=1,
                         move_domains=((0,), (-1, 0, 1)))
        guest, measure = branched("guest"), branched("measure")
        on_time = plan_rod(guest, measure, rods=1, handoff_ticks=0, start_home="measure")
        self.assertTrue(on_time.feasible)
        late = plan_rod(guest, measure, rods=1, handoff_ticks=2, start_home="measure")
        self.assertFalse(late.feasible)
        # Two rods sidestep the handoff entirely.
        two = plan_rod(guest, measure, rods=2, handoff_ticks=5, start_home="measure")
        self.assertTrue(two.feasible)

    def test_rail_preinstall_deadline_is_first_guest_move(self):
        # Stay low on tick 0 (x: 0 -> 1), then rise on tick 1 (1 -> 0).
        guest = _stage("guest", (self.LOW, self.HIGH),
                       stations=((1,), (0,)), tokens=0)
        measure = _stage("measure", (self.LOW, self.HIGH),
                         stations=((1,), (0,)), tokens=1)
        order = plan_rail(guest, measure)
        self.assertTrue(order.feasible)
        # Guest first has to move on tick 1 -> rail must be installed by then.
        self.assertEqual(order.rail_stages["guest"], 1)


if __name__ == "__main__":
    unittest.main()
