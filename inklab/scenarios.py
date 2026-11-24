"""Worked scenarios from the exhibit brief, computed exactly.

Each function returns a plain dict of exact facts (Fractions rendered as
``"n/d"`` strings by :func:`_jsonable`) so a demo or a test can assert on them.
Nothing here uses host float.
"""

from __future__ import annotations

from fractions import Fraction

from .instrument import exact_instrument, group_instrument
from .ledger import InkLedger
from .model import Beat, H, Pen, State, V, beat, run
from .planner import Body, DryFixture, DryTest, Test, World, evaluate
from .solver import Observation, Trial, solve


def _f(v) -> str:
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def _jsonable(obj):
    if isinstance(obj, Fraction):
        return _f(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# S1: rate determined uniquely from one weighing
# ---------------------------------------------------------------------------

def s1_rate_from_one_weighing() -> dict:
    """caps 2/2, full start, horiz cost 2 -> reading nib1 belly1 => rate 1."""
    pen = Pen("p", 2, 2)
    trial = Trial(
        pen=pen,
        initial=State(2, 2),
        beats=(Beat(H, 2),),
        candidates={H: (0, 1)},
        observations=(
            Observation(0, "nib", exact_instrument().key(Fraction(1))),
            Observation(0, "belly", exact_instrument().key(Fraction(1))),
        ),
    )
    result = solve(trial)
    return {
        "readings": {"nib": 1, "belly": 1},
        "explanations": [{d: r for d, r in e.rates.items()} for e in result.explanations],
        "unique": result.unique,
        "conflict": result.conflict,
        "rate_h": result.explanations[0].rates[H] if result.unique else None,
    }


# ---------------------------------------------------------------------------
# S2 / S3 / S4: the exhibit line
# ---------------------------------------------------------------------------

def s2_exhibit_completes() -> dict:
    """Full pen, rate 1 both directions: horiz cost2 then vert cost1 finishes."""
    pen = Pen("p", 2, 2)
    result = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 1)), {H: 1, V: 1})
    return {
        "completed": result.completed,
        "final": {"nib": result.final.nib, "belly": result.final.belly},
        "beats": [
            {"dir": r.direction, "drew": r.drew, "refill": r.refilled,
             "nib": r.after.nib, "belly": r.after.belly}
            for r in result.results
        ],
    }


def s3_corrected_final_zero_two() -> dict:
    """Correct trial final readings to nib0 belly2 => horiz rate 0 => vertical breaks."""
    pen = Pen("p", 2, 2)
    trial = Trial(
        pen=pen, initial=State(2, 2), beats=(Beat(H, 2),),
        candidates={H: (0, 1)},
        observations=(
            Observation(0, "nib", exact_instrument().key(Fraction(0))),
            Observation(0, "belly", exact_instrument().key(Fraction(2))),
        ),
    )
    solved = solve(trial)
    rate_h = solved.explanations[0].rates[H] if solved.unique else None
    exhibit = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 1)), {H: rate_h, V: 1}) if solved.unique else None
    return {
        "rate_h": rate_h,
        "exhibit_completed": None if exhibit is None else exhibit.completed,
        "broke_at": None if exhibit is None else exhibit.broke_at,
        "after_horiz": None if exhibit is None or not exhibit.results else {
            "nib": exhibit.results[0].after.nib,
            "belly": exhibit.results[0].after.belly,
        },
    }


def s4_wide_vertical_rejected() -> dict:
    """Keep rates 1/1, only last beat becomes wide vertical cost 2 -> breaks -> reject."""
    pen = Pen("p", 2, 2)
    result = run(pen, State(2, 2), (Beat(H, 2), Beat(V, 2)), {H: 1, V: 1})
    return {
        "completed": result.completed,
        "broke_at": result.broke_at,
        "rejected": not result.completed,
        "beats": [
            {"dir": r.direction, "cost": r.cost, "drew": r.drew,
             "nib": r.after.nib, "belly": r.after.belly}
            for r in result.results
        ],
    }


# ---------------------------------------------------------------------------
# S5: one test stroke reads 0 or 1 with nib cap 1, belly 2
# ---------------------------------------------------------------------------

