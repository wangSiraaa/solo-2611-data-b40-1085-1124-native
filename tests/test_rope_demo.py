"""Assert the end-to-end rope demo produces the documented outcomes."""

from pathlib import Path

from obsdelivery.rope_demo import DEFAULT_INPUT, run


def test_rope_demo_end_to_end():
    log = run(Path(DEFAULT_INPUT))

    led = log["ledger"]
    assert led["jia_residuals"] == ["2", "-1"]
    assert led["yi_residuals"] == ["-2", "1"]
    assert led["head_to_head_ok"] is True
    assert led["total_still_cancels"] is True
    assert led["middle_change_totals_cancel_but_per_inch_fails"] is True
    assert led["tail_attachment_ok"] is False
    # interior defect poisons only the two incident segments; the third is clean
    assert [row["signable"] for row in led["defect_missing_sheet"]] == [False, False, True]
    assert led["defect_missing_sheet"][2]["residual"] == "1"
    assert led["defect_nan_signable"] == [False, False, True]
    # printing rounding is non-authoritative
    assert led["printed_segment_1"]["authoritative"] is False
    assert led["print_equals_exact"] is False
    assert led["verification_source"] == "original_exact_bits"

    fl = log["full_length"]
    assert fl["J_total"] == "5"
    assert fl["Y_total"] == "-5"
    assert fl["totals_cancel"] is True
    assert fl["full_length_ok"] is True
    assert fl["every_inch_flat"] is True


def test_rope_demo_benches_and_contention():
    log = run(Path(DEFAULT_INPUT))
    b = log["middle_inch_benches"]
    assert b["before_supplement"] == {"A-B": True, "A-C": True}
    assert b["after_clamp_supplement_B"] == {"A-B": False, "A-C": True}
    assert b["after_cancel_old_prep"] == {"A-B": False, "A-C": True}
    assert b["cut_delivered"] == ["A#1", "C#0"]
    assert b["after_cut"]["A-B"] is False
    assert b["reallocate_two_from_one_inch"]["ok"] is False
    assert b["reallocate_one_from_one_inch"]["ok"] is True
    assert b["audit"]["exclusive"] is True

    c = log["contention"]
    assert c["decision"]["O1"]["decision"] == "cut_now"
    assert c["decision"]["O2"]["decision"] == "wait"
    assert c["waiter_cut_refused"] is True
    assert c["O2_after"]["ok"] is False
    assert c["audit_exclusive"] is True


def test_rope_demo_late_and_uncontended():
    log = run(Path(DEFAULT_INPUT))
    lo = log["late_order"]
    assert lo["transport_available"] is False
    assert lo["both_tickets_on_time"]["decision"] == "cut_now"
    assert lo["both_tickets_on_time"]["path"] == "tickets"
    assert lo["one_ticket_voided"]["cut_refused"] is True
    assert lo["both_tickets_expired_by_one_shift"]["cut_refused"] is True

    assert log["uncontended"]["decision"] == "cut_now"
    assert log["uncontended"]["fulfilled"] == ["J#0", "J#1", "Y#0", "Y#1"]


def test_rope_demo_two_bench_distinct_rights():
    log = run(Path(DEFAULT_INPUT))
    t = log["two_bench_audit"]
    assert t["plan"]["B1-CUT"]["decision"] == "cut_now"
    assert t["plan"]["B1-CUT"]["path"] == "transport"
    assert t["plan"]["B2-CUT"]["decision"] == "cut_now"
    assert t["plan"]["B2-CUT"]["path"] == "tickets"
    assert t["bench_cut_rights"] == {
        "bench-1": ["J#0", "J#1", "Y#0", "Y#1"],
        "bench-2": ["J#2", "J#3", "Y#2", "Y#3"],
    }
    assert t["audit_exclusive"] is True
    assert t["collisions"] == {}
