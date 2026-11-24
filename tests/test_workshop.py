"""End-to-end tests for the two-bench workshop, tickets, and contention."""

from fractions import Fraction

import pytest

from obsdelivery.errors import ValidationError
from obsdelivery.ropes import Interval
from obsdelivery.twist import encode_bits
from obsdelivery.workshop import (
    BENCH_A,
    BENCH_B,
    BLOCKED,
    CUT_NOW,
    WAIT,
    CutOrder,
    SpanRef,
    Workshop,
)

ZERO = Interval(Fraction(0), Fraction(0))


def cum(*residuals):
    out = [Fraction(0)]
    for r in residuals:
        out.append(out[-1] + Fraction(r))
    return out


def readings(*residuals):
    return [{"bits": encode_bits(v)} for v in cum(*residuals)]


def make_shop(**axes):
    ws = Workshop()
    for name, res in axes.items():
        bench = BENCH_B if name.startswith("b") else BENCH_A
        ws.add_yarn(name, readings(*res))
    return ws


def order(ws, cut_id, lyarn, lstart, ryarn, rstart, length, shift, seq, bench, intervals=None):
    ivs = intervals or tuple([ZERO] * length)
    o = CutOrder(
        cut_id,
        SpanRef(lyarn, lstart, length),
        SpanRef(ryarn, rstart, length),
        ivs,
        claim_shift=shift,
        arrival_seq=seq,
        bench=bench,
    )
    ws.add_cut(o)
    return o


# ---------------------------------------------------------------------------
# ledger residuals flow through the workshop
# ---------------------------------------------------------------------------

def test_workshop_computes_residuals_from_cumulative():
    ws = make_shop(J=[2, -1, 1])
    assert ws.yarns["J"].residuals() == [Fraction(2), Fraction(-1), Fraction(1)]
    assert ws.yarns["J"].length() == 3


# ---------------------------------------------------------------------------
# A's middle inch can pair with B or C; clamp supplement to B forces C
# ---------------------------------------------------------------------------

