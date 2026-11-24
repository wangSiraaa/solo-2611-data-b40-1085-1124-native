"""Adaptive verification planning and the release decision.

Before any observation the packaging room holds **one** order whose vessel may
still be several different configurations.  Two supplementary readings of the
same order may not each walk away with a different lid before the evidence is
taken.  The planner therefore grows one decision tree from the questions the
work order actually permits:

* at a node, if some fixed-piece packing sheet fits *every* world still alive
  on that branch, the branch commits that sheet immediately and asks nothing
  more;
* otherwise the branch asks the cheapest still-runnable question that splits
  the surviving worlds and recurses on the two possible answers;
* when no permitted and affordable question can distinguish worlds that
  disagree about the sheet, the branch cannot be signed off.

A gauge dwells two beats and never touches a wax tongue; a trial bite costs one
beat and only consumes its named one-time tongue on success.  Questions whose
success would spend a tongue that must ship, layers already pressed, and
beat-budget overruns are all excluded structurally, so a planned question can
actually be executed on every branch that reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import LacquerError
from .model import VesselConfiguration
from .packing import (
    PackingProblem,
    PackingSheet,
    configuration_signature,
    enumerate_worlds,
    find_robust_sheets,
)
from .workbench import (
    GaugeQuestion,
    Question,
    ResourceLedger,
    TrialBiteQuestion,
    WorkOrder,
    answer_question,
    partitions_worlds,
    question_cost,
    run_question,
)


@dataclass
class PlanNode:
    """One node of the verification decision tree."""

    question: Question | None
    # answer (True/False) -> child node; a leaf has an empty mapping
    children: dict[bool, "PlanNode"] = field(default_factory=dict)
    committable: bool = False
    note: str = ""
    # Populated on a committable leaf: the sheet this branch signs off.
    committed_sheet: PackingSheet | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "question": None if self.question is None else self.question.as_dict(),
            "committable": self.committable,
            "note": self.note,
        }
        if self.committed_sheet is not None:
            out["committed_sheet"] = self.committed_sheet.as_dict()
        if self.children:
            out["on_answer"] = {
                str(answer): child.as_dict()
                for answer, child in sorted(self.children.items())
            }
        return out


def candidate_questions(
    order: WorkOrder,
    configurations: list[VesselConfiguration],
) -> list[Question]:
    """Every question the order offers that still discriminates the worlds."""

    by_vessel: dict[str, list[VesselConfiguration]] = {}
    for configuration in configurations:
        by_vessel.setdefault(configuration.item_id, []).append(configuration)

    questions: list[Question] = []
    for offer in order.offers:
        options = by_vessel.get(offer.vessel_id)
        if not options or offer.vessel_id in order.pressed_vessels:
            continue
        for rim, cell in offer.gauge_cells:
            question = GaugeQuestion(offer.station_id, offer.vessel_id, rim, cell)
            if partitions_worlds(question, options):
                questions.append(question)
        for rim, cell, tongue in offer.trial_bites:
            question = TrialBiteQuestion(
                offer.station_id, offer.vessel_id, rim, cell, tongue
            )
            if partitions_worlds(question, options):
                questions.append(question)
    return questions


def _question_runnable(
    order: WorkOrder,
    ledger: ResourceLedger,
    question: Question,
) -> bool:
    """A branch may only choose a question it can execute on every answer."""

    if ledger.beats_used + question_cost(question) > order.beat_budget:
        return False
    if isinstance(question, GaugeQuestion):
        return True
    required = order.required_original_tongues.get(question.vessel_id, frozenset())
    if question.tongue_id in required:
        # A required original tongue cannot be spent on a test, nor faked.
        return False
    return ledger.tongue_available(order, question.vessel_id, question.tongue_id)


def _commit_sheet(
    problem: PackingProblem,
    worlds: list[dict[str, VesselConfiguration]],
) -> PackingSheet | None:
    """A sheet releasable on a branch must fit every world alive on it."""

    if not worlds:
        return None
    # Evaluate against exactly the surviving worlds; the configurations keep
    # their original worn-base identities, so the rotations cannot shift.
    sheets = find_robust_sheets(problem, worlds=worlds)
    if not sheets:
        return None
    # Deterministic choice: fixed base, then lid, then lid direction.
    sheets.sort(key=lambda sheet: (sheet.base_id, sheet.lid_id, sheet.lid_rotation))
    return sheets[0]


def build_plan(order: WorkOrder, problem: PackingProblem, *, max_depth: int = 6) -> PlanNode:
    """Grow the verification tree for one order over its still-possible worlds."""

    all_worlds = enumerate_worlds(problem)

    def build(
        worlds: list[dict[str, VesselConfiguration]],
        ledger: ResourceLedger,
        depth: int,
    ) -> PlanNode:
        sheet = _commit_sheet(problem, worlds)
        if sheet is not None:
            return PlanNode(
                question=None,
                committable=True,
                note="one fixed sheet fits every world still possible",
                committed_sheet=sheet,
            )
        if depth >= max_depth:
            return PlanNode(
                question=None,
                committable=False,
                note="observation depth exhausted with worlds needing different sheets",
            )

        flat_configurations = [
            configuration for world in worlds for configuration in world.values()
        ]
        questions = [
            question
            for question in candidate_questions(order, flat_configurations)
            if _question_runnable(order, ledger, question)
        ]
        if not questions:
            return PlanNode(
                question=None,
                committable=False,
                note="no permitted, affordable question can distinguish the worlds",
            )

        # Prefer the cheapest discriminating question, then a stable identity.
        questions.sort(key=lambda question: (question_cost(question), _question_key(question)))
        question = questions[0]
        node = PlanNode(question=question)
        for outcome in (True, False):
            branch_worlds = [
                world
                for world in worlds
                if answer_question(question, [world[question.vessel_id]])[
                    configuration_signature(world)
                ]
                == outcome
            ]
            if not branch_worlds:
                continue
            branch_ledger = _clone_ledger(ledger)
            branch_ledger.beats_used += question_cost(question)
            if isinstance(question, TrialBiteQuestion) and outcome:
                branch_ledger.consumed_tongues.setdefault(
                    question.vessel_id, set()
                ).add(question.tongue_id)
            node.children[outcome] = build(branch_worlds, branch_ledger, depth + 1)
        return node

    return build(all_worlds, ResourceLedger(), 0)


def simulate_shift(
    order: WorkOrder,
    problem: PackingProblem,
    real_world: dict[str, VesselConfiguration],
) -> dict[str, Any]:
    """Run the adaptive plan against one real world and decide go/no-go."""

    plan = build_plan(order, problem)
    ledger = ResourceLedger()
    worlds = enumerate_worlds(problem)
    node = plan
    observations: list[dict[str, Any]] = []

    while node.question is not None:
        real_configuration = real_world[node.question.vessel_id]
        try:
            record = run_question(order, ledger, node.question, real_configuration)
        except LacquerError as exc:
            return {
                "order_id": order.order_id,
                "real_world": configuration_signature(real_world),
                "committable": False,
                "reason": f"{exc.code}: {exc.message}",
                "observations": observations,
                "beats_used": ledger.beats_used,
                "beats_remaining": order.beat_budget - ledger.beats_used,
                "consumed_tongues": _sorted_map(ledger.consumed_tongues),
                "used_spares": _sorted_map(ledger.used_spares),
                "committed_sheet": None,
                "plan": plan.as_dict(),
            }
        observations.append(record)
        worlds = [
            world
            for world in worlds
            if answer_question(node.question, [world[node.question.vessel_id]])[
                configuration_signature(world)
            ]
            == record["outcome"]
        ]
        node = node.children[record["outcome"]]

    original_tongues_intact = all(
        tongue not in ledger.consumed_tongues.get(vessel_id, set())
        for vessel_id, tongues in order.required_original_tongues.items()
        for tongue in tongues
    )
    can_commit = node.committable and original_tongues_intact

    return {
        "order_id": order.order_id,
        "real_world": configuration_signature(real_world),
        "committable": can_commit,
        "reason": None if can_commit else node.note or "sheet not guaranteed",
        "observations": observations,
        "beats_used": ledger.beats_used,
        "beats_remaining": order.beat_budget - ledger.beats_used,
        "consumed_tongues": _sorted_map(ledger.consumed_tongues),
        "used_spares": _sorted_map(ledger.used_spares),
        "committed_sheet": (
            node.committed_sheet.as_dict() if node.committed_sheet is not None else None
        ),
        "plan": plan.as_dict(),
    }


def evaluate_all_worlds(
    order: WorkOrder,
    problem: PackingProblem,
) -> list[dict[str, Any]]:
    """The same fixed plan exercised under every still-possible world."""

    return [
        simulate_shift(order, problem, real_world)
        for real_world in enumerate_worlds(problem)
    ]


def _sorted_map(values: dict[str, set[str]]) -> dict[str, list[str]]:
    return {vessel_id: sorted(tongues) for vessel_id, tongues in values.items()}


def _clone_ledger(ledger: ResourceLedger) -> ResourceLedger:
    return ResourceLedger(
        beats_used=ledger.beats_used,
        consumed_tongues={
            vessel_id: set(tongues)
            for vessel_id, tongues in ledger.consumed_tongues.items()
        },
        used_spares={
            vessel_id: set(tongues)
            for vessel_id, tongues in ledger.used_spares.items()
        },
        log=list(ledger.log),
    )


def _question_key(question: Question) -> tuple[str, ...]:
    if isinstance(question, GaugeQuestion):
        return (
            question.station,
            question.vessel_id,
            "gauge",
            question.rim,
            str(question.cell),
        )
    return (
        question.station,
        question.vessel_id,
        "trial_bite",
        question.rim,
        str(question.cell),
        question.tongue_id,
    )
