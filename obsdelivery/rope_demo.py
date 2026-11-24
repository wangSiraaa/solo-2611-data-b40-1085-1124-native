"""End-to-end rope workshop demo: ledger, pairing, two benches, tickets.

Run::

    python3 -m obsdelivery.rope_demo [--json] [path/to/materials/rope/input.json]

It loads the real float32 boundary readings from
``materials/rope/input.json`` and walks the requested scenarios:

1. Residual-twist ledger from cumulative readings (with backoff and clamp
   direction), including a bad middle reading that keeps the *total* at zero
   while breaking per-inch pairing, and a tail-attachment that also fails.
2. Pairing two spans on different axes inch by inch against a closed interval.
3. Two benches: A's middle inch can pair with B or C; a clamp-direction
   supplement to B forces B to yield that inch to C; cutting gives it to the
   first customer, and cancelling the old prep order cannot restore it.
4. Re-audit each bench's cutting rights by physical object numbers.
5. A ticket order that arrives one shift late: with both exclusive tickets on
   time it cuts; if one ticket is voided, the two stale-looking tickets cannot.
6. Two orders contesting the same inch: which bench may cut now, which waits.
7. An uncontended valid cut order on its claim day is fulfillable.
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .canonical import frac_str
from .contracts import ReaderContract
from .errors import ValidationError
from .ropes import HEAD, TAIL, Axis, Interval, Span, evaluate_pair
from .twist import (
    build_ledger,
    encode_bits,
    print_residual,
    verify_against_original,
    verify_ledger,
)
from .workshop import (
    BENCH_A,
    BENCH_B,
    CUT_NOW,
    CutOrder,
    SpanRef,
    Workshop,
)

DEFAULT_INPUT = Path(__file__).resolve().parent.parent / "materials" / "rope" / "input.json"
ZERO = Interval(Fraction(0), Fraction(0))


def _s(value: Fraction | None) -> str | None:
    return None if value is None else frac_str(value)


def _load_axes(path: Path) -> dict[str, dict[str, Any]]:
    return {axis["axis_id"]: axis for axis in json.loads(path.read_text())["axes"]}


def _readings(bits: list[str], backoff: Fraction = Fraction(0), flip: bool = False) -> list[dict]:
    return [{"bits": b, "backoff": frac_str(backoff), "flip": flip} for b in bits]


# ---------------------------------------------------------------------------
# 1. ledger scenarios (Jia / Yi)
# ---------------------------------------------------------------------------

def ledger_scenarios() -> dict[str, Any]:
    # Jia cumulative 0,2,1 ; Yi cumulative 0,-2,-1 (fixtures all zero/none).
    jia_bits = ["00000000", "40000000", "3f800000"]
    yi_bits = ["00000000", "c0000000", "bf800000"]
    jia = build_ledger(_readings(jia_bits), version="jia")
    yi = build_ledger(_readings(yi_bits), version="yi")

    # per-segment provenance without evaluating the whole sheet
    provenance_seg2 = jia.provenance(2)

    # jiayi head-to-head pairing on two inches, zero interval
    zero2 = [ZERO, ZERO]
    jia_axis = Axis.from_cumulative("jia", "JY", [0, 2, 1])
    yi_axis = Axis.from_cumulative("yi", "YY", [0, -2, -1])
    head = evaluate_pair(jia_axis, Span(0, 2, HEAD), yi_axis, Span(0, 2, HEAD), zero2)

    # only middle reading of Yi changes to -1: cumulative 0,-1,-1 => [-1,0].
    # Endpoints (0 and -1) are untouched, so the whole-length total is still
    # -1 and still cancels Jia's +1; only the per-inch split changes.
    yi_bad_bits = ["00000000", "bf800000", "bf800000"]
    yi_bad = build_ledger(_readings(yi_bad_bits), version="yi-bad")
    yi_bad_axis = Axis.from_cumulative("yi-bad", "YY", [0, -1, -1])
    bad = evaluate_pair(jia_axis, Span(0, 2, HEAD), yi_bad_axis, Span(0, 2, HEAD), zero2)
    combined_total_before = sum(jia_axis.residuals) + sum(yi_axis.residuals)
    combined_total_after = sum(jia_axis.residuals) + sum(yi_bad_axis.residuals)

    # restore Yi readings, attach from the tail instead
    tail = evaluate_pair(jia_axis, Span(0, 2, HEAD), yi_axis, Span(0, 2, TAIL), zero2)

    # verify the adopted ledgers from source bits (no printed number trusted)
    verify_ledger(_readings(jia_bits), jia)
    verify_ledger(_readings(yi_bits), yi)

    # --- defect: a missing / non-finite boundary point poisons only the
    # segments touching it; the rest of the ledger is still restored. ---
    # Four points -> three inches; remove the interior point 1, so segments 1
    # and 2 (which touch it) are un-signable but segment 3 is still restored.
    four_bits = [encode_bits(v) for v in (0, 1, 2, 3)]
    defect_bits: list[str | None] = list(four_bits)
    defect_bits[1] = None
    defect = build_ledger([{"bits": b} for b in defect_bits], version="jia-defect")
    defect_sheet = [
        {"segment": s.index, "signable": s.signable,
         "residual": None if s.residual is None else frac_str(s.residual)}
        for s in defect.segments
    ]
    nan_bits = list(four_bits)
    nan_bits[1] = "7fc00001"  # NaN at the interior point
    nan_ledger = build_ledger([{"bits": b} for b in nan_bits], version="jia-nan")
    nan_sheet = [s.signable for s in nan_ledger.segments]

    # --- printing with rounding must not be used for re-verification. ---
    # 0.1f (3dcccccd) as one inch: exact residual is 13421773/134217728. A
    # coarse printing scale cannot show it, so the printed value is rounded.
    tenth = build_ledger(
        _readings(["00000000", "3dcccccd"]), version="tenth"
    )
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [0], "max_abs_coefficient": 999, "max_abs_error": "1/2"}
    )
    printed = print_residual(tenth, 1, contract)
    recheck = verify_against_original(tenth, 1, printed.printed_value)

    return {
        "jia_residuals": [_s(s.residual) for s in jia.segments],
        "yi_residuals": [_s(s.residual) for s in yi.segments],
        "provenance_segment_2": provenance_seg2,
        "head_to_head_ok": head.ok,
        "head_to_head_rows": [r.canonical() for r in head.rows],
        "combined_total_before": frac_str(combined_total_before),
        "combined_total_after": frac_str(combined_total_after),
        "total_still_cancels": combined_total_after == 0,
        "middle_change_totals_cancel_but_per_inch_fails": (
            combined_total_after == 0 and not bad.ok
        ),
        "middle_change_rows": [r.canonical() for r in bad.rows],
        "tail_attachment_ok": tail.ok,
        "tail_rows": [r.canonical() for r in tail.rows],
        "defect_missing_sheet": defect_sheet,
        "defect_nan_signable": nan_sheet,
        "printed_segment_1": printed.canonical(),
        "print_equals_exact": recheck["equals_exact"],
        "verification_source": recheck["verification_source"],
        "reprint_note": recheck["note"],
    }


# ---------------------------------------------------------------------------
# 2. two-bench middle-inch scenario (A pairs with B or C)
# ---------------------------------------------------------------------------

def bench_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ws = Workshop()
    for name in ("A", "B", "C"):
        ws.add_yarn(name, _readings(axes[name]["cumulative_bits"]),
                    bench=BENCH_A if name != "B" else BENCH_B)
    A_res = ws.yarns["A"].residuals()
    # A's middle inch is index 1 (residual 1); B and C carry -1.
    left = SpanRef("A", 1, 1)
    ab = CutOrder("A-B", left, SpanRef("B", 0, 1), (ZERO,),
                  claim_shift=1, arrival_seq=1, bench=BENCH_A)
    ac = CutOrder("A-C", left, SpanRef("C", 0, 1), (ZERO,),
                  claim_shift=1, arrival_seq=2, bench=BENCH_A)
    ws.add_cut(ab)
    ws.add_cut(ac)
    before = {"A-B": ws.evaluate("A-B")["ok"], "A-C": ws.evaluate("A-C")["ok"]}

    # prep orders share A's uncut middle inch
    prep_ab = ws.add_prep("PREP-AB", left, SpanRef("B", 0, 1), (ZERO,), shift=1)
    ws.add_prep("PREP-AC", left, SpanRef("C", 0, 1), (ZERO,), shift=1)
    shared_inch = prep_ab.left.inches()[0].canonical()

    # clamp-direction supplement to B: B gains +1, so A+B now breaks
    ws.supplement_clamp_direction("B", flip=True)
    after_supplement = {"A-B": ws.evaluate("A-B")["ok"], "A-C": ws.evaluate("A-C")["ok"]}

    # cancel the stale A-B prep; that must not resurrect the line or the inch
    ws.cancel_prep("PREP-AB")
    after_cancel = {"A-B": ws.evaluate("A-B")["ok"], "A-C": ws.evaluate("A-C")["ok"]}

    # C gets the inch: cut A-C at shift 1 (both axes at bench-1, transport path)
    cut_result = ws.cut("A-C", 1)
    after_cut = {"A-B": ws.evaluate("A-B")["ok"], "A-C": ws.evaluate("A-C")["ok"]}
    # cancellation after the cut still cannot bring the delivered inch back
    ws.cancel_prep("PREP-AC")
    pieces = ws.remaining_pieces("A")
    realloc_over = ws.reallocate_returned("A[0:1]", SpanRef("A", 1, 2))
    realloc_ok = ws.reallocate_returned("A[0:1]", SpanRef("A", 0, 1))

    return {
        "A_residuals": [_s(v) for v in A_res],
        "A_middle_inch": shared_inch,
        "before_supplement": before,
        "shared_uncut_inch_between_preps": shared_inch,
        "after_clamp_supplement_B": after_supplement,
        "after_cancel_old_prep": after_cancel,
        "cut_delivered": cut_result["physical_numbers"],
        "cut_path": cut_result["path"],
        "after_cut": after_cut,
        "A_consumed": sorted(ws.yarns["A"].consumed),
        "A_remaining_pieces": [{"piece": p["piece_id"], "length": p["length"]}
                                for p in pieces],
        "reallocate_two_from_one_inch": {"ok": realloc_over[0], "why": realloc_over[1]},
        "reallocate_one_from_one_inch": {"ok": realloc_ok[0], "why": realloc_ok[1]},
        "delivered_inch_cannot_be_revived_by_cancel": not ws.evaluate("A-C")["ok"]
        or 1 in ws.yarns["A"].consumed,
        "audit": ws.audit_physical_exclusivity(),
    }


# ---------------------------------------------------------------------------
# 3. contention: two short yarns contest the same inch
# ---------------------------------------------------------------------------

def contention_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ws = Workshop()
    # reuse A (residual inch 2 = +1) and make two -1 tails Q1/Q2 from B and C
    ws.add_yarn("A", _readings(axes["A"]["cumulative_bits"]), bench=BENCH_A)
    ws.add_yarn("B", _readings(axes["B"]["cumulative_bits"]), bench=BENCH_A)
    ws.add_yarn("C", _readings(axes["C"]["cumulative_bits"]), bench=BENCH_A)
    left = SpanRef("A", 2, 1)  # residual +1
    o1 = CutOrder("O1", left, SpanRef("B", 0, 1), (ZERO,),
                  claim_shift=3, arrival_seq=1, bench=BENCH_A)
    o2 = CutOrder("O2", left, SpanRef("C", 0, 1), (ZERO,),
                  claim_shift=3, arrival_seq=2, bench=BENCH_A)
    ws.add_cut(o1)
    ws.add_cut(o2)
    plan = ws.plan_shift(3)
    # O2 may not cut while O1 wins
    refused = None
    try:
        ws.cut("O2", 3)
    except ValidationError as exc:
        refused = type(exc).__name__
    winner_cut = ws.cut("O1", 3)
    o2_after = ws.evaluate("O2")
    return {
        "decision": {cid: {"decision": p["decision"], "note": p["note"]}
                     for cid, p in plan.items()},
        "waiter_cut_refused": refused is not None,
        "winner_cut": winner_cut["physical_numbers"],
        "O2_after": {"ok": o2_after["ok"], "reasons": o2_after["reasons"]},
        "audit_exclusive": ws.audit_physical_exclusivity()["exclusive"],
    }


# ---------------------------------------------------------------------------
# 4. late order: tickets on time vs one voided
# ---------------------------------------------------------------------------

def late_order_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def build(mutation: str | None) -> tuple[Workshop, dict[str, Any]]:
        ws = Workshop()
        ws.add_yarn("J", _readings(axes["J"]["cumulative_bits"]), bench=BENCH_A)
        ws.add_yarn("Y", _readings(axes["Y"]["cumulative_bits"]), bench=BENCH_A)
        # J head 2 inches [2,-1] vs Y [ -2,1] -> zero. Axes start at bench-1.
        order = CutOrder("LATE", SpanRef("J", 0, 2), SpanRef("Y", 0, 2),
                         (ZERO, ZERO), claim_shift=2, arrival_seq=1, bench=BENCH_B)
        ws.add_cut(order)
        ws.issue_ticket("TJ", "J", SpanRef("J", 0, 2), "LATE", valid_shift=2)
        ws.issue_ticket("TY", "Y", SpanRef("Y", 0, 2), "LATE", valid_shift=2)
        ws.send_ticket("TJ", BENCH_B)
        ws.send_ticket("TY", BENCH_B)
        if mutation == "void":
            ws.void_ticket("TY")
        return ws, ws.plan_shift(2)["LATE"]

    ws_ok, plan_ok = build(None)
    ws_void, plan_void = build("void")
    # transport cannot make it (axes not moved), but tickets can
    transport_ok = ws_ok._transport_authority(ws_ok.cuts["LATE"], 2)[0]
    cut_ok = ws_ok.cut("LATE", 2) if plan_ok["decision"] == CUT_NOW else None
    void_refused = None
    try:
        ws_void.cut("LATE", 2)
    except ValidationError as exc:
        void_refused = str(exc)

    # expired tickets: both physical tickets are on the bench and tied to the
    # same order, but they were valid only for shift 2 and the bench now opens
    # at shift 3.  Two seemingly-same-order stale tickets cannot cut.
    ws_exp, _ = build(None)
    exp_authority = ws_exp.authority("LATE", 3)
    exp_refused = None
    try:
        ws_exp.cut("LATE", 3)
    except ValidationError as exc:
        exp_refused = str(exc)

    return {
        "arrival_late_by_one_shift": True,
        "transport_available": transport_ok,
        "both_tickets_on_time": {
            "decision": plan_ok["decision"],
            "path": plan_ok["path"],
            "cut": None if cut_ok is None else cut_ok["physical_numbers"],
        },
        "one_ticket_voided": {
            "decision": plan_void["decision"],
            "note": plan_void["note"],
            "cut_refused": void_refused is not None,
        },
        "both_tickets_expired_by_one_shift": {
            "authority_ok": exp_authority[0],
            "note": exp_authority[1],
            "cut_refused": exp_refused is not None,
        },
    }


# ---------------------------------------------------------------------------
# 5. uncontended valid order in its claim day is fulfillable
# ---------------------------------------------------------------------------

def uncontended_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ws = Workshop()
    ws.add_yarn("J", _readings(axes["J"]["cumulative_bits"]), bench=BENCH_A)
    ws.add_yarn("Y", _readings(axes["Y"]["cumulative_bits"]), bench=BENCH_A)
    order = CutOrder("FREE", SpanRef("J", 0, 2), SpanRef("Y", 0, 2),
                     (ZERO, ZERO), claim_shift=5, arrival_seq=1, bench=BENCH_A)
    ws.add_cut(order)
    plan = ws.plan_shift(5)["FREE"]
    result = ws.cut("FREE", 5)
    return {
        "decision": plan["decision"],
        "path": plan["path"],
        "fulfilled": result["physical_numbers"],
    }


# ---------------------------------------------------------------------------
# 6. two benches each hold distinct physical cutting rights (audit)
# ---------------------------------------------------------------------------

def two_bench_audit_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Bench-1 cuts J[0:2]+Y[0:2] by transport; bench-2 cuts J[2:4]+Y[2:4] by tickets.

    Both orders are physically disjoint, so no inch is cut twice.  The audit
    reports each bench's delivered physical object numbers independently.
    """
    ws = Workshop()
    ws.add_yarn("J", _readings(axes["J"]["cumulative_bits"]), bench=BENCH_A)
    ws.add_yarn("Y", _readings(axes["Y"]["cumulative_bits"]), bench=BENCH_A)
    o1 = CutOrder("B1-CUT", SpanRef("J", 0, 2), SpanRef("Y", 0, 2),
                  (ZERO, ZERO), claim_shift=1, arrival_seq=1, bench=BENCH_A)
    o2 = CutOrder("B2-CUT", SpanRef("J", 2, 2), SpanRef("Y", 2, 2),
                  (ZERO, ZERO), claim_shift=1, arrival_seq=2, bench=BENCH_B)
    ws.add_cut(o1)
    ws.add_cut(o2)
    # bench-2 acts through tickets on the disjoint second span
    ws.issue_ticket("TJ2", "J", SpanRef("J", 2, 2), "B2-CUT", valid_shift=1)
    ws.issue_ticket("TY2", "Y", SpanRef("Y", 2, 2), "B2-CUT", valid_shift=1)
    ws.send_ticket("TJ2", BENCH_B)
    ws.send_ticket("TY2", BENCH_B)
    plan = ws.plan_shift(1)
    first = ws.cut("B1-CUT", 1)
    second = ws.cut("B2-CUT", 1)
    audit = ws.audit_physical_exclusivity()
    rights = ws.bench_cut_rights()
    return {
        "plan": {cid: {"decision": p["decision"], "path": p["path"]}
                 for cid, p in plan.items()},
        "bench_1": first["physical_numbers"],
        "bench_2": second["physical_numbers"],
        "bench_cut_rights": rights,
        "audit_exclusive": audit["exclusive"],
        "collisions": audit["collisions"],
    }