def test_middle_inch_pairs_with_b_or_c_then_b_yields_to_c():
    ws = make_shop(A=[0, 1, 1, 0], B=[-1], C=[-1])
    order(ws, "A-B", "A", 1, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    order(ws, "A-C", "A", 1, "C", 0, 1, shift=1, seq=2, bench=BENCH_A)
    assert ws.evaluate("A-B")["ok"]
    assert ws.evaluate("A-C")["ok"]

    # prep orders both drawn from A's uncut middle inch and B/C
    p1 = ws.add_prep("PREP-AB", SpanRef("A", 1, 1), SpanRef("B", 0, 1), (ZERO,), shift=1)
    ws.add_prep("PREP-AC", SpanRef("A", 1, 1), SpanRef("C", 0, 1), (ZERO,), shift=1)
    assert p1.left.inches()[0].canonical() == {"yarn": "A", "inch": 1}

    # supplementary clamp record: B was clamped the other way -> residual +1
    ws.supplement_clamp_direction("B", flip=True)
    assert ws.yarns["B"].residuals() == [Fraction(1)]
    assert not ws.evaluate("A-B")["ok"]
    assert ws.evaluate("A-C")["ok"]


def test_cancel_old_prep_cannot_restore_line_or_inch():
    ws = make_shop(A=[0, 1, 1, 0], B=[-1], C=[-1])
    order(ws, "A-B", "A", 1, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    order(ws, "A-C", "A", 1, "C", 0, 1, shift=1, seq=2, bench=BENCH_A)
    ws.add_prep("PREP-AB", SpanRef("A", 1, 1), SpanRef("B", 0, 1), (ZERO,), shift=1)
    ws.supplement_clamp_direction("B", flip=True)
    ws.cancel_prep("PREP-AB")
    # cancelling a prep does not make a failing order pass
    assert not ws.evaluate("A-B")["ok"]

    # C cuts the inch; then cancelling the OLD prep still cannot revive A-B
    ws.cut("A-C", 1)
    assert 1 in ws.yarns["A"].consumed
    ws.cancel_prep("PREP-AB")
    assert not ws.evaluate("A-B")["ok"]
    assert 1 in ws.yarns["A"].consumed


def test_cut_consumes_inch_forever_and_cancel_cannot_restore():
    ws = make_shop(A=[0, 1, 1, 0], B=[-1], C=[-1])
    order(ws, "A-C", "A", 1, "C", 0, 1, shift=1, seq=1, bench=BENCH_A)
    ws.add_prep("PREP-X", SpanRef("A", 1, 1), SpanRef("B", 0, 1), (ZERO,), shift=1)
    result = ws.cut("A-C", 1)
    assert result["physical_numbers"] == ["A#1", "C#0"]
    # the delivered inch is consumed forever
    ws.cancel_prep("PREP-X")
    # a new order trying to reuse A#1 is materially invalid
    order(ws, "REUSE", "A", 1, "B", 0, 1, shift=2, seq=9, bench=BENCH_A)
    assert not ws.evaluate("REUSE")["ok"]


def test_returned_piece_reallocation_by_actual_remaining_length():
    ws = make_shop(A=[0, 1, 1, 0], C=[-1])
    order(ws, "A-C", "A", 1, "C", 0, 1, shift=1, seq=1, bench=BENCH_A)
    ws.cut("A-C", 1)
    pieces = {p["piece_id"]: p for p in ws.remaining_pieces("A")}
    assert pieces["A[0:1]"]["length"] == 1
    assert pieces["A[2:4]"]["length"] == 2
    # cannot hand 2 inches out of the 1-inch returned piece
    ok, why = ws.reallocate_returned("A[0:1]", SpanRef("A", 1, 2))
    assert not ok and "exceed" in why
    # can hand exactly its actual length
    ok2, _ = ws.reallocate_returned("A[0:1]", SpanRef("A", 0, 1))
    assert ok2


# ---------------------------------------------------------------------------
# physical exclusivity / audit
# ---------------------------------------------------------------------------

def test_physical_object_numbers_are_exclusive():
    ws = make_shop(A=[0, 1, 1, 0], B=[-1], C=[-1])
    order(ws, "O1", "A", 1, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    ws.cut("O1", 1)
    rights = ws.bench_cut_rights()
    assert rights[BENCH_A] == ["A#1", "B#0"]
    audit = ws.audit_physical_exclusivity()
    assert audit["exclusive"]
    assert audit["collisions"] == {}
    assert audit["delivered_physical_numbers"] == ["A#1", "B#0"]


# ---------------------------------------------------------------------------
# contention: two short yarns contest the same inch
# ---------------------------------------------------------------------------

def test_contention_winner_cuts_waiter_must_wait_then_blocked():
    ws = make_shop(A=[1], B=[-1], C=[-1])
    # both want A#0 (residual +1) joined to a -1 tail
    order(ws, "O1", "A", 0, "B", 0, 1, shift=3, seq=1, bench=BENCH_A)
    order(ws, "O2", "A", 0, "C", 0, 1, shift=3, seq=2, bench=BENCH_A)
    plan = ws.plan_shift(3)
    assert plan["O1"]["decision"] == CUT_NOW
    assert plan["O2"]["decision"] == WAIT
    # the waiter cannot sneak in
    with pytest.raises(ValidationError):
        ws.cut("O2", 3)
    ws.cut("O1", 3)
    # now the contested inch is gone for good; O2 is materially blocked
    assert ws.evaluate("O2")["ok"] is False
    assert ws.plan_shift(3)["O2"]["decision"] == BLOCKED


def test_no_contention_cut_now():
    ws = make_shop(A=[1, 1], B=[-1], C=[-1])
    # disjoint inches: A#0 with B, and A#1 with C
    order(ws, "X", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    order(ws, "Y", "A", 1, "C", 0, 1, shift=1, seq=2, bench=BENCH_A)
    plan = ws.plan_shift(1)
    assert plan["X"]["decision"] == CUT_NOW
    assert plan["Y"]["decision"] == CUT_NOW


# ---------------------------------------------------------------------------
# one-inch-per-two-yarns rule / same axis
# ---------------------------------------------------------------------------

def test_same_axis_pairing_refused():
    ws = make_shop(A=[0, 1, 1])
    with pytest.raises(ValidationError):
        order(ws, "BAD", "A", 0, "A", 1, 1, shift=1, seq=1, bench=BENCH_A)


# ---------------------------------------------------------------------------
# tickets and late order
# ---------------------------------------------------------------------------

def test_late_order_cuts_with_both_tickets_on_time():
    ws = make_shop(J=[2, -1], Y=[-2, 1])
    o = order(ws, "LATE", "J", 0, "Y", 0, 2, shift=2, seq=1, bench=BENCH_B)
    ws.issue_ticket("TJ", "J", SpanRef("J", 0, 2), "LATE", valid_shift=2)
    ws.issue_ticket("TY", "Y", SpanRef("Y", 0, 2), "LATE", valid_shift=2)
    ws.send_ticket("TJ", BENCH_B)
    ws.send_ticket("TY", BENCH_B)
    # transport cannot make it (axes at bench-1), but tickets can
    assert ws._transport_authority(o, 2)[0] is False
    plan = ws.plan_shift(2)["LATE"]
    assert plan["decision"] == CUT_NOW and plan["path"] == "tickets"
    result = ws.cut("LATE", 2)
    assert result["physical_numbers"] == ["J#0", "J#1", "Y#0", "Y#1"]


def test_late_order_with_one_voided_ticket_cannot_cut():
    ws = make_shop(J=[2, -1], Y=[-2, 1])
    order(ws, "LATE", "J", 0, "Y", 0, 2, shift=2, seq=1, bench=BENCH_B)
    ws.issue_ticket("TJ", "J", SpanRef("J", 0, 2), "LATE", valid_shift=2)
    ws.issue_ticket("TY", "Y", SpanRef("Y", 0, 2), "LATE", valid_shift=2)
    ws.send_ticket("TJ", BENCH_B)
    ws.send_ticket("TY", BENCH_B)
    ws.void_ticket("TY")
    assert ws.plan_shift(2)["LATE"]["decision"] == WAIT
    with pytest.raises(ValidationError):
        ws.cut("LATE", 2)


def test_two_stale_same_order_tickets_cannot_cut():
    ws = make_shop(J=[2, -1], Y=[-2, 1])
    order(ws, "LATE", "J", 0, "Y", 0, 2, shift=2, seq=1, bench=BENCH_B)
    ws.issue_ticket("TJ", "J", SpanRef("J", 0, 2), "LATE", valid_shift=2)
    ws.issue_ticket("TY", "Y", SpanRef("Y", 0, 2), "LATE", valid_shift=2)
    ws.send_ticket("TJ", BENCH_B)
    ws.send_ticket("TY", BENCH_B)
    # shift has advanced to 3: tickets look right (same order, same bench)
    # but are expired for this shift.
    assert not ws.authority("LATE", 3)[0]
    with pytest.raises(ValidationError):
        ws.cut("LATE", 3)


def test_ticket_exclusivity_blocks_double_ticketing():
    ws = make_shop(A=[0, 1], B=[-1])
    ws.issue_ticket("T1", "A", SpanRef("A", 0, 1), "O1", valid_shift=1)
    with pytest.raises(ValidationError):
        ws.issue_ticket("T2", "A", SpanRef("A", 0, 1), "O2", valid_shift=1)
    # releasing allows a re-issue
    ws.release_ticket("T1")
    ws.issue_ticket("T2", "A", SpanRef("A", 0, 1), "O2", valid_shift=1)


def test_cannot_ticket_consumed_inches():
    ws = make_shop(A=[1], B=[-1], C=[-1])
    order(ws, "O1", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    ws.cut("O1", 1)
    with pytest.raises(ValidationError):
        ws.issue_ticket("T2", "A", SpanRef("A", 0, 1), "O2", valid_shift=2)


# ---------------------------------------------------------------------------
# uncontended valid order in its claim day is fulfillable
# ---------------------------------------------------------------------------

def test_uncontended_valid_order_is_fulfilled():
    ws = make_shop(J=[2, -1], Y=[-2, 1])
    order(ws, "FREE", "J", 0, "Y", 0, 2, shift=5, seq=1, bench=BENCH_A)
    plan = ws.plan_shift(5)["FREE"]
    assert plan["decision"] == CUT_NOW
    assert ws.cut("FREE", 5)["physical_numbers"] == ["J#0", "J#1", "Y#0", "Y#1"]


# ---------------------------------------------------------------------------
# transport / routing / handover rules
# ---------------------------------------------------------------------------

def test_route_costs_a_shift_and_blocks_during_transit():
    ws = make_shop(A=[1], B=[-1])
    # move B to bench-1: dispatch now, arrives next shift
    arrive = ws.route("B", BENCH_A, shift=1)
    assert arrive == 2
    order(ws, "O", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    # during transit (shift 1) the axis is not drawable
    assert ws._transport_authority(ws.cuts["O"], 1)[0] is False
    # after arrival (shift 2) it is
    assert ws._transport_authority(ws.cuts["O"], 2)[0] is True
    assert ws.cut("O", 2)["physical_numbers"] == ["A#0", "B#0"]


def test_reserved_axis_not_drawable_by_other_bench():
    ws = make_shop(A=[1], B=[-1])
    ws.bind_pair("PAIR-B", "A", "B", BENCH_B)  # reserve both for bench-2
    order(ws, "O", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    # reserved for B, so bench-1 cannot draw them even though physically there
    assert ws._transport_authority(ws.cuts["O"], 1)[0] is False
    # recalling both (one shift each) releases the reservations
    ws.recall_for_rebind("A", 1)
    ws.recall_for_rebind("B", 1)
    assert ws._transport_authority(ws.cuts["O"], 2)[0] is True


def test_one_handover_per_bench_before_cut():
    ws = make_shop(A=[1], B=[-1])
    assert ws.handover(BENCH_A, 1)["handovers_used"] == 1
    with pytest.raises(ValidationError):
        ws.handover(BENCH_A, 1)


def test_handover_refused_after_cut():
    ws = make_shop(A=[1], B=[-1])
    order(ws, "O", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    ws.cut("O", 1)
    with pytest.raises(ValidationError):
        ws.handover(BENCH_A, 2)


def test_swap_axis_and_cancel_prep():
    ws = make_shop(A=[0, 1, 1, 0], B=[-1], C=[-1])
    ws.add_prep("P", SpanRef("A", 1, 1), SpanRef("B", 0, 1), (ZERO,), shift=1)
    ws.swap_axis("P", "right", SpanRef("C", 0, 1))
    assert ws.prep["P"].right.yarn == "C"
    ws.cancel_prep("P")
    assert ws.prep["P"].status == "canceled"
    # swapping to the same axis is refused
    with pytest.raises(ValidationError):
        ws.swap_axis("P", "left", SpanRef("C", 1, 1))


def test_other_bench_holds_old_prep_shared_material():
    """Two benches each hold a prep sharing A's uncut inch; one bench cuts.

    The prep held at the other bench refers to material that is now gone, so
    it can no longer be cut - even though its ticket/order objects still exist.
    """
    ws = make_shop(A=[1], B=[-1], C=[-1])
    # bench-1 prep and bench-2 prep both reserve the same A#0 inch
    ws.add_prep("PREP-1", SpanRef("A", 0, 1), SpanRef("B", 0, 1), (ZERO,), shift=1)
    ws.add_prep("PREP-2", SpanRef("A", 0, 1), SpanRef("C", 0, 1), (ZERO,), shift=1)
    # bench-1 order cuts A#0 with B
    order(ws, "B1", "A", 0, "B", 0, 1, shift=1, seq=1, bench=BENCH_A)
    # bench-2 order (holding PREP-2) also wants A#0 with C
    order(ws, "B2", "A", 0, "C", 0, 1, shift=1, seq=2, bench=BENCH_B)
    assert ws.evaluate("B1")["ok"] and ws.evaluate("B2")["ok"]
    ws.cut("B1", 1)
    # bench-2's held prep is now stale: the shared inch is consumed forever
    assert not ws.evaluate("B2")["ok"]
    assert "already cut" in " ".join(ws.evaluate("B2")["reasons"])
    # cancelling the bench-1 prep does not restore the material either
    ws.cancel_prep("PREP-1")
    assert not ws.evaluate("B2")["ok"]
    assert 0 in ws.yarns["A"].consumed