def s5_test_stroke_reads() -> dict:
    """nib cap1 belly cap2, full nib1 belly2: test horiz cost1, weigh nib -> 0 or 1."""
    pen = Pen("p", 1, 2)
    out = {}
    for rate in (0, 1):
        r = beat(pen, State(1, 2), Beat(H, 1), Fraction(rate))
        out[rate] = {"nib": r.after.nib, "belly": r.after.belly, "drew": r.drew}
    return {
        "rate0_nib": out[0]["nib"], "rate1_nib": out[1]["nib"],
        "distinct": out[0]["nib"] != out[1]["nib"],
        "detail": out,
    }


# ---------------------------------------------------------------------------
# S6: packing list A/B, caps nib1 belly3, three- and four-stroke bodies
# ---------------------------------------------------------------------------

def _ab_worlds() -> tuple[list[World], dict[str, Pen], dict[str, State]]:
    pens = {"A": Pen("A", 1, 3), "B": Pen("B", 1, 3)}
    initial = {"A": State(1, 3), "B": State(1, 3)}
    worlds = [
        World("A_fast", {"A": {H: 1, V: 1}, "B": {H: 0, V: 0}}),
        World("B_fast", {"A": {H: 0, V: 0}, "B": {H: 1, V: 1}}),
    ]
    return worlds, pens, initial


def _fast_from_onsite(obs, tested="A", other="B", fast_bucket=None):
    """Selector: the on-site test's nib reading is 1 iff the tested pen is fast."""
    for ob in obs:
        if ob.tag == f"test-{tested}":
            return tested if ob.bucket == fast_bucket else other
    return None


def _fast_from_dry(obs, tested="A", other="B"):
    """Selector: same tier => tested pen is the fast one."""
    for ob in obs:
        if ob.tag == f"dry-{tested}":
            return tested if ob.bucket == ("tier", "same") else other
    return None


def s6_three_strokes() -> dict:
    """Three consecutive horiz strokes cost1, no switch. Which plans guarantee?"""
    worlds, pens, initial = _ab_worlds()
    inst = exact_instrument()
    fast_sig = inst.key(Fraction(1))  # fast pen nib reads 1 after a 1-unit test
    beats3 = (Beat(H, 1), Beat(H, 1), Beat(H, 1))

    no_test = evaluate(
        "no-test", pens, initial, worlds,
        [Body("body", beats3, lambda obs: "A")], inst,
    )
    onsite_test = evaluate(
        "onsite-test", pens, initial, worlds,
        [Test("test-A", "A", Beat(H, 1)),
         Body("body", beats3, lambda obs: _fast_from_onsite(obs, fast_bucket=fast_sig))],
        inst,
    )
    dry_test = evaluate(
        "dry-test", pens, initial, worlds,
        [DryTest("dry-A", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)))),
         Body("body", beats3, lambda obs: _fast_from_dry(obs))],
        inst,
    )
    return {
        "no_test_guaranteed": no_test.guaranteed,
        "onsite_guaranteed": onsite_test.guaranteed,
        "onsite_max_test_ink": onsite_test.max_test_ink,
        "dry_guaranteed": dry_test.guaranteed,
        "dry_max_test_ink": dry_test.max_test_ink,
        "best": (lambda r: r.name if r else None)(
            __import__("inklab.planner", fromlist=["best_guaranteed"]).best_guaranteed(
                [no_test, onsite_test, dry_test]
            )
        ),
        "plans": {r.name: r.summary() for r in (no_test, onsite_test, dry_test)},
    }


