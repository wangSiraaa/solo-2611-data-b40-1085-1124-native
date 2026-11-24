"""Residual-twist ledger over float32 bit-pattern readings (pure, storage-free).

The rope club measures one yarn in equal one-inch segments.  A sensor records
a *cumulative* revolution count at every segment boundary, signed by twist
direction.  Consecutive segments share a boundary point.  The residual twist
of inch ``i`` is the corrected difference between its right and left boundary
readings.

Corrections (fixture records)
-----------------------------
* ``backoff`` (回拨量): when the chuck is changed, one reading is taken with
  the yarn held still; that offset is subtracted from later readings.
* ``flip`` (夹向): swapping clamp direction negates the reading per the
  fixture card.

For a point ``j`` with original ``bits``, ``backoff`` and ``flip``::

    adjusted_j = (raw_j - backoff_j) * (-1 if flip_j else +1)

and inch ``i`` (between points ``i-1`` and ``i``)::

    residual_i = adjusted_i - adjusted_{i-1}

Exactness / non-finite / missing
--------------------------------
Every value is a ``Fraction`` decoded from the original bits; no host float
is ever used.  A *missing* reading (``bits is None``) or a *non-finite* one
(NaN / +/-Inf) is a defect: every segment that *touches* that boundary point
becomes un-signable, but every other segment is still restored exactly.  This
mirrors the observation service: a bad point contaminates only incident
segments.

Printing vs. verification
-------------------------
A residual may be rendered for a decimal printing machine (a
``coefficient / 10**scale`` value under an independent reader contract).  That
printed decimal can be rounded.  Sign-off and re-verification therefore use
the *original exact* readings only; a reprinted number is never used as the
source of truth.  ``verify_ledger`` re-derives everything from the source bits
and fixture records and refuses any drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

from . import semantics
from .canonical import canonical_json, digest, frac_str, parse_fraction
from .contracts import ReaderContract
from .delivery_algo import select_representation
from .errors import IntegrityError, ValidationError

SIGNED = "SIGNED"
UNSIGNABLE = "UNSIGNABLE"

MISSING_POINT = "missing_reading"
NONFINITE_POINT = "nonfinite_reading"


# ---------------------------------------------------------------------------
# exact float32 encoding (only for values that are exactly representable)
# ---------------------------------------------------------------------------

def encode_bits(value: Fraction | int | str) -> str:
    """Encode an exactly-representable rational as an 8-hex float32 pattern.

    Only values that round-trip through :func:`semantics.decode` exactly are
    accepted; this keeps the test corpus to *real* bit patterns rather than
    host-float approximations.
    """
    v = parse_fraction(value, field="value") if isinstance(value, str) else Fraction(value)
    sign = 0
    if v < 0:
        sign, v = 1, -v
    if v == 0:
        return f"{sign << 31:08x}"
    two = Fraction(2)
    exponent = v.numerator.bit_length() - v.denominator.bit_length()
    while two ** exponent > v:
        exponent -= 1
    while two ** (exponent + 1) <= v:
        exponent += 1
    if exponent < -126:
        scaled = v * (1 << 149)
        if scaled.denominator != 1 or not 0 <= scaled.numerator < (1 << 23):
            raise ValueError(f"{v} is not an exact float32 subnormal")
        return f"{(sign << 31) | scaled.numerator:08x}"
    if exponent > 127:
        raise ValueError(f"{v} overflows float32")
    mantissa = v * two ** (23 - exponent)
    if mantissa.denominator != 1:
        raise ValueError(f"{v} is not exactly representable as float32")
    m = mantissa.numerator
    if not (1 << 23) <= m < (1 << 24):
        raise ValueError(f"internal mantissa range error for {v}")
    exp_field = exponent + 127
    return f"{(sign << 31) | (exp_field << 23) | (m - (1 << 23)):08x}"


# ---------------------------------------------------------------------------
# readings -> points
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PointReading:
    """One supplied boundary reading plus its fixture record."""

    bits: str | None
    backoff: Fraction = Fraction(0)
    flip: bool = False


def _coerce_reading(raw: Mapping[str, Any] | PointReading) -> PointReading:
    if isinstance(raw, PointReading):
        return raw
    if not isinstance(raw, Mapping):
        raise ValidationError(f"point reading must be a mapping, got {raw!r}")
    bits = raw.get("bits", None)
    if bits is not None:
        try:
            bits = f"{semantics.parse_bits(bits):08x}"
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    backoff = parse_fraction(raw.get("backoff", 0), field="backoff")
    flip = raw.get("flip", False)
    if not isinstance(flip, bool):
        raise ValidationError(f"flip must be a bool, got {flip!r}")
    return PointReading(bits, backoff, flip)


@dataclass(frozen=True)
class Point:
    index: int
    bits: str | None
    kind: str
    sign: int | None
    raw_value: Fraction | None
    backoff: Fraction
    flip: bool
    adjusted: Fraction | None
    defect: str | None

    @property
    def usable(self) -> bool:
        return self.defect is None

    def canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "bits": self.bits,
            "kind": self.kind,
            "sign": self.sign,
            "raw_value": None if self.raw_value is None else frac_str(self.raw_value),
            "backoff": frac_str(self.backoff),
            "flip": self.flip,
            "adjusted": None if self.adjusted is None else frac_str(self.adjusted),
            "defect": self.defect,
        }


def _build_point(index: int, reading: PointReading) -> Point:
    if reading.bits is None:
        return Point(index, None, semantics.MISSING, None, None,
                     reading.backoff, reading.flip, None, MISSING_POINT)
    decoded = semantics.decode(semantics.parse_bits(reading.bits))
    if not decoded.finite:
        return Point(index, decoded.bits, decoded.kind, decoded.sign, None,
                     reading.backoff, reading.flip, None, NONFINITE_POINT)
    raw_value = decoded.value  # Fraction, possibly negative zero -> 0
    adjusted = (raw_value - reading.backoff) * (-1 if reading.flip else 1)
    return Point(index, decoded.bits, decoded.kind, decoded.sign, raw_value,
                 reading.backoff, reading.flip, adjusted, None)


# ---------------------------------------------------------------------------
# segments + ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    index: int  # 1-based inch number
    left: Point
    right: Point
    residual: Fraction | None
    signable: bool
    reason: str | None

    def canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "left": self.left.canonical(),
            "right": self.right.canonical(),
            "residual": None if self.residual is None else frac_str(self.residual),
            "signable": self.signable,
            "reason": self.reason,
        }


def _build_segment(index: int, left: Point, right: Point) -> Segment:
    if not left.usable:
        return Segment(index, left, right, None, False,
                       f"left point {left.index} {left.defect}")
    if not right.usable:
        return Segment(index, left, right, None, False,
                       f"right point {right.index} {right.defect}")
    residual = right.adjusted - left.adjusted
    return Segment(index, left, right, residual, True, None)


@dataclass(frozen=True)
class TwistLedger:
    """An adopted, immutable snapshot of readings + fixtures and its sheet."""

    version: str
    points: tuple[Point, ...]
    segments: tuple[Segment, ...]
    source_digest: str
    fixture_digest: str
    # original readings kept (privately, not part of identity) so the sheet can
    # be re-derived from source bits alone on verification.
    _source_readings: tuple[PointReading, ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def length(self) -> int:
        return len(self.segments)

    def segment(self, index: int) -> Segment:
        """Query one segment's provenance (只查某段的来由)."""
        if not 1 <= index <= len(self.segments):
            raise ValidationError(
                f"segment index {index} outside 1..{len(self.segments)}"
            )
        return self.segments[index - 1]

    def residuals(self) -> list[Fraction | None]:
        return [segment.residual for segment in self.segments]

    def signable_residuals(self) -> list[Fraction | None]:
        return [s.residual if s.signable else None for s in self.segments]

    def provenance(self, index: int) -> dict[str, Any]:
        """Exact, self-contained derivation of a single segment."""
        segment = self.segment(index)
        left, right = segment.left, segment.right
        body = {
            "version": self.version,
            "source_digest": self.source_digest,
            "fixture_digest": self.fixture_digest,
            "segment": segment.index,
            "formula": "residual = adjusted(right) - adjusted(left)",
            "left": {
                **left.canonical(),
                "adjust_formula": "(raw - backoff) * sign",
            },
            "right": {
                **right.canonical(),
                "adjust_formula": "(raw - backoff) * sign",
            },
            "residual": None if segment.residual is None else frac_str(segment.residual),
            "signable": segment.signable,
            "reason": segment.reason,
            "verification_source": "original_exact_bits",
        }
        body["provenance_digest"] = digest(body)
        return body

    def verify(self) -> None:
        """Re-derive every segment from source bits; raise on any drift."""
        reparsed = _build_points(self._source_readings)
        rebuilt = [_build_segment(i, reparsed[i - 1], reparsed[i])
                   for i in range(1, len(reparsed))]
        for stored, fresh in zip(self.segments, rebuilt):
            if canonical_json(stored.canonical()) != canonical_json(fresh.canonical()):
                raise IntegrityError(
                    f"twist ledger {self.version!r}: segment {stored.index} drift"
                )
        if len(self.segments) != len(rebuilt):
            raise IntegrityError(f"twist ledger {self.version!r}: segment count drift")

    # ``_source_readings`` is populated by build_ledger for exact re-derivation.


