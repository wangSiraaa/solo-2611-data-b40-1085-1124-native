"""Workbench: gauges, trial bites, beat budgets and one-time wax tongues.

Two instruments can distinguish the still-possible vessel configurations:

* **Gauge** (``gauge``): on the reference line it answers one boolean --
  whether a mark is exposed at a given cell.  The vessel must sit untouched on
  the gauge table for two beats (``dwell_beats = 2``) before a reading is
  taken; the wax tongue is never moved and no tongue is consumed.
* **Trial-bite clamp** (``trial_bite``): one beat after clamping, it reveals
  whether a named test tooth enters the notch at a given corner of a given
  rim.  A *success* consumes one named, one-time wax tongue of this vessel; a
  failure only spends the beat.

Everything a station may ask, how many beats the order may borrow, and which
named spare tongues may be used comes from the work order -- never from
defaults.  A vessel whose original tongue must ship cannot use a spare tongue
to impersonate it: a trial bite that would consume a required original tongue
is not runnable, and once a neighbor layer is pressed it is never reopened for
another test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import (
    BUDGET_EXHAUSTED,
    COMMITMENT_BLOCKED,
    ORIGINAL_TONGUE_REQUIRED,
    TEST_NOT_OFFERED,
    TONGUE_UNAVAILABLE,
    LacquerError,
)
from .model import KIND_NOTCH, VesselConfiguration

GAUGE = "gauge"
TRIAL_BITE = "trial_bite"
GAUGE_DWELL_BEATS = 2
TRIAL_BITE_BEATS = 1


@dataclass(frozen=True)
class GaugeQuestion:
    """Is a mark exposed at ``cell`` of ``rim`` on the reference line?"""

    station: str
    vessel_id: str
    rim: str  # bottom / top
    cell: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": GAUGE,
            "station": self.station,
            "vessel_id": self.vessel_id,
            "rim": self.rim,
            "cell": self.cell,
        }


@dataclass(frozen=True)
class TrialBiteQuestion:
    """Does the named test tooth enter the notch at a corner?"""

    station: str
    vessel_id: str
    rim: str
    cell: int
    tongue_id: str  # the named one-time tongue a success would consume

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": TRIAL_BITE,
            "station": self.station,
            "vessel_id": self.vessel_id,
            "rim": self.rim,
            "cell": self.cell,
            "tongue_id": self.tongue_id,
        }


Question = GaugeQuestion | TrialBiteQuestion


@dataclass(frozen=True)
class StationOffer:
    """What one workstation is allowed to ask for one vessel in this order."""

    station_id: str
    vessel_id: str
    gauge_cells: tuple[tuple[str, int], ...]  # (rim, cell) the gauge may read
    trial_bites: tuple[tuple[str, int, str], ...]  # (rim, cell, tongue_id)

    def offers(self, question: Question) -> bool:
        if question.station != self.station_id or question.vessel_id != self.vessel_id:
            return False
        if isinstance(question, GaugeQuestion):
            return (question.rim, question.cell) in self.gauge_cells
        return (
            question.rim,
            question.cell,
            question.tongue_id,
        ) in self.trial_bites


@dataclass(frozen=True)
class WorkOrder:
    """Everything the workshop is allowed to do for one order."""

    order_id: str
    beat_budget: int
    # vessel_id -> named tongues that must be present at shipping
    required_original_tongues: dict[str, frozenset[str]]
    # vessel_id -> named spare tongues the order explicitly authorizes
    allowed_spare_tongues: dict[str, frozenset[str]]
    # vessel_id -> tongues physically available at order start (originals)
    original_tongues: dict[str, frozenset[str]]
    offers: tuple[StationOffer, ...]
    # Once a layer is pressed its vessel is closed to further questions.
    pressed_vessels: frozenset[str] = frozenset()
    # The fixed sheet the order must ship; observation may only gate go/no-go.
    spare_sheet_authorized: bool = True

    def offer_for(self, station_id: str, vessel_id: str) -> StationOffer | None:
        for offer in self.offers:
            if offer.station_id == station_id and offer.vessel_id == vessel_id:
                return offer
        return None


@dataclass
class ResourceLedger:
    """Beats and tongues actually spent in this shift."""

    beats_used: int = 0
    # vessel -> tongues already consumed (originals or named spares)
    consumed_tongues: dict[str, set[str]] = field(default_factory=dict)
    # vessel -> spare tongues already substituted in
    used_spares: dict[str, set[str]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def tongue_available(self, order: WorkOrder, vessel_id: str, tongue_id: str) -> bool:
        consumed = self.consumed_tongues.get(vessel_id, set())
        if tongue_id in consumed:
            return False
        originals = order.original_tongues.get(vessel_id, frozenset())
        spares = order.allowed_spare_tongues.get(vessel_id, frozenset())
        return tongue_id in originals or tongue_id in spares


def answer_question(
    question: Question,
    configurations: Iterable[VesselConfiguration],
) -> dict[str, bool]:
    """What each still-possible world would answer at this station."""

    answers: dict[str, bool] = {}
    for configuration in configurations:
        ring = (
            configuration.bottom if question.rim == "bottom" else configuration.top
        )
        if isinstance(question, GaugeQuestion):
            # A mark is exposed when the cell is not blank.
            answer = ring.feature(question.cell) is not None
        else:
            # The test tooth enters only a genuine notch at that corner.
            answer = ring.feature(question.cell) == KIND_NOTCH
        answers[f"{configuration.item_id}:o{configuration.origin}"] = answer
    return answers


def question_cost(question: Question) -> int:
    return GAUGE_DWELL_BEATS if isinstance(question, GaugeQuestion) else TRIAL_BITE_BEATS


def run_question(
    order: WorkOrder,
    ledger: ResourceLedger,
    question: Question,
    real_world: VesselConfiguration,
) -> dict[str, Any]:
    """Execute one question against the real vessel, honoring every rule.

    Raises :class:`LacquerError` with a stable code when the question is not
    runnable (station not offered, layer already pressed, budget or tongue
    rules).  A failed trial bite spends only the beat; a successful one spends
    the beat and the named tongue.
    """

    offer = order.offer_for(question.station, question.vessel_id)
    if offer is None or not offer.offers(question):
        raise LacquerError(
            TEST_NOT_OFFERED,
            "the work order does not allow this question at this station",
            details={"question": question.as_dict()},
        )
    if question.vessel_id in order.pressed_vessels:
        raise LacquerError(
            COMMITMENT_BLOCKED,
            "neighbor layer already pressed; it cannot be reopened",
            details={"vessel_id": question.vessel_id},
        )

    cost = question_cost(question)
    if ledger.beats_used + cost > order.beat_budget:
        raise LacquerError(
            BUDGET_EXHAUSTED,
            "not enough beats remain on this shift",
            details={
                "beats_used": ledger.beats_used,
                "needed": cost,
                "budget": order.beat_budget,
            },
        )

    answers = answer_question(question, [real_world])
    outcome = next(iter(answers.values()))

    consumed_tongue: str | None = None
    substituted_spare: str | None = None
    if isinstance(question, TrialBiteQuestion):
        tongue = question.tongue_id
        required = order.required_original_tongues.get(question.vessel_id, frozenset())
        originals = order.original_tongues.get(question.vessel_id, frozenset())
        spares = order.allowed_spare_tongues.get(question.vessel_id, frozenset())
        if tongue in required:
            # A required original tongue cannot be spent on a test, nor faked
            # with a spare.
            raise LacquerError(
                ORIGINAL_TONGUE_REQUIRED,
                "this original tongue must ship; it cannot be spent on a bite",
                details={"vessel_id": question.vessel_id, "tongue_id": tongue},
            )
        if not ledger.tongue_available(order, question.vessel_id, tongue):
            raise LacquerError(
                TONGUE_UNAVAILABLE,
                "the named tongue is already consumed or not carried",
                details={"vessel_id": question.vessel_id, "tongue_id": tongue},
            )
        if tongue not in originals and tongue not in spares:
            raise LacquerError(
                TONGUE_UNAVAILABLE,
                "the named tongue is not carried by this order",
                details={"vessel_id": question.vessel_id, "tongue_id": tongue},
            )
        if outcome:
            # Success consumes the tongue.  A spare is only legal when the
            # order explicitly authorizes that named spare.
            if tongue in spares and tongue not in originals:
                substituted_spare = tongue
                ledger.used_spares.setdefault(question.vessel_id, set()).add(tongue)
            ledger.consumed_tongues.setdefault(question.vessel_id, set()).add(tongue)
            consumed_tongue = tongue

    ledger.beats_used += cost
    record = {
        "question": question.as_dict(),
        "outcome": outcome,
        "beats_spent": cost,
        "consumed_tongue": consumed_tongue,
        "substituted_spare": substituted_spare,
        "beats_remaining": order.beat_budget - ledger.beats_used,
    }
    ledger.log.append(record)
    return record


def partitions_worlds(
    question: Question,
    configurations: Iterable[VesselConfiguration],
) -> bool:
    """True when the answer differs across still-possible worlds."""

    answers = set(answer_question(question, configurations).values())
    return len(answers) > 1
