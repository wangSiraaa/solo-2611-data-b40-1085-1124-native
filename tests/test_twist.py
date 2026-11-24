"""Exact tests for the residual-twist ledger over real float32 readings."""

from fractions import Fraction

import pytest

from obsdelivery.contracts import ReaderContract
from obsdelivery.errors import IntegrityError, ValidationError
from obsdelivery.twist import (
    PointReading,
    build_ledger,
    combine_residuals,
    encode_bits,
    print_residual,
    verify_against_original,
    verify_ledger,
)


def cum(*residuals):
    """Cumulative boundary readings from per-inch residuals."""
    out = [Fraction(0)]
    for r in residuals:
        out.append(out[-1] + Fraction(r))
    return out


def bits_from(values):
    return [encode_bits(v) for v in values]


def readings_from_residuals(residuals, **kw):
    return [{"bits": b, **kw} for b in bits_from(cum(*residuals))]


def test_encode_bits_roundtrips_exact_values():
    from obsdelivery.semantics import decode, parse_bits

    for v in [0, 1, 2, -1, -2, 3, -3, Fraction(1, 2), Fraction(-1, 2),
              Fraction(1, 8), Fraction(-1, 8)]:
        b = encode_bits(v)
        assert decode(parse_bits(b)).value == Fraction(v)


def test_encode_bits_rejects_nonrepresentable():
    for bad in [Fraction(1, 3), Fraction(1, 10)]:
        with pytest.raises(ValueError):
            encode_bits(bad)


def test_ledger_residuals_from_cumulative():
    # Jia: 0,2,1 -> [2,-1]; Yi: 0,-2,-1 -> [-2,1]
    jia = build_ledger(bits_from([0, 2, 1]) and [{"bits": b} for b in bits_from([0, 2, 1])])
    yi = build_ledger([{"bits": b} for b in bits_from([0, -2, -1])])
    assert [s.residual for s in jia.segments] == [Fraction(2), Fraction(-1)]
    assert [s.residual for s in yi.segments] == [Fraction(-2), Fraction(1)]


def test_backoff_is_subtracted():
    # raw 0,2,1 with backoff 1 on every point -> same residuals
    rd = [{"bits": b, "backoff": "1"} for b in bits_from([0, 2, 1])]
    ledger = build_ledger(rd)
    assert [s.residual for s in ledger.segments] == [Fraction(2), Fraction(-1)]


def test_flip_negates_reading():
    rd = [{"bits": b, "flip": True} for b in bits_from([0, 2, 1])]
    ledger = build_ledger(rd)
    # adjusted: 0,-2,-1 -> [-2,1]
    assert [s.residual for s in ledger.segments] == [Fraction(-2), Fraction(1)]


def test_point_carries_backoff_flip():
    rd = [
        {"bits": encode_bits(0), "backoff": "1/2", "flip": True},
        {"bits": encode_bits(2)},
    ]
    ledger = build_ledger(rd)
    p0 = ledger.points[0]
    assert p0.backoff == Fraction(1, 2) and p0.flip is True
    assert p0.adjusted == Fraction(1, 2)  # (0 - 1/2) * -1
    assert ledger.segments[0].residual == Fraction(2) - Fraction(1, 2)


def test_segment_provenance_single_segment():
    ledger = build_ledger([{"bits": b} for b in bits_from([0, 2, 1])])
    prov = ledger.provenance(2)
    assert prov["segment"] == 2
    assert prov["residual"] == "-1"
    assert prov["left"]["raw_value"] == "2"
    assert prov["right"]["raw_value"] == "1"
    assert prov["verification_source"] == "original_exact_bits"
    assert prov["provenance_digest"]
    # provenance does not require evaluating other segments
    with pytest.raises(ValidationError):
        ledger.provenance(3)


def test_defect_only_poisons_incident_segments():
    # 4 points -> 3 inches; make the interior point 1 missing.
    rd = [{"bits": b} for b in bits_from([0, 1, 2, 3])]
    rd[1] = {"bits": None}
    ledger = build_ledger(rd)
    assert [s.signable for s in ledger.segments] == [False, False, True]
    assert ledger.segments[0].reason.startswith("right point 1")
    assert ledger.segments[1].reason.startswith("left point 1")
    assert ledger.segments[2].residual == Fraction(1)
    assert ledger.signable_residuals() == [None, None, Fraction(1)]


def test_nonfinite_point_only_poisons_incident_segments():
    rd = [{"bits": b} for b in bits_from([0, 1, 2, 3])]
    rd[2] = {"bits": "7fc00001"}  # NaN
    ledger = build_ledger(rd)
    assert [s.signable for s in ledger.segments] == [True, False, False]
    # segment 1 is clean and restored
    assert ledger.segments[0].residual == Fraction(1)


def test_verification_from_original_bits_not_printed():
    # a 0.1f inch cannot be printed at scale 0 without rounding
    ledger = build_ledger([{"bits": "00000000"}, {"bits": "3dcccccd"}])
    exact = ledger.segments[0].residual
    assert exact == Fraction(13421773, 134217728)
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [0], "max_abs_coefficient": 999, "max_abs_error": "1/2"}
    )
    printed = print_residual(ledger, 1, contract)
    assert printed.authoritative is False
    assert printed.printed_value == 0
    assert printed.exact_error == -exact
    check = verify_against_original(ledger, 1, printed.printed_value)
    assert check["equals_exact"] is False
    assert check["verification_source"] == "original_exact_bits"
    # re-verifying the ORIGINAL keeps the exact value
    assert verify_against_original(ledger, 1, exact)["equals_exact"] is True


def test_print_residual_refuses_unsignable():
    rd = [{"bits": b} for b in bits_from([0, 1])]
    rd[0] = {"bits": None}
    ledger = build_ledger(rd)
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [0], "max_abs_coefficient": 9, "max_abs_error": "0"}
    )
    with pytest.raises(ValidationError):
        print_residual(ledger, 1, contract)


def test_verify_ledger_detects_tampering():
    rd = [{"bits": b} for b in bits_from([0, 2, 1])]
    ledger = build_ledger(rd)
    verify_ledger(rd, ledger)  # clean
    tampered = [{"bits": b} for b in bits_from([0, 2, 2])]
    with pytest.raises(IntegrityError):
        verify_ledger(tampered, ledger)


def test_ledger_needs_two_points():
    with pytest.raises(ValidationError):
        build_ledger([{"bits": encode_bits(0)}])


def test_invalid_bits_rejected():
    with pytest.raises(ValidationError):
        build_ledger([{"bits": "zzzzzzzz"}, {"bits": "00000000"}])


def test_combine_residuals_closed_interval():
    ok, total, why = combine_residuals(Fraction(2), Fraction(-2), Fraction(0), Fraction(0))
    assert ok and total == 0 and why is None
    ok2, total2, why2 = combine_residuals(Fraction(2), Fraction(-1), Fraction(0), Fraction(0))
    assert not ok2 and total2 == 1 and "outside" in why2
    # inclusive endpoints
    ok3, _, _ = combine_residuals(Fraction(1), Fraction(1), Fraction(1), Fraction(2))
    assert ok3
    ok4, _, _ = combine_residuals(Fraction(1), Fraction(1), Fraction(2), Fraction(3))
    assert ok4
    # un-signable
    ok5, total5, why5 = combine_residuals(None, Fraction(0), Fraction(0), Fraction(0))
    assert not ok5 and total5 is None
