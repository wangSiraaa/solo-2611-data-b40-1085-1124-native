"""Ink-flow model for the dip pen (供墨规约).

A pen has two chambers: the nib (尖) and the belly (腹).  Per *beat* (拍):

1. The nib first pays the ink the stroke needs.  If it cannot, the stroke
   **breaks** (断线): nothing is drawn for that beat.
2. Then the belly refills the nib, bounded by three quantities:
   the direction's supply rate, the ink left in the belly, and the spare
   capacity of the nib::

       refill = min(rate, belly, nib_capacity - nib_after_spend)

Rates are fixed per ``(pen, direction)`` for the whole experiment.  Everything
is an exact ``Fraction``; no host float is involved.

Directions are ``h`` (horizontal) and ``v`` (vertical).  A stroke's *width* is
folded into its ink ``cost`` (宽窄笔耗), so a "wide vertical" is just a ``v``
beat with a larger cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

H = "h"
V = "v"
DIRECTIONS = (H, V)

NIB = "nib"
BELLY = "belly"
CHAMBERS = (NIB, BELLY)


@dataclass(frozen=True)
class Pen:
    name: str
    nib_cap: Fraction
    belly_cap: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("pen name must be a non-empty string")
        object.__setattr__(self, "nib_cap", Fraction(self.nib_cap))
        object.__setattr__(self, "belly_cap", Fraction(self.belly_cap))
        if self.nib_cap < 0 or self.belly_cap < 0:
            raise ValueError("pen capacities must be non-negative")


@dataclass(frozen=True)
class State:
    nib: Fraction
    belly: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "nib", Fraction(self.nib))
        object.__setattr__(self, "belly", Fraction(self.belly))

    def chamber(self, name: str) -> Fraction:
        return self.nib if name == NIB else self.belly

    @property
    def total(self) -> Fraction:
        return self.nib + self.belly


@dataclass(frozen=True)
class Beat:
    direction: str
    cost: Fraction

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"unknown direction {self.direction!r}")
        object.__setattr__(self, "cost", Fraction(self.cost))
        if self.cost < 0:
            raise ValueError("stroke cost must be non-negative")


@dataclass(frozen=True)
class BeatResult:
    index: int
    direction: str
    cost: Fraction
    drew: bool
    refilled: Fraction
    before: State
    after: State

    def chamber(self, name: str) -> Fraction:
        return self.after.chamber(name)


@dataclass(frozen=True)
class RunResult:
    results: tuple[BeatResult, ...]
    completed: bool
    final: State
    broke_at: int | None

    @property
    def text_ink(self) -> Fraction:
        """Ink actually spent drawing (excludes ink still held)."""
        return sum((r.cost for r in self.results if r.drew), Fraction(0))


class Rates:
    """Fixed per-(pen,direction) supply rates."""

    def __init__(self, mapping: Mapping[tuple[str, str], Fraction]):
        self._m: dict[tuple[str, str], Fraction] = {
            (p, d): Fraction(r) for (p, d), r in mapping.items()
        }
        for (p, d), r in self._m.items():
            if d not in DIRECTIONS:
                raise ValueError(f"unknown direction {d!r}")
            if r < 0:
                raise ValueError("rate must be non-negative")

    def get(self, pen: str, direction: str) -> Fraction:
        return self._m[(pen, direction)]

    def __getitem__(self, key: tuple[str, str]) -> Fraction:
        return self._m[key]

    def items(self):
        return self._m.items()

    def subset(self, pens: Iterable[str]) -> "Rates":
        keep = set(pens)
        return Rates({k: v for k, v in self._m.items() if k[0] in keep})

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Rates) and self._m == other._m

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._m.items())))

    def as_dict(self) -> dict[tuple[str, str], Fraction]:
        return dict(self._m)


def beat(pen: Pen, state: State, b: Beat, rate: Fraction, index: int = 0) -> BeatResult:
    """Apply one beat to ``state`` and return its result; ``state`` is unchanged."""
    if rate < 0:
        raise ValueError("rate must be non-negative")
    if state.nib < b.cost:
        # Not enough ink in the nib: the stroke breaks and nothing is drawn.
        return BeatResult(index, b.direction, b.cost, False, Fraction(0), state, state)
    nib = state.nib - b.cost
    refill = min(Fraction(rate), state.belly, pen.nib_cap - nib)
    if refill < 0:
        refill = Fraction(0)
    after = State(nib + refill, state.belly - refill)
    return BeatResult(index, b.direction, b.cost, True, refill, state, after)


def run(
    pen: Pen,
    initial: State,
    beats: Iterable[Beat],
    rates: Mapping[str, Fraction] | Rates,
) -> RunResult:
    """Simulate a full beat sequence; stop at the first break.

    ``rates`` may be a ``Rates`` object or a ``direction -> rate`` mapping for a
    single pen.
    """
    if isinstance(rates, Rates):
        def rate_for(direction: str) -> Fraction:
            return rates.get(pen.name, direction)
    else:
        def rate_for(direction: str) -> Fraction:
            return Fraction(rates[direction])

    state = initial
    results: list[BeatResult] = []
    broke_at: int | None = None
    for index, b in enumerate(beats):
        res = beat(pen, state, b, rate_for(b.direction), index)
        results.append(res)
        if not res.drew:
            broke_at = index
            break
        state = res.after
    return RunResult(tuple(results), broke_at is None, state, broke_at)


def dip(pen: Pen, *, nib: Fraction | None = None, belly: Fraction | None = None,
        state: State | None = None) -> State:
    """『蘸至台卡的两室指定量』 — set chambers to desk-card amounts (clipped to caps)."""
    base = state or State(Fraction(0), Fraction(0))
    new_nib = base.nib if nib is None else Fraction(nib)
    new_belly = base.belly if belly is None else Fraction(belly)
    if new_nib < 0 or new_belly < 0:
        raise ValueError("dipped amounts must be non-negative")
    return State(min(new_nib, pen.nib_cap), min(new_belly, pen.belly_cap))
