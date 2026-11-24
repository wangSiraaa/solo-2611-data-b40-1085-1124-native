"""Docking-script planner (task 1105, part 2: the "exhibition stage").

A *performance* runs on a small one-dimensional stack of wind bands (layers).
Before each tick the ball may move to an adjacent layer or stay; the tick then
drifts it by that layer's wind::

    x <- x + wind[new_layer]

At agreed tick-ends the ball must line up with a designated station, or with an
approved backup station; the last tick is caught on the ball rack.

Two things make this interesting:

* **Partial observation.** The operator sees the true horizontal coordinate only
  at tick-ends that carry a score line; between score lines they know only the
  commands already issued.  The wind may therefore still be uncertain when a
  decision is made, and a good controller must branch on what the score lines
  reveal.
* **Devices.** A *fixed guide rail* is installed before the show; every
  transition time and direction is frozen, it never costs a token, and it cannot
  react to a score line.  A *movable push-rod* is held at one stage at a time,
  moves layers on what is actually seen, and each move spends that stage's one
  token; handing it between stages takes a given number of ticks during which
  neither stage can use it.

The two worked examples in the task reduce to a single distinction:

* with a **known** wind, a rail can schedule the needed transition and succeed;
* with **two candidate** winds that only a score line can tell apart, any fixed
  rail schedule fulfils at most one candidate, while a push-rod delivered in
  time branches on the observation and fulfils both.

This module models exactly that: a :class:`Stage` carries a set of candidate
wind assignments, and :func:`rail_feasible` asks for one frozen transition
schedule good for *every* candidate, whereas :func:`rod_feasible` asks for a
reactive policy that may depend on score-line observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Sequence

DELTAS = (-1, 0, 1)  # down, stay, up


@dataclass(frozen=True)
class Stage:
    """One stage's script.

    ``candidates`` is the uncertainty set: each entry is a full per-layer wind
    list ``(w0, w1, ...)``.  A *known* wind is a one-element set.  ``stations``
    is per tick a tuple of acceptable x values (the designated station plus any
    approved backups), or ``None`` for "anywhere" (e.g. the ball rack).
    ``score_lines`` are tick indices whose end reveals the true x.
    ``move_domains`` optionally restricts the layer delta allowed on a tick
    (needed when the script *requires* travelling a particular layer).
    """

    name: str
    layers: int
    candidates: tuple[tuple[int, ...], ...]
    start_layer: int
    start_x: int
    stations: tuple[tuple[int, ...] | None, ...]
    score_lines: frozenset[int] = frozenset()
    tokens: int = 0
    move_domains: tuple[tuple[int, ...], ...] | None = None

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError(f"stage {self.name!r}: at least one wind candidate")
        for c in self.candidates:
            if len(c) != self.layers:
                raise ValueError(
                    f"stage {self.name!r}: candidate has wrong length {len(c)}"
                )
        if not 0 <= self.start_layer < self.layers:
            raise ValueError(f"stage {self.name!r}: start_layer out of range")
        if self.move_domains is not None and len(self.move_domains) != len(self.stations):
            raise ValueError(
                f"stage {self.name!r}: move_domains must match station count"
            )

    @property
    def ticks(self) -> int:
        return len(self.stations)

    def domain(self, tick: int) -> tuple[int, ...]:
        if self.move_domains is not None:
            return self.move_domains[tick]
        return DELTAS

    def _station_ok(self, tick: int, x: int) -> bool:
        allowed = self.stations[tick]
        return allowed is None or x in allowed


# ----------------------------------------------------------------------
# Fixed guide rail: one frozen transition schedule for ALL candidates
# ----------------------------------------------------------------------
def rail_schedule(stage: Stage) -> tuple[int, ...] | None:
    """Return a single delta sequence that satisfies every candidate wind.

    The rail cannot observe a score line, so the same schedule must work in all
    candidates; if none does, the rail cannot carry this stage.
    """

    for deltas in product(*(stage.domain(t) for t in range(stage.ticks))):
        if _fixed_works(stage, deltas):
            return deltas
    return None


def _fixed_works(stage: Stage, deltas: Sequence[int]) -> bool:
    for winds in stage.candidates:
        layer, x = stage.start_layer, stage.start_x
        for t, delta in enumerate(deltas):
            nl = layer + delta
            if not 0 <= nl < stage.layers:
                return False
            x += winds[nl]
            layer = nl
            if not stage._station_ok(t, x):
                return False
    return True


def rail_feasible(stage: Stage) -> bool:
    return rail_schedule(stage) is not None


# ----------------------------------------------------------------------
# Moveable push-rod: reactive policy over score-line observations
# ----------------------------------------------------------------------
@dataclass
class RodPlan:
    feasible: bool
    # Union over all policy branches of ticks where a non-zero move happens.
    # The rod must physically be at this stage on all of these ticks.
    move_ticks: frozenset[int] = frozenset()
    # Tokens used in the worst single realised branch (the true wind picks one).
    max_tokens_used: int = 0


@dataclass(frozen=True)
class _BeliefEntry:
    cand: int
    layer: int
    x: int


def rod_plan(stage: Stage) -> RodPlan:
    """Search for a reactive policy that satisfies all candidates.

    A belief is the set of ``(candidate, layer, x)`` worlds still possible.
    Between score lines one delta must serve the whole belief; at a score line
    the belief splits by the observed ``x`` and each split continues on its own.
    The policy prefers to *stay* (delta 0) when that remains feasible, which
    delays the first move as far as the script allows and conserves tokens.
    """

    start = frozenset(
        _BeliefEntry(ci, stage.start_layer, stage.start_x)
        for ci in range(len(stage.candidates))
    )
    memo: dict[tuple[int, frozenset[_BeliefEntry], int], RodPlan | None] = {}
    result = _rod_search(stage, 0, start, 0, memo)
    return result if result is not None else RodPlan(feasible=False)


def _rod_search(
    stage: Stage,
    tick: int,
    belief: frozenset[_BeliefEntry],
    tokens_used: int,
    memo: dict[tuple[int, frozenset[_BeliefEntry], int], RodPlan | None],
) -> RodPlan | None:
    if tick == stage.ticks:
        return RodPlan(feasible=True, move_ticks=frozenset(), max_tokens_used=tokens_used)

    key = (tick, belief, tokens_used)
    if key in memo:
        return memo[key]

    # Prefer staying, then up/down, to keep the first move late.
    ordered = sorted(stage.domain(tick), key=lambda d: (d != 0, abs(d), d))
    for delta in ordered:
        new_tokens = tokens_used + (1 if delta != 0 else 0)
        if new_tokens > stage.tokens:
            continue
        advanced: list[_BeliefEntry] = []
        ok = True
        for entry in belief:
            winds = stage.candidates[entry.cand]
            nl = entry.layer + delta
            if not 0 <= nl < stage.layers:
                ok = False
                break
            nx = entry.x + winds[nl]
            if not stage._station_ok(tick, nx):
                ok = False
                break
            advanced.append(_BeliefEntry(entry.cand, nl, nx))
        if not ok:
            continue

        new_belief = frozenset(advanced)
        move_here = {tick} if delta != 0 else set()
        if tick in stage.score_lines:
            groups: dict[int, list[_BeliefEntry]] = {}
            for entry in advanced:
                groups.setdefault(entry.x, []).append(entry)
            plans = []
            for group in groups.values():
                sub = _rod_search(stage, tick + 1, frozenset(group), new_tokens, memo)
                if sub is None:
                    plans = None
                    break
                plans.append(sub)
            if plans is None:
                continue
            combined = RodPlan(
                feasible=True,
                move_ticks=frozenset(move_here.union(*[p.move_ticks for p in plans]))
                if plans else frozenset(move_here),
                max_tokens_used=max([new_tokens] + [p.max_tokens_used for p in plans]),
            )
            memo[key] = combined
            return combined
        else:
            sub = _rod_search(stage, tick + 1, new_belief, new_tokens, memo)
            if sub is None:
                continue
            combined = RodPlan(
                feasible=True,
                move_ticks=frozenset(move_here.union(sub.move_ticks)),
                max_tokens_used=sub.max_tokens_used,
            )
            memo[key] = combined
            return combined

    memo[key] = None
    return None


def rod_feasible(stage: Stage) -> bool:
    return rod_plan(stage).feasible


# ----------------------------------------------------------------------
# Dual-stage device selection and work order
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Device:
    """Where the hardware is, and when it can act."""

    kind: str  # "rail" or "rod"
    stages: frozenset[str]  # stages this device is installed on / starts at
    available_from: int = 0  # first tick the device may act (rod handoff)


@dataclass
class WorkOrder:
    option: str
    feasible: bool
    reason: str
    rail_stages: dict[str, int] = field(default_factory=dict)  # stage -> latest install tick
    rod_home: str | None = None
    rod_deadlines: dict[str, int] = field(default_factory=dict)  # stage -> must arrive by
    tokens: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "option": self.option,
            "feasible": self.feasible,
            "reason": self.reason,
            "rail_stages": self.rail_stages,
            "rod_home": self.rod_home,
            "rod_deadlines": self.rod_deadlines,
            "tokens": self.tokens,
        }


def _needed_ticks(stage: Stage, *, via_rail: bool) -> frozenset[int] | None:
    """Ticks at which this stage must move, or None if it cannot be served."""

    if via_rail:
        schedule = rail_schedule(stage)
        if schedule is None:
            return None
        return frozenset(t for t, d in enumerate(schedule) if d != 0)
    plan = rod_plan(stage)
    if not plan.feasible:
        return None
    return plan.move_ticks


def plan_rail(
    guest: Stage,
    measure: Stage,
    *,
    rod_home: str = "measure",
    handoff_ticks: int = 0,
) -> WorkOrder:
    """Guest rides a pre-installed fixed rail; measure uses the push-rod.

    The rail schedule is frozen, so it must be good for *all* wind candidates.
    The latest it can be installed is the earliest tick the guest must move.
    """

    guest_moves = _needed_ticks(guest, via_rail=True)
    measure_ok = rod_feasible(measure)
    if guest_moves is None:
        return WorkOrder(
            "rail", False,
            "guest has no single fixed rail schedule that fits every wind candidate",
        )
    if not measure_ok:
        return WorkOrder(
            "rail", False,
            "measure stage has no push-rod policy fitting its script",
        )
    earliest = min(guest_moves) if guest_moves else guest.ticks
    return WorkOrder(
        "rail",
        True,
        "guest on pre-installed rail; measure on push-rod",
        rail_stages={guest.name: earliest},
        rod_home=rod_home,
        rod_deadlines={measure.name: 0},
        tokens={measure.name: measure.tokens},
    )


def plan_rod(
    guest: Stage,
    measure: Stage,
    *,
    rods: int = 1,
    handoff_ticks: int = 0,
    start_home: str = "measure",
) -> WorkOrder:
    """Both stages driven by the movable push-rod(s).

    With a single rod the stages must be served in an order that respects the
    handoff time: whoever is served second cannot act on the first ``handoff``
    ticks after being picked up.
    """

    guest_moves = _needed_ticks(guest, via_rail=False)
    measure_moves = _needed_ticks(measure, via_rail=False)
    if guest_moves is None:
        return WorkOrder("rod", False, "guest has no push-rod policy for its script")
    if measure_moves is None:
        return WorkOrder("rod", False, "measure has no push-rod policy for its script")
    if rods >= 2:
        return WorkOrder(
            "rod",
            True,
            "one rod per stage",
            rod_deadlines={
                guest.name: (min(guest_moves) if guest_moves else guest.ticks),
                measure.name: (min(measure_moves) if measure_moves else measure.ticks),
            },
            tokens={guest.name: guest.tokens, measure.name: measure.tokens},
        )

    # Single rod: it starts at ``start_home``.  Decide which stage keeps it and
    # which waits for a handoff.  The handed-over stage must not need to move
    # before the handoff completes.
    stages = {guest.name: guest, measure.name: measure}
    other = measure.name if start_home == guest.name else guest.name
    first_moves = {
        guest.name: min(guest_moves) if guest_moves else guest.ticks,
        measure.name: min(measure_moves) if measure_moves else measure.ticks,
    }

    # Simplest feasible orders: home stage first, then hand over if the other
    # does not need to move until after the handoff.
    handoff_done = handoff_ticks
    if first_moves[other] >= handoff_done:
        return WorkOrder(
            "rod",
            True,
            f"single rod starts at {start_home}; hand to {other} by tick {handoff_done}",
            rod_home=start_home,
            rod_deadlines={
                start_home: first_moves[start_home],
                other: handoff_done,
            },
            tokens={guest.name: guest.tokens, measure.name: measure.tokens},
        )
    return WorkOrder(
        "rod",
        False,
        f"single rod at {start_home} cannot reach {other} before its first move "
        f"(tick {first_moves[other]}) given handoff of {handoff_ticks} ticks",
    )


def plan_dual(
    guest: Stage,
    measure: Stage,
    *,
    rail_installed_on_guest: bool = False,
    rods: int = 1,
    handoff_ticks: int = 0,
    rod_home: str = "measure",
) -> list[WorkOrder]:
    """Enumerate the venue's allowed devices and return each work order."""

    orders = [
        plan_rod(
            guest, measure,
            rods=rods, handoff_ticks=handoff_ticks, start_home=rod_home,
        )
    ]
    orders.append(
        plan_rail(guest, measure, rod_home=rod_home, handoff_ticks=handoff_ticks)
    )
    return orders