def _build_points(readings: Sequence[PointReading]) -> list[Point]:
    return [_build_point(i, reading) for i, reading in enumerate(readings)]


def build_ledger(
    readings: Sequence[Mapping[str, Any] | PointReading],
    *,
    version: str = "adopted",
) -> TwistLedger:
    """Adopt one exact reading/fixture version and open its residual sheet."""
    if not isinstance(version, str) or not version:
        raise ValidationError("ledger version must be a non-empty string")
    if isinstance(readings, (str, bytes)) or not isinstance(readings, Sequence):
        raise ValidationError("readings must be a sequence")
    points_in = tuple(_coerce_reading(r) for r in readings)
    if len(points_in) < 2:
        raise ValidationError("a ledger needs at least two boundary points")

    points = tuple(_build_points(points_in))
    segments = tuple(
        _build_segment(i, points[i - 1], points[i]) for i in range(1, len(points))
    )

    source = [{"bits": r.bits} for r in points_in]
    fixtures = [{"backoff": frac_str(r.backoff), "flip": r.flip} for r in points_in]
    ledger = TwistLedger(
        version=version,
        points=points,
        segments=segments,
        source_digest=digest(source),
        fixture_digest=digest(fixtures),
        _source_readings=points_in,
    )
    ledger.verify()
    return ledger


