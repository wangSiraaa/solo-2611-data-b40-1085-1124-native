"""Adapter from ``citation-product/1`` rows to banquet inputs.

The citation system already decides, from a single catalog snapshot, which
citations are usable and which need attention.  This module does *not* re-read
a catalog or re-resolve labels: it consumes the exported JSONL only, exactly
like ``materials/726/citation_client.py``.  Two responsibilities:

* a **usable PINNED** citation is the sign-in sheet -- the set of guests who
  actually came.  A ``needs_attention`` PINNED (an inactive member) cannot be
  a trustworthy roster, so the sitting is suspended.
* a **usable RANGE** citation is the ordinance; its pages, in current order,
  are the ordinance pages.  A ``needs_attention`` RANGE (inactive/reversed
  endpoint) is suspended rather than guessed.

Any session lacking a required citation is suspended: ``suspend_reasons`` is
non-empty and no chart should be produced from "whatever pages happen to
exist".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REQUIRED_FIELDS = (
    "format",
    "catalog_revision",
    "citation_id",
    "text",
    "kind",
    "status",
    "source",
    "pages",
    "reason",
)


class ProductError(ValueError):
    """A citation product row violates the published contract."""


@dataclass(frozen=True)
class ProductRow:
    raw: dict[str, Any]

    @property
    def citation_id(self) -> str:
        return self.raw["citation_id"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def status(self) -> str:
        return self.raw["status"]

    @property
    def usable(self) -> bool:
        return self.status == "usable"

    @property
    def revision(self) -> int:
        return int(self.raw["catalog_revision"])

    @property
    def pages(self) -> list[dict[str, Any]]:
        return list(self.raw["pages"])

    @property
    def page_keys(self) -> list[tuple[str, str]]:
        return [(p["volume_id"], p["page_id"]) for p in self.pages]


def _validate_row(row: dict[str, Any], where: str) -> None:
    missing = [k for k in REQUIRED_FIELDS if k not in row]
    if missing:
        raise ProductError(f"{where}: missing fields {missing}")
    if row["format"] != "citation-product/1":
        raise ProductError(f"{where}: bad format {row['format']!r}")
    if row["kind"] not in ("PINNED", "RANGE"):
        raise ProductError(f"{where}: bad kind {row['kind']!r}")
    if row["status"] not in ("usable", "needs_attention"):
        raise ProductError(f"{where}: bad status {row['status']!r}")
    if row["status"] == "usable" and row["reason"] is not None:
        raise ProductError(f"{where}: usable row carries a reason")


@dataclass
class ProductSet:
    rows: dict[str, ProductRow] = field(default_factory=dict)
    revisions: set[int] = field(default_factory=set)

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> "ProductSet":
        out = cls()
        for i, row in enumerate(rows, 1):
            _validate_row(row, f"row {i}")
            cid = row["citation_id"]
            if cid in out.rows:
                raise ProductError(f"citation {cid!r} appears more than once")
            out.rows[cid] = ProductRow(row)
            out.revisions.add(int(row["catalog_revision"]))
        return out

    # -- decisions ------------------------------------------------------
    def usable_pinned(self) -> list[ProductRow]:
        return [r for r in self.rows.values() if r.kind == "PINNED" and r.usable]

    def usable_ranges(self) -> list[ProductRow]:
        return [r for r in self.rows.values() if r.kind == "RANGE" and r.usable]

    def suspended_pinned(self) -> list[ProductRow]:
        return [r for r in self.rows.values() if r.kind == "PINNED" and not r.usable]

    def suspended_ranges(self) -> list[ProductRow]:
        return [r for r in self.rows.values() if r.kind == "RANGE" and not r.usable]

    def suspend_reasons(self) -> list[str]:
        """Why a sitting cannot be built from these products alone.

        Empty means the materials are complete and usable; a non-empty list is
        a reviewable basis for suspending the session.
        """

        reasons: list[str] = []
        if not self.usable_pinned():
            reasons.append("no_usable_pinned_signin")
        if not self.usable_ranges():
            reasons.append("no_usable_range_ordinance")
        for row in self.suspended_pinned():
            reasons.append(f"pinned_needs_attention:{row.citation_id}:{row.raw['reason']}")
        for row in self.suspended_ranges():
            reasons.append(f"range_needs_attention:{row.citation_id}:{row.raw['reason']}")
        if len(self.revisions) > 1:
            reasons.append("mixed_catalog_revisions")
        return reasons

    def single_revision(self) -> int:
        if len(self.revisions) != 1:
            raise ProductError("products span multiple catalog revisions")
        return next(iter(self.revisions))


def load_product_files(*paths: str | Path) -> ProductSet:
    """Read one or more JSONL files (usable + needs_attention) into a set."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                cid = row.get("citation_id")
                if cid in seen:
                    raise ProductError(
                        f"{path}:{number}: citation {cid!r} appears more than once"
                    )
                seen.add(cid)
                rows.append(row)
    return ProductSet.from_rows(rows)
