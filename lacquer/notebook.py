"""Gauge notebooks: append-only rim transcriptions and rim reconstruction.

Notebook model
--------------
A :class:`Journal` is the gauge note for one physical rim of one vessel.  A
journal is read page by page under a *stable identity* (``journal_id``); pages
are never renumbered.  Each page records how many cells the gauge walked
clockwise from the previous mark and which feature (``lug`` / ``notch``) was
seen at the landing cell.  The equal-division count and the first reference
mark are recorded separately (they are not ordinary pages).

* The page walks must close: the cumulative walk ends back on the reference
  cell, i.e. the steps sum to exactly one circle (``divisions``).  A journal
  that does not close is a ``closure_failure`` and yields no rim.
* Two pages may not land on the same cell (a cell holds at most one recorded
  mark): a ``cell_collision`` likewise yields no rim.

A worn reference mark gives a finite list of candidate starting offsets.  The
top and bottom rims of one vessel share that choice, so each candidate offset
produces one candidate :class:`~lacquer.model.VesselConfiguration`.

Revisions and the old export
----------------------------
The :class:`JournalBook` keeps every revision of a named page.  A correction
is appended as a new revision of the same page identity; old readings stay
replayable, which is what the "same-name page revision rerun" needs.  Sampling
only happens over *usable page segments* of an old export (the
``citation-product/1`` usable file): when a segment list is supplied, pages
absent from it are treated as missing, and a journal whose pages are all
unavailable is not sampled at all rather than guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import LacquerError
from .model import (
    KIND_LUG,
    KIND_NOTCH,
    LUG,
    NOTCH,
    Ring,
    VesselConfiguration,
    ring_from_features,
)

CLOSURE_FAILURE = "closure_failure"
CELL_COLLISION = "cell_collision"
UNKNOWN_FEATURE = "unknown_feature"
JOURNAL_NOT_FOUND = "journal_not_found"
PAGE_NOT_FOUND = "page_not_found"
INVALID_NOTEBOOK = "invalid_notebook"
NO_USABLE_PAGES = "no_usable_pages"

FEATURE_BY_KIND = {KIND_LUG: LUG, KIND_NOTCH: NOTCH}


@dataclass(frozen=True)
class Page:
    """One gauge page: walk ``step`` cells clockwise and see ``feature``."""

    page_id: str
    step: int
    feature: str  # KIND_LUG / KIND_NOTCH

    def as_dict(self) -> dict[str, Any]:
        return {"page_id": self.page_id, "step": self.step, "feature": self.feature}


@dataclass(frozen=True)
class Journal:
    """One rim's gauge note under a stable identity."""

    journal_id: str
    divisions: int
    reference_feature: str | None  # feature at cell 0; None means blank
    pages: tuple[Page, ...]
    # Finite candidate starting points when the reference is worn.  A crisp
    # reference is the single-element list [0].
    origin_candidates: tuple[int, ...] = (0,)

    def page_ids(self) -> tuple[str, ...]:
        return tuple(p.page_id for p in self.pages)


@dataclass(frozen=True)
class Reconstruction:
    """Result of reconstructing one rim journal."""

    journal_id: str
    divisions: int
    conflicts: tuple[str, ...]
    # One ring per worn-base candidate (same length as origin_candidates);
    # empty when the transcription itself is contradictory.
    rings: tuple[Ring, ...]
    origins: tuple[int, ...]
    sampled_pages: tuple[str, ...]


@dataclass
class _Revision:
    page_id: str
    step: int
    feature: str
    revision: int


