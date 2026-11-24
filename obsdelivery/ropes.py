"""Real yarns on axes and exact per-inch pairing checks (pure, storage-free).

A real yarn lies on one axis; it is measured in equal one-inch segments, each
carrying an exact residual twist (a ``Fraction``).  A *span* is a contiguous
run of inches taken from one axis, optionally reversed (attached from the
tail).  Two spans on **different** axes can be joined only if, inch by inch,
their residual sum lies in the per-inch closed interval given by the order.

Nothing here consumes or moves material; that is the workshop layer's job.
This layer only answers "would these two runs, combined this way, pass?" and
"which bench/material is involved", all in exact rationals.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .canonical import frac_str, parse_fraction
from .errors import ValidationError

HEAD = "head"
TAIL = "tail"
ORIENTATIONS = (HEAD, TAIL)


def _as_fraction(value: Fraction | int | str, field: str) -> Fraction:
    if isinstance(value, Fraction) or (isinstance(value, int) and not isinstance(value, bool)):
        return Fraction(value)
    return parse_fraction(value, field=field)


@dataclass(frozen=True)
class Axis:
    """A real yarn on an axis: per-inch residuals in head->tail order."""

    axis_id: str
    yarn_id: str
    residuals: tuple[Fraction, ...]

    def __post_init__(self) -> None:
        if not self.axis_id or not self.yarn_id:
            raise ValidationError("axis_id and yarn_id must be non-empty")
        if not self.residuals:
            raise ValidationError(f"axis {self.axis_id} has no inches")

    @property
    def length(self) -> int:
        return len(self.residuals)

    @staticmethod
    def from_readings(
        axis_id: str,
        yarn_id: str,
        readings: Sequence[Fraction | int | str],
    ) -> "Axis":
        values = tuple(_as_fraction(r, "residual") for r in readings)
        return Axis(axis_id, yarn_id, values)

    @staticmethod
    def from_cumulative(
        axis_id: str,
        yarn_id: str,
        cumulative: Sequence[Fraction | int | str],
    ) -> "Axis":
        """Build per-inch residuals from cumulative boundary readings."""
        cum = [_as_fraction(c, "cumulative") for c in cumulative]
        if len(cum) < 2:
            raise ValidationError("need at least two cumulative readings")
        residuals = tuple(cum[i] - cum[i - 1] for i in range(1, len(cum)))
        return Axis(axis_id, yarn_id, residuals)

    def canonical(self) -> dict[str, Any]:
        return {
            "axis_id": self.axis_id,
            "yarn_id": self.yarn_id,
            "length": self.length,
            "residuals": [frac_str(v) for v in self.residuals],
        }


@dataclass(frozen=True)
class Span:
    """A contiguous run of inches on an axis (0-based, half-open)."""

    start: int
    length: int
    orientation: str = HEAD

    def __post_init__(self) -> None:
        if self.orientation not in ORIENTATIONS:
            raise ValidationError(f"orientation must be one of {ORIENTATIONS}")
        if self.start < 0 or self.length <= 0:
            raise ValidationError("span needs start >= 0 and length > 0")

    def inches(self) -> range:
        return range(self.start, self.start + self.length)

    def slice(self, axis: Axis) -> tuple[Fraction, ...]:
        if self.start + self.length > axis.length:
            raise ValidationError(
                f"span {self.start}+{self.length} exceeds axis {axis.axis_id} "
                f"length {axis.length}"
            )
        run = axis.residuals[self.start:self.start + self.length]
        return tuple(reversed(run)) if self.orientation == TAIL else tuple(run)

    def canonical(self) -> dict[str, Any]:
        return {"start": self.start, "length": self.length, "orientation": self.orientation}


@dataclass(frozen=True)
class Interval:
    """Closed residual interval ``[lo, hi]`` for one inch."""

    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValidationError(f"interval lo {self.lo} > hi {self.hi}")

    def contains(self, value: Fraction) -> bool:
        return self.lo <= value <= self.hi

    def canonical(self) -> dict[str, str]:
        return {"lo": frac_str(self.lo), "hi": frac_str(self.hi)}


@dataclass(frozen=True)
class InchRow:
    index: int  # 1-based
    left_residual: Fraction
    right_residual: Fraction
    total: Fraction
    interval: Interval
    ok: bool

    def canonical(self) -> dict[str, Any]:
        return {
            "inch": self.index,
            "left": frac_str(self.left_residual),
            "right": frac_str(self.right_residual),
            "total": frac_str(self.total),
            "interval": self.interval.canonical(),
            "ok": self.ok,
        }


@dataclass(frozen=True)
class PairEvaluation:
    left_axis: str
    left_span: Span
    right_axis: str
    right_span: Span
    rows: tuple[InchRow, ...]
    ok: bool
    reason: str | None

    def canonical(self) -> dict[str, Any]:
        return {
            "left_axis": self.left_axis,
            "left_span": self.left_span.canonical(),
            "right_axis": self.right_axis,
            "right_span": self.right_span.canonical(),
            "rows": [row.canonical() for row in self.rows],
            "ok": self.ok,
            "reason": self.reason,
        }


def evaluate_pair(
    left: Axis,
    left_span: Span,
    right: Axis,
    right_span: Span,
    intervals: Sequence[Interval],
) -> PairEvaluation:
    """Check two spans inch by inch against a closed residual interval each.

    ``intervals`` must have exactly ``left_span.length`` entries and the two
    spans must be the same length.  Different axes are required.
    """
    if left.axis_id == right.axis_id:
        raise ValidationError(
            f"cannot pair an axis with itself ({left.axis_id}); two different axes required"
        )
    if left_span.length != right_span.length:
        raise ValidationError(
            f"span lengths differ: {left_span.length} vs {right_span.length}"
        )
    if len(intervals) != left_span.length:
        raise ValidationError(
            f"need {left_span.length} intervals, got {len(intervals)}"
        )
    lvals = left_span.slice(left)
    rvals = right_span.slice(right)
    rows: list[InchRow] = []
    good = True
    for i, (lv, rv, interval) in enumerate(zip(lvals, rvals, intervals), start=1):
        if not isinstance(interval, Interval):
            raise ValidationError("intervals must be Interval instances")
        total = lv + rv
        ok = interval.contains(total)
        good = good and ok
        rows.append(InchRow(i, lv, rv, total, interval, ok))
    reason = None
    if not good:
        bad = [row.index for row in rows if not row.ok]
        reason = f"inch(es) {bad} outside their closed interval"
    return PairEvaluation(
        left.axis_id, left_span, right.axis_id, right_span, tuple(rows), good, reason
    )


def _as_fraction(value: Fraction | int | str, field: str) -> Fraction:
    if isinstance(value, Fraction) or (isinstance(value, int) and not isinstance(value, bool)):
        return Fraction(value)
    return parse_fraction(value, field=field)


def make_intervals(spec: Sequence[Sequence[Fraction | int | str] | Mapping[str, Any]]) -> tuple[Interval, ...]:
    """Coerce ``[lo, hi]`` pairs or ``{"lo","hi"}`` mappings to Intervals."""
    out: list[Interval] = []
    for item in spec:
        if isinstance(item, Mapping):
            lo = _as_fraction(item.get("lo"), "lo")
            hi = _as_fraction(item.get("hi"), "hi")
        else:
            seq = list(item)
            if len(seq) != 2:
                raise ValidationError(f"interval must be [lo, hi], got {item!r}")
            lo = _as_fraction(seq[0], "lo")
            hi = _as_fraction(seq[1], "hi")
        out.append(Interval(lo, hi))
    return tuple(out)


__all__ = [
    "HEAD",
    "TAIL",
    "ORIENTATIONS",
    "Axis",
    "Span",
    "Interval",
    "InchRow",
    "PairEvaluation",
    "evaluate_pair",
    "make_intervals",
]