def s6_four_strokes() -> dict:
    """Four consecutive horiz strokes cost1, no refill: fast pen is exactly 4 units."""
    worlds, pens, initial = _ab_worlds()
    inst = exact_instrument()
    fast_sig = inst.key(Fraction(1))
    beats4 = (Beat(H, 1), Beat(H, 1), Beat(H, 1), Beat(H, 1))

    onsite_test = evaluate(
        "onsite-test", pens, initial, worlds,
        [Test("test-A", "A", Beat(H, 1)),
         Body("body", beats4, lambda obs: _fast_from_onsite(obs, fast_bucket=fast_sig))],
        inst,
    )
    dry_test = evaluate(
        "dry-test", pens, initial, worlds,
        [DryTest("dry-A", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)))),
         Body("body", beats4, lambda obs: _fast_from_dry(obs))],
        inst,
    )
    # Removing the only clamp: the dry test yields no observation.
    removed = evaluate(
        "dry-test-removed", pens, initial, worlds,
        [DryTest("dry-A", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)), available=False)),
         Body("body", beats4, lambda obs: _fast_from_dry(obs))],
        inst,
    )
    fast_only = evaluate(
        "fast-pen-baseline", pens, initial, worlds,
        [Body("body", beats4, lambda obs: "A")], inst,
    )
    return {
        "onsite_guaranteed": onsite_test.guaranteed,
        "onsite_reasons": {w: o.failure for w, o in onsite_test.outcomes.items()},
        "dry_guaranteed": dry_test.guaranteed,
        "dry_max_test_ink": dry_test.max_test_ink,
        "removed_guaranteed": removed.guaranteed,
        "plans": {r.name: r.summary() for r in (onsite_test, dry_test, removed)},
    }


def s6_merged_tier_fails() -> dict:
    """Merge 0 and 1 into one reading tier: on-site test can no longer discriminate."""
    worlds, pens, initial = _ab_worlds()
    grouped = group_instrument([[Fraction(0), Fraction(1)]], name="merged-0-1")
    beats4 = (Beat(H, 1), Beat(H, 1), Beat(H, 1), Beat(H, 1))
    fast_sig = grouped.key(Fraction(1))  # == grouped.key(0) -> useless
    plan = evaluate(
        "onsite-merged", pens, initial, worlds,
        [Test("test-A", "A", Beat(H, 1)),
         Body("body", beats4, lambda obs: _fast_from_onsite(obs, fast_bucket=fast_sig))],
        grouped,
    )
    return {
        "guaranteed": plan.guaranteed,
        "fast_sig_equals_slow_sig": grouped.key(Fraction(0)) == grouped.key(Fraction(1)),
        "reasons": {w: o.failure for w, o in plan.outcomes.items()},
        "plans": {plan.name: plan.summary()},
    }


# ---------------------------------------------------------------------------
# S7: two rate sets — one line passes for both, another cannot be guaranteed
# ---------------------------------------------------------------------------

def s7_two_rate_sets() -> dict:
    worlds, pens, initial = _ab_worlds()
    inst = exact_instrument()
    # line-1: a single horiz stroke cost1 -> either pen can draw it.
    line1 = evaluate(
        "line-1", pens, initial, worlds,
        [Body("body", (Beat(H, 1),), lambda obs: "A")], inst,
    )
    # line-2: four horiz strokes -> needs the fast pen; picking A blindly fails one world.
    line2 = evaluate(
        "line-2", pens, initial, worlds,
        [Body("body", (Beat(H, 1),) * 4, lambda obs: "A")], inst,
    )
    return {
        "line1_guaranteed": line1.guaranteed,
        "line2_guaranteed": line2.guaranteed,
        "line1_worlds": {w: o.completed for w, o in line1.outcomes.items()},
        "line2_worlds": {w: o.completed for w, o in line2.outcomes.items()},
    }


# ---------------------------------------------------------------------------
# Optional test stroke availability (one slot) and site interchangeability
# ---------------------------------------------------------------------------

def s8_optional_single_opportunity() -> dict:
    """Only a single probe slot exists; venues are not interchangeable."""
    return {
        "onsite_venue": "shift gap: one test stroke + nib weighing (spends ink)",
        "bench_venue": "pre-open clamp: dry nib vs reference nib (no ink)",
        "interchangeable": False,
        "two_demo_programs": False,
    }


# ---------------------------------------------------------------------------
# Ledger: register weighings as original bits, verify re-openable supply table
# ---------------------------------------------------------------------------