class JournalBook:
    """Append-only store of rim journals with revision-tracked pages."""

    def __init__(self) -> None:
        self._journals: dict[str, Journal] = {}
        self._revisions: dict[tuple[str, str], list[_Revision]] = {}

    # ------------------------------------------------------------------
    # loading / revision-safe mutations
    # ------------------------------------------------------------------
    def add_journal(self, journal: Journal) -> None:
        if journal.journal_id in self._journals:
            raise LacquerError(
                INVALID_NOTEBOOK,
                f"journal {journal.journal_id!r} already exists",
                details={"journal_id": journal.journal_id},
            )
        _validate_journal_shape(journal)
        self._journals[journal.journal_id] = journal
        seen: set[str] = set()
        for page in journal.pages:
            if page.page_id in seen:
                raise LacquerError(
                    INVALID_NOTEBOOK,
                    f"duplicate page {page.page_id!r} in journal {journal.journal_id!r}",
                    details={"journal_id": journal.journal_id, "page_id": page.page_id},
                )
            seen.add(page.page_id)
            self._revisions[(journal.journal_id, page.page_id)] = [
                _Revision(page.page_id, page.step, page.feature, revision=1)
            ]

    def revise_page(
        self, journal_id: str, page_id: str, *, step: int, feature: str
    ) -> int:
        """Append a correction to an existing named page; return the revision.

        The previous reading is retained so any earlier export can be replayed.
        """

        key = (journal_id, page_id)
        if key not in self._revisions:
            raise LacquerError(
                PAGE_NOT_FOUND,
                f"page {journal_id}/{page_id} does not exist",
                details={"journal_id": journal_id, "page_id": page_id},
            )
        _validate_step_feature(step, feature)
        history = self._revisions[key]
        revision = len(history) + 1
        history.append(_Revision(page_id, step, feature, revision))
        return revision

    def revision_of(self, journal_id: str, page_id: str) -> int:
        history = self._revisions.get((journal_id, page_id))
        if history is None:
            raise LacquerError(
                PAGE_NOT_FOUND,
                f"page {journal_id}/{page_id} does not exist",
                details={"journal_id": journal_id, "page_id": page_id},
            )
        return len(history)

    def journal(self, journal_id: str) -> Journal:
        try:
            return self._journals[journal_id]
        except KeyError:
            raise LacquerError(
                JOURNAL_NOT_FOUND,
                f"journal {journal_id!r} does not exist",
                details={"journal_id": journal_id},
            ) from None

    def journal_ids(self) -> list[str]:
        return sorted(self._journals)

    # ------------------------------------------------------------------
    # replay / reconstruction
    # ------------------------------------------------------------------
    def pages_at(
        self, journal_id: str, *, revision_pins: dict[str, int] | None = None
    ) -> tuple[Page, ...]:
        """Return the journal's pages, optionally pinned to old revisions.

        ``revision_pins`` maps a page_id to the revision to read; unpinned
        pages read their latest revision.
        """

        journal = self.journal(journal_id)
        pins = revision_pins or {}
        pages: list[Page] = []
        for page in journal.pages:
            history = self._revisions[(journal_id, page.page_id)]
            wanted = pins.get(page.page_id, len(history))
            if wanted < 1 or wanted > len(history):
                raise LacquerError(
                    PAGE_NOT_FOUND,
                    f"revision {wanted} of {journal_id}/{page.page_id} does not exist",
                    details={
                        "journal_id": journal_id,
                        "page_id": page.page_id,
                        "available_revisions": len(history),
                    },
                )
            rev = history[wanted - 1]
            pages.append(Page(rev.page_id, rev.step, rev.feature))
        return tuple(pages)

    def reconstruct(
        self,
        journal_id: str,
        *,
        usable_pages: Iterable[str] | None = None,
        revision_pins: dict[str, int] | None = None,
    ) -> Reconstruction:
        """Reconstruct every still-possible rim reading of one journal."""

        journal = self.journal(journal_id)
        pages = self.pages_at(journal_id, revision_pins=revision_pins)

        if usable_pages is not None:
            allowed = set(usable_pages)
            pages = tuple(p for p in pages if p.page_id in allowed)
            if not pages:
                return Reconstruction(
                    journal_id=journal_id,
                    divisions=journal.divisions,
                    conflicts=(NO_USABLE_PAGES,),
                    rings=(),
                    origins=(),
                    sampled_pages=(),
                )

        conflicts = _check_pages(journal.divisions, pages)
        sampled = tuple(p.page_id for p in pages)
        if conflicts:
            return Reconstruction(
                journal_id=journal_id,
                divisions=journal.divisions,
                conflicts=tuple(conflicts),
                rings=(),
                origins=(),
                sampled_pages=sampled,
            )

        # Local frame: absolute cell = (origin + cumulative walk) mod n.
        n = journal.divisions
        rings: list[Ring] = []
        for origin in journal.origin_candidates:
            lugs: list[int] = []
            notches: list[int] = []
            if journal.reference_feature == LUG:
                lugs.append(origin % n)
            elif journal.reference_feature == NOTCH:
                notches.append(origin % n)
            position = 0
            for page in pages:
                position = (position + page.step) % n
                cell = (origin + position) % n
                if page.feature == KIND_LUG:
                    lugs.append(cell)
                else:
                    notches.append(cell)
            rings.append(
                ring_from_features(
                    n,
                    lugs=sorted(set(lugs)),
                    notches=sorted(set(notches)),
                    name=journal_id,
                )
            )
        return Reconstruction(
            journal_id=journal_id,
            divisions=n,
            conflicts=(),
            rings=tuple(rings),
            origins=tuple(origin % n for origin in journal.origin_candidates),
            sampled_pages=sampled,
        )

    def reconstruct_vessel(
        self,
        item_id: str,
        *,
        bottom_journal: str,
        top_journal: str,
        usable_pages_by_journal: dict[str, Iterable[str]] | None = None,
        revision_pins_by_journal: dict[str, dict[str, int]] | None = None,
    ) -> tuple[list[VesselConfiguration], list[Reconstruction]]:
        """Pair the two rims of one vessel over the shared worn-base choice."""

        usable = usable_pages_by_journal or {}
        pins = revision_pins_by_journal or {}
        bottom = self.reconstruct(
            bottom_journal,
            usable_pages=usable.get(bottom_journal),
            revision_pins=pins.get(bottom_journal),
        )
        top = self.reconstruct(
            top_journal,
            usable_pages=usable.get(top_journal),
            revision_pins=pins.get(top_journal),
        )
        reports = [bottom, top]
        if bottom.conflicts or top.conflicts:
            return [], reports

        bottom_j = self.journal(bottom_journal)
        top_j = self.journal(top_journal)
        if bottom_j.origin_candidates != top_j.origin_candidates:
            raise LacquerError(
                INVALID_NOTEBOOK,
                f"vessel {item_id}: top and bottom rims must share the worn "
                f"base candidate list",
                details={
                    "bottom_origins": list(bottom_j.origin_candidates),
                    "top_origins": list(top_j.origin_candidates),
                },
            )

        configurations: list[VesselConfiguration] = []
        for index, origin in enumerate(bottom.origins):
            configurations.append(
                VesselConfiguration(
                    item_id=item_id,
                    bottom=bottom.rings[index],
                    top=top.rings[index],
                    origin=origin,
                    based_on=f"{bottom_journal}+{top_journal}",
                )
            )
        return configurations, reports

    # ------------------------------------------------------------------
    # JSON I/O
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "JournalBook":
        if document.get("format") != "gauge-notebook/1":
            raise LacquerError(
                INVALID_NOTEBOOK,
                "unsupported notebook format",
                details={"format": document.get("format")},
            )
        book = cls()
        for entry in document.get("journals", []):
            pages = tuple(
                Page(
                    str(p["page_id"]),
                    int(p["step"]),
                    str(p["feature"]),
                )
                for p in entry.get("pages", [])
            )
            origins = entry.get("origin_candidates", [0])
            if not isinstance(origins, list) or not origins:
                raise LacquerError(
                    INVALID_NOTEBOOK,
                    "origin_candidates must be a non-empty list",
                    details={"journal_id": entry.get("journal_id")},
                )
            journal = Journal(
                journal_id=str(entry["journal_id"]),
                divisions=int(entry["divisions"]),
                reference_feature=_optional_feature(entry.get("reference_feature")),
                pages=pages,
                origin_candidates=tuple(int(o) for o in origins),
            )
            book.add_journal(journal)
        return book

    @classmethod
    def from_file(cls, path: str | Path) -> "JournalBook":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def _optional_feature(value: Any) -> str | None:
    if value is None:
        return None
    if value not in (KIND_LUG, KIND_NOTCH):
        raise LacquerError(
            UNKNOWN_FEATURE,
            f"reference feature {value!r} is not lug/notch",
            details={"feature": value},
        )
    return FEATURE_BY_KIND[value]


