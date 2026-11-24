"""Exact tests for the ink-flow model, solver, instruments, ledger and planner."""

from fractions import Fraction

import pytest

from inklab.bits import decode_bits, encode_bits
from inklab.instrument import contract_instrument, exact_instrument, group_instrument
from inklab.ledger import InkLedger
from inklab.model import (
    Beat, H, Pen, Rates, State, V, beat, dip, run,
)
from inklab.planner import (
    Body, DryFixture, DryTest, Test, World, best_guaranteed, evaluate,
    indistinguishable_worlds,
)
from inklab.solver import Observation, Trial, solve
from inklab import scenarios
from obsdelivery.contracts import ReaderContract


# ---------------------------------------------------------------------------
# binary32 original bits
# ---------------------------------------------------------------------------

def test_encode_integers_exact():
    for n in range(0, 64):
        bits, exact = encode_bits(n)
        assert exact and decode_bits(bits) == n


def test_encode_known_patterns():
    assert encode_bits(1)[0] == "3f800000"
    assert encode_bits(2)[0] == "40000000"
    assert encode_bits(Fraction(1, 8))[0] == "3e000000"
    assert encode_bits(0, negative_zero=True)[0] == "80000000"
    # 0.1f pattern round-trips exactly.
    bits, exact = encode_bits(Fraction(13421773, 134217728))
    assert bits == "3dcccccd" and exact


def test_encode_non_dyadic_is_inexact_but_round_trips():
    bits, exact = encode_bits(Fraction(1, 10))
    assert not exact
    assert decode_bits(bits) == Fraction(13421773, 134217728)


# ---------------------------------------------------------------------------
# flow model
# ---------------------------------------------------------------------------

def test_beat_spend_then_refill_capped_by_three_minima():
    pen = Pen("p", 2, 2)
    # spare capacity after spending = 2-0 = 2, belly 2, rate 1 -> refill 1.
    res = beat(pen, State(2, 2), Beat(H, 2), Fraction(1))
    assert res.drew and res.refilled == 1
    assert res.after == State(1, 1)
    # rate 5 is still capped by belly 2 and spare capacity 2.
    res2 = beat(pen, State(2, 2), Beat(H, 2), Fraction(5))
    assert res2.refilled == 2 and res2.after == State(2, 0)


def test_break_when_nib_short():
    pen = Pen("p", 2, 2)
    res = beat(pen, State(0, 2), Beat(V, 1), Fraction(1))
    assert not res.drew and res.refilled == 0
    assert res.after == State(0, 2)  # nothing drawn, no refill on a break


def test_run_stops_at_break():
    pen = Pen("p", 2, 2)
    r = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 1)), {H: 1, V: 1})
    assert r.completed and r.final == State(1, 0)
    r2 = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 2)), {H: 1, V: 1})
    assert not r2.completed and r2.broke_at == 1


def test_text_ink_counts_only_drawn_strokes():
    pen = Pen("p", 2, 2)
    r = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 2)), {H: 1, V: 1})
    assert r.text_ink == 2  # only the horizontal was drawn


def test_rates_fixed_per_direction_and_bad_input():
    with pytest.raises(ValueError):
        Beat("diagonal", 1)
    with pytest.raises(ValueError):
        Rates({("p", "h"): -1})
    with pytest.raises(ValueError):
        Rates({("p", "x"): 1})
    rs = Rates({("p", H): 1, ("p", V): 0})
    assert rs.get("p", H) == 1 and rs.subset(["p"]) == rs


def test_dip_clips_to_caps():
    pen = Pen("p", 1, 3)
    assert dip(pen, nib=5, belly=5) == State(1, 3)
    assert dip(pen, nib=0, belly=2, state=State(1, 1)) == State(0, 2)


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

def test_solver_unique_rate_from_one_weighing():
    pen = Pen("p", 2, 2)
    trial = Trial(
        pen, State(2, 2), (Beat(H, 2),), {H: (0, 1)},
        (Observation(0, "nib", exact_instrument().key(Fraction(1))),
         Observation(0, "belly", exact_instrument().key(Fraction(1)))),
    )
    r = solve(trial)
    assert r.unique and r.explanations[0].rates[H] == 1