# ---------------------------------------------------------------------------
# printing (non-authoritative) + exact re-verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrintedResidual:
    """A decimal rendering of one residual.  NEVER a source of truth."""

    segment_index: int
    coefficient: int
    scale: int
    printed_value: Fraction
    exact_residual: Fraction
    exact_error: Fraction
    authoritative: bool = False

    def canonical(self) -> dict[str, Any]:
        return {
            "segment_index": self.segment_index,
            "coefficient": self.coefficient,
            "scale": self.scale,
            "printed_value": frac_str(self.printed_value),
            "exact_residual": frac_str(self.exact_residual),
            "exact_error": frac_str(self.exact_error),
            "authoritative": False,
        }


def print_residual(ledger: TwistLedger, index: int, contract: ReaderContract) -> PrintedResidual:
    """Render one exact residual for the printing machine (may be rounded)."""
    segment = ledger.segment(index)
    if not segment.signable:
        raise ValidationError(f"segment {index} is un-signable: {segment.reason}")
    exact = segment.residual
    assert exact is not None
    selection = select_representation(
        abs(exact),
        contract.allowed_scales,
        contract.max_abs_coefficient,
        contract.max_abs_error,
    )
    if not selection.feasible:
        raise ValidationError(
            f"segment {index} has no legal decimal under the reader contract"
        )
    chosen = selection.chosen
    assert chosen is not None
    coefficient = chosen.coefficient if exact >= 0 else -chosen.coefficient
    printed = Fraction(coefficient, 10 ** chosen.scale)
    return PrintedResidual(
        segment_index=index,
        coefficient=coefficient,
        scale=chosen.scale,
        printed_value=printed,
        exact_residual=exact,
        exact_error=printed - exact,
    )


def verify_against_original(ledger: TwistLedger, index: int, printed: Fraction) -> dict[str, Any]:
    """Check a reprinted decimal against the original exact residual.

    The ledger's own verdict is unaffected by ``printed``; this only reports
    whether the printed number happens to equal the exact value.  A rounded
    reprint must never be fed back as authoritative evidence.
    """
    segment = ledger.segment(index)
    if not segment.signable:
        raise ValidationError(f"segment {index} is un-signable: {segment.reason}")
    exact = segment.residual
    assert exact is not None
    return {
        "segment": index,
        "exact_residual": frac_str(exact),
        "printed": frac_str(Fraction(printed)),
        "equals_exact": Fraction(printed) == exact,
        "verification_source": "original_exact_bits",
        "note": "printed decimals are non-authoritative; re-verify from bits",
    }


# ---------------------------------------------------------------------------
# per-inch combination check used by the pairing layer
# ---------------------------------------------------------------------------

def combine_residuals(
    left: Fraction | None,
    right: Fraction | None,
    lo: Fraction,
    hi: Fraction,
) -> tuple[bool, Fraction | None, str | None]:
    """Combine two inch residuals against a closed interval: ``lo <= sum <= hi``."""
    if left is None or right is None:
        return False, None, "un-signable inch present"
    total = left + right
    if lo <= total <= hi:
        return True, total, None
    return False, total, f"sum {frac_str(total)} outside [{frac_str(lo)}, {frac_str(hi)}]"


def verify_ledger(
    readings: Sequence[Mapping[str, Any] | PointReading],
    ledger: TwistLedger,
) -> None:
    """Independent re-derivation: rebuild from source bits and compare.

    Neither printed decimals nor any stored residual are trusted; the sheet is
    re-derived from the original bits and fixture records only.
    """
    fresh = build_ledger(readings, version=ledger.version)
    if fresh.source_digest != ledger.source_digest:
        raise IntegrityError("twist ledger: source digest drift")
    if fresh.fixture_digest != ledger.fixture_digest:
        raise IntegrityError("twist ledger: fixture digest drift")
    if canonical_json([s.canonical() for s in fresh.segments]) != canonical_json(
        [s.canonical() for s in ledger.segments]
    ):
        raise IntegrityError("twist ledger: segment drift on re-derivation")
    ledger.verify()


__all__ = [
    "SIGNED",
    "UNSIGNABLE",
    "PointReading",
    "Point",
    "Segment",
    "TwistLedger",
    "PrintedResidual",
    "encode_bits",
    "build_ledger",
    "print_residual",
    "verify_against_original",
    "combine_residuals",
    "verify_ledger",
]
