"""Tests for the banquet reconstruction layer.

Each test reproduces a concrete case from the task specification:

* the four-chair case (甲 red, 乙 blue, 丙 both, 丁 green) with its three
  verdicts (feasible via 丙; errata to 甲乙丙丁 blocks it; revoking 丙's
  permission blocks it);
* the red/blue rounds with 丙/戊, under a sound screen (with and without an
  empty installation round) and under a seat swap, plus the final twist that
  makes the designated pair itself the breach;
* the seating enumerator, suspension on missing/unusable citations, and the
  citation-product adapter round-trip.
"""

from __future__ import annotations

import unittest

from banquet.clues import (
    ClueLedger,
    evaluate_secrecy,
    record_round_clues,
    secret_requirements,
)
from banquet.control import (
    ControlError,
    Night,
    ScreenControl,
    SeatSwap,
    SwapControl,
    book_swap,
    can_install_screen,
    chart_for_round,
    find_swap_seats,
    listener_hears,
    simulate_night,
)
from banquet.model import (
    AdjacencyClause,
    Conversation,
    Guest,
    OrderClause,
    PageOrdinance,
    RevocationClause,
    Round,
    SeatChart,
    SecretGroup,
    SubstituteClause,
    build_ordinance,
    enumerate_compliant_charts,
)
from banquet.products import ProductError, ProductSet
from banquet.relay import plan_round
from banquet.seating import attendance_from_products, seat_session
from banquet.products import load_product_files
from banquet.decode import Script, decode_ordinance
from banquet.sheet import build_sheet
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

A = Guest("甲", frozenset({"red"}))
B = Guest("乙", frozenset({"blue"}))
C = Guest("丙", frozenset({"red", "blue"}))
D = Guest("丁", frozenset({"green"}))
FOUR = {g.guest_id: g for g in (A, B, C, D)}


def ab_talk(authorized=frozenset({"丙"})):
    return Conversation(
        conv_id="AB",
        speaker_a="甲",
        speaker_b="乙",
        language="red",
        secret="s_ab",
        authorized=frozenset(authorized),
    )


class FourChairTests(unittest.TestCase):
    def test_original_order_relays_via_bilingual_interpreter(self):
        spec = build_ordinance(
            [PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁")))]
        )
        enum = enumerate_compliant_charts(spec, ["甲", "乙", "丙", "丁"])
        self.assertEqual([c.seats for c in enum.charts], [("甲", "丙", "乙", "丁")])

        chart = enum.charts[0]
        plan = plan_round(chart, Round("r1", (ab_talk(),)), FOUR)
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.routes), 1)
        route = plan.routes[0]
        self.assertEqual(route.path, ("甲", "丙", "乙"))
        self.assertEqual(route.languages, ("red", "blue"))
        self.assertEqual(route.interpreters, frozenset({"丙"}))

    def test_errata_to_jia_yi_bing_ding_blocks_talk(self):
        spec = build_ordinance(
            [
                PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁"))),
                PageOrdinance("op2", RevocationClause("op2", "op1")),
                PageOrdinance("op3", OrderClause("op3", ("甲", "乙", "丙", "丁"))),
            ]
        )
        self.assertEqual(spec.revoked_pages, ("op1",))
        enum = enumerate_compliant_charts(spec, ["甲", "乙", "丙", "丁"])
        self.assertEqual([c.seats for c in enum.charts], [("甲", "乙", "丙", "丁")])
        # 甲/乙 directly adjacent have no common language; the other arc must
        # pass 丁 (green) who is not authorised and shares no language.
        plan = plan_round(enum.charts[0], Round("r1", (ab_talk(),)), FOUR)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failures[0].reason, "no_authorized_relay_path")

    def test_restore_order_but_revoke_c_permission_blocks_talk(self):
        spec = build_ordinance(
            [PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁")))]
        )
        chart = enumerate_compliant_charts(spec, ["甲", "乙", "丙", "丁"]).charts[0]
        plan = plan_round(chart, Round("r1", (ab_talk(authorized=frozenset()),)), FOUR)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failures[0].reason, "no_authorized_relay_path")