def test_solver_clipped_plateau_keeps_two_rates():
    pen = Pen("p", 2, 2)
    # cost1, spare capacity 1 caps refill, so rates 1 and 2 both read nib2 belly1.
    trial = Trial(
        pen, State(2, 2), (Beat(H, 1),), {H: (1, 2)},
        (Observation(0, "nib", exact_instrument().key(Fraction(2))),
         Observation(0, "belly", exact_instrument().key(Fraction(1)))),
    )
    r = solve(trial)
    assert not r.unique and len(r.explanations) == 2


def test_solver_conflict_empty_set():
    pen = Pen("p", 2, 2)
    trial = Trial(
        pen, State(2, 2), (Beat(H, 2),), {H: (0, 1)},
        (Observation(0, "nib", exact_instrument().key(Fraction(1))),
         Observation(0, "belly", exact_instrument().key(Fraction(0)))),
    )
    r = solve(trial)
    assert r.conflict and r.explanations == ()


def test_solver_respects_instrument_buckets():
    # Both rates 0 and 1 read *different* nib values, but a merged instrument
    # makes them the same reading -> both survive (cannot be split).
    pen = Pen("p", 1, 2)
    trial = Trial(
        pen, State(1, 2), (Beat(H, 1),), {H: (0, 1)},
        (Observation(0, "nib", group_instrument([[Fraction(0), Fraction(1)]]).key(Fraction(0))),),
    )
    r = solve(trial, group_instrument([[Fraction(0), Fraction(1)]]))
    assert len(r.explanations) == 2


# ---------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------

def test_group_instrument_merges_and_separates():
    g = group_instrument([[Fraction(0), Fraction(1)]])
    assert g.same_reading(Fraction(0), Fraction(1))
    assert g.distinguishes(Fraction(1), Fraction(2))


def test_contract_instrument_merges_same_delivered_decimal():
    c = ReaderContract.from_mapping(
        {"allowed_scales": [0], "max_abs_coefficient": 99, "max_abs_error": "1/2"}
    )
    inst = contract_instrument(c)
    # 0.4 and 0.1 both round to 0 at scale 0 -> one reading.
    assert inst.same_reading(Fraction(2, 5), Fraction(1, 10))
    assert inst.distinguishes(Fraction(2, 5), Fraction(3, 5))


# ---------------------------------------------------------------------------
# ledger: weighings as original bits + re-openable supply table
# ---------------------------------------------------------------------------

def test_ledger_register_and_verify_roundtrip():
    ledger = InkLedger()
    try:
        pen = Pen("p", 2, 2)
        beats = (Beat(H, 2),)
        ledger.register_weighings("T", pen, State(2, 2), beats, {H: 1})
        good = ledger.verify_supply_table("T", pen, State(2, 2), beats, {H: 1},
                                          reference_version="T-ok")
        assert good["all_zero"] and good["max_abs_difference"] == 0
        bad = ledger.verify_supply_table("T", pen, State(2, 2), beats, {H: 0},
                                         reference_version="T-bad", product_id="T-bad-p")
        assert not bad["all_zero"] and bad["max_abs_difference"] == 1
        rep = ledger.build_report("r", "T-ok")
        assert ledger.reopen_report("r")["content_digest"] == rep["content_digest"]
    finally:
        ledger.close()


def test_ledger_weighted_bits_match_values():
    ledger = InkLedger()
    try:
        pen = Pen("p", 2, 2)
        ws = ledger.register_weighings("T", pen, State(2, 2), (Beat(H, 2),), {H: 1})
        values = {(w.beat_index, w.chamber): w.value for w in ws}
        assert values[(0, "nib")] == 1 and values[(0, "belly")] == 1
        for w in ws:
            assert decode_bits(w.bits) == w.value
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------

def _ab():
    pens = {"A": Pen("A", 1, 3), "B": Pen("B", 1, 3)}
    init = {"A": State(1, 3), "B": State(1, 3)}
    worlds = [
        World("A_fast", {"A": {H: 1, V: 1}, "B": {H: 0, V: 0}}),
        World("B_fast", {"A": {H: 0, V: 0}, "B": {H: 1, V: 1}}),
    ]
    return pens, init, worlds


def _onsite_selector(obs, fast_sig, tested="A", other="B"):
    for ob in obs:
        if ob.tag == "t":
            return tested if ob.bucket == fast_sig else other
    return None