def ledger_demo() -> dict:
    """Register S1 weighings as bit patterns; verify a re-openable supply table."""
    ledger = InkLedger()
    try:
        pen = Pen("p", 2, 2)
        beats = (Beat(H, 2),)
        # True readings for rate 1 are nib1 belly1: register them.
        ledger.register_weighings("S1", pen, State(2, 2), beats, {H: 1})
        good = ledger.verify_supply_table("S1", pen, State(2, 2), beats, {H: 1},
                                          reference_version="S1-correct")
        bad = ledger.verify_supply_table("S1", pen, State(2, 2), beats, {H: 0},
                                         reference_version="S1-wrong",
                                         product_id="S1-wrong-product")
        report = ledger.build_report("S1-sup-report", "S1-correct")
        reopened = ledger.reopen_report("S1-sup-report")
        return {
            "good_all_zero": good["all_zero"],
            "good_max_abs_difference": good["max_abs_difference"],
            "bad_all_zero": bad["all_zero"],
            "bad_max_abs_difference": bad["max_abs_difference"],
            "report_digest": report["content_digest"],
            "reopen_same_digest": reopened["content_digest"] == report["content_digest"],
        }
    finally:
        ledger.close()


def nonunique_rates_demo() -> dict:
    """A pen can keep two supply rates: a plateau the scale cannot split."""
    pen = Pen("p", 2, 2)
    # cost 1; refill is capped by spare capacity 1, so any rate >= 1 reads nib2 belly1.
    trial = Trial(
        pen=pen, initial=State(2, 2), beats=(Beat(H, 1),),
        candidates={H: (1, 2)},
        observations=(
            Observation(0, "nib", exact_instrument().key(Fraction(2))),
            Observation(0, "belly", exact_instrument().key(Fraction(1))),
        ),
    )
    solved = solve(trial)
    return {
        "explanations": [{d: r for d, r in e.rates.items()} for e in solved.explanations],
        "unique": solved.unique,
        "count": len(solved.explanations),
    }


def conflict_demo() -> dict:
    """Observations no candidate can explain -> empty set -> conflict."""
    pen = Pen("p", 2, 2)
    trial = Trial(
        pen=pen, initial=State(2, 2), beats=(Beat(H, 2),),
        candidates={H: (0, 1)},
        observations=(
            # No candidate yields nib1 belly0 for a cap-2 pen after cost-2.
            Observation(0, "nib", exact_instrument().key(Fraction(1))),
            Observation(0, "belly", exact_instrument().key(Fraction(0))),
        ),
    )
    solved = solve(trial)
    return {"conflict": solved.conflict, "count": len(solved.explanations)}


# ---------------------------------------------------------------------------
# Bounded trial generation: local weighing originals (≤5 pens, ≤10 beats)
# ---------------------------------------------------------------------------

def originals_demo() -> dict:
    """Generate exact weighing originals for a small two-pen, six-beat script."""
    from .trialgen import generate_originals

    pens = {"A": Pen("A", 1, 3), "B": Pen("B", 1, 3)}
    initial = {"A": State(1, 3), "B": State(1, 3)}
    script = [("A", Beat(H, 1)), ("A", Beat(H, 1)), ("A", Beat(H, 1)),
              ("B", Beat(V, 1)), ("B", Beat(V, 1))]
    rates = {"A": {H: 1, V: 1}, "B": {H: 1, V: 1}}
    originals = generate_originals(pens, initial, script, rates)
    # Bound enforcement.
    too_long = [("A", Beat(H, 1))] * 11
    rejected = False
    try:
        generate_originals(pens, initial, too_long, rates)
    except ValueError:
        rejected = True
    return {
        "pens": list(originals.pens),
        "beats": originals.beats,
        "records": originals.as_json()["records"],
        "over_bound_rejected": rejected,
    }


# ---------------------------------------------------------------------------
# Signed handoff: continuation only from the signed record; no ink restored
# ---------------------------------------------------------------------------

