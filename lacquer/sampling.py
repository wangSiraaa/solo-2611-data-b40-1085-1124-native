"""Sample rim pages only from usable segments of an old citation export.

The sampling room must never invent readings: a rim is reconstructed only from
pages that belong to usable segments of a previously exported
``citation-product/1`` product.  A usable RANGE contributes its current ordered
pages; a usable PINNED contributes its fixed members.  Needs-attention rows and
pages absent from every usable segment are simply not sampleable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import LacquerError
from .notebook import NO_USABLE_PAGES, JournalBook, Reconstruction

UNSUPPORTED_PRODUCT = "unsupported_product"


def read_product_rows(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("format") != "citation-product/1":
                raise LacquerError(
                    UNSUPPORTED_PRODUCT,
                    f"line {line_number}: not a citation-product/1 row",
                    details={"format": row.get("format")},
                )
            if row.get("status") != "usable":
                continue
            rows.append(row)
    return rows


def usable_page_segments(
    usable_paths: Iterable[str | Path],
    *,
    volume_id: str,
) -> dict[str, list[str]]:
    """Map citation_id -> ordered usable page_ids inside one volume."""

    segments: dict[str, list[str]] = {}
    for path in usable_paths:
        for row in read_product_rows(path):
            source = row.get("source", {})
            if source.get("volume_id") != volume_id:
                continue
            page_ids = [
                page["page_id"]
                for page in row.get("pages", [])
                if page.get("volume_id") == volume_id and page.get("active")
            ]
            if page_ids:
                segments[row["citation_id"]] = page_ids
    return segments


def usable_pages_union(
    usable_paths: Iterable[str | Path],
    *,
    volume_id: str,
) -> set[str]:
    """Every page covered by at least one usable segment of the volume."""

    covered: set[str] = set()
    for page_ids in usable_page_segments(usable_paths, volume_id=volume_id).values():
        covered.update(page_ids)
    return covered


def reconstruct_from_export(
    book: JournalBook,
    journal_id: str,
    usable_paths: Iterable[str | Path],
    *,
    volume_id: str,
    revision_pins: dict[str, int] | None = None,
) -> Reconstruction:
    """Reconstruct a journal using only usable export pages.

    A journal with no page inside a usable segment is reported as
    ``no_usable_pages`` and yields no rim rather than being guessed.
    """

    allowed = usable_pages_union(usable_paths, volume_id=volume_id)
    journal_pages = {page.page_id for page in book.journal(journal_id).pages}
    if not (allowed & journal_pages):
        return Reconstruction(
            journal_id=journal_id,
            divisions=book.journal(journal_id).divisions,
            conflicts=(NO_USABLE_PAGES,),
            rings=(),
            origins=(),
            sampled_pages=(),
        )
    return book.reconstruct(
        journal_id,
        usable_pages=allowed,
        revision_pins=revision_pins,
    )
