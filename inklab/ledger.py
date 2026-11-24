"""Ledger: bind weighings to the old observation library (``obsdelivery``).

The task says: 『用旧观测库登记同批称量的真实位型，以规范产品的精确差量核一份
可重开的供墨表』 — register the true bit patterns of a batch of weighings in the
old observation library, then verify a re-openable supply table by the exact
difference of the canonical product.

This module:

* turns exact chamber readings into original binary32 patterns (``bits.encode_bits``)
  and stores them as observations (one sample per beat+chamber);
* publishes a reference whose *expected* value is what the proposed supply
  table (a ``Rates`` for one pen) predicts for that same weighing;
* publishes the canonical semantic product, and reads every item's
  ``abs_difference`` back: a supply table is correct for a trial exactly when
  every difference is zero (『精确差量』).  The product/report is immutable and
  re-openable (``read_report`` / ``get_semantic_product``).

A supply table that no candidate can reproduce raises ``SupplyConflict``
(『空集时报出冲突』).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

from obsdelivery.errors import ProductConflict, ReportConflict
from obsdelivery.service import SemanticsService
from obsdelivery.storage import Store

from .bits import encode_bits
from .model import Beat, Pen, Rates, State, run


def sample_id(trial: str, index: int, chamber: str) -> str:
    return f"{trial}/b{index:02d}/{chamber}"


@dataclass(frozen=True)
class Weighing:
    beat_index: int
    chamber: str
    bits: str
    value: Fraction


def readings_of(pen: Pen, initial: State, beats: Iterable[Beat], rates: Mapping[str, Fraction]) -> tuple:
    """Exact per-beat chamber readings produced by a rate assignment."""
    result = run(pen, initial, beats, rates)
    return tuple(result.results)


class InkLedger:
    """A thin, exact wrapper over one ``obsdelivery`` store."""

    def __init__(self, store: Store | None = None, *, path: str | None = None):
        self.store = store or Store(path or ":memory:")
        self.semantics = SemanticsService(self.store)

    def close(self) -> None:
        self.store.close()

    # -- registering weighings ------------------------------------------------
    def register_weighings(
        self,
        trial: str,
        pen: Pen,
        initial: State,
        beats: Iterable[Beat],
        rates: Mapping[str, Fraction],
        *,
        revision: int = 1,
        negative_zero_belly: bool = False,
    ) -> list[Weighing]:
        """Simulate and register the *true* readings of a trial as bit patterns."""
        beats = tuple(beats)
        results = readings_of(pen, initial, beats, rates)
        weighings: list[Weighing] = []
        records: list[dict] = []
        for res in results:
            for chamber in ("nib", "belly"):
                value = res.after.chamber(chamber)
                nz = negative_zero_belly and chamber == "belly" and value == 0
                bits, exact = encode_bits(value, negative_zero=nz)
                if not exact:
                    raise ValueError(
                        f"reading {value} is not exactly representable in binary32"
                    )
                sid = sample_id(trial, res.index, chamber)
                weighings.append(Weighing(res.index, chamber, bits, value))
                records.append({"sample_id": sid, "revision": revision, "bits": bits})
        self.semantics.admit_observations(records)
        return weighings

    def register_weighing(
        self, trial: str, beat_index: int, chamber: str, value: Fraction,
        *, revision: int = 1, negative_zero: bool = False,
    ) -> Weighing:
        bits, exact = encode_bits(value, negative_zero=negative_zero)
        if not exact:
            raise ValueError(f"reading {value} is not exactly representable in binary32")
        sid = sample_id(trial, beat_index, chamber)
        self.semantics.admit_observations(
            [{"sample_id": sid, "revision": revision, "bits": bits}]
        )
        return Weighing(beat_index, chamber, bits, value)

    # -- verifying a supply table by exact difference -------------------------
    def verify_supply_table(
        self,
        trial: str,
        pen: Pen,
        initial: State,
        beats: Iterable[Beat],
        table: Mapping[str, Fraction],
        *,
        reference_version: str | None = None,
        product_id: str | None = None,
    ) -> dict:
        """Publish a reference+product and read back the exact differences.

        The expected value for each weighing sample is what ``table`` predicts.
        A correct table yields zero difference everywhere and ``PASS`` status.
        Returns a dict with ``all_zero``, ``max_abs_difference`` and the items.
        """
        beats = tuple(beats)
        results = readings_of(pen, initial, beats, table)
        expected: dict[str, Fraction] = {}
        for res in results:
            for chamber in ("nib", "belly"):
                sid = sample_id(trial, res.index, chamber)
                expected[sid] = res.after.chamber(chamber)

        version = reference_version or f"{trial}-sup"
        items = [
            {"sample_id": sid, "expected": _frac_str(v), "tolerance": "0"}
            for sid, v in sorted(expected.items())
        ]
        try:
            self.semantics.publish_reference(version, items, copy_from=None)
        except Exception:  # ReferenceConflict -> reference already present
            pass

        samples = sorted(expected)
        pid = product_id or f"{trial}-sup-product"
        try:
            product = self.semantics.publish_semantic_product(pid, samples, version)
        except ProductConflict:
            product = self.semantics.get_semantic_product(pid)

        diffs = []
        all_zero = True
        for item in product["items"]:
            if item["status"] == "PASS":
                diffs.append(Fraction(0))
            elif item["abs_difference"] is None:
                all_zero = False
                diffs.append(None)
            else:
                d = Fraction(item["abs_difference"])
                diffs.append(d)
                if d != 0:
                    all_zero = False
        max_abs = max((d for d in diffs if d is not None), default=Fraction(0))
        return {
            "trial": trial,
            "reference_version": version,
            "product_id": pid,
            "product_digest": product["content_digest"],
            "items": product["items"],
            "all_zero": all_zero,
            "max_abs_difference": max_abs,
            "differences": diffs,
        }

    # -- re-opening -----------------------------------------------------------
    def reopen_report(self, report_id: str) -> dict:
        return self.semantics.read_report(report_id)

    def build_report(self, report_id: str, reference_version: str) -> dict:
        try:
            return self.semantics.report(report_id, reference_version)
        except ReportConflict:
            return self.semantics.read_report(report_id)


def _frac_str(value) -> str:
    f = Fraction(value)
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"
