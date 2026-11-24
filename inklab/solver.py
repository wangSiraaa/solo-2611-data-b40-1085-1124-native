"""Rate solver: which fixed supply rates explain every chamber weighing?

A *trial* (试验) fixes:

* the pen (its two capacities);
* each beat's direction and stroke cost;
* the starting state and any dip/refill gaps;
* the observed readings, each tagged by chamber and instrument bucket.

Rates are fixed per ``(pen, direction)`` across the whole trial.  The solver
enumerates the candidate rate tuples from the trial's candidate list and keeps
those whose simulated readings match every observation *as the instrument can
see them* (same bucket), never as exact values.  If none survive, the trial is
reported as a conflict (『空集时报出冲突』).

Because the candidate space is tiny ("试验只列少量候选供墨率"), exhaustive
enumeration is exact and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Hashable, Mapping, Sequence

from .instrument import Instrument, exact_instrument
from .model import Beat, Pen, Rates, State, beat


@dataclass(frozen=True)
class Observation:
    """One chamber reading after the beat at ``beat_index``."""

    beat_index: int
    chamber: str  # "nib" | "belly"
    bucket: Hashable


@dataclass(frozen=True)
class Trial:
    pen: Pen
    initial: State
    beats: tuple[Beat, ...]
    candidates: Mapping[str, Sequence[Fraction]]  # direction -> candidate rates
    observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        beats = tuple(self.beats)
        object.__setattr__(self, "beats", beats)
        cands = {d: tuple(Fraction(r) for r in rs) for d, rs in self.candidates.items()}
        object.__setattr__(self, "candidates", cands)
        obs = tuple(self.observations)
        object.__setattr__(self, "observations", obs)
        for direction in {b.direction for b in beats}:
            if direction not in cands:
                raise ValueError(f"no candidate rates given for direction {direction!r}")
        for o in obs:
            if o.chamber not in ("nib", "belly"):
                raise ValueError(f"bad chamber {o.chamber!r}")
            if not 0 <= o.beat_index < len(beats):
                raise ValueError(f"observation beat_index {o.beat_index} out of range")


@dataclass(frozen=True)
class Explanation:
    rates: dict[str, Fraction]

    def as_rates(self, pen: str) -> Rates:
        return Rates({(pen, d): r for d, r in self.rates.items()})


@dataclass(frozen=True)
class SolveResult:
    explanations: tuple[Explanation, ...]
    conflict: bool
    total_combinations: int

    @property
    def unique(self) -> bool:
        return len(self.explanations) == 1

    def rate_tuples(self) -> list[dict[str, Fraction]]:
        return [e.rates for e in self.explanations]


def _simulate_readings(
    trial: Trial, rates: Mapping[str, Fraction], instrument: Instrument
) -> list[tuple[int, str, Hashable]]:
    """Simulate the trial and return (beat, chamber, bucket) readings."""
    state = trial.initial
    out: list[tuple[int, str, Hashable]] = []
    for index, b in enumerate(trial.beats):
        res = beat(trial.pen, state, b, Fraction(rates[b.direction]), index)
        out.append((index, "nib", instrument.key(res.after.nib)))
        out.append((index, "belly", instrument.key(res.after.belly)))
        if not res.drew:
            break  # rest of the trial is broken; observations beyond are impossible
        state = res.after
    return out


def solve(trial: Trial, instrument: Instrument | None = None) -> SolveResult:
    """Enumerate candidate rates and keep those matching every observation."""
    inst = instrument or exact_instrument()
    directions = sorted(trial.candidates)
    axis = [trial.candidates[d] for d in directions]
    combos = list(product(*axis))
    want = {(o.beat_index, o.chamber): o.bucket for o in trial.observations}

    survivors: list[Explanation] = []
    for combo in combos:
        rates = dict(zip(directions, combo))
        readings = _simulate_readings(trial, rates, inst)
        got = {(i, ch): bucket for (i, ch, bucket) in readings}
        if all(got.get(k) == v for k, v in want.items()):
            survivors.append(Explanation(rates))
    return SolveResult(tuple(survivors), not survivors, len(combos))