def handoff_demo() -> dict:
    from .handoff import HandoffError, ObservationRecord, continue_from, sign_handoff

    pen = Pen("A", 1, 3)
    # On-site probe: fast pen spends one unit, nib goes 1 -> (refill capped) ...
    res = beat(pen, State(1, 3), Beat(H, 1), Fraction(1))
    record = sign_handoff(
        trial="S6", instrument="exact",
        observations=(ObservationRecord("test-A", ("value", Fraction(1)), "3f800000"),),
        chosen_pen="A",
        carried_states={"A": res.after},
        spent_test_ink={"A": Fraction(1)},
        successor="worker-2",
    )
    # Correct continuation: same (spent) state is allowed.
    ok = continue_from(record, carried_states={"A": res.after}, chosen_pen="A")
    # Restoring ink (pretending the probe never happened) is refused.
    restored = False
    try:
        continue_from(record, carried_states={"A": State(1, 3)})
    except HandoffError:
        restored = True
    # Rolling back the spent-test-ink accounting is refused.
    rolled = False
    try:
        continue_from(record, carried_states={"A": res.after},
                      spent_test_ink={"A": Fraction(0)})
    except HandoffError:
        rolled = True
    return {
        "digest": record.digest,
        "verified": ok.digest == record.digest,
        "successor": record.successor,
        "carried_state": {"nib": record.carried_states["A"].nib,
                          "belly": record.carried_states["A"].belly},
        "ink_restore_refused": restored,
        "ink_rollback_refused": rolled,
    }


def _merge_check_demo() -> dict:
    """Merge 0 and 1 into one reading tier, then re-check the nib-only probe.

    The S5 test stroke with nib capacity 1 reads 0 (slow) or 1 (fast).  Merging
    those two into a single reported tier makes the probe useless: both
    candidates survive, so the two rates cannot be split from this reading
    (『并入同一读数档后再核』 / 『不得偷分』).
    """
    merged = group_instrument([[Fraction(0), Fraction(1)]], name="merged-0-1")
    pen = Pen("p", 1, 2)
    trial = Trial(
        pen=pen, initial=State(1, 2), beats=(Beat(H, 1),),
        candidates={H: (0, 1)},
        observations=(Observation(0, "nib", merged.key(Fraction(1))),),
    )
    exact_solved = solve(
        Trial(
            pen=pen, initial=State(1, 2), beats=(Beat(H, 1),),
            candidates={H: (0, 1)},
            observations=(Observation(0, "nib", exact_instrument().key(Fraction(1))),),
        )
    )
    solved = solve(trial, merged)
    return {
        "exact_resolved": exact_solved.unique,
        "merged_0_1": merged.same_reading(Fraction(0), Fraction(1)),
        "still_split_1_2": merged.distinguishes(Fraction(1), Fraction(2)),
        "merged_explanations": [{d: r for d, r in e.rates.items()} for e in solved.explanations],
        "merged_resolved": solved.unique,
        "merged_count": len(solved.explanations),
    }


def all_scenarios() -> dict:
    return _jsonable({
        "S1_rate_from_one_weighing": s1_rate_from_one_weighing(),
        "S2_exhibit_completes": s2_exhibit_completes(),
        "S3_corrected_final_zero_two": s3_corrected_final_zero_two(),
        "S4_wide_vertical_rejected": s4_wide_vertical_rejected(),
        "S5_test_stroke_reads": s5_test_stroke_reads(),
        "S6_three_strokes": s6_three_strokes(),
        "S6_four_strokes": s6_four_strokes(),
        "S6_merged_tier_fails": s6_merged_tier_fails(),
        "S7_two_rate_sets": s7_two_rate_sets(),
        "S8_optional_single_opportunity": s8_optional_single_opportunity(),
        "ledger_demo": ledger_demo(),
        "nonunique_rates": nonunique_rates_demo(),
        "conflict": conflict_demo(),
        "originals_demo": originals_demo(),
        "handoff_demo": handoff_demo(),
        "merge_check": _merge_check_demo(),
        "worth_discriminating": worth_discriminating_demo(),
        "dip_gap": dip_gap_demo(),
        "briefing": briefing_demo(),
    })


# ---------------------------------------------------------------------------
# When is it worth spending ink to discriminate?
# ---------------------------------------------------------------------------

