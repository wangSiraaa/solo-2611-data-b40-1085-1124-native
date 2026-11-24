"""Wind-corridor inference (task 1105, part 1: the "wind notebook").

A *calibration flight* is a traversal of pages.  Each page records the wind
band it sits in and how many ticks the paper ball dwells there ("每页记所在风带
和逗留拍数").  A flight's total drift is::

    drift = sum(dwell * wind[band] for every visited page)

and must match a measured displacement, either exactly (a single ticket) or as
a closed interval ("实测总位移或闭区间").  Winds are per band, integers in
``[-2, 2]``, fixed for a whole session; every flight must agree on the same
band winds ("几次试飞遇到的同层风须一致").  A band may be entered more than
once; only its dwell adds up.

The flights themselves are read straight off the catalog/product:

* a **RANGE** citation is a flight -- the pages that are *currently* inside the
  range (active, endpoints inclusive) are exactly the pages the ball visits.
  Inserting a real page in the middle therefore adds its dwell
  ("插入区间中间的实页会增添逗留时间"), and suspending an endpoint takes the
  whole flight out of service ("端页挂起须指出哪趟试飞暂不能用").
* a **PINNED** citation is a fixed departure/catch ticket -- its member set is
  fixed regardless of later inserts, and carries the measured total
  displacement.

The measured displacement travels on the citation's ``origin`` (the raw
reference record) under ``"displacement"`` so the repository needs no schema
change::

    {"exact": 3}          # measured total displacement is exactly 3
    {"min": 1, "max": 3}  # measured total displacement is in [1, 3]

If the session's *required* calibration sheets are incomplete (a required
flight is missing or temporarily unusable), the drawing is withheld
("本场委托指定的校准单不齐则暂缓发图").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Iterable, Mapping

from .catalog import Catalog, Page
from .repository import PINNED, RANGE, CitationRepository

# Candidate band winds: integers in [-2, 2].
WIND_VALUES: tuple[int, ...] = (-2, -1, 0, 1, 2)


@dataclass(frozen=True)
class Flight:
    """One calibration flight: an ordered list of (band, dwell) visits plus a
    measured displacement (exact, or a closed interval)."""

    name: str
    visits: tuple[tuple[int, int], ...]
    exact: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    usable: bool = True
    source: str = "flight"

    def __post_init__(self) -> None:
        if self.exact is not None and (self.minimum is not None or self.maximum is not None):
            raise ValueError(
                f"flight {self.name!r}: give either exact or an interval, not both"
            )
        if self.exact is None:
            if self.minimum is not None and self.maximum is not None:
                if self.minimum > self.maximum:
                    raise ValueError(
                        f"flight {self.name!r}: minimum exceeds maximum"
                    )

    @property
    def constrained(self) -> bool:
        return (
            self.exact is not None
            or self.minimum is not None
            or self.maximum is not None
        )

    def displacement(self, winds: Mapping[int, int]) -> int:
        return sum(dwell * winds[band] for band, dwell in self.visits)

    def bands(self) -> Iterable[int]:
        for band, _dwell in self.visits:
            yield band

    def satisfied(self, winds: Mapping[int, int]) -> bool:
        if not self.usable:
            return True  # an out-of-service flight constrains nothing
        drift = self.displacement(winds)
        if self.exact is not None:
            return drift == self.exact
        if self.minimum is not None and drift < self.minimum:
            return False
        if self.maximum is not None and drift > self.maximum:
            return False
        return True


@dataclass
class Inference:
    """Outcome of reading a whole wind notebook."""

    bands: int
    solutions: list[dict[int, int]]
    usable_flights: list[str]
    unusable_flights: list[str]
    conflicting: list[str] = field(default_factory=list)
    withheld: bool = False
    withheld_reason: str | None = None

    @property
    def unique(self) -> bool:
        return len(self.solutions) == 1

    @property
    def consistent(self) -> bool:
        return bool(self.solutions)


def _assignment_satisfies(flights: Iterable[Flight], winds: Mapping[int, int]) -> bool:
    return all(f.satisfied(winds) for f in flights)


def _has_solution(flights: list[Flight], bands: int) -> bool:
    return any(
        _assignment_satisfies(flights, dict(enumerate(combo)))
        for combo in product(WIND_VALUES, repeat=bands)
    )


def minimal_conflict(flights: list[Flight], bands: int) -> list[Flight]:
    """A minimal set of (usable, constrained) flights that cannot be satisfied.

    Deletion-based: start from every usable flight and drop one at a time while
    the remainder is still unsatisfiable.  The survivors are pairwise necessary
    to the contradiction, i.e. removing any one of them restores a solution.
    """

    subset = [f for f in flights if f.usable]
    # Flights without any measurement cannot create a contradiction; dropping
    # them first is safe and keeps the reported set meaningful.
    subset = [f for f in subset if f.constrained]
    i = 0
    while i < len(subset):
        trial = subset[:i] + subset[i + 1 :]
        if trial and not _has_solution(trial, bands):
            subset = trial  # this flight is not needed for the contradiction
        else:
            i += 1
    return subset


def infer(
    flights: Iterable[Flight],
    bands: int,
    *,
    required: Iterable[str] | None = None,
) -> Inference:
    """Enumerate every band-wind assignment in ``[-2, 2]`` consistent with all
    usable flights, or report the flights that conflict.

    ``required`` names the calibration sheets this session *must* have.  If a
    required sheet is missing or unusable, the inference is marked withheld
    (the drawing is not released).
    """

    flights = list(flights)
    if bands < 1:
        raise ValueError("bands must be at least 1")
    known_bands = {b for f in flights for b in f.bands()}
    if known_bands and (min(known_bands) < 0 or max(known_bands) >= bands):
        raise ValueError(
            f"flight references band outside 0..{bands - 1}: {sorted(known_bands)}"
        )

    usable = [f for f in flights if f.usable]
    unusable = [f for f in flights if not f.usable]

    solutions = [
        dict(enumerate(combo))
        for combo in product(WIND_VALUES, repeat=bands)
        if _assignment_satisfies(usable, dict(enumerate(combo)))
    ]

    result = Inference(
        bands=bands,
        solutions=solutions,
        usable_flights=[f.name for f in usable],
        unusable_flights=[f.name for f in unusable],
    )

    if not solutions:
        result.conflicting = [f.name for f in minimal_conflict(flights, bands)]

    if required is not None:
        required_set = list(required)
        usable_names = {f.name for f in usable}
        missing = [name for name in required_set if name not in usable_names]
        if missing:
            result.withheld = True
            result.withheld_reason = (
                "required calibration sheet(s) missing or unusable: "
                + ", ".join(missing)
            )

    return result


# ----------------------------------------------------------------------
# Building flights from the catalog / repository
# ----------------------------------------------------------------------
def _band_lookup(
    band_map: Mapping[Any, tuple[int, int]],
) -> Callable[[str, str], tuple[int, int]]:
    """Accept ``{(volume, page): (band, dwell)}`` or ``{"volume/page": ...}``."""

    def lookup(volume_id: str, page_id: str) -> tuple[int, int]:
        for key in ((volume_id, page_id), f"{volume_id}/{page_id}"):
            if key in band_map:
                band, dwell = band_map[key]  # type: ignore[index]
                if not isinstance(dwell, int) or dwell < 0:
                    raise ValueError(
                        f"page {volume_id}/{page_id}: dwell must be a non-negative int"
                    )
                return int(band), int(dwell)
        raise KeyError(
            f"no band/dwell recorded for page {volume_id}/{page_id}"
        )

    return lookup


def _displacement_from_origin(origin: dict[str, Any] | None) -> dict[str, int]:
    if not origin:
        return {}
    disp = origin.get("displacement")
    if not isinstance(disp, dict):
        return {}
    out: dict[str, int] = {}
    if "exact" in disp:
        out["exact"] = int(disp["exact"])
    if "min" in disp:
        out["minimum"] = int(disp["min"])
    if "lo" in disp:
        out["minimum"] = int(disp["lo"])
    if "max" in disp:
        out["maximum"] = int(disp["max"])
    if "hi" in disp:
        out["maximum"] = int(disp["hi"])
    return out


def _visits(pages: Iterable[Page], lookup: Callable[[str, str], tuple[int, int]]):
    return tuple(lookup(p.volume_id, p.page_id) for p in pages)


def flights_from_catalog(
    catalog: Catalog,
    repo: CitationRepository,
    band_map: Mapping[Any, tuple[int, int]],
    *,
    displacements: Mapping[str, dict[str, int]] | None = None,
) -> list[Flight]:
    """Read every citation as a flight.

    RANGE flights use the *current* active pages between the endpoints, so an
    interior insertion genuinely changes the flight's dwell.  A RANGE whose
    endpoint is inactive or reversed, or a PINNED with an inactive member, is
    still reported but marked ``usable=False`` (the flight is temporarily
    unusable).
    """

    lookup = _band_lookup(band_map)
    displacements = displacements or {}
    flights: list[Flight] = []

    for citation in repo.all():
        cid = citation.citation_id
        origin = repo.get(cid).origin or {}
        disp = dict(displacements.get(cid, {}))
        disp.update(_displacement_from_origin(origin))

        if citation.kind == RANGE:
            start, end = repo.range_endpoints(cid)
            usable = start.active and end.active and start.seq <= end.seq
            if usable:
                pages = catalog.ordered_range(
                    citation.volume_id, start.page_id, end.page_id,
                    include_inactive=False,
                )
            else:
                # Keep the physical span for the record even when unusable.
                pages = catalog.ordered_range(
                    citation.volume_id, start.page_id, end.page_id,
                    include_inactive=True,
                )
            flights.append(
                Flight(
                    name=cid,
                    visits=_visits(pages, lookup),
                    usable=usable,
                    source=RANGE,
                    **disp,
                )
            )
        elif citation.kind == PINNED:
            members = repo.pinned_members(cid)
            usable = all(p.active for p in members)
            flights.append(
                Flight(
                    name=cid,
                    visits=_visits(members, lookup),
                    usable=usable,
                    source=PINNED,
                    **disp,
                )
            )
        else:  # pragma: no cover - schema constrains this
            raise ValueError(f"unknown citation kind {citation.kind!r}")
    return flights