def test_planner_three_strokes_needs_evidence():
    pens, init, worlds = _ab()
    inst = exact_instrument()
    fast = inst.key(Fraction(1))
    no_test = evaluate("no", pens, init, worlds,
                       [Body("b", (Beat(H, 1),) * 3, lambda o: "A")], inst)
    assert not no_test.guaranteed
    tested = evaluate("test", pens, init, worlds,
                      [Test("t", "A", Beat(H, 1)),
                       Body("b", (Beat(H, 1),) * 3, lambda o: _onsite_selector(o, fast))],
                      inst)
    assert tested.guaranteed and tested.max_test_ink == 1


def test_planner_four_strokes_onsite_cannot_guarantee():
    # The on-site probe costs the fast pen one of its exactly-four units.
    pens, init, worlds = _ab()
    inst = exact_instrument()
    fast = inst.key(Fraction(1))
    tested = evaluate("test", pens, init, worlds,
                      [Test("t", "A", Beat(H, 1)),
                       Body("b", (Beat(H, 1),) * 4, lambda o: _onsite_selector(o, fast))],
                      inst)
    assert not tested.guaranteed


def test_planner_dry_test_guarantees_four_without_ink():
    pens, init, worlds = _ab()
    inst = exact_instrument()

    def sel(obs):
        for ob in obs:
            if ob.tag == "d":
                return "A" if ob.bucket == ("tier", "same") else "B"
        return None
    dry = evaluate("dry", pens, init, worlds,
                   [DryTest("d", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)))),
                    Body("b", (Beat(H, 1),) * 4, sel)], inst)
    assert dry.guaranteed and dry.max_test_ink == 0
    # Remove the only clamp -> no observation -> selector cannot choose.
    gone = evaluate("gone", pens, init, worlds,
                    [DryTest("d", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)),
                                                  available=False)),
                     Body("b", (Beat(H, 1),) * 4, sel)], inst)
    assert not gone.guaranteed


def test_planner_merged_tier_cannot_split_rates():
    pens, init, worlds = _ab()
    g = group_instrument([[Fraction(0), Fraction(1)]])
    sig = g.key(Fraction(1))  # equals g.key(0)
    plan = evaluate("m", pens, init, worlds,
                    [Test("t", "A", Beat(H, 1)),
                     Body("b", (Beat(H, 1),) * 4, lambda o: _onsite_selector(o, sig))],
                    g)
    assert not plan.guaranteed
    # Both worlds share one observation trail -> not distinguishable.
    groups = indistinguishable_worlds(
        pens, init, worlds,
        [Test("t", "A", Beat(H, 1))], g,
    )
    assert groups == [["A_fast", "B_fast"]]


def test_best_guaranteed_prefers_least_probe_ink():
    pens, init, worlds = _ab()
    inst = exact_instrument()
    fast = inst.key(Fraction(1))

    def sel(obs):
        for ob in obs:
            if ob.tag == "d":
                return "A" if ob.bucket == ("tier", "same") else "B"
        return None
    onsite = evaluate("onsite", pens, init, worlds,
                      [Test("t", "A", Beat(H, 1)),
                       Body("b", (Beat(H, 1),) * 3, lambda o: _onsite_selector(o, fast))], inst)
    dry = evaluate("dry", pens, init, worlds,
                   [DryTest("d", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)))),
                    Body("b", (Beat(H, 1),) * 3, sel)], inst)
    assert best_guaranteed([onsite, dry]).name == "dry"


# ---------------------------------------------------------------------------
# scenario regression (pins every worked number in the brief)
# ---------------------------------------------------------------------------

def test_scenario_s1():
    s = scenarios.s1_rate_from_one_weighing()
    assert s["unique"] and s["rate_h"] == 1


def test_scenario_s2_completes():
    assert scenarios.s2_exhibit_completes()["completed"]


def test_scenario_s3_breaks():
    s = scenarios.s3_corrected_final_zero_two()
    assert s["rate_h"] == 0 and not s["exhibit_completed"] and s["broke_at"] == 1


def test_scenario_s4_wide_vertical_rejected():
    assert scenarios.s4_wide_vertical_rejected()["rejected"]


def test_scenario_s5_reads():
    s = scenarios.s5_test_stroke_reads()
    assert s["rate0_nib"] == 0 and s["rate1_nib"] == 1 and s["distinct"]


