"""Core data model for the banquet reconstruction.

Vocabulary (aligned with the task spec):

* **guest** -- a named participant with the languages they can hear/speak.
* **seat chart** -- an assignment of guests to chairs around a round table.
  ``seats[0]`` is the head seat (主席座); chairs are read from the head's left
  in a fixed direction, so ``seats[i]`` is the chair "i steps to the left".
  Adjacency is cyclic (the last chair neighbours the head chair).
* **clause / ordinance page** -- one ritual instruction supplied by a page of
  the RANGE citation.  A page can fix *order*, require an *adjacency*, or
  declare a *substitute* (who either inherits the original's ritual order or
  merely borrows their chair).
* **revocation** -- a later page that cancels exactly one clause on the stable
  page it points at.
* **conversation** -- a private talk between two guests in one round, in a
  stated language, relayed only along adjacent chairs.
* **round** -- a group of simultaneous conversations.  A round may be *empty*
  (used to install a sound screen) or marked so that unscreened neighbours
  also overhear (顺带听闻 / incidental hearing).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# People and languages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guest:
    guest_id: str
    languages: frozenset[str] = frozenset()
    display_name: str | None = None

    def knows(self, language: str) -> bool:
        return language in self.languages

    @property
    def name(self) -> str:
        return self.display_name or self.guest_id


def guest_index(guests: Iterable[Guest]) -> dict[str, Guest]:
    index: dict[str, Guest] = {}
    for g in guests:
        if g.guest_id in index:
            raise ValueError(f"duplicate guest_id {g.guest_id!r}")
        index[g.guest_id] = g
    return index


# ---------------------------------------------------------------------------
# Seating
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatChart:
    """A round table seating.

    ``seats[0]`` is the head seat.  ``seats[i]`` is reached from the head by
    moving ``i`` chairs in the fixed direction (the head's left).  Adjacency is
    cyclic: seat ``i`` is adjacent to ``(i-1) % n`` and ``(i+1) % n``.

    A chair may be empty (``None``).  An empty chair is a physical gap: two
    occupied chairs with an empty chair between them are *not* adjacent, so a
    relayed talk cannot cross the gap and incidental sound dies in it.  This is
    how a spare chair lets an interpreter be moved out of earshot.
    """

    seats: tuple[str | None, ...]

    def __post_init__(self) -> None:
        occupied = [s for s in self.seats if s is not None]
        if len(set(occupied)) != len(occupied):
            raise ValueError("a seat chart places each guest exactly once")

    @property
    def size(self) -> int:
        return len(self.seats)

    @property
    def guests(self) -> tuple[str, ...]:
        return tuple(s for s in self.seats if s is not None)

    def positions(self) -> dict[str, int]:
        return {g: i for i, g in enumerate(self.seats) if g is not None}

    def position(self, guest_id: str) -> int:
        if guest_id not in self.seats:
            raise KeyError(guest_id)
        return self.seats.index(guest_id)

    def neighbours(self, guest_id: str) -> tuple[str, ...]:
        """Occupied chairs immediately adjacent to ``guest_id`` (0..2)."""

        i = self.position(guest_id)
        n = self.size
        out: list[str] = []
        for j in ((i - 1) % n, (i + 1) % n):
            if self.seats[j] is not None:
                out.append(self.seats[j])  # type: ignore[arg-type]
        return tuple(out)

    def adjacent(self, a: str, b: str) -> bool:
        if a == b:
            return False
        n = self.size
        pa, pb = self.position(a), self.position(b)
        return (pa - pb) % n in (1, n - 1)

    def edge(self, a: str, b: str) -> "Edge":
        return Edge(*sorted((a, b)))

    def edges(self) -> frozenset["Edge"]:
        """Every seam between two *immediately adjacent occupied* chairs."""

        out: set[Edge] = set()
        n = self.size
        for i in range(n):
            a = self.seats[i]
            b = self.seats[(i + 1) % n]
            if a is not None and b is not None:
                out.add(Edge(*sorted((a, b))))
        return frozenset(out)

    def as_dict(self) -> dict:
        return {"seats": list(self.seats)}


@dataclass(frozen=True, order=True)
class Edge:
    """An undirected chair-to-chair seam, stable regardless of reading order.

    The two ends are stored in sorted order so ``Edge("戊", "丁")`` and
    ``Edge("丁", "戊")`` are the same seam, however they were written.
    """

    a: str
    b: str

    def __post_init__(self) -> None:
        if self.b > self.a:
            lo, hi = self.b, self.a
            object.__setattr__(self, "a", lo)
            object.__setattr__(self, "b", hi)


# ---------------------------------------------------------------------------
# Ordinance clauses (one per RANGE page)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderClause:
    """Fix the ritual precedence of named guests (and optionally the head)."""

    page_id: str
    sequence: tuple[str, ...]
    head: str | None = None


@dataclass(frozen=True)
class AdjacencyClause:
    """Require named pairs to sit next to each other."""

    page_id: str
    pairs: tuple[Edge, ...]


@dataclass(frozen=True)
class SubstituteClause:
    """A substitute replaces an original.

    ``mode`` is either ``"inherit_order"`` (the substitute inherits the
    original's ritual precedence, taking part in order/adjacency constraints
    that name the original) or ``"borrow_chair_only"`` (the substitute merely
    occupies the chair the original would have occupied and inherits none of
    the original's relations).
    """

    page_id: str
    original: str
    substitute: str
    mode: str = "inherit_order"

    def __post_init__(self) -> None:
        if self.mode not in ("inherit_order", "borrow_chair_only"):
            raise ValueError(f"unknown substitute mode {self.mode!r}")


@dataclass(frozen=True)
class RevocationClause:
    """Cancel exactly one clause on the stable page it points at."""

    page_id: str
    target_page_id: str


Clause = OrderClause | AdjacencyClause | SubstituteClause | RevocationClause


@dataclass(frozen=True)
class PageOrdinance:
    page_id: str
    clause: Clause


# ---------------------------------------------------------------------------
# Conversations, rounds, clue groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conversation:
    conv_id: str
    speaker_a: str
    speaker_b: str
    language: str
    secret: str
    authorized: frozenset[str] = frozenset()
    incidental: bool = False

    def involves(self, guest_id: str) -> bool:
        return guest_id in (self.speaker_a, self.speaker_b)


@dataclass(frozen=True)
class Round:
    round_id: str
    conversations: tuple[Conversation, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.conversations

    @property
    def is_incidental(self) -> bool:
        return any(c.incidental for c in self.conversations)


@dataclass(frozen=True)
class SecretGroup:
    """A set of secret labels that must not be fully gathered by one actor."""

    group_id: str
    secrets: frozenset[str]


# ---------------------------------------------------------------------------
# Normalised ordinance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrdinanceSpec:
    """The effective constraints after applying clause order and revocations."""

    precedence: tuple[tuple[str, str], ...] = ()
    head: str | None = None
    adjacency: tuple[Edge, ...] = ()
    inherit_map: Mapping[str, str] = field(default_factory=dict)
    chair_borrows: Mapping[str, str] = field(default_factory=dict)
    applied_pages: tuple[str, ...] = ()
    revoked_pages: tuple[str, ...] = ()

    def allows(self, chart: SeatChart) -> tuple[bool, list[str]]:
        """Return ``(ok, reasons)`` for a candidate chart."""

        reasons: list[str] = []
        pos = chart.positions()

        if self.head is not None and (not chart.seats or chart.seats[0] != self.head):
            reasons.append(f"head:{self.head}")

        for a, b in self.precedence:
            if a not in pos or b not in pos:
                reasons.append(f"missing:{a if a not in pos else b}")
                continue
            if pos[a] >= pos[b]:
                reasons.append(f"precedence:{a}<{b}")

        for edge in self.adjacency:
            if edge.a not in pos or edge.b not in pos:
                reasons.append(f"missing:{edge.a if edge.a not in pos else edge.b}")
                continue
            if not chart.adjacent(edge.a, edge.b):
                reasons.append(f"adjacency:{edge.a}-{edge.b}")

        # A "borrow the chair only" substitute takes the original's seat and
        # inherits none of the original's relations; the original must be gone.
        for original, substitute in self.chair_borrows.items():
            if substitute not in pos:
                reasons.append(f"missing:{substitute}")
            if original in pos:
                reasons.append(f"borrow_chair:{original}_still_seated")

        return (not reasons, reasons)


def build_ordinance(pages: Sequence[PageOrdinance]) -> OrdinanceSpec:
    """Apply RANGE pages in order, honouring single-clause revocations.

    A later ``RevocationClause`` removes exactly one already-effective clause
    on the stable page it targets; a page is effective only while it has not
    been revoked.  The resulting precedence/adjacency/substitution constraints
    are folded into one ``OrdinanceSpec``.

    Substitution is folded in before precedence/adjacency are built:

    * ``inherit_order`` renames the original to the substitute everywhere, so
      the substitute inherits the original's ritual precedence and adjacency;
    * ``borrow_chair_only`` leaves relations untouched and records that the
      substitute must be present while the original is out of the chair.
    """

    effective: dict[str, Clause] = {}          # page_id -> live clause

    for page in pages:
        clause = page.clause
        if isinstance(clause, RevocationClause):
            effective.pop(clause.target_page_id, None)
            continue
        if page.page_id in effective:
            raise ValueError(f"page {page.page_id!r} already carries a clause")
        effective[page.page_id] = clause

    inherit: dict[str, str] = {}
    borrows: dict[str, str] = {}
    for clause in effective.values():
        if isinstance(clause, SubstituteClause):
            if clause.mode == "inherit_order":
                inherit[clause.original] = clause.substitute
            else:
                borrows[clause.original] = clause.substitute

    def rn(name: str) -> str:
        return inherit.get(name, name)

    precedence: list[tuple[str, str]] = []
    adjacency: list[Edge] = []
    head: str | None = None

    for clause in effective.values():
        if isinstance(clause, OrderClause):
            seq = tuple(rn(g) for g in clause.sequence)
            for a, b in zip(seq, seq[1:]):
                precedence.append((a, b))
            if clause.head is not None:
                head = rn(clause.head)
        elif isinstance(clause, AdjacencyClause):
            adjacency.extend(Edge(rn(e.a), rn(e.b)) for e in clause.pairs)

    return OrdinanceSpec(
        precedence=tuple(dict.fromkeys(precedence)),
        head=head,
        adjacency=tuple(dict.fromkeys(adjacency)),
        inherit_map=dict(inherit),
        chair_borrows=dict(borrows),
        applied_pages=tuple(effective.keys()),
        revoked_pages=tuple(
            p.clause.target_page_id
            for p in pages
            if isinstance(p.clause, RevocationClause)
        ),
    )


# ---------------------------------------------------------------------------
# Enumerating compliant charts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartEnumeration:
    charts: tuple[SeatChart, ...]
    basis: tuple[str, ...]                # why there are none (empty if any)
    tested: int

    @property
    def feasible(self) -> bool:
        return bool(self.charts)


def enumerate_compliant_charts(
    spec: OrdinanceSpec,
    attendees: Iterable[str],
    *,
    max_results: int = 2000,
) -> ChartEnumeration:
    """All seat charts that place every attendee once and satisfy the ordinance.

    ``attendees`` is the sign-in roster (the PINNED citation).  Chairs equal to
    heads-of-order are fixed by the ordinance; remaining attendees may permute
    freely, which is what yields *several* compliant charts in general.  With
    at most eight people this exhaustive search is trivial.
    """

    roster = tuple(dict.fromkeys(attendees))  # stable, de-duplicated
    tested = 0
    found: list[SeatChart] = []
    reasons: set[str] = set()

    for perm in itertools.permutations(roster):
        tested += 1
        chart = SeatChart(tuple(perm))
        ok, why = spec.allows(chart)
        if ok:
            found.append(chart)
            if len(found) >= max_results:
                break
        else:
            reasons.update(why)

    if found:
        return ChartEnumeration(tuple(found), (), tested)
    if not roster:
        reasons.add("no_attendees")
    basis = tuple(sorted(reasons))
    return ChartEnumeration((), basis, tested)