# ---------------------------------------------------------------------------
# 7. headline: two full-length yarns' residual twist cancels; each inch flat
# ---------------------------------------------------------------------------

def full_length_scenario(axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """J and Y are 8 inches each with equal-and-opposite full-length twist.

    Pairing the *whole* length head-to-head must be flat inch by inch, and the
    two disjoint 2-inch sub-spans may be cut to the two benches.
    """
    j = Axis.from_cumulative(
        "J", "JY", [Fraction(x) for x in _cum_from_bits(axes["J"]["cumulative_bits"])]
    )
    y = Axis.from_cumulative(
        "Y", "YY", [Fraction(x) for x in _cum_from_bits(axes["Y"]["cumulative_bits"])]
    )
    n = min(j.length, y.length)
    zeros = [ZERO] * n
    full = evaluate_pair(j, Span(0, n, HEAD), y, Span(0, n, HEAD), zeros)
    flat_rows = [r.total for r in full.rows]
    return {
        "J_total": frac_str(sum(j.residuals)),
        "Y_total": frac_str(sum(y.residuals)),
        "totals_cancel": sum(j.residuals) + sum(y.residuals) == 0,
        "full_length_ok": full.ok,
        "every_inch_flat": all(v == 0 for v in flat_rows),
        "inch_totals": [frac_str(v) for v in flat_rows],
    }


def _cum_from_bits(bits: list[str]) -> list[Fraction]:
    from .semantics import decode, parse_bits

    out = []
    for b in bits:
        dec = decode(parse_bits(b))
        value = dec.value
        assert value is not None
        out.append(value)
    return out


def run(input_path: Path) -> dict[str, Any]:
    axes = _load_axes(input_path)
    out: dict[str, Any] = {}
    out["ledger"] = ledger_scenarios()
    out["full_length"] = full_length_scenario(axes)
    out["middle_inch_benches"] = bench_scenario(axes)
    out["contention"] = contention_scenario(axes)
    out["late_order"] = late_order_scenario(axes)
    out["uncontended"] = uncontended_scenario(axes)
    out["two_bench_audit"] = two_bench_audit_scenario(axes)
    out["bench_cut_rights_physical_numbers"] = out["two_bench_audit"]["bench_cut_rights"]
    return out


def _print(log: dict[str, Any]) -> None:
    print("== 1. residual-twist ledger (Jia/Yi) ==")
    led = log["ledger"]
    print(f"  Jia residuals: {led['jia_residuals']}")
    print(f"  Yi  residuals: {led['yi_residuals']}")
    print(f"  segment-2 provenance residual = {led['provenance_segment_2']['residual']}")
    print(f"  head+head per-inch ok: {led['head_to_head_ok']}")
    print(f"  combined total before={led['combined_total_before']} "
          f"after middle change={led['combined_total_after']} "
          f"(still zero: {led['total_still_cancels']})")
    print(f"  middle-change breaks per-inch: "
          f"{led['middle_change_totals_cancel_but_per_inch_fails']}")
    print(f"  tail attachment ok: {led['tail_attachment_ok']}")
    print(f"  defect (missing interior point) sheet: {led['defect_missing_sheet']}")
    print(f"  NaN interior point signable per segment: {led['defect_nan_signable']}")
    print(f"  printed seg1 = {led['printed_segment_1']['printed_value']} "
          f"(authoritative={led['printed_segment_1']['authoritative']}); "
          f"equals exact={led['print_equals_exact']}; "
          f"exact residual={led['printed_segment_1']['exact_residual']} "
          f"err={led['printed_segment_1']['exact_error']}")

    fl = log["full_length"]
    print(f"  full J/Y: totals {fl['J_total']} + {fl['Y_total']} cancel="
          f"{fl['totals_cancel']}; full-length ok={fl['full_length_ok']}; "
          f"every inch flat={fl['every_inch_flat']}")

    print("\n== 2. two benches: A's middle inch with B or C ==")
    b = log["middle_inch_benches"]
    print(f"  A residuals: {b['A_residuals']}; middle inch {b['A_middle_inch']}")
    print(f"  before supplement: {b['before_supplement']}")
    print(f"  after B clamp supplement: {b['after_clamp_supplement_B']}")
    print(f"  after cancelling old prep: {b['after_cancel_old_prep']}")
    print(f"  cut delivered: {b['cut_delivered']} via {b['cut_path']}")
    print(f"  after cut: {b['after_cut']}; A consumed {b['A_consumed']}")
    print(f"  remaining pieces: {b['A_remaining_pieces']}")
    print(f"  reallocate 2 from a 1-inch piece: {b['reallocate_two_from_one_inch']}")
    print(f"  reallocate 1 from a 1-inch piece: {b['reallocate_one_from_one_inch']}")

    print("\n== 3. contention for one inch ==")
    c = log["contention"]
    for cid, d in c["decision"].items():
        print(f"  {cid}: {d['decision']} - {d['note']}")
    print(f"  waiter cut refused: {c['waiter_cut_refused']}; winner cut {c['winner_cut']}")
    print(f"  loser after: {c['O2_after']}")
    print(f"  physical audit exclusive: {c['audit_exclusive']}")

    print("\n== 4. late order (tickets) ==")
    lo = log["late_order"]
    print(f"  transport available: {lo['transport_available']}")
    print(f"  both tickets on time: {lo['both_tickets_on_time']}")
    print(f"  one voided: {lo['one_ticket_voided']}")
    print(f"  both expired by one shift: {lo['both_tickets_expired_by_one_shift']}")

    print("\n== 5. uncontended valid order ==")
    u = log["uncontended"]
    print(f"  decision {u['decision']} via {u['path']}: {u['fulfilled']}")

    print("\n== 6. two benches, each bench's physical cutting rights ==")
    t = log["two_bench_audit"]
    for cid, d in t["plan"].items():
        print(f"  {cid}: {d['decision']} via {d['path']}")
    print(f"  bench-1 delivered: {t['bench_1']}")
    print(f"  bench-2 delivered: {t['bench_2']}")
    print(f"  audit exclusive: {t['audit_exclusive']}; collisions: {t['collisions']}")
    print(f"  rights by bench: {t['bench_cut_rights']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default=str(DEFAULT_INPUT))
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    log = run(Path(args.input))
    if args.json:
        print(json.dumps(log, indent=2, ensure_ascii=False, default=str))
    else:
        _print(log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
