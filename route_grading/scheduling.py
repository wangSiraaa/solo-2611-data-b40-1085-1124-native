"""Multi-group scheduling and concurrency-safety for graded routes.

A competition desk runs several groups at once.  Each *slot* is one group
climbing one graded route with that group's actual arm span.  Two mechanisms
can keep simultaneous climbers off the same physical hold:

**Lane covers (分道罩)**
    Before the round starts, a shared hold may be covered *for one line*; the
    cover is permanent for the session.  A cover is only admissible when the
    covered line still has its **original** minimum hand changes (we never
    re-grade the problem at the longer, post-cover length) and the lines in a
    round end up sharing no hold at all ("同轮不得共用岩点").

**Relay numbers (临场号)**
    No covers.  An authorized group gets a start number at a beat; the relay
    station hands over at most one number per beat, and a number only arrives
    *before* the group's real move.  A free group keeps choosing its own hands
    ("不能命令自由组改手"), so safety must hold for **every** minimum-length
    path it could pick and every simultaneous ordering.  A bare "perfect
    replay" on paper is not enough.

Two climbers conflict when, at the same beat, their gripped-or-moving hand
sets share a physical hold: because neither can see the other's not-yet-made
choice, a *possible* collision among admissible minimum paths is unsafe.
Already-gripped shared holds stay occupied ("已抓的公共点须按实际占用"); a
later number does not turn a gripped hold back into a free one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from typing import Iterable, Iterator

from .model import (
    ClimbPath,
    GradeResult,
    RequiredVisits,
    Route,
    enumerate_min_paths,
    grade,
)


# ---------------------------------------------------------------------------
# A slot = one group's graded climb on one route.
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    group_id: str
    route: Route
    start: tuple[str, str]
    finish: tuple[str, str]
    arm_span: int
    required: RequiredVisits = field(default_factory=dict)
    covered: frozenset[str] = frozenset()

    @property
    def effective(self) -> Route:
        return self.route.restricted(self.covered) if self.covered else self.route

    def with_cover(self, hold_id: str) -> "Slot":
        return Slot(
            self.group_id, self.route, self.start, self.finish, self.arm_span,
            dict(self.required), self.covered | {hold_id},
        )

    def grade(self) -> GradeResult:
        return grade(
            self.effective, self.start, self.finish, self.arm_span,
            required=self.required,
        )

    def original_grade(self) -> GradeResult:
        return grade(
            self.route, self.start, self.finish, self.arm_span,
            required=self.required,
        )

    def steps(self) -> int | None:
        g = self.grade()
        return g.steps if g.gradable else None

    def paths(self) -> list[ClimbPath]:
        return enumerate_min_paths(
            self.effective, self.start, self.finish, self.arm_span,
            required=self.required,
        )

    def footprint(self) -> frozenset[str]:
        return self.effective.hold_ids()


def shared_hold_map(slots: Iterable[Slot]) -> dict[str, list[str]]:
    """``hold_id -> [group_id, ...]`` for holds used by more than one slot."""

    holders: dict[str, list[str]] = {}
    for slot in slots:
        for hid in slot.footprint():
            holders.setdefault(hid, []).append(slot.group_id)
    return {h: g for h, g in holders.items() if len(g) > 1}


# ---------------------------------------------------------------------------
# Same-round hold disjointness (the strict "同轮不得共用岩点" rule).
# ---------------------------------------------------------------------------
def round_shared_holds(slots: Iterable[Slot]) -> dict[str, list[str]]:
    return shared_hold_map(slots)


def round_is_disjoint(slots: Iterable[Slot]) -> bool:
    return not round_shared_holds(slots)


def _grades_preserved(slots: list[Slot]) -> bool:
    for slot in slots:
        orig = slot.original_grade()
        now = slot.grade()
        if not orig.gradable or not now.gradable or orig.steps != now.steps:
            return False
    return True


# ---------------------------------------------------------------------------
# Cover planning.
# ---------------------------------------------------------------------------
@dataclass
class CoverPlan:
    covers: dict[str, frozenset[str]]  # group_id -> covered hold ids
    shared_after: dict[str, list[str]]

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.covers.values())

    def as_dict(self) -> dict:
        return {
            "covers": {g: sorted(h) for g, h in sorted(self.covers.items())},
            "size": self.size,
        }


def find_cover_plan(
    slots: list[Slot],
    *,
    forced: dict[str, Iterable[str]] | None = None,
    max_plans: int = 5000,
) -> CoverPlan | None:
    """Find a minimal permanent lane-cover plan for one round.

    Every group keeps its **original** minimum hand changes; after covering,
    the round's effective hold sets are pairwise disjoint.  ``forced`` pins
    covers that must be applied (used to show that removing an option breaks
    completion).  Returns ``None`` when no admissible plan exists.
    """

    forced = {g: set(h) for g, h in (forced or {}).items()}
    group_ids = [s.group_id for s in slots]

    # Baseline must be gradable before we touch anything.  Work on copies so
    # the caller's slots are never mutated (they may be reused for a relay
    # schedule afterwards).
    if any(not s.original_grade().gradable for s in slots):
        return None

    def clone(s: Slot, covered: Iterable[str]) -> Slot:
        return Slot(
            s.group_id, s.route, s.start, s.finish, s.arm_span,
            dict(s.required), frozenset(covered),
        )

    base_covered: dict[str, set[str]] = {g: set(forced.get(g, set())) for g in group_ids}
    for slot in slots:
        if clone(slot, base_covered[slot.group_id]).steps() != slot.original_grade().steps:
            return None

    solutions: list[dict[str, frozenset[str]]] = []

    def recurse(covered: dict[str, set[str]]) -> None:
        if len(solutions) >= max_plans:
            return
        eff = [clone(s, covered[s.group_id]) for s in slots]
        contested = shared_hold_map(eff)
        if not contested:
            if _grades_preserved(eff):
                solutions.append({g: frozenset(v) for g, v in covered.items()})
            return
        # Take the first contested hold (deterministic: sorted).
        hold = sorted(contested)[0]
        holders = contested[hold]
        # Options: exactly one holder keeps it, or nobody keeps it.
        for keeper in holders + [None]:
            new = {g: set(v) for g, v in covered.items()}
            for g in holders:
                if g != keeper:
                    new[g].add(hold)
            # prune: drop obviously broken branches early
            ok = True
            for s in slots:
                if s.group_id in holders and s.group_id != keeper:
                    if clone(s, new[s.group_id]).steps() != s.original_grade().steps:
                        ok = False
                        break
            if ok:
                recurse(new)

    recurse(base_covered)

    if not solutions:
        return None

    def key(plan: dict[str, frozenset[str]]):
        size = sum(len(v) for v in plan.values())
        return (size, tuple((g, tuple(sorted(v))) for g, v in sorted(plan.items())))

    best = min(solutions, key=key)
    eff = [clone(s, best[s.group_id]) for s in slots]
    return CoverPlan(covers=best, shared_after=shared_hold_map(eff))


# ---------------------------------------------------------------------------
# Relay-number occupancy / safety.
# ---------------------------------------------------------------------------
def _pose_at(path: ClimbPath, start_beat: int, beat: int) -> frozenset[str]:
    """Holds occupied by this climber during ``beat``.

    A climber that has not yet received its number is not on the wall.  On the
    beat of a move we count **both** the pose it starts from and the pose it
    ends in: under an arbitrary same-beat ordering the other climber might grab
    a hold before this one has released it ("同拍先后", "已抓的公共点须按实际
    占用").  After the last move the finish pose stays gripped.  This is the
    conservative, adversarial reading: a held shared hold is never treated as
    free just because someone might let go in time.
    """

    L = len(path.poses) - 1
    if beat < start_beat:
        return frozenset()
    k = beat - start_beat
    if k >= L:
        return frozenset(path.poses[-1])
    return frozenset(path.poses[k]) | frozenset(path.poses[k + 1])


def paths_conflict(
    pa: ClimbPath, sa: int, pb: ClimbPath, sb: int,
) -> bool:
    """True when some beat has both climbers gripping a shared hold."""

    end = max(sa + len(pa.poses) - 1, sb + len(pb.poses) - 1)
    for beat in range(0, end + 1):
        if _pose_at(pa, sa, beat) & _pose_at(pb, sb, beat):
            return True
    return False


def slots_safe_at_beats(
    slot_a: Slot, sa: int, slot_b: Slot, sb: int,
) -> tuple[bool, str | None]:
    """Adversarial safety: safe iff *every* pair of min paths is conflict-free.

    Returns ``(safe, witness)``; when unsafe, ``witness`` explains which pair of
    paths collides (a path on paper, never assumed to be the chosen one).
    """

    paths_a = slot_a.paths()
    paths_b = slot_b.paths()
    if not paths_a or not paths_b:
        return False, f"ungradable:{slot_a.group_id if not paths_a else slot_b.group_id}"
    for pa in paths_a:
        for pb in paths_b:
            if paths_conflict(pa, sa, pb, sb):
                return False, "possible_collision"
    return True, None


@dataclass
class RelayResult:
    feasible: bool
    start_beats: dict[str, int] | None
    reason: str | None
    detail: dict = field(default_factory=dict)
    #: Actual first-move beats derived from arrival beats (arrival + 1).
    first_move_beats: dict[str, int] | None = None

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "start_beats": self.start_beats,
            "first_move_beats": self.first_move_beats,
            "reason": self.reason,
            "detail": self.detail,
        }


def relay_schedule(
    slots: list[Slot],
    send_beats: Iterable[int],
) -> RelayResult:
    """Stagger relay numbers using at most one number per beat.

    ``send_beats`` are the beats at which a number may arrive.  Each group
    receives a distinct arrival beat ``a``; because a number may only arrive
    *before* the group's real move ("只能在组真实出手前...抵达"), the group's
    first move happens at ``a + 1`` and safety is evaluated there.  Feasible
    iff some assignment is conflict-free for every admissible minimum path
    ("在任意允许的自选、同拍先后下保持原评分且保证所有签约组可完赛").
    Grades are preserved by construction: a number changes *when* a group
    starts, never *what* it climbs.
    """

    send_beats = sorted(set(int(b) for b in send_beats))
    ids = [s.group_id for s in slots]
    if any(not s.original_grade().gradable for s in slots):
        bad = [s.group_id for s in slots if not s.original_grade().gradable]
        return RelayResult(False, None, "ungradable_group", {"groups": bad})
    if len(send_beats) < len(slots):
        return RelayResult(
            False, None, "too_few_send_beats",
            {"needed": len(slots), "available": len(send_beats)},
        )

    tried = []
    for combo in permutations(send_beats, len(slots)):
        assignment = dict(zip(ids, combo))
        first_move = {g: b + 1 for g, b in assignment.items()}
        ok = True
        for i in range(len(slots)):
            for j in range(i + 1, len(slots)):
                safe, why = slots_safe_at_beats(
                    slots[i], first_move[slots[i].group_id],
                    slots[j], first_move[slots[j].group_id],
                )
                if not safe:
                    ok = False
                    tried.append({"assignment": assignment, "conflict": why})
                    break
            if not ok:
                break
        if ok:
            return RelayResult(True, assignment, None, {}, first_move)
    return RelayResult(
        False, None, "no_conflict_free_staggering",
        {"send_beats": send_beats, "attempts": tried},
    )


def required_gap(slot_a: Slot, slot_b: Slot, *, limit: int = 12) -> int | None:
    """Smallest beat gap ``b - a`` making every min-path pair conflict-free.

    ``None`` when even a large gap cannot separate them (e.g. permanently
    shared finish holds).
    """

    for gap in range(0, limit + 1):
        safe, _ = slots_safe_at_beats(slot_a, 0, slot_b, gap)
        if safe:
            return gap
    return None


# ---------------------------------------------------------------------------
# Relay *delivery*: a number must really arrive before the group's real move.
# ---------------------------------------------------------------------------
@dataclass
class DeliveryRecord:
    group_id: str
    number: int
    arrival_beat: int
    first_move_beat: int | None

    def valid(self) -> bool:
        return self.first_move_beat is not None and self.arrival_beat < self.first_move_beat


@dataclass
class DeliveryCheck:
    ok: bool
    reason: str | None
    records: list[DeliveryRecord] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "records": [
                {
                    "group_id": r.group_id,
                    "number": r.number,
                    "arrival_beat": r.arrival_beat,
                    "first_move_beat": r.first_move_beat,
                    "valid": r.valid(),
                }
                for r in self.records
            ],
        }


def validate_delivery(
    assignments: dict[str, int],
    first_move_beats: dict[str, int | None],
) -> DeliveryCheck:
    """Check that each relay number arrives strictly before that group's real
    first move, and that no two numbers share a beat.

    This is the "接收签必须真实到位" / "不能回填成已分开" rule.  A number that
    arrives on or after the group has already moved cannot be used to claim the
    two groups were *separated* -- the move already happened, and you may not
    retroactively rewrite the observed first-move beat.  If the second number
    is pushed back until both groups have already moved simultaneously, the
    delivery is invalid: you cannot backfill separation.
    """

    records: list[DeliveryRecord] = []
    seen_beats: set[int] = set()
    for group_id, beat in assignments.items():
        if beat in seen_beats:
            return DeliveryCheck(
                False, "two_numbers_same_beat",
                records + [DeliveryRecord(group_id, -1, beat, first_move_beats.get(group_id))],
            )
        seen_beats.add(beat)
        rec = DeliveryRecord(
            group_id=group_id,
            number=0,
            arrival_beat=beat,
            first_move_beat=first_move_beats.get(group_id),
        )
        records.append(rec)

    for rec in records:
        if rec.first_move_beat is None:
            return DeliveryCheck(False, f"group_never_moved:{rec.group_id}", records)
        if not rec.valid():
            return DeliveryCheck(
                False,
                f"number_arrived_too_late:{rec.group_id}",
                records,
            )
    return DeliveryCheck(True, None, records)


def relay_with_delivery(
    slots: list[Slot],
    send_beats: Iterable[int],
    first_move_beats: dict[str, int | None],
) -> dict:
    """Combine conflict-free stagger analysis with real delivery validation.

    ``first_move_beats`` records when each group *actually* first moved (a
    fact observed by the referee), used to reject backfilled separation.
    """

    schedule = relay_schedule(slots, send_beats)
    if not schedule.feasible:
        return {"schedule": schedule.as_dict(), "delivery": None, "safe": False}
    check = validate_delivery(schedule.start_beats, first_move_beats)
    return {
        "schedule": schedule.as_dict(),
        "delivery": check.as_dict(),
        "safe": bool(check.ok),
    }
