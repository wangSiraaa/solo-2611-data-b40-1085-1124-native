"""Decode a ceremony script into ordinance pages and guests.

The citation products decide *which* pages are in force and in what order.
This module supplies the *meaning* of each page -- what ritual clause it
carries -- and the guest directory (who a page identity denotes, and which
languages they know).

A *script* is plain data so it can be reviewed next to the citation products::

    {
      "guests": {
        "甲": {"languages": ["red"]},
        "乙": {"languages": ["blue"]},
        "丙": {"languages": ["red", "blue"]}
      },
      # optional: map real page identities to guest ids for the sign-in sheet
      "identities": {"V1/p2": "甲", "V1/p3": "乙"},
      # ordinance pages, keyed by the page identity the RANGE yields
      "ordinances": [
        {"page_id": "op1", "kind": "order", "sequence": ["甲", "丙", "乙", "丁"]},
        {"page_id": "op2", "kind": "adjacency", "pairs": [["甲", "乙"]]},
        {"page_id": "op3", "kind": "substitute",
         "original": "丙", "substitute": "戊", "mode": "inherit_order"},
        {"page_id": "op4", "kind": "revoke", "target_page_id": "op1"}
      ]
    }

Ordinance page ids in the script are matched against the identities the usable
RANGE actually covers, in the RANGE's current order.  A page present in the
RANGE but absent from the script is reported (not silently ignored); this is
how a mis-keyed script becomes a reviewable suspension instead of a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AdjacencyClause,
    Clause,
    Edge,
    Guest,
    OrderClause,
    PageOrdinance,
    RevocationClause,
    SubstituteClause,
)
from .products import ProductRow, ProductSet


class ScriptError(ValueError):
    pass


@dataclass(frozen=True)
class Script:
    guests: dict[str, Guest]
    identities: dict[str, str]
    ordinances: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Script":
        if not isinstance(data, dict):
            raise ScriptError("script must be an object")
        raw_guests = data.get("guests", {})
        guests: dict[str, Guest] = {}
        for gid, spec in raw_guests.items():
            langs = spec.get("languages", []) if isinstance(spec, dict) else spec
            if not isinstance(langs, (list, tuple)):
                raise ScriptError(f"guest {gid!r} languages must be a list")
            guests[gid] = Guest(gid, frozenset(langs))
        identities = dict(data.get("identities", {}))
        ordinances = {o["page_id"]: o for o in data.get("ordinances", [])}
        for pid, o in ordinances.items():
            if "kind" not in o:
                raise ScriptError(f"ordinance {pid!r} has no kind")
        return cls(guests=guests, identities=identities, ordinances=ordinances)

    @classmethod
    def load(cls, path: str | Path) -> "Script":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def decode_clause(page_id: str, spec: dict[str, Any]) -> Clause:
    kind = spec.get("kind")
    if kind == "order":
        seq = tuple(spec["sequence"])
        return OrderClause(page_id, seq, head=spec.get("head"))
    if kind == "adjacency":
        pairs = tuple(Edge(*sorted(pair)) for pair in spec.get("pairs", []))
        return AdjacencyClause(page_id, pairs)
    if kind == "substitute":
        return SubstituteClause(
            page_id,
            original=spec["original"],
            substitute=spec["substitute"],
            mode=spec.get("mode", "inherit_order"),
        )
    if kind == "revoke":
        return RevocationClause(page_id, spec["target_page_id"])
    raise ScriptError(f"unknown ordinance kind {kind!r} on page {page_id!r}")


@dataclass(frozen=True)
class DecodedSession:
    ordinance_pages: tuple[PageOrdinance, ...]
    guests: dict[str, Guest]
    identity_of: dict[str, str]
    problems: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.problems


def decode_ordinance(
    products: ProductSet,
    script: Script,
    *,
    range_citation_id: str | None = None,
) -> DecodedSession:
    """Turn the usable RANGE's pages, in order, into ordinance pages.

    The RANGE's current page order is the ordinance order.  Each fairly-named
    page must have a script entry.  Undescribed pages, or script entries that
    never appear, are reported as problems (the session should then be
    reviewed, not improvised).
    """

    problems: list[str] = []
    ranges = products.usable_ranges()
    if range_citation_id is not None:
        ranges = [r for r in ranges if r.citation_id == range_citation_id]
        if not ranges:
            return DecodedSession((), dict(script.guests), dict(script.identities),
                                  (f"range_citation_not_usable:{range_citation_id}",))

    if not ranges:
        return DecodedSession((), dict(script.guests), dict(script.identities),
                              ("no_usable_range_ordinance",))
    if len(ranges) > 1:
        problems.append("multiple_usable_ranges:choose_one_with_range_citation_id")
        return DecodedSession((), dict(script.guests), dict(script.identities),
                              tuple(problems))

    row: ProductRow = ranges[0]
    pages: list[PageOrdinance] = []
    seen_ids: set[str] = set()
    for volume_id, page_id in row.page_keys:
        key = f"{volume_id}/{page_id}"
        spec = script.ordinances.get(page_id) or script.ordinances.get(key)
        if spec is None:
            problems.append(f"ordination_page_undescribed:{key}")
            continue
        seen_ids.add(spec["page_id"])
        pages.append(PageOrdinance(spec["page_id"], decode_clause(spec["page_id"], spec)))

    for pid in script.ordinances:
        if pid not in seen_ids:
            problems.append(f"script_ordinance_not_in_range:{pid}")

    return DecodedSession(
        ordinance_pages=tuple(pages),
        guests=dict(script.guests),
        identity_of=dict(script.identities),
        problems=tuple(problems),
    )
