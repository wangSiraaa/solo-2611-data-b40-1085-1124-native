"""Packing: stack named pieces on a base and seal a lid.

A *seating* chooses one physical configuration per vessel (a worn-base
reading), one whole-cell rotation per piece, and one lid, such that:

1. every lug of each lower rim drops into a notch of the rim above it;
2. the top vessel's top lugs drop into the chosen lid's notches;
3. every fixed-reference witness still lands on one vertical line;
4. vessel/lid direction restrictions (upright, pinned lid direction) hold.

Adjacent joints share the real rotation of the middle piece: a vessel has one
rotation shared by its bottom and its top, so the two neighboring interfaces
can never be "turned independently".

A packing sheet may only be handed over when one seating -- the same named
base, vessels and lid, the same lid direction **and the same whole-cell
rotation for each vessel** -- fits every configuration that can still be the
real vessel.  A vessel's rotation is a single physical decision for the whole
shift shared by both of its joints; the bench may not turn it differently for
different supplementary readings of one order.  When no such seating exists,
nothing is releasable until an observation (gauge or trial bite) removes
worlds; the planner handles that step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Sequence

from .errors import NO_FEASIBLE_PACKING, LacquerError
from .model import (
    Base,
    Lid,
    Ring,
    Vessel,
    VesselConfiguration,
    lower_lugs_fit_upper,
)
from .witness import Witness, witness_holds


@dataclass(frozen=True)
class Seat:
    """One seated piece in a stack."""

    item_id: str
    kind: str  # base / vessel / lid
    rotation: int

    def as_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "kind": self.kind, "rotation": self.rotation}


@dataclass(frozen=True)
class PackingSheet:
    """A complete, releasable packing sheet.

    Every decision is fixed: the named base, each vessel and its one shared
    seat rotation, the lid and the lid direction.  The sheet is only valid
    when this exact seating fits every surviving configuration.
    """

    base_id: str
    lid_id: str
    lid_rotation: int
    vessel_ids: tuple[str, ...]
    vessel_rotations: dict[str, int]
    fits_configurations: tuple[str, ...]
    divisions: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_id": self.base_id,
            "lid_id": self.lid_id,
            "lid_rotation": self.lid_rotation,
            "vessel_ids": list(self.vessel_ids),
            "vessel_rotations": dict(self.vessel_rotations),
            "fits_configurations": list(self.fits_configurations),
            "divisions": self.divisions,
        }


@dataclass
class PackingProblem:
    bases: list[Base]
    # vessel_id -> the configurations that may still be the real vessel
    vessel_options: dict[str, list[VesselConfiguration]]
    lids: list[Lid]
    # vessel ids in bottom-to-top order
    stack_order: list[str]
    witnesses: list[Witness] = field(default_factory=list)
    # When a vessel must be seated upright in one fixed whole-cell direction
    # for the whole shift, its rotation is pinned here (e.g. 0 = no turn).
    # A pinned rotation is part of the sheet: it cannot differ between the
    # supplementary readings of one order.
    pinned_vessel_rotations: dict[str, int] = field(default_factory=dict)
    max_pieces: int = 6

    def __post_init__(self) -> None:
        if not self.bases:
            raise LacquerError(NO_FEASIBLE_PACKING, "a packing problem needs a base")
        if not self.stack_order:
            raise LacquerError(NO_FEASIBLE_PACKING, "a packing problem needs vessels")
        if not self.lids:
            raise LacquerError(NO_FEASIBLE_PACKING, "a packing problem needs a lid")
        total = 1 + len(self.stack_order) + 1
        if total > self.max_pieces:
            raise LacquerError(
                NO_FEASIBLE_PACKING,
                f"stack of {total} pieces exceeds the workshop limit "
                f"of {self.max_pieces}",
            )
        divisions = {b.divisions for b in self.bases}
        divisions |= {c.divisions for opts in self.vessel_options.values() for c in opts}
        divisions |= {lid.divisions for lid in self.lids}
        if len(divisions) != 1:
            raise LacquerError(
                NO_FEASIBLE_PACKING,
                "all pieces must share one circle division",
                details={"divisions": sorted(divisions)},
            )
        missing = [v for v in self.stack_order if v not in self.vessel_options]
        if missing:
            raise LacquerError(
                NO_FEASIBLE_PACKING,
                "stack order names vessels without configurations",
                details={"vessel_ids": missing},
            )
        empty = [v for v, opts in self.vessel_options.items() if not opts]
        if empty:
            raise LacquerError(
                NO_FEASIBLE_PACKING,
                "vessel has no surviving configuration",
                details={"vessel_ids": sorted(empty)},
            )

    @property
    def divisions(self) -> int:
        return self.bases[0].divisions


def _vessel_from_configuration(configuration: VesselConfiguration) -> Vessel:
    return Vessel(
        configuration.item_id,
        configuration.bottom,
        configuration.top,
        upright=True,
    )


def find_seatings_for_world(
    problem: PackingProblem,
    world: dict[str, VesselConfiguration],
) -> list[dict[str, Any]]:
    """All seatings that fit one fully-determined world (one config/vessel)."""

    n = problem.divisions
    solutions: list[dict[str, Any]] = []
    directions = tuple(range(n))

    for base in problem.bases:
        for lid in problem.lids:
            lid_directions = (
                (lid.pinned_rotation,)
                if lid.pinned_rotation is not None
                else directions
            )
            # One rotation per vessel, shared by both of its joints.  An
            # upright seating pin removes that vessel from the search.
            rotation_choices = [
                (problem.pinned_vessel_rotations[vid],)
                if vid in problem.pinned_vessel_rotations
                else directions
                for vid in problem.stack_order
            ]
            for rotations in product(*rotation_choices):
                rotation_by_item: dict[str, int] = {
                    problem.stack_order[i]: rotations[i]
                    for i in range(len(problem.stack_order))
                }
                rotation_by_item[base.item_id] = 0
                for lid_rotation in lid_directions:
                    rotation_by_item[lid.item_id] = lid_rotation
                    if not _joints_fit(problem, world, base, lid, rotation_by_item):
                        continue
                    if not _witnesses_fit(problem, world, rotation_by_item):
                        continue
                    seats = tuple(
                        Seat(item_id=vid, kind="vessel", rotation=rotation_by_item[vid])
                        for vid in problem.stack_order
                    )
                    solutions.append(
                        {
                            "base": base,
                            "lid": lid,
                            "lid_rotation": lid_rotation,
                            "seats": seats,
                            "rotations": dict(rotation_by_item),
                        }
                    )
    return solutions


def _joints_fit(
    problem: PackingProblem,
    world: dict[str, VesselConfiguration],
    base: Base,
    lid: Lid,
    rotation_by_item: dict[str, int],
) -> bool:
    n = problem.divisions
    lower_ring = base.ring.rotated(rotation_by_item[base.item_id])
    for vessel_id in problem.stack_order:
        vessel = _vessel_from_configuration(world[vessel_id]).rotated(
            rotation_by_item[vessel_id]
        )
        if not lower_lugs_fit_upper(lower_ring, vessel.bottom):
            return False
        lower_ring = vessel.top
    lid_ring = lid.ring.rotated(rotation_by_item[lid.item_id])
    return lower_lugs_fit_upper(lower_ring, lid_ring)


def _witnesses_fit(
    problem: PackingProblem,
    world: dict[str, VesselConfiguration],
    rotation_by_item: dict[str, int],
) -> bool:
    if not problem.witnesses:
        return True
    # Witnesses name rims explicitly; vessel rims are addressed as
    # "<item>.bottom" / "<item>.top", bases and lids by their item id.
    flat_rings: dict[str, Ring] = {}
    rotations: dict[str, int] = {}
    for base in problem.bases:
        flat_rings[base.item_id] = base.ring
        rotations[base.item_id] = rotation_by_item[base.item_id]
    for vessel_id, configuration in world.items():
        flat_rings[f"{vessel_id}.bottom"] = configuration.bottom
        flat_rings[f"{vessel_id}.top"] = configuration.top
        rotations[f"{vessel_id}.bottom"] = rotation_by_item[vessel_id]
        rotations[f"{vessel_id}.top"] = rotation_by_item[vessel_id]
    for lid in problem.lids:
        flat_rings[lid.item_id] = lid.ring
        rotations[lid.item_id] = rotation_by_item[lid.item_id]
    adjusted = [
        Witness(
            lower_item=(
                f"{witness.lower_item}.{witness.lower_rim}"
                if witness.lower_rim in ("bottom", "top")
                else witness.lower_item
            ),
            lower_rim=witness.lower_rim,
            lower_cell=witness.lower_cell,
            upper_item=(
                f"{witness.upper_item}.{witness.upper_rim}"
                if witness.upper_rim in ("bottom", "top")
                else witness.upper_item
            ),
            upper_rim=witness.upper_rim,
            upper_cell=witness.upper_cell,
        )
        for witness in problem.witnesses
    ]
    return all(
        witness_holds(
            witness,
            rings_by_item=flat_rings,
            rotation_by_item=rotations,
            divisions=problem.divisions,
        )
        for witness in adjusted
    )


def enumerate_worlds(
    problem: PackingProblem,
) -> list[dict[str, VesselConfiguration]]:
    """Cartesian product of the still-possible configurations per vessel."""

    vessel_ids = problem.stack_order
    option_lists = [problem.vessel_options[v] for v in vessel_ids]
    worlds: list[dict[str, VesselConfiguration]] = []
    for combo in product(*option_lists):
        worlds.append({vessel_ids[i]: combo[i] for i in range(len(vessel_ids))})
    return worlds


def configuration_signature(world: dict[str, VesselConfiguration]) -> str:
    return "|".join(
        f"{vid}:o{world[vid].origin}" for vid in sorted(world)
    )


def _seating_identity(solution: dict[str, Any]) -> tuple[Any, ...]:
    """The whole seating decision, including every vessel rotation."""

    return (
        solution["base"].item_id,
        solution["lid"].item_id,
        solution["lid_rotation"],
        tuple(
            (seat.item_id, seat.rotation) for seat in solution["seats"]
        ),
    )


def find_robust_sheets(
    problem: PackingProblem,
    *,
    worlds: list[dict[str, VesselConfiguration]] | None = None,
) -> list[PackingSheet]:
    """Return the fully-fixed seatings that fit **every** still-possible world.

    A sheet names one base, one lid, one lid direction and one rotation per
    vessel, and is releasable only when that exact seating fits every world.
    """

    if worlds is None:
        worlds = enumerate_worlds(problem)
    per_world: list[set[tuple[Any, ...]]] = [
        {_seating_identity(solution) for solution in find_seatings_for_world(problem, world)}
        for world in worlds
    ]
    if any(not identities for identities in per_world):
        return []

    common = per_world[0].intersection(*per_world[1:])
    sheets: list[PackingSheet] = []
    for identity in sorted(common):
        base_id, lid_id, lid_rotation, seat_tuple = identity
        sheets.append(
            PackingSheet(
                base_id=base_id,
                lid_id=lid_id,
                lid_rotation=lid_rotation,
                vessel_ids=tuple(item_id for item_id, _ in seat_tuple),
                vessel_rotations={item_id: rotation for item_id, rotation in seat_tuple},
                fits_configurations=tuple(
                    configuration_signature(world) for world in worlds
                ),
                divisions=problem.divisions,
            )
        )
    return sheets


def worlds_fit_sheet(
    problem: PackingProblem,
    sheet: PackingSheet,
) -> dict[str, bool]:
    """Which still-possible worlds a previously fixed sheet fits."""

    base = next(b for b in problem.bases if b.item_id == sheet.base_id)
    lid = next(l for l in problem.lids if l.item_id == sheet.lid_id)
    result: dict[str, bool] = {}
    for world in enumerate_worlds(problem):
        rotations = dict(sheet.vessel_rotations)
        rotations[base.item_id] = 0
        rotations[lid.item_id] = sheet.lid_rotation
        joints = _joints_fit(problem, world, base, lid, rotations)
        witnesses = _witnesses_fit(problem, world, rotations)
        result[configuration_signature(world)] = joints and witnesses
    return result