class OrdinanceTests(unittest.TestCase):
    def test_revocation_only_cancels_the_targeted_page(self):
        # Two order clauses that are *separately* listed on their pages;
        # revoking p1 leaves p2's precedence.  (Each OrderClause contributes
        # its own adjacent pairs, so p1 gives 甲<乙 and p2 gives 丙<丁.)
        spec = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲", "乙"))),
                PageOrdinance("p2", OrderClause("p2", ("丙", "丁"))),
            ]
        )
        self.assertIn(("甲", "乙"), spec.precedence)
        self.assertIn(("丙", "丁"), spec.precedence)
        spec2 = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲", "乙"))),
                PageOrdinance("p2", OrderClause("p2", ("丙", "丁"))),
                PageOrdinance("p3", RevocationClause("p3", "p1")),
            ]
        )
        self.assertNotIn(("甲", "乙"), spec2.precedence)
        self.assertIn(("丙", "丁"), spec2.precedence)
        self.assertEqual(spec2.applied_pages, ("p2",))

    def test_inherit_order_substitute_takes_over_precedence(self):
        spec = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲", "丙", "乙", "丁"))),
                PageOrdinance(
                    "p2",
                    SubstituteClause("p2", original="丙", substitute="戊",
                                     mode="inherit_order"),
                ),
            ]
        )
        # 丙 is replaced by 戊 everywhere in the precedence chain.
        self.assertIn(("甲", "戊"), spec.precedence)
        self.assertIn(("戊", "乙"), spec.precedence)
        self.assertNotIn(("甲", "丙"), spec.precedence)

    def test_borrow_chair_only_substitute_inherits_nothing(self):
        spec = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲", "乙"))),
                PageOrdinance(
                    "p2",
                    SubstituteClause("p2", original="丁", substitute="戊",
                                     mode="borrow_chair_only"),
                ),
            ]
        )
        # Precedence untouched; the borrower recorded separately.
        self.assertIn(("甲", "乙"), spec.precedence)
        self.assertEqual(spec.chair_borrows, {"丁": "戊"})
        # 戊 present, 丁 absent: compliant on substitution grounds.
        chart = SeatChart(("甲", "乙", "戊"))
        ok, why = spec.allows(chart)
        self.assertTrue(ok, why)
        # 丁 still seated: borrow impossible.
        chart2 = SeatChart(("甲", "乙", "丁", "戊"))
        ok2, why2 = spec.allows(chart2)
        self.assertFalse(ok2)
        self.assertTrue(any("borrow_chair" in r for r in why2))

    def test_adjacency_clause(self):
        spec = build_ordinance(
            [PageOrdinance("p1", AdjacencyClause("p1", (__import__("banquet.model", fromlist=["Edge"]).Edge("甲", "乙"),)))]
        )
        enum = enumerate_compliant_charts(spec, ["甲", "乙", "丙"])
        for chart in enum.charts:
            self.assertTrue(chart.adjacent("甲", "乙"))

    def test_no_compliant_chart_gives_basis(self):
        # Require 甲 adjacent 乙 AND 甲 adjacent 丙 AND 甲 head, with 丁 also
        # present; impossible if 乙 and 丙 each need the single seat left.
        from banquet.model import Edge

        spec = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲",), head="甲")),
                PageOrdinance("p2", AdjacencyClause("p2", (Edge("甲", "乙"), Edge("甲", "丙")))),
            ]
        )
        enum = enumerate_compliant_charts(spec, ["甲", "乙", "丙"])
        # 甲 at head has exactly two neighbours; 乙 and 丙 can be those two, so
        # this *is* feasible with three people -- assert the feasible path.
        self.assertTrue(enum.feasible)
        enum_bad = enumerate_compliant_charts(spec, ["甲", "乙", "丙", "丁"])
        # With 丁 also present, 甲 still only has two neighbours; 乙 and 丙 can
        # both be adjacent to 甲, so still feasible.  Force an impossible one:
        from banquet.model import Edge as E2
        spec2 = build_ordinance(
            [
                PageOrdinance("p1", OrderClause("p1", ("甲",), head="甲")),
                PageOrdinance(
                    "p2",
                    AdjacencyClause("p2", (E2("甲", "乙"), E2("甲", "丙"), E2("甲", "丁"))),
                ),
            ]
        )
        enum2 = enumerate_compliant_charts(spec2, ["甲", "乙", "丙", "丁"])
        self.assertFalse(enum2.feasible)
        self.assertTrue(any("adjacency" in b for b in enum2.basis))


