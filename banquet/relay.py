"""Relaying a private conversation along adjacent chairs.

Rules encoded here (from the task spec):

* talk only travels between *adjacent* chairs around the round table;
* every person *along the way* (every intermediate) must be an interpreter
  authorised to hear that talk;
* each consecutive pair of chairs must share at least one common language;
  that shared language is reported per segment;
* within one round, an interpreter may carry at most one talk-path.

Charts are small (``<= 8`` chairs), so we enumerate the (at most two) simple
paths between two speakers and, for a whole round, pick one valid route per
conversation with pairwise-disjoint interpreter sets.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Mapping

from .model import Conversation, Edge, Guest, Round, SeatChart


@dataclass(frozen=True)
class Segment:
    frm: str
    to: str
    language: str
    interpreter: str | None      # internal chair that bridges this step, if any

    def as_dict(self) -> dict:
        return {
            "from": self.frm,
            "to": self.to,
            "language": self.language,
            "interpreter": self.interpreter,
        }


@dataclass(frozen=True)
class Route:
    conv_id: str
    path: tuple[str, ...]
    segments: tuple[Segment, ...]

    @property
    def interpreters(self) -> frozenset[str]:
        """Internal chairs that actually handle a segment (no endpoints)."""

        endpoints = {self.path[0], self.path[-1]} if len(self.path) >= 2 else set()
        return frozenset(p for p in self.path if p not in endpoints)

    @property
    def participants(self) -> frozenset[str]:
        return frozenset(self.path)

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(s.language for s in self.segments)

    def as_dict(self) -> dict:
        return {
            "conv_id": self.conv_id,
            "path": list(self.path),
            "segments": [s.as_dict() for s in self.segments],
            "interpreters": sorted(self.interpreters),
        }


@dataclass(frozen=True)
class RouteFailure:
    conv_id: str
    reason: str


@dataclass(frozen=True)
class RoundPlan:
    round_id: str
    status: str                              # "ok" | "infeasible"
    routes: tuple[Route, ...] = ()
    failures: tuple[RouteFailure, ...] = ()
    blocked_edges: tuple[Edge, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "status": self.status,
            "routes": [r.as_dict() for r in self.routes],
            "failures": [
                {"conv_id": f.conv_id, "reason": f.reason} for f in self.failures
            ],
            "blocked_edges": [list(e.__dict__.values()) for e in self.blocked_edges],
        }


def _shared_language(a: Guest, b: Guest) -> str | None:
    """A common language two adjacent chairs can talk in, or ``None``.

    The interpreter bridges languages: the segment language need only be shared
    by *that* pair.  This is precisely why a red-only guest and a blue-only
    guest can talk through a bilingual interpreter (red on one segment, blue on
    the next).
    """

    common = a.languages & b.languages
    if not common:
        return None
    return sorted(common)[0]


def _simple_paths(chart: SeatChart, a: str, b: str, blocked: frozenset[Edge]) -> list[tuple[str, ...]]:
    """All simple chair-paths from ``a`` to ``b`` avoiding blocked seams.

    A relay travels only chair-to-chair between *occupied* neighbours; an empty
    chair is a gap and cannot be stepped over, and a blocked seam cannot be
    crossed.
    """

    seats = chart.seats
    n = len(seats)
    start, goal = chart.position(a), chart.position(b)
    results: list[tuple[str, ...]] = []

    def walk(idx: int, visited: list[int]) -> None:
        if idx == goal:
            results.append(tuple(seats[i] for i in visited))  # type: ignore[misc]
            return
        for step in (-1, 1):
            nxt = (idx + step) % n
            if nxt in visited:
                continue
            if seats[nxt] is None:          # empty chair: a gap, not a relay
                continue
            seam = Edge(*sorted((seats[idx], seats[nxt])))  # type: ignore[arg-type]
            if seam in blocked:
                continue
            walk(nxt, visited + [nxt])

    walk(start, [start])
    # De-duplicate identical index cycles (e.g. tiny tables).
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, ...]] = []
    for p in results:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def candidate_routes(
    chart: SeatChart,
    conv: Conversation,
    guests: Mapping[str, Guest],
    *,
    blocked: Iterable[Edge] = (),
) -> list[Route]:
    """Every individually-valid route for one conversation on this chart."""

    blocked_set = frozenset(blocked)
    a, b = conv.speaker_a, conv.speaker_b
    if a not in chart.seats or b not in chart.seats:
        return []
    if a == b:
        return []
    routes: list[Route] = []
    for path in _simple_paths(chart, a, b, blocked_set):
        if len(path) < 2:
            continue
        # Interpreters: any intermediate chair must be authorised.
        internal = path[1:-1]
        if any(p not in conv.authorized for p in internal):
            continue
        segments: list[Segment] = []
        ok = True
        for u, v in zip(path, path[1:]):
            lang = _shared_language(guests[u], guests[v])
            if lang is None:
                ok = False
                break
            if v in internal:
                interpreter = v
            elif u in internal:
                interpreter = u
            else:
                interpreter = None
            segments.append(Segment(u, v, lang, interpreter))
        if not ok:
            continue
        routes.append(Route(conv.conv_id, path, tuple(segments)))
    return routes


def plan_round(
    chart: SeatChart,
    round_spec: Round,
    guests: Mapping[str, Guest],
    *,
    blocked: Iterable[Edge] = (),
) -> RoundPlan:
    """Route every conversation of a round simultaneously.

    A round is feasible iff, for each conversation, one valid route can be
    chosen so that the interpreters used across the whole round are disjoint
    (each interpreter carries at most one path).
    """

    blocked_tuple = tuple(sorted(set(blocked)))
    conversations = round_spec.conversations
    if not conversations:
        return RoundPlan(round_spec.round_id, "ok", (), (), blocked_tuple)

    candidates = [
        candidate_routes(chart, conv, guests, blocked=blocked_tuple)
        for conv in conversations
    ]
    for conv, routes in zip(conversations, candidates):
        if not routes:
            return RoundPlan(
                round_spec.round_id,
                "infeasible",
                (),
                (RouteFailure(conv.conv_id, "no_authorized_relay_path"),),
                blocked_tuple,
            )

    # Search an assignment with disjoint interpreter sets.
    for choice in itertools.product(*candidates):
        used: set[str] = set()
        clash = False
        for route in choice:
            if used & route.interpreters:
                clash = True
                break
            used |= route.interpreters
        if not clash:
            return RoundPlan(
                round_spec.round_id, "ok", tuple(choice), (), blocked_tuple
            )

    # No simultaneous assignment: report the interpreter contention clearly.
    return RoundPlan(
        round_spec.round_id,
        "infeasible",
        (),
        (RouteFailure(
            ",".join(c.conv_id for c in conversations),
            "interpreter_budget_exhausted",
        ),),
        blocked_tuple,
    )