def test_scenario_s6():
    three = scenarios.s6_three_strokes()
    assert not three["no_test_guaranteed"]
    assert three["onsite_guaranteed"] and three["dry_guaranteed"]
    four = scenarios.s6_four_strokes()
    assert not four["onsite_guaranteed"] and four["dry_guaranteed"]
    assert not four["removed_guaranteed"]
    merged = scenarios.s6_merged_tier_fails()
    assert merged["fast_sig_equals_slow_sig"] and not merged["guaranteed"]


def test_scenario_s7():
    s = scenarios.s7_two_rate_sets()
    assert s["line1_guaranteed"] and not s["line2_guaranteed"]


def test_scenario_ledger_and_nonunique():
    led = scenarios.ledger_demo()
    assert led["good_all_zero"] and not led["bad_all_zero"]
    assert led["bad_max_abs_difference"] == 1
    assert led["reopen_same_digest"]
    nu = scenarios.nonunique_rates_demo()
    assert not nu["unique"] and nu["count"] == 2
    assert scenarios.conflict_demo()["conflict"]


# ---------------------------------------------------------------------------
# local weighing originals (bounded trial generation)
# ---------------------------------------------------------------------------

def test_originals_bounded_and_exact():
    from inklab.trialgen import MAX_BEATS, generate_originals

    pens = {"A": Pen("A", 1, 3), "B": Pen("B", 1, 3)}
    init = {"A": State(1, 3), "B": State(1, 3)}
    rates = {"A": {H: 1, V: 1}, "B": {H: 1, V: 1}}
    script = [("A", Beat(H, 1))] * 3 + [("B", Beat(V, 1))] * 2
    out = generate_originals(pens, init, script, rates)
    assert out.beats == 5 and out.pens == ("A", "B")
    for r in out.records:
        assert decode_bits(r.bits) == r.value
    with pytest.raises(ValueError):
        generate_originals(pens, init, [("A", Beat(H, 1))] * (MAX_BEATS + 1), rates)


def test_scenario_originals_demo():
    d = scenarios.originals_demo()
    assert d["over_bound_rejected"] and d["beats"] == 5
    for r in d["records"]:
        assert decode_bits(r["bits"]) == Fraction(r["value"])


# ---------------------------------------------------------------------------
# signed handoff
# ---------------------------------------------------------------------------

def test_handoff_signs_and_refuses_restore():
    d = scenarios.handoff_demo()
    assert d["verified"] and d["successor"] == "worker-2"
    assert d["ink_restore_refused"] and d["ink_rollback_refused"]


def test_handoff_digest_detects_tampering():
    from inklab.handoff import ObservationRecord, sign_handoff, continue_from, HandoffError

    rec = sign_handoff(
        trial="X", instrument="exact",
        observations=(ObservationRecord("t", ("value", Fraction(1))),),
        chosen_pen="A", carried_states={"A": State(0, 3)},
        spent_test_ink={"A": Fraction(1)}, successor="w2",
    )
    # Tamper the stored digest directly -> verification must fail.
    import dataclasses
    bad = dataclasses.replace(rec, digest="deadbeef")
    with pytest.raises(HandoffError):
        continue_from(bad, carried_states={"A": State(0, 3)})
    # Changing the chosen pen is refused.
    with pytest.raises(HandoffError):
        continue_from(rec, carried_states={"A": State(0, 3)}, chosen_pen="B")


def test_merge_check_merged_tier_cannot_split():
    d = scenarios._merge_check_demo()
    assert d["exact_resolved"]
    assert d["merged_0_1"]
    assert not d["merged_resolved"] and d["merged_count"] == 2


def test_worth_discriminating_three_vs_four():
    d = scenarios.worth_discriminating_demo()
    assert d["three_strokes"]["worth_it"] and not d["three_strokes"]["baseline_guaranteed"]
    # Four strokes: the on-site probe spends the very unit it would need.
    assert not d["four_strokes_onsite"]["probe_guaranteed"]


def test_dip_gap_does_not_restore_belly():
    d = scenarios.dip_gap_demo()
    assert d["belly_not_restored"] and d["second"]["drew"]


def test_briefing_chooses_zero_ink_plan():
    d = scenarios.briefing_demo()
    assert d["chosen_plan"] == "dry-clamp" and d["guaranteed"]
    assert d["max_test_ink"] == 0
    assert d["all_plans"]["onsite-probe"]["guaranteed"] is False
