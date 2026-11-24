"""Session-level policy: one wall-safety device per session.

The venue offers two devices -- permanent lane covers (分道罩) and relay
numbers (临场号).  A single session picks exactly one set ("各场只选一套"):
you either install covers or hand out relay numbers, never both in the same
session.  This module makes that exclusivity explicit and checks that a chosen
plan is well-formed for its device.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .scheduling import (
    CoverPlan,
    Slot,
    find_cover_plan,
    relay_schedule,
    validate_delivery,
)


class Device(str, Enum):
    COVERS = "covers"
    RELAY_NUMBERS = "relay_numbers"
    NONE = "none"


@dataclass
class SessionPlan:
    device: Device
    feasible: bool
    reason: str | None
    covers: CoverPlan | None = None
    relay: dict | None = None
    detail: dict | None = None

    def as_dict(self) -> dict:
        return {
            "device": self.device.value,
            "feasible": self.feasible,
            "reason": self.reason,
            "covers": self.covers.as_dict() if self.covers else None,
            "relay": self.relay,
            "detail": self.detail or {},
        }


def plan_session(
    slots: list[Slot],
    device: Device,
    *,
    send_beats: Iterable[int] = (),
    observed_first_move: dict[str, int | None] | None = None,
    forced_covers: dict[str, Iterable[str]] | None = None,
) -> SessionPlan:
    """Plan one session using exactly one device.

    * ``Device.COVERS`` -- find a permanent lane-cover plan that keeps every
      group's original grade and makes the round hold-disjoint.
    * ``Device.RELAY_NUMBERS`` -- stagger starts with distinct relay numbers
      and verify the numbers really arrive before each group's first move.
    * ``Device.NONE`` -- both groups must already be hold-disjoint.
    """

    if device is Device.NONE:
        from .scheduling import round_shared_holds

        shared = round_shared_holds(slots)
        if shared:
            return SessionPlan(Device.NONE, False, "shared_holds_without_device",
                               detail={"shared": {h: g for h, g in shared.items()}})
        return SessionPlan(Device.NONE, True, None)

    if device is Device.COVERS:
        plan = find_cover_plan(slots, forced=forced_covers)
        if plan is None:
            return SessionPlan(Device.COVERS, False, "no_admissible_cover_plan")
        if plan.shared_after:
            # Defensive: a valid cover plan must leave no shared holds.
            return SessionPlan(Device.COVERS, False, "covers_left_shared_holds",
                               covers=plan)
        return SessionPlan(Device.COVERS, True, None, covers=plan)

    if device is Device.RELAY_NUMBERS:
        sched = relay_schedule(slots, send_beats)
        if not sched.feasible:
            return SessionPlan(
                Device.RELAY_NUMBERS, False,
                sched.reason or "relay_infeasible",
                relay=sched.as_dict(),
            )
        if observed_first_move is not None:
            check = validate_delivery(sched.start_beats, observed_first_move)
            if not check.ok:
                return SessionPlan(
                    Device.RELAY_NUMBERS, False,
                    check.reason or "delivery_invalid",
                    relay=sched.as_dict(), detail=check.as_dict(),
                )
        return SessionPlan(Device.RELAY_NUMBERS, True, None, relay=sched.as_dict())

    raise ValueError(f"unknown device {device!r}")


def assert_single_device(devices: Iterable[Device]) -> Device:
    """Return the one device used, rejecting a mixed session."""

    used = {d for d in devices if d is not Device.NONE}
    if len(used) > 1:
        raise ValueError(f"a session may use only one device, got {sorted(d.value for d in used)}")
    return used.pop() if used else Device.NONE