def worth_discriminating_demo() -> dict:
    from .planner import worth_discriminating

    worlds, pens, initial = _ab_worlds()
    inst = exact_instrument()
    fast = inst.key(Fraction(1))

    def sel(obs):
        for ob in obs:
            if ob.tag == "t":
                return "A" if ob.bucket == fast else "B"
        return None

    # Three strokes: no-probe fails in one world; probe guarantees.
    base3 = evaluate("base3", pens, initial, worlds,
                     [Body("b", (Beat(H, 1),) * 3, lambda o: "A")], inst)
    probe3 = evaluate("probe3", pens, initial, worlds,
                      [Test("t", "A", Beat(H, 1)),
                       Body("b", (Beat(H, 1),) * 3, sel)], inst)
    d3 = worth_discriminating([probe3], [base3])

    # Four strokes: even the probe cannot rescue the fast pen (it eats a unit),
    # but the dry probe can.  On-site probe -> not worth it.
    base4 = evaluate("base4", pens, initial, worlds,
                     [Body("b", (Beat(H, 1),) * 4, lambda o: "A")], inst)
    probe4 = evaluate("probe4", pens, initial, worlds,
                      [Test("t", "A", Beat(H, 1)),
                       Body("b", (Beat(H, 1),) * 4, sel)], inst)
    d4 = worth_discriminating([probe4], [base4])
    return {
        "three_strokes": {k: v for k, v in d3.items()},
        "four_strokes_onsite": {k: v for k, v in d4.items()},
    }


# ---------------------------------------------------------------------------
# Dip gap: refill both chambers at the desk card, then continue
# ---------------------------------------------------------------------------

def dip_gap_demo() -> dict:
    """Between strokes, dip to desk-card amounts, then draw — ink is not restored."""
    from .model import dip

    pen = Pen("p", 2, 2)
    # Start full, draw a horiz 2 (nib drains to 1 belly1 with rate1).
    first = beat(pen, State(2, 2), Beat(H, 2), Fraction(1))
    # Dip only the nib up to 2; belly stays at its current 1.
    dipped = dip(pen, nib=2, state=first.after)
    # Now a vertical cost1 completes.
    second = beat(pen, dipped, Beat(V, 1), Fraction(1))
    return {
        "after_first": {"nib": first.after.nib, "belly": first.after.belly},
        "after_dip": {"nib": dipped.nib, "belly": dipped.belly},
        "second": {"drew": second.drew, "nib": second.after.nib,
                   "belly": second.after.belly},
        "belly_not_restored": dipped.belly == first.after.belly,
    }


# ---------------------------------------------------------------------------
# Consolidated shift briefing (recommendation + signed handoff + reopen)
# ---------------------------------------------------------------------------

def briefing_demo() -> dict:
    """The recommendation a shift worker gets, plus a signed handoff record."""
    from .handoff import ObservationRecord, sign_handoff
    from .planner import best_guaranteed

    worlds, pens, initial = _ab_worlds()
    inst = exact_instrument()

    def sel(obs):
        for ob in obs:
            if ob.tag == "d":
                return "A" if ob.bucket == ("tier", "same") else "B"
        return None

    beats4 = (Beat(H, 1),) * 4

    def onsite_sel(obs):
        return "A" if obs and obs[0].bucket == inst.key(Fraction(1)) else "B"

    plans = [
        evaluate("no-evidence", pens, initial, worlds,
                 [Body("b", beats4, lambda o: "A")], inst),
        evaluate("onsite-probe", pens, initial, worlds,
                 [Test("t", "A", Beat(H, 1)), Body("b", beats4, onsite_sel)], inst),
        evaluate("dry-clamp", pens, initial, worlds,
                 [DryTest("d", "A", DryFixture(Fraction(1), (Fraction(0), Fraction(1)))),
                  Body("b", beats4, sel)], inst),
    ]
    best = best_guaranteed(plans)

    # Sign the chosen plan's handoff: dry test, no ink spent, both pens untouched.
    record = sign_handoff(
        trial="S6-four",
        instrument="exact-dry",
        observations=(ObservationRecord("d", ("tier", "same")),),
        chosen_pen="A",
        carried_states={"A": State(1, 3), "B": State(1, 3)},
        spent_test_ink={},
        successor="worker-2",
    )
    return {
        "chosen_plan": best.name if best else None,
        "guaranteed": best.guaranteed if best else False,
        "max_test_ink": best.max_test_ink if best else None,
        "handoff_digest": record.digest,
        "handoff_successor": record.successor,
        "all_plans": {p.name: {"guaranteed": p.guaranteed, "max_test_ink": p.max_test_ink}
                      for p in plans},
    }
