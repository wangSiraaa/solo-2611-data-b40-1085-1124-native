"""Round / lane assignment for several groups.

Rules encoded here ("赛事台要同时给几组安排试攀"):

* Every group starts from its own registration (签到) turn and uses a
  **different** route.
* Specified groups must face a route with the **same shortest number of hand
  changes** ("指定组间须面对同一最短步数的线路").  This is compared using each
  group's **actual arm span**, not a nominal one.
* Same-round groups may not share a physical hold, even if the route plates
  have different names ("同轮不得共用岩点，哪怕线路牌的名字不同").
* Once a group has started climbing, its actual hand positions and the route
  it occupies are locked; other groups may still switch to another *listed*
  backup route ("已有一组开攀后要锁定实际手位及其占用线路，其他组仍可换到已列的备线").
* One warm-up board may legitimately be the shared overlap of two lines
  ("有的交叉赛将一块热身岩板算作两条线的重叠区").  Such a declared overlap is
  not a violation; only *undeclared* shared holds are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Iterable

from .model import Route, grade
from .scheduling import Slot


@dataclass(frozen=True)
class Group:
    group_id: str
    arm_span: int
    signup_turn: int


@dataclass(frozen=True)
class RouteEntry:
    """A listed route available to a round."""

    route_id: str
    route: Route
    start: tuple[str, str]
    finish: tuple[str, str]
    #: Holds that may be shared with another line as a declared warm-up
    #: overlap ("热身岩板重叠区").
    declared_overlap: frozenset[str] = frozenset()

    def slot_for(self, group: Group) -> Slot:
        return Slot(group.group_id, self.route, self.start, self.finish, group.arm_span)

    def steps_for(self, group: Group) -> int | None:
        result = grade(self.route, self.start, self.finish, group.arm_span)
        return result.steps if result.gradable else None


@dataclass
class Assignment:
    by_group: dict[str, str]          # group_id -> route_id
    slots: dict[str, Slot]
    grades: dict[str, int | None]
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "by_group": self.by_group,
            "grades": self.grades,
            "reason": self.reason,
        }


def _overlap_allowed(a: RouteEntry, b: RouteEntry, shared: frozenset[str]) -> bool:
    allowed = a.declared_overlap & b.declared_overlap
    return shared <= allowed


def assign_round(
    groups: list[Group],
    routes: list[RouteEntry],
    *,
    equal_grade_pairs: Iterable[tuple[str, str]] = (),
    locked: dict[str, str] | None = None,
) -> Assignment:
    """Find a valid route assignment for one round.

    ``equal_grade_pairs`` lists group-id pairs that must face the same
    shortest hand-change count.  ``locked`` fixes group -> route assignments
    already in progress; the search only varies the unlocked groups.
    """

    locked = dict(locked or {})
    equal_pairs = list(equal_grade_pairs)
    by_id = {g.group_id: g for g in groups}
    route_by_id = {r.route_id: r for r in routes}

    # All groups get a distinct route (signup order just fixes who registers
    # first; we enumerate over route permutations restricted to >= len).
    if len(routes) < len(groups):
        return Assignment({}, {}, {}, "not_enough_routes")

    # Locked groups must reference listed routes and be respected.
    for gid, rid in locked.items():
        if gid not in by_id:
            return Assignment({}, {}, {}, f"locked_unknown_group:{gid}")
        if rid not in route_by_id:
            return Assignment({}, {}, {}, f"locked_unknown_route:{rid}")

    unlocked = [g for g in groups if g.group_id not in locked]

    best_failure = "no_valid_assignment"
    for choice in product(routes, repeat=len(unlocked)):
        rid_for = dict(locked)
        for g, r in zip(unlocked, choice):
            rid_for[g.group_id] = r.route_id
        chosen = list(rid_for.values())
        if len(set(chosen)) != len(chosen):
            continue  # each group must use a different route
        slots = {g.group_id: route_by_id[rid_for[g.group_id]].slot_for(g) for g in groups}
        grades = {g.group_id: slots[g.group_id].steps() for g in groups}
        if any(v is None for v in grades.values()):
            best_failure = "some_group_ungradable"
            continue
        # Equal-grade constraint, by actual arm span.
        bad = False
        for a, b in equal_pairs:
            if grades[a] != grades[b]:
                bad = True
                best_failure = f"grade_mismatch:{a}:{b}"
                break
        if bad:
            continue
        # Same-round hold disjointness, allowing declared overlaps.
        bad = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                ga, gb = groups[i], groups[j]
                ra, rb = route_by_id[rid_for[ga.group_id]], route_by_id[rid_for[gb.group_id]]
                shared = slots[ga.group_id].footprint() & slots[gb.group_id].footprint()
                if shared and not _overlap_allowed(ra, rb, shared):
                    bad = True
                    best_failure = "undeclared_shared_hold"
                    break
            if bad:
                break
        if bad:
            continue
        return Assignment(rid_for, slots, grades, None)

    return Assignment({}, {}, {}, best_failure)


def assign_by_signup(
    groups: list[Group],
    routes: list[RouteEntry],
    *,
    equal_grade_pairs: Iterable[tuple[str, str]] = (),
    locked: dict[str, str] | None = None,
) -> Assignment:
    """Like :func:`assign_round` but reports the deterministic signup-ordered
    choice when several are valid (stable tie-break on route id order)."""

    ordered_groups = sorted(groups, key=lambda g: (g.signup_turn, g.group_id))
    ordered_routes = sorted(routes, key=lambda r: r.route_id)
    return assign_round(
        ordered_groups, ordered_routes,
        equal_grade_pairs=equal_grade_pairs, locked=locked,
    )


@dataclass
class LaneLock:
    """Tracks in-progress groups so their route + hand positions stay fixed."""

    started: dict[str, str] = field(default_factory=dict)  # group -> route

    def start(self, group_id: str, route_id: str) -> None:
        if group_id in self.started:
            raise ValueError(f"group {group_id!r} already started")
        self.started[group_id] = route_id

    @property
    def locked_map(self) -> dict[str, str]:
        return dict(self.started)
