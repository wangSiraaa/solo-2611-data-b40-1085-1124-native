"""Bounded trial generation: local weighing originals (称量原件).

The brief caps the sample at 『最多五笔十拍』 — at most five pens and ten beats —
and asks that the weighing originals be generated *locally* as true binary32
patterns (『本地生成称量原件』), not produced through host float.

``generate_originals`` builds a small trial, simulates it exactly with a chosen
true rate assignment, and emits one original bit pattern per (beat, chamber)
reading, refusing any spec that exceeds the bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

from .bits import encode_bits
from .model import Beat, Pen, State, beat

MAX_PENS = 5
MAX_BEATS = 10


@dataclass(frozen=True)
class OriginalRecord:
    pen: str
    beat_index: int
    chamber: str
    value: Fraction
    bits: str


@dataclass(frozen=True)
class Originals:
    records: tuple[OriginalRecord, ...]
    pens: tuple[str, ...]
    beats: int

    def as_json(self) -> dict:
        return {
            "pens": list(self.pens),
            "beats": self.beats,
            "records": [
                {
                    "pen": r.pen,
                    "beat_index": r.beat_index,
                    "chamber": r.chamber,
                    "value": _s(r.value),
                    "bits": r.bits,
                }
                for r in self.records
            ],
        }


def generate_originals(
    pens: Mapping[str, Pen],
    initial: Mapping[str, State],
    script: Sequence[tuple[str, Beat]],
    rates: Mapping[str, Mapping[str, Fraction]],
) -> Originals:
    """Simulate ``script`` (pen, beat) pairs and emit exact weighing originals.

    Raises ``ValueError`` if the script uses more than ``MAX_PENS`` pens or more
    than ``MAX_BEATS`` beats, or if any reading is not exactly binary32.
    """
    if len(script) > MAX_BEATS:
        raise ValueError(f"script has {len(script)} beats > {MAX_BEATS}")
    used = {p for p, _ in script}
    if len(used) > MAX_PENS:
        raise ValueError(f"script uses {len(used)} pens > {MAX_PENS}")

    states = {p: State(initial[p].nib, initial[p].belly) for p in pens}
    records: list[OriginalRecord] = []
    for index, (pen_name, b) in enumerate(script):
        pen = pens[pen_name]
        rate = Fraction(rates[pen_name][b.direction])
        res = beat(pen, states[pen_name], b, rate, index)
        states[pen_name] = res.after
        for chamber in ("nib", "belly"):
            value = res.after.chamber(chamber)
            bits, exact = encode_bits(value)
            if not exact:
                raise ValueError(
                    f"{pen_name} beat {index} {chamber}={value} not exactly binary32"
                )
            records.append(OriginalRecord(pen_name, index, chamber, value, bits))
        if not res.drew:
            break  # a break ends drawing; later beats produce no reading
    return Originals(tuple(records), tuple(sorted(used)), len(script))


def _s(v: Fraction) -> str:
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