# ---------------------------------------------------------------------------
# Red / blue rounds: 丙 and 戊
# ---------------------------------------------------------------------------
#
# Geometry.  On a simple ring an *internal* interpreter has exactly two
# neighbours, both used by its relayed path -- so "丙 sits adjacent 戊" cannot
# coexist with "戊 interprets a path that avoids the 丙-戊 seam".  The spec's
# round-2 line "由戊转蓝片，丙在戊邻位" therefore has 戊 as the one who
# *conveys* the blue slip (a path endpoint), leaving its other neighbour free
# for 丙.  Red (round 1) is "已实际开口" -- already spoken in an earlier
# seating -- so its clue stays with 丙 (clues follow the person) even though
# the ring is later arranged for blue.
#
# Base ring (6 chairs, one empty seat), read from the head's left:
#   0 甲  1 丙  2 戊  3 丁  4 乙  5 (empty)
#   * 丙 is adjacent 戊 (seam 丙-戊) -> 丙 would overhear the blue slip;
#   * round-2 path is 戊 <-seam-> 丁, which does NOT use the 丙-戊 seam;
#   * the empty chair is 丙's unlocked fallback seat under a swap.

JIA = A                       # 甲 red
YI = B                        # 乙 blue
BING = C                      # 丙 red+blue
DING = Guest("丁", frozenset({"blue", "green"}))
WU = Guest("戊", frozenset({"blue", "green"}))
FIVE = {g.guest_id: g for g in (JIA, YI, BING, DING, WU)}


def red_conv():
    # 丙 relays a red slip between 甲 (red) and 乙 (blue).  Only 丙 may relay red.
    return Conversation(
        conv_id="R1", speaker_a="甲", speaker_b="乙",
        language="red", secret="red", authorized=frozenset({"丙"}),
    )


def blue_conv(incidental=True):
    # 戊 conveys the blue slip to 丁, both in blue.  One segment, so a neighbour
    # of 戊 who hears that segment hears the whole blue slip.
    return Conversation(
        conv_id="R2", speaker_a="戊", speaker_b="丁",
        language="blue", secret="blue",
        incidental=incidental,
    )


def base_chart():
    return SeatChart(("甲", "丙", "戊", "丁", "乙", None))


def red_prior_chart():
    # Earlier seating in which 丙 sits between 甲 and 乙 and relays red.
    return SeatChart(("甲", "丙", "乙", "丁", "戊", None))