def _validate_journal_shape(journal: Journal) -> None:
    if not isinstance(journal.journal_id, str) or not journal.journal_id:
        raise LacquerError(INVALID_NOTEBOOK, "journal_id must be a non-empty string")
    if journal.divisions < 1:
        raise LacquerError(
            INVALID_NOTEBOOK,
            "divisions must be >= 1",
            details={"journal_id": journal.journal_id},
        )
    for origin in journal.origin_candidates:
        if not 0 <= origin < journal.divisions:
            raise LacquerError(
                INVALID_NOTEBOOK,
                "origin candidate out of range",
                details={"journal_id": journal.journal_id, "origin": origin},
            )
    for page in journal.pages:
        _validate_step_feature(page.step, page.feature)


def _validate_step_feature(step: int, feature: str) -> None:
    if not isinstance(step, int) or step < 1:
        raise LacquerError(
            INVALID_NOTEBOOK, f"step must be a positive integer, got {step!r}"
        )
    if feature not in (KIND_LUG, KIND_NOTCH):
        raise LacquerError(
            UNKNOWN_FEATURE, f"feature must be lug/notch, got {feature!r}"
        )


def _check_pages(divisions: int, pages: Sequence[Page]) -> list[str]:
    """Validate the closed walk and the one-mark-per-cell rule.

    Each page lands on one cell; the same cell may not be claimed twice by
    two different pages (the walk would describe two marks in one place).
    """

    conflicts: list[str] = []
    total = sum(p.step for p in pages)
    if total != divisions:
        conflicts.append(CLOSURE_FAILURE)

    seen: set[int] = set()
    position = 0
    for page in pages:
        position = (position + page.step) % divisions
        if position in seen:
            conflicts.append(CELL_COLLISION)
        seen.add(position)
    # Stable, de-duplicated conflict list.
    return sorted(set(conflicts))
