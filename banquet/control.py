"""The single control measure the ritual office may take for the event.

The office may take **exactly one** kind of control for the whole event:

* **screen** -- borrow a sound screen.  Installing it occupies one *empty*
  round; from the next round on it blocks one seam (a chair-to-chair join).
  Blocking a seam stops incidental overhearing across it, but also blocks any
  *legitimate* direct relay that would have used that seam.  Once guests are
  seated a chair is locked and the screen cannot be moved until the next round
  at the earliest.
* **seat swap** -- no screen.  Using each round's swap quota, move a guest
  (typically a substitute interpreter) to another compliant, unlocked chair.
  Clues follow the person; the registered mutual understanding holds as
  actually seated.

We simulate the whole night under a control.  Clues accumulate monotonically:
a segment that has been spoken is never "unheard" afterwards, even if the
chart or an interpreter is later changed.  The resulting breaches let the
venue point at exactly which incidental hearing or which boundary crossing
defeats the evening's secrecy promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .clues import (
    Breach,
    ClueLedger,
    evaluate_secrecy,
    record_round_clues,
    secret_requirements,
)
from .model import Edge, Guest, OrdinanceSpec, Round, SeatChart, SecretGroup
from .relay import RoundPlan, plan_round


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoControl:
    kind: str = "none"


@dataclass(frozen=True)
class ScreenControl:
    seam: Edge
    install_round: int          # index of an *empty* round used to install
    kind: str = "screen"


@dataclass(frozen=True)
class SeatSwap:
    round_index: int
    guest_id: str
    target_index: int
    kind: str = "swap"


@dataclass(frozen=True)
class SwapControl:
    swaps: tuple[SeatSwap, ...] = ()
    kind: str = "swap"


Control = NoControl | ScreenControl | SwapControl


class ControlError(ValueError):
    """The chosen control cannot be applied as described."""


# ---------------------------------------------------------------------------
# Night description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Night:
    guests: Mapping[str, Guest]
    base_chart: SeatChart
    rounds: tuple[Round, ...]
    ordinance: OrdinanceSpec
    designated_pairs: tuple[frozenset[str], ...] = ()
    secret_groups: tuple[SecretGroup, ...] = ()
    # Per-round swap quotas.  A round may move at most this many guests.
    swap_quota: tuple[int, ...] = ()
    # Rounds already spoken before the controlled part of the evening, each on
    # its own seating.  They are simulated first to establish clues (e.g. a red
    # slip relayed by 丙 in an earlier arrangement).  Clues follow the person,
    # so they survive a later re-seating -- "已经开口的一段不能因追改席图或换
    # 译员而'没听见'".
    prior: tuple[tuple[SeatChart, Round], ...] = ()


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundReport:
    round_id: str
    chart: SeatChart
    plan: RoundPlan
    screen_active: bool

    def as_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "chart": self.chart.as_dict(),
            "plan": self.plan.as_dict(),
            "screen_active": self.screen_active,
        }


@dataclass(frozen=True)
class NightResult:
    valid: bool
    status: str                              # ok | invalid_control | breach
    round_reports: tuple[RoundReport, ...] = ()
    ledger: ClueLedger = field(default_factory=ClueLedger)
    breaches: tuple[Breach, ...] = ()
    requirements: Mapping[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "status": self.status,
            "rounds": [r.as_dict() for r in self.round_reports],
            "clues": self.ledger.as_dict(),
            "breaches": [
                {
                    "group": b.group_id,
                    "actor": list(b.actor),
                    "gathered": sorted(b.gathered),
                    "via": b.via,
                }
                for b in self.breaches
            ],
            "notes": list(self.notes),
        }


def _apply_swap(
    chart: SeatChart, swap: SeatSwap, ordinance: OrdinanceSpec
) -> tuple[SeatChart, list[str]]:
    """Move ``swap.guest_id`` to ``target_index`` (swap with occupant or empty).

    The resulting chart must remain compliant with the ordinance.
    """

    seats = list(chart.seats)
    if swap.guest_id not in seats:
        return chart, [f"swap_guest_absent:{swap.guest_id}"]
    if not (0 <= swap.target_index < len(seats)):
        return chart, [f"swap_target_out_of_range:{swap.target_index}"]
    src = seats.index(swap.guest_id)
    if src == swap.target_index:
        return chart, [f"swap_noop:{swap.guest_id}"]
    seats[src], seats[swap.target_index] = seats[swap.target_index], seats[src]
    new_chart = SeatChart(tuple(seats))
    ok, why = ordinance.allows(new_chart)
    if not ok:
        return chart, [f"swap_noncompliant:{r}" for r in why]
    return new_chart, []


def _screen_install_round(control: ScreenControl, rounds: tuple[Round, ...]) -> None:
    idx = control.install_round
    if not (0 <= idx < len(rounds)):
        raise ControlError(f"install_round {idx} out of range")
    if not rounds[idx].is_empty:
        raise ControlError(
            "a screen must be installed in an empty round; "
            f"round {idx} has conversations"
        )


def _validate_swaps(
    control: SwapControl, night: Night
) -> dict[int, list[SeatSwap]]:
    quotas = list(night.swap_quota) + [0] * (len(night.rounds) - len(night.swap_quota))
    by_round: dict[int, list[SeatSwap]] = {}
    for sw in control.swaps:
        if not (0 <= sw.round_index < len(night.rounds)):
            raise ControlError(f"swap round {sw.round_index} out of range")
        by_round.setdefault(sw.round_index, []).append(sw)
    for r, items in by_round.items():
        if len(items) > quotas[r]:
            raise ControlError(
                f"round {r} swap quota {quotas[r]} exceeded by {len(items)} swaps"
            )
    return by_round


def simulate_night(night: Night, control: Control = NoControl()) -> NightResult:
    """Run every round under ``control`` and evaluate the secrecy agreement."""

    notes: list[str] = []
    chart = night.base_chart
    blocked: set[Edge] = set()
    ledger = ClueLedger()
    reports: list[RoundReport] = []

    screen_seam: Edge | None = None
    install_at: int | None = None
    if isinstance(control, ScreenControl):
        _screen_install_round(control, night.rounds)
        screen_seam = control.seam
        install_at = control.install_round
        if screen_seam not in night.base_chart.edges():
            notes.append(
                f"screen_seam_not_on_chart:{screen_seam.a}-{screen_seam.b}"
            )

    swaps_by_round: dict[int, list[SeatSwap]] = {}
    if isinstance(control, SwapControl):
        swaps_by_round = _validate_swaps(control, night)

    # Rounds spoken before the controlled part: establish clues on their own
    # seatings.  They never involve a screen or swap.
    prior_rounds: list[Round] = []
    prior_plans: list[RoundPlan] = []
    for pi, (prior_chart, prior_round) in enumerate(night.prior):
        prior_plan = plan_round(prior_chart, prior_round, night.guests)
        record_round_clues(ledger, prior_chart, prior_round, prior_plan)
        prior_rounds.append(prior_round)
        prior_plans.append(prior_plan)
        notes.append(f"prior_round:{prior_round.round_id}")

    for ri, rnd in enumerate(night.rounds):
        # Screen is active strictly *after* its installation round.
        screen_here = install_at is not None and ri > install_at
        if screen_here and screen_seam is not None:
            blocked.add(screen_seam)

        # Apply this round's swaps before planning.
        for sw in swaps_by_round.get(ri, []):
            new_chart, problems = _apply_swap(chart, sw, night.ordinance)
            if problems:
                raise ControlError("; ".join(problems))
            chart = new_chart
            notes.append(f"swap_round{ri}:{sw.guest_id}->chair{sw.target_index}")

        plan = plan_round(chart, rnd, night.guests, blocked=blocked)
        if not plan.ok:
            notes.append(f"round_infeasible:{rnd.round_id}")
        record_round_clues(ledger, chart, rnd, plan, blocked=blocked)
        reports.append(RoundReport(rnd.round_id, chart, plan, screen_here))

    requirements = secret_requirements(
        tuple(prior_rounds) + night.rounds,
        tuple(prior_plans) + tuple(r.plan for r in reports),
    )
    breaches = evaluate_secrecy(
        ledger,
        requirements=requirements,
        groups=night.secret_groups,
        designated_pairs=night.designated_pairs,
    )

    if any(not r.plan.ok for r in reports):
        status, valid = "invalid_control", False
    elif breaches:
        status, valid = "breach", False
    else:
        status, valid = "ok", True

    return NightResult(
        valid=valid,
        status=status,
        round_reports=tuple(reports),
        ledger=ledger,
        breaches=tuple(breaches),
        requirements=dict(requirements),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Venue-facing diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenCheck:
    feasible: bool
    seam: Edge
    install_round: int | None
    reason: str


def can_install_screen(night: Night, seam: Edge, *, before_round: int) -> ScreenCheck:
    """Can ``seam`` be screened before ``before_round`` using an empty round?

    A screen is active only from the round *after* installation, so we need an
    empty round strictly before ``before_round``.  If there is none, screening
    is impossible in time -- and no amount of rewriting the record can fix it.
    """

    for idx in range(before_round):
        if night.rounds[idx].is_empty:
            return ScreenCheck(True, seam, idx, "empty_installation_round_available")
    return ScreenCheck(
        False, seam, None,
        "no_empty_round_before_target",
    )


@dataclass(frozen=True)
class SwapSeat:
    target_index: int
    target_guest: str | None
    resulting_chart: SeatChart
    reason: str


def find_swap_seats(
    night: Night,
    guest_id: str,
    *,
    round_index: int,
    control: Control = NoControl(),
    avoid_adjacent_to: Iterable[str] = (),
) -> list[SwapSeat]:
    """Every compliant, unlocked chair ``guest_id`` may move to this round.

    A seat qualifies when the swap stays within the round's quota, the resulting
    chart is still compliant, and -- when ``avoid_adjacent_to`` is given -- the
    guest ends up *not* adjacent to any of those people (so they cannot
    incidentally overhear them).
    """

    avoid = set(avoid_adjacent_to)
    base = chart_for_round(night, round_index, control)
    if guest_id not in base.seats:
        return []
    quota = (
        night.swap_quota[round_index]
        if round_index < len(night.swap_quota)
        else 0
    )
    if quota < 1:
        return []

    out: list[SwapSeat] = []
    for idx in range(base.size):
        if idx == base.position(guest_id):
            continue
        sw = SeatSwap(round_index=round_index, guest_id=guest_id, target_index=idx)
        new_chart, problems = _apply_swap(base, sw, night.ordinance)
        if problems:
            continue
        if any(new_chart.adjacent(guest_id, other) for other in avoid):
            continue
        out.append(
            SwapSeat(
                target_index=idx,
                target_guest=base.seats[idx],
                resulting_chart=new_chart,
                reason="compliant_unlocked_nonadjacent",
            )
        )
    # Prefer genuinely empty ("not yet seated") chairs over displacing a guest.
    out.sort(key=lambda s: (s.target_guest is not None, s.target_index))
    return out


@dataclass(frozen=True)
class SwapBooking:
    bookable: bool
    round_index: int
    reason: str


def book_swap(
    night: Night,
    guest_id: str,
    *,
    round_index: int,
    target_index: int,
) -> SwapBooking:
    """Check whether moving ``guest_id`` to ``target_index`` before a round is
    bookable: within quota and compliant.  Returning ``bookable=False`` with a
    reason is the reviewable result -- the plan is not silently rewritten."""

    quota = (
        night.swap_quota[round_index]
        if round_index < len(night.swap_quota)
        else 0
    )
    if quota < 1:
        return SwapBooking(False, round_index, "swap_quota_exhausted")
    sw = SeatSwap(round_index=round_index, guest_id=guest_id, target_index=target_index)
    _, problems = _apply_swap(night.base_chart, sw, night.ordinance)
    if problems:
        return SwapBooking(False, round_index, ";".join(problems))
    return SwapBooking(True, round_index, "bookable")


def chart_for_round(
    night: Night, round_index: int, control: Control = NoControl()
) -> SeatChart:
    """The chart actually in force at a given round under ``control``."""

    chart = night.base_chart
    if isinstance(control, SwapControl):
        for sw in sorted(control.swaps, key=lambda s: s.round_index):
            if sw.round_index <= round_index:
                chart, problems = _apply_swap(chart, sw, night.ordinance)
                if problems:
                    raise ControlError("; ".join(problems))
    return chart


def blocked_for_round(
    night: Night, round_index: int, control: Control = NoControl()
) -> frozenset[Edge]:
    if isinstance(control, ScreenControl):
        if round_index > control.install_round:
            return frozenset({control.seam})
    return frozenset()


def listener_hears(
    night: Night,
    round_index: int,
    conv_id: str,
    listener: str,
    *,
    control: Control = NoControl(),
) -> bool:
    """Would ``listener`` hear ``conv_id`` in that round (spoken or incidental)?

    This is the venue-facing diagnostic: it lets the hall point at *which*
    incidental hearing would blow the promise.  It uses the chart and blocked
    seams actually in force for that round under ``control``.
    """

    rnd = night.rounds[round_index]
    conv = next((c for c in rnd.conversations if c.conv_id == conv_id), None)
    if conv is None:
        raise KeyError(conv_id)

    chart = chart_for_round(night, round_index, control)
    blocked = blocked_for_round(night, round_index, control)

    plan = plan_round(chart, rnd, night.guests, blocked=blocked)
    if not plan.ok:
        return False
    route = next((r for r in plan.routes if r.conv_id == conv_id), None)
    if route is None:
        return False
    if listener in route.participants:
        return True

    incidental = conv.incidental or rnd.is_incidental
    if not incidental:
        return False
    for seg in route.segments:
        for endpoint in (seg.frm, seg.to):
            if endpoint == listener:
                continue
            if chart.adjacent(listener, endpoint):
                seam = Edge(*sorted((listener, endpoint)))
                if seam not in blocked:
                    return True
    return False