class RoundTests(unittest.TestCase):
    def _night(self, rounds, chart=None, *, quota=(), pairs=(), groups=(), prior=()):
        return Night(
            guests=FIVE,
            base_chart=chart or base_chart(),
            rounds=tuple(rounds),
            ordinance=build_ordinance([]),
            designated_pairs=tuple(pairs),
            secret_groups=tuple(groups),
            swap_quota=tuple(quota),
            prior=tuple(prior),
        )

    def test_red_round_relayed_by_c(self):
        chart = red_prior_chart()
        plan = plan_round(chart, Round("R1", (red_conv(),)), FIVE)
        self.assertTrue(plan.ok)
        route = plan.routes[0]
        self.assertEqual(route.path, ("甲", "丙", "乙"))
        self.assertEqual(route.languages, ("red", "blue"))
        self.assertEqual(route.interpreters, frozenset({"丙"}))

    def test_blue_round_delivered_by_wu_and_path_avoids_c_seam(self):
        chart = base_chart()
        self.assertTrue(chart.adjacent("丙", "戊"))
        plan = plan_round(chart, Round("R2", (blue_conv(),)), FIVE)
        self.assertTrue(plan.ok)
        route = plan.routes[0]
        self.assertEqual(route.path, ("戊", "丁"))
        # The path does not use the 丙-戊 seam that carries 丙's leak.
        path_edges = {frozenset((u, v)) for u, v in zip(route.path, route.path[1:])}
        self.assertNotIn(frozenset(("丙", "戊")), path_edges)

    def test_no_control_c_incidentally_hears_blue(self):
        night = self._night(
            [Round("r0", (blue_conv(),))],
            groups=[SecretGroup("red_vs_blue", frozenset({"red", "blue"}))],
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        self.assertTrue(listener_hears(night, 0, "R2", "丙"))
        # Without a screen 丙 overhears the blue slip and, holding red from the
        # prior round, alone gathers both -> single-interpreter breach.
        res = simulate_night(night)
        self.assertFalse(res.valid)
        self.assertTrue(res.breaches)
        self.assertTrue(any(b.via == "single_interpreter" for b in res.breaches))

    def test_screen_from_empty_round_seals_c_leak(self):
        from banquet.model import Edge

        night = self._night(
            [Round("r0", ()), Round("r1", (blue_conv(),))],
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        seam = Edge("丙", "戊")
        check = can_install_screen(night, seam, before_round=1)
        self.assertTrue(check.feasible)
        self.assertEqual(check.install_round, 0)
        ctrl = ScreenControl(seam=seam, install_round=0)
        self.assertFalse(listener_hears(night, 1, "R2", "丙", control=ctrl))
        self.assertTrue(listener_hears(night, 1, "R2", "丙"))  # no control
        # Round 2's legitimate path still succeeds with the seam blocked.
        res = simulate_night(night, ctrl)
        self.assertTrue(res.round_reports[1].plan.ok)
        self.assertFalse(any(b.via == "single_interpreter" for b in res.breaches))

    def test_screen_cannot_be_installed_without_empty_round(self):
        from banquet.model import Edge

        night = self._night(
            [Round("r0", (blue_conv(),))],
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        seam = Edge("丙", "戊")
        check = can_install_screen(night, seam, before_round=0)
        self.assertFalse(check.feasible)
        self.assertEqual(check.reason, "no_empty_round_before_target")
        # Installing at a speaking round is refused outright; we cannot rescue
        # the sheet by rewriting the record.
        with self.assertRaises(ControlError):
            simulate_night(night, ScreenControl(seam=seam, install_round=0))

    def test_swap_moves_c_to_nonadjacent_compliant_seat(self):
        night = self._night(
            [Round("r0", (blue_conv(),))],
            quota=(1,),
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        seats = find_swap_seats(night, "丙", round_index=0, avoid_adjacent_to=["戊"])
        self.assertTrue(seats)
        chosen = seats[0]
        self.assertFalse(chosen.resulting_chart.adjacent("丙", "戊"))

        ctrl = SwapControl(
            swaps=(SeatSwap(round_index=0, guest_id="丙", target_index=chosen.target_index),)
        )
        self.assertFalse(listener_hears(night, 0, "R2", "丙", control=ctrl))
        booking = book_swap(night, "丙", round_index=0, target_index=chosen.target_index)
        self.assertTrue(booking.bookable)

    def test_swap_outside_quota_refused(self):
        night = self._night(
            [Round("r0", (blue_conv(),))],
            quota=(0,),
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        booking = book_swap(night, "丙", round_index=0, target_index=5)
        self.assertFalse(booking.bookable)
        self.assertEqual(booking.reason, "swap_quota_exhausted")

    def test_designated_pair_breaks_even_with_leak_screened(self):
        from banquet.model import Edge

        night = self._night(
            [Round("r0", ()), Round("r1", (blue_conv(),))],
            pairs=[frozenset({"丙", "戊"})],
            groups=[SecretGroup("red_vs_blue", frozenset({"red", "blue"}))],
            prior=[(red_prior_chart(), Round("R1", (red_conv(),)))],
        )
        ctrl = ScreenControl(seam=Edge("丙", "戊"), install_round=0)
        res = simulate_night(night, ctrl)
        # 丙's incidental hearing is blocked, yet 戊 (who conveyed blue) is 丙's
        # registered mutual communicator; together they pool red + blue.
        self.assertFalse(res.valid)
        self.assertEqual(res.status, "breach")
        self.assertTrue(any(b.via == "designated_pair" for b in res.breaches))
        self.assertFalse(any(b.via == "single_interpreter" for b in res.breaches))

    def test_ordinary_round_only_path_audience(self):
        chart = base_chart()
        conv = blue_conv(incidental=False)
        plan = plan_round(chart, Round("R2", (conv,)), FIVE)
        self.assertTrue(plan.ok)
        ledger = ClueLedger()
        record_round_clues(ledger, chart, Round("R2", (conv,)), plan)
        # Path participants 戊,丁 hear; 丙 (adjacent 戊) does NOT in an ordinary
        # round, because incidental hearing is off for this conversation.
        self.assertIn("戊", ledger.heard)
        self.assertIn("丁", ledger.heard)
        self.assertNotIn("丙", ledger.heard)

    def test_screen_on_legitimate_seam_blocks_the_relay(self):
        # A screen also blocks a legitimate direct relay across that seam.  If
        # the only blue route must cross 戊-丁, screening it makes the round
        # infeasible -- the screen cannot magically preserve the talk.
        from banquet.model import Edge

        chart = SeatChart(("甲", "丙", "戊", "丁", "乙", None))
        conv = blue_conv()
        seam = Edge("戊", "丁")
        plan = plan_round(chart, Round("R2", (conv,)), FIVE, blocked={seam})
        self.assertFalse(plan.ok)
        self.assertEqual(plan.failures[0].reason, "no_authorized_relay_path")
        # Without the screen the same round is fine.
        self.assertTrue(plan_round(chart, Round("R2", (conv,)), FIVE).ok)

    def test_screen_seam_not_on_chart_is_flagged(self):
        from banquet.model import Edge

        night = Night(
            guests=FIVE,
            base_chart=base_chart(),
            rounds=(Round("r0", ()), Round("r1", (blue_conv(),))),
            ordinance=build_ordinance([]),
            prior=((red_prior_chart(), Round("R1", (red_conv(),))),),
        )
        # 甲-丁 is not an actual chair join on the base chart.
        sheet = build_sheet(
            night, ScreenControl(seam=Edge("甲", "丁"), install_round=0)
        )
        self.assertTrue(
            any("screen_seam_not_on_chart" in n for n in sheet.result.notes)
        )

    def test_secret_split_across_conversations_needs_all_segments(self):
        # A toast pieced together from two whisper conversations (two segments
        # each) is fully held only after *every* segment is heard.
        c1 = Conversation("T1", "甲", "乙", "red", "toast", authorized=frozenset({"丙"}))
        c2 = Conversation("T2", "甲", "乙", "red", "toast", authorized=frozenset({"丙"}))
        r1, r2 = Round("r1", (c1,)), Round("r2", (c2,))
        p1 = plan_round(red_prior_chart(), r1, FIVE)
        p2 = plan_round(red_prior_chart(), r2, FIVE)
        reqs = secret_requirements([r1, r2], [p1, p2])
        self.assertEqual(
            sorted(reqs["toast"].clues),
            [("T1", 0), ("T1", 1), ("T2", 0), ("T2", 1)],
        )
        ledger = ClueLedger()
        record_round_clues(ledger, red_prior_chart(), r1, p1)
        self.assertFalse(reqs["toast"].clues <= ledger.clues_of("丙"))
        record_round_clues(ledger, red_prior_chart(), r2, p2)
        self.assertTrue(reqs["toast"].clues <= ledger.clues_of("丙"))

    def test_clues_are_monotonic_after_recharting(self):
        # A spoken segment is never "unheard" if the chart is later changed.
        ledger = ClueLedger()
        plan = plan_round(red_prior_chart(), Round("R1", (red_conv(),)), FIVE)
        record_round_clues(ledger, red_prior_chart(), Round("R1", (red_conv(),)), plan)
        before = ledger.clues_of("丙")
        self.assertTrue(before)
        # Re-record on a different chart that does not relay this talk: the
        # already-heard clues persist.
        record_round_clues(
            ledger, base_chart(), Round("R1", (red_conv(),)),
            plan_round(base_chart(), Round("R1", (red_conv(),)), FIVE),
        )
        self.assertTrue(before <= ledger.clues_of("丙"))


class ProductAdapterTests(unittest.TestCase):
    def _row(self, cid, kind, status, pages, reason=None, rev=1):
        return {
            "format": "citation-product/1",
            "catalog_revision": rev,
            "citation_id": cid,
            "text": cid,
            "kind": kind,
            "status": status,
            "source": {"volume_id": "V1", "page_ids": [p[1] for p in pages]},
            "pages": [
                {
                    "volume_id": p[0], "page_id": p[1],
                    "section_id": "body", "label": p[1], "active": True,
                }
                for p in pages
            ],
            "reason": reason,
        }

    def test_usable_products_seat_attendees(self):
        rows = [
            self._row("SIGNIN", "PINNED", "usable",
                      [("V1", "g甲"), ("V1", "g乙"), ("V1", "g丙"), ("V1", "g丁")]),
            self._row("ORD", "RANGE", "usable", [("V1", "op1")]),
        ]
        products = ProductSet.from_rows(rows)
        pages = [PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁")))]
        identity = {
            "V1/g甲": "甲", "V1/g乙": "乙", "V1/g丙": "丙", "V1/g丁": "丁",
        }
        result = seat_session(products, pages, identity_of=identity)
        self.assertTrue(result.ok)
        self.assertEqual([c.seats for c in result.charts], [("甲", "丙", "乙", "丁")])

    def test_suspended_pinned_blocks_seating(self):
        rows = [
            self._row("SIGNIN", "PINNED", "needs_attention",
                      [("V1", "甲"), ("V1", "乙")], reason="pinned_inactive_member"),
            self._row("ORD", "RANGE", "usable", [("V1", "op1")]),
        ]
        products = ProductSet.from_rows(rows)
        result = seat_session(products, [])
        self.assertEqual(result.status, "suspended")
        self.assertIn("pinned_needs_attention:SIGNIN:pinned_inactive_member",
                      result.suspended)

    def test_missing_ordinance_suspends(self):
        rows = [self._row("SIGNIN", "PINNED", "usable", [("V1", "甲")])]
        products = ProductSet.from_rows(rows)
        result = seat_session(products, [])
        self.assertEqual(result.status, "suspended")
        self.assertIn("no_usable_range_ordinance", result.suspended)

    def test_mixed_revisions_suspend(self):
        rows = [
            self._row("SIGNIN", "PINNED", "usable", [("V1", "甲")], rev=1),
            self._row("ORD", "RANGE", "usable", [("V1", "op1")], rev=2),
        ]
        products = ProductSet.from_rows(rows)
        self.assertIn("mixed_catalog_revisions", products.suspend_reasons())

    def test_contract_violation_rejected(self):
        with self.assertRaises(ProductError):
            ProductSet.from_rows([{"format": "bogus"}])


class DecodeTests(unittest.TestCase):
    def _row(self, cid, kind, status, pages, reason=None, rev=1):
        return {
            "format": "citation-product/1",
            "catalog_revision": rev,
            "citation_id": cid,
            "text": cid,
            "kind": kind,
            "status": status,
            "source": {"volume_id": "V1", "page_ids": [p[1] for p in pages]},
            "pages": [
                {
                    "volume_id": p[0], "page_id": p[1],
                    "section_id": "body", "label": p[1], "active": True,
                }
                for p in pages
            ],
            "reason": reason,
        }

    def test_decode_maps_range_pages_to_matching_clauses(self):
        products = ProductSet.from_rows([
            self._row("S", "PINNED", "usable",
                      [("V1", "g1"), ("V1", "g2"), ("V1", "g3")]),
            self._row("O", "RANGE", "usable",
                      [("V1", "p1"), ("V1", "p2")]),
        ])
        script = Script.from_dict({
            "guests": {"甲": ["red"], "乙": ["blue"], "丙": ["red", "blue"]},
            "identities": {"V1/g1": "甲", "V1/g2": "乙", "V1/g3": "丙"},
            "ordinances": [
                {"page_id": "p1", "kind": "order", "sequence": ["甲", "丙", "乙"]},
                {"page_id": "p2", "kind": "revoke", "target_page_id": "p1"},
            ],
        })
        decoded = decode_ordinance(products, script)
        self.assertTrue(decoded.clean, decoded.problems)
        self.assertEqual([p.page_id for p in decoded.ordinance_pages], ["p1", "p2"])
        # p1 revoked by p2, so no precedence survives.
        spec = build_ordinance(decoded.ordinance_pages)
        self.assertEqual(spec.precedence, ())

    def test_undescribed_ordination_page_is_reported(self):
        products = ProductSet.from_rows([
            self._row("S", "PINNED", "usable", [("V1", "g1")]),
            self._row("O", "RANGE", "usable", [("V1", "p1"), ("V1", "pX")]),
        ])
        script = Script.from_dict({
            "guests": {"甲": ["red"]},
            "ordinances": [{"page_id": "p1", "kind": "order", "sequence": ["甲"]}],
        })
        decoded = decode_ordinance(products, script)
        self.assertFalse(decoded.clean)
        self.assertIn("ordination_page_undescribed:V1/pX", decoded.problems)

    def test_script_ordinate_not_in_range_is_reported(self):
        products = ProductSet.from_rows([
            self._row("S", "PINNED", "usable", [("V1", "g1")]),
            self._row("O", "RANGE", "usable", [("V1", "p1")]),
        ])
        script = Script.from_dict({
            "guests": {"甲": ["red"]},
            "ordinances": [
                {"page_id": "p1", "kind": "order", "sequence": ["甲"]},
                {"page_id": "pZ", "kind": "order", "sequence": ["乙"]},
            ],
        })
        decoded = decode_ordinance(products, script)
        self.assertIn("script_ordinance_not_in_range:pZ", decoded.problems)


class RealMaterialsTests(unittest.TestCase):
    """Consume the *unmodified* materials/726 exports end to end."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_initial_export_feeds_banquet_layer(self):
        usable = self.ROOT / "exports/scenario/00_initial/usable.jsonl"
        attn = self.ROOT / "exports/scenario/00_initial/needs_attention.jsonl"
        products = load_product_files(usable, attn)
        self.assertEqual(products.suspend_reasons(), [])
        self.assertEqual(products.single_revision(), 1)
        # C_PIN pages are the sign-in sheet, C_RANGE pages the ordinance.
        attendees, problems = attendance_from_products(products)
        self.assertEqual(problems, [])
        self.assertEqual(
            attendees, ["V1/p2", "V1/p3"]
        )
        self.assertEqual(
            products.usable_ranges()[0].page_keys,
            [("V1", "p2"), ("V1", "p3"), ("V1", "p4")],
        )

    def test_attention_export_suspends(self):
        usable = self.ROOT / "exports/scenario/03_pinned_inactive/usable.jsonl"
        attn = self.ROOT / "exports/scenario/03_pinned_inactive/needs_attention.jsonl"
        products = load_product_files(usable, attn)
        self.assertTrue(products.suspend_reasons())
        result = seat_session(products, [])
        self.assertEqual(result.status, "suspended")


class SheetTests(unittest.TestCase):
    def test_sheet_signed_only_when_clean(self):
        night = Night(
            guests=FIVE,
            base_chart=base_chart(),
            rounds=(Round("r0", (blue_conv(incidental=False),)),),
            ordinance=build_ordinance([]),
        )
        sheet = build_sheet(night)
        self.assertTrue(sheet.signed)

    def test_sheet_unsignd_on_breach(self):
        night = Night(
            guests=FIVE,
            base_chart=base_chart(),
            rounds=(Round("r0", (blue_conv(),)),),
            ordinance=build_ordinance([]),
            secret_groups=(SecretGroup("g", frozenset({"red", "blue"})),),
            prior=((red_prior_chart(), Round("R1", (red_conv(),))),),
        )
        sheet = build_sheet(night)
        self.assertFalse(sheet.signed)
        self.assertEqual(sheet.result.status, "breach")
        self.assertTrue(sheet.diagnostics)  # 丙's incidental hearing reported


class CliTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def _run(self, *args):
        import subprocess, sys
        return subprocess.run(
            [sys.executable, "-m", "banquet", *args],
            cwd=self.ROOT, capture_output=True, text=True,
        )

    def setUp(self):
        import json, tempfile
        self._json = json
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "script.json"
        self.script.write_text(json.dumps({
            "guests": {
                "甲": ["red"], "乙": ["blue"], "丙": ["red", "blue"],
                "丁": ["blue", "green"], "戊": ["blue", "green"],
            },
            "ordinances": [],
        }), encoding="utf-8")

    def test_seat_cli_uses_real_export(self):
        usable = self.ROOT / "exports/scenario/00_initial/usable.jsonl"
        attn = self.ROOT / "exports/scenario/00_initial/needs_attention.jsonl"
        script = self.tmp / "s2.json"
        script.write_text(self._json.dumps({
            "guests": {"甲": ["red"], "乙": ["blue"], "丙": ["red", "blue"]},
            "identities": {"V1/p2": "甲", "V1/p3": "乙"},
            "ordinances": [
                {"page_id": "p2", "kind": "order", "sequence": ["甲", "丙", "乙"]},
                {"page_id": "p3", "kind": "adjacency", "pairs": [["甲", "乙"]]},
                {"page_id": "p4", "kind": "revoke", "target_page_id": "p2"},
            ],
        }), encoding="utf-8")
        out = self._run("seat", "--products", str(usable), str(attn),
                        "--script", str(script))
        self.assertEqual(out.returncode, 0, out.stderr)
        data = self._json.loads(out.stdout)
        self.assertEqual(data["status"], "seated")
        self.assertGreaterEqual(data["chart_count"], 1)

    def _night_payload(self):
        return {
            "chart": ["甲", "丙", "戊", "丁", "乙", None],
            "rounds": [
                {"round_id": "empty", "conversations": []},
                {"round_id": "R2r", "conversations": [{
                    "conv_id": "R2", "speaker_a": "戊", "speaker_b": "丁",
                    "language": "blue", "secret": "blue", "incidental": True,
                }]},
            ],
            "prior": [{
                "chart": ["甲", "丙", "乙", "丁", "戊", None],
                "round": {"round_id": "R1r", "conversations": [{
                    "conv_id": "R1", "speaker_a": "甲", "speaker_b": "乙",
                    "language": "red", "secret": "red", "authorized": ["丙"],
                }]},
            }],
            "secret_groups": [{"group_id": "g", "secrets": ["red", "blue"]}],
        }

    def test_sheet_cli_screen_vs_none(self):
        night = self.tmp / "night.json"
        night.write_text(self._json.dumps(self._night_payload()), encoding="utf-8")
        screen = self._run("sheet", "--script", str(self.script), "--night", str(night),
                           "--control", "screen", "--seam", "丙-戊", "--before-round", "1")
        self.assertEqual(screen.returncode, 0, screen.stderr)
        self.assertTrue(self._json.loads(screen.stdout)["signed"])

        none = self._run("sheet", "--script", str(self.script), "--night", str(night),
                         "--control", "none")
        self.assertEqual(none.returncode, 1)
        data = self._json.loads(none.stdout)
        self.assertFalse(data["signed"])
        self.assertEqual(data["status"], "breach")

    def test_sheet_cli_swap_requires_quota(self):
        payload = self._night_payload()
        night = self.tmp / "night.json"
        night.write_text(self._json.dumps(payload), encoding="utf-8")
        # No quota: refused (exit 2), never silently applied.
        bad = self._run("sheet", "--script", str(self.script), "--night", str(night),
                        "--control", "swap", "--swap", "1:丙:0")
        self.assertEqual(bad.returncode, 2)
        # With quota on round 1: bookable and signed.
        payload["swap_quota"] = [0, 1]
        night.write_text(self._json.dumps(payload), encoding="utf-8")
        ok = self._run("sheet", "--script", str(self.script), "--night", str(night),
                       "--control", "swap", "--swap", "1:丙:0")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(self._json.loads(ok.stdout)["signed"])


if __name__ == "__main__":
    unittest.main()
