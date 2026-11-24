"""Instrument resolution and "same reading" indistinguishability.

The workshop scale does not report an arbitrary real; it reports whatever tick
is actually distinguishable at that moment (『称量只读仪器当时真实可辨的刻度』).
An ``Instrument`` maps an exact rational reading to a *bucket key*:

* two readings with the same key are indistinguishable — two supply rates that
  register as the same reading may **not** be split apart
  (『登记为同一读数的两种供墨率不得偷分』);
* two readings with different keys are distinguishable.

Three instruments are provided:

* ``exact_instrument``  — every exact value is its own bucket (perfect scale);
* ``group_instrument``  — explicit groups, e.g. merging 0 and 1 into one tick
  (『把 0 和 1 并为同一读数档』);
* ``contract_instrument`` — the bucket is the decimal a reader contract can
  actually deliver (``coefficient/10^scale``).  This reuses the existing
  ``obsdelivery`` delivery logic, so two distinct values that round to the same
  delivered decimal are *one* reading and cannot be told apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Hashable, Iterable, Sequence

from obsdelivery.contracts import ReaderContract
from obsdelivery.delivery_algo import select_representation


def _frac_key(value: Fraction) -> Hashable:
    return value


@dataclass(frozen=True)
class Instrument:
    """A scale whose distinguishable ticks are the buckets of ``bucket``."""

    name: str
    bucket: Callable[[Fraction], Hashable]
    describe: str = ""

    def key(self, value: Fraction) -> Hashable:
        return self.bucket(Fraction(value))

    def same_reading(self, a: Fraction, b: Fraction) -> bool:
        return self.key(a) == self.key(b)

    def distinguishes(self, a: Fraction, b: Fraction) -> bool:
        return not self.same_reading(a, b)


def exact_instrument() -> Instrument:
    return Instrument("exact", _frac_key, "every exact value is distinguishable")


def group_instrument(groups: Sequence[Sequence[Fraction]], *, name: str = "grouped") -> Instrument:
    """Merge the listed values into shared ticks; unlisted values stay exact."""
    table: dict[Fraction, int] = {}
    for index, group in enumerate(groups):
        for value in group:
            table[Fraction(value)] = index

    def bucket(value: Fraction) -> Hashable:
        if value in table:
            return ("group", table[value])
        return ("value", value)

    return Instrument(name, bucket, f"groups={[[str(Fraction(v)) for v in g] for g in groups]}")


def contract_instrument(contract: ReaderContract, *, name: str = "contract") -> Instrument:
    """Bucket = the decimal a reader contract can deliver.

    A value without a legal representation gets a unique (value-specific)
    bucket, so it can never be silently merged with a deliverable one.
    """

    def bucket(value: Fraction) -> Hashable:
        selection = select_representation(
            abs(value), contract.allowed_scales, contract.max_abs_coefficient,
            contract.max_abs_error,
        )
        if not selection.feasible:
            return ("nondeliverable", value)
        chosen = selection.chosen
        coef = chosen.coefficient if value >= 0 else -chosen.coefficient
        return ("delivered", coef, chosen.scale)

    return Instrument(name, bucket, f"contract={contract.canonical()}")


def readings_from_instrument(
    instrument: Instrument, values: Iterable[Fraction]
) -> list[Hashable]:
    return [instrument.key(Fraction(v)) for v in values]
