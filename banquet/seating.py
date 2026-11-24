"""Seating: from citation products to compliant charts (or a suspension).

The ceremonial officer must, for the guests who have actually arrived, list
*all* compliant seat charts, or a reviewable basis for seating nobody.  A
session with missing citations is suspended -- we never improvise a table from
"whatever pages still exist".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import (
    ChartEnumeration,
    Guest,
    OrdinanceSpec,
    PageOrdinance,
    SeatChart,
    build_ordinance,
    enumerate_compliant_charts,
    guest_index,
)
from .products import ProductSet


@dataclass(frozen=True)
class SeatingResult:
    status: str                      # "seated" | "no_seating" | "suspended"
    charts: tuple[SeatChart, ...] = ()
    basis: tuple[str, ...] = ()
    suspended: tuple[str, ...] = ()
    ordinance: OrdinanceSpec | None = None
    tested: int = 0
    catalog_revision: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "seated"

    def as_dict(self) -> dict:
        out: dict = {
            "status": self.status,
            "catalog_revision": self.catalog_revision,
            "chart_count": len(self.charts),
            "charts": [c.as_dict() for c in self.charts],
            "basis": list(self.basis),
        }
        if self.suspended:
            out["suspended"] = list(self.suspended)
        if self.ordinance is not None:
            out["applied_pages"] = list(self.ordinance.applied_pages)
            out["revoked_pages"] = list(self.ordinance.revoked_pages)
        out["tested_permutations"] = self.tested
        return out


def attendance_from_products(
    products: ProductSet,
    *,
    identity_of: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(attendees, problems)`` from usable PINNED citations.

    A PINNED citation is a sign-in sheet: its pages are the people who came.
    Because a page identity is ``(volume_id, page_id)``, we key guests by that
    full identity so two volumes never collide -- unless ``identity_of`` maps
    a page key (``"V1/p3"``) to a guest id already used by the ordinance and
    conversation model (e.g. ``"丙"``).
    """

    identity_of = identity_of or {}
    attendees: list[str] = []
    problems: list[str] = []
    for row in products.usable_pinned():
        for volume_id, page_id in row.page_keys:
            key = f"{volume_id}/{page_id}"
            guest_id = identity_of.get(key, key)
            if guest_id not in attendees:
                attendees.append(guest_id)
    for row in products.suspended_pinned():
        problems.append(
            f"pinned_needs_attention:{row.citation_id}:{row.raw['reason']}"
        )
    return attendees, problems


def ordinance_from_pages(
    pages: Sequence[PageOrdinance],
) -> OrdinanceSpec:
    return build_ordinance(pages)


def seat_session(
    products: ProductSet,
    pages: Sequence[PageOrdinance] | None = None,
    *,
    guests: Iterable[Guest] | None = None,
    identity_of: Mapping[str, str] | None = None,
    max_results: int = 2000,
) -> SeatingResult:
    """Build all compliant charts for the signed-in guests, or suspend.

    ``pages`` are the ordinance pages decoded from the usable RANGE citation.
    If the products are incomplete/unusable the result is ``suspended`` with
    the reasons, and no chart is produced.
    """

    reasons = products.suspend_reasons()
    if reasons:
        return SeatingResult(
            status="suspended",
            suspended=tuple(reasons),
            catalog_revision=(
                products.single_revision() if len(products.revisions) == 1 else None
            ),
        )

    attendees, problems = attendance_from_products(products, identity_of=identity_of)
    if problems:
        return SeatingResult(
            status="suspended", suspended=tuple(problems),
            catalog_revision=products.single_revision(),
        )

    if guests is not None:
        idx = guest_index(guests)
        missing = [a for a in attendees if a not in idx]
        if missing:
            # Unknown attendees are themselves a suspension basis, not silent.
            return SeatingResult(
                status="suspended",
                suspended=tuple(f"attendee_not_described:{m}" for m in missing),
                catalog_revision=products.single_revision(),
            )

    spec = build_ordinance(pages or ())
    enumeration: ChartEnumeration = enumerate_compliant_charts(
        spec, attendees, max_results=max_results
    )
    if enumeration.feasible:
        return SeatingResult(
            status="seated",
            charts=enumeration.charts,
            ordinance=spec,
            tested=enumeration.tested,
            catalog_revision=products.single_revision(),
        )
    return SeatingResult(
        status="no_seating",
        basis=enumeration.basis,
        ordinance=spec,
        tested=enumeration.tested,
        catalog_revision=products.single_revision(),
    )
