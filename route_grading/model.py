"""Core bouldering-ascent model and grader.

A *route* is a set of holds.  Each hold has a stable number (``hold_id``), a
position in a local integer grid (``x`` rightwards, ``y`` upwards), a colour
name and the set of hands allowed to use it.  **Colour is descriptive only**:
two holds with the same colour name are different holds, and a hold is only
ever identified by its ``hold_id``.

A *pose* is ``(left_hold_id, right_hold_id)`` with two distinct holds.

Legal ascent
------------
From an ordered start pose a climber repeatedly moves **one** hand to another
*empty* hold on the same route (the hold the other hand grips is occupied and
may not be used; the hold the moving hand already grips is a no-op).  A move
is legal when

* the destination hold allows that hand;
* the destination height does not decrease: ``y_new >= y_old`` for the moving
  hand (points never go down);
* after the move the two hands are within the group's arm span:
  ``|dx| + |dy| <= arm_span`` (checked in every pose, i.e. while the other
  hand is held fixed).

The climb succeeds at the ordered finish pose ``(left_finish, right_finish)``
with two *distinct* top holds, each reached by the hand it is designated for.
The **grade** is the minimum number of hand changes (BFS depth).  When no
finish is reachable the grader reports the furthest reachable pose instead.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator

LEFT = "L"
RIGHT = "R"

#: A hold that both hands may use.
BOTH_HANDS = frozenset({LEFT, RIGHT})


def _norm_hands(hands: Iterable[str] | str) -> frozenset[str]:
    if isinstance(hands, str):
        if hands in (LEFT, RIGHT):
            return frozenset({hands})
        if hands in ("B", "BOTH", "BOTH_HANDS", "*"):
            return BOTH_HANDS
        raise ValueError(f"unknown hand specifier {hands!r}")
    out = frozenset(hands)
    if not out or not out <= {LEFT, RIGHT}:
        raise ValueError(f"hands must be a subset of {{L,R}}: {hands!r}")
    return out


@dataclass(frozen=True)
class Hold:
    """A single, stably-numbered hand hold."""

    hold_id: str
    x: int
    y: int
    color: str = ""
    hands: frozenset[str] = BOTH_HANDS
    #: The page that "opens" this hold in the case-file product, if any.
    page_id: str | None = None

    def allows(self, hand: str) -> bool:
        return hand in self.hands

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)

    def as_dict(self) -> dict:
        return {
            "hold_id": self.hold_id,
            "x": self.x,
            "y": self.y,
            "color": self.color,
            "hands": "".join(sorted(self.hands)),
            "page_id": self.page_id,
        }


def make_hold(
    hold_id: str,
    x: int,
    y: int,
    *,
    color: str = "",
    hands: Iterable[str] | str = BOTH_HANDS,
    page_id: str | None = None,
) -> Hold:
    return Hold(hold_id, int(x), int(y), color, _norm_hands(hands), page_id)


def manhattan(a: Hold, b: Hold) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y)


@dataclass
class Route:
    """An ordered-free set of holds that make up one boulder problem."""

    route_id: str
    holds: dict[str, Hold]
    #: Human-readable route plate name; never used for hold identity.
    name: str = ""

    def __post_init__(self) -> None:
        if not self.holds:
            raise ValueError("a route needs at least one hold")

    @classmethod
    def from_holds(cls, route_id: str, holds: Iterable[Hold], *, name: str = "") -> "Route":
        table: dict[str, Hold] = {}
        for hold in holds:
            if hold.hold_id in table:
                raise ValueError(f"duplicate hold_id {hold.hold_id!r} in route {route_id!r}")
            table[hold.hold_id] = hold
        return cls(route_id=route_id, holds=table, name=name)

    def get(self, hold_id: str) -> Hold:
        try:
            return self.holds[hold_id]
        except KeyError:
            raise KeyError(f"route {self.route_id!r} has no hold {hold_id!r}") from None

    def __contains__(self, hold_id: object) -> bool:
        return hold_id in self.holds

    def hold_ids(self) -> frozenset[str]:
        return frozenset(self.holds)

    def shares_holds_with(self, other: "Route") -> frozenset[str]:
        """Shared **hold numbers**.

        Colour names are irrelevant: two holds that merely share a colour or a
        grid cell are distinct unless their ``hold_id`` matches.
        """

        return self.hold_ids() & other.hold_ids()

    def restricted(self, removed: Iterable[str]) -> "Route":
        removed = set(removed)
        return Route(
            route_id=self.route_id,
            name=self.name,
            holds={k: v for k, v in self.holds.items() if k not in removed},
        )


@dataclass(frozen=True)
class Step:
    hand: str
    from_id: str
    to_id: str
    pose: tuple[str, str]

    def as_dict(self) -> dict:
        return {
            "hand": self.hand,
            "from": self.from_id,
            "to": self.to_id,
            "pose": list(self.pose),
        }


@dataclass
class GradeResult:
    route_id: str
    arm_span: int
    start: tuple[str, str]
    finish: tuple[str, str]
    gradable: bool
    steps: int | None
    path: tuple[Step, ...]
    final_pose: tuple[str, str] | None
    reachable_states: int
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "arm_span": self.arm_span,
            "start": list(self.start),
            "finish": list(self.finish),
            "gradable": self.gradable,
            "grade": self.steps,
            "path": [s.as_dict() for s in self.path],
            "final_pose": list(self.final_pose) if self.final_pose else None,
            "reachable_states": self.reachable_states,
            "reason": self.reason,
        }


#: ``required`` maps a hand to one or more sets of hold ids; that hand must at
#: some point occupy a hold from *each* set before the finish is accepted.  A
#: typical use is the relay observation sign: the left hand must touch one of
#: {甲,乙} before touching the finish hold.
RequiredVisits = dict[str, list[frozenset[str]]]


def _req_mask_for(
    required: RequiredVisits, hand: str, hold_id: str, mask: int,
) -> int:
    """Return ``mask`` with bits for satisfied ``hand`` requirements set."""

    sets = required.get(hand)
    if not sets:
        return mask
    bit = 1
    for req in sets:
        if not (mask & bit) and hold_id in req:
            mask |= bit
        bit <<= 1
    return mask


def _req_total(required: RequiredVisits) -> int:
    return sum(len(v) for v in required.values())


def validate_poses(
    route: Route,
    start: tuple[str, str],
    finish: tuple[str, str],
) -> str | None:
    """Return a reason string when the start/finish are structurally invalid."""

    sl, sr = start
    gl, gr = finish
    for hid in (sl, sr, gl, gr):
        if hid not in route:
            return f"missing_hold:{hid}"
    if sl == sr:
        return "start_hands_on_same_hold"
    if gl == gr:
        return "finish_hands_on_same_hold"
    if not route.get(sl).allows(LEFT):
        return f"start_left_hold_disallows_left:{sl}"
    if not route.get(sr).allows(RIGHT):
        return f"start_right_hold_disallows_right:{sr}"
    if not route.get(gl).allows(LEFT):
        return f"finish_left_hold_disallows_left:{gl}"
    if not route.get(gr).allows(RIGHT):
        return f"finish_right_hold_disallows_right:{gr}"
    return None


def _neighbours(
    route: Route,
    pose: tuple[str, str],
    arm_span: int,
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(hand, destination_id, new_pose)`` legal moves from ``pose``."""

    l, r = pose
    lh, rh = route.get(l), route.get(r)
    for hid, hold in route.holds.items():
        if hid == l or hid == r:
            continue
        # Move the left hand.
        if hold.allows(LEFT) and hold.y >= lh.y and manhattan(hold, rh) <= arm_span:
            yield LEFT, hid, (hid, r)
        # Move the right hand.
        if hold.allows(RIGHT) and hold.y >= rh.y and manhattan(lh, hold) <= arm_span:
            yield RIGHT, hid, (l, hid)


def grade(
    route: Route,
    start: tuple[str, str],
    finish: tuple[str, str],
    arm_span: int,
    *,
    removed: Iterable[str] = (),
    required: RequiredVisits | None = None,
) -> GradeResult:
    """Find a minimum-hand-change legal ascent.

    ``removed`` hides holds (for example a page moved out of the range, or a
    shared hold covered for this route) before grading.

    ``required`` adds *formal* touch requirements (for a relay observation
    sign): each listed hand must occupy a hold from every one of its hold-set
    requirements before the finish pose counts.  Touching is part of the
    scored climb, so the depth still counts every hand change.
    """

    if removed:
        route = route.restricted(removed)
    start = (str(start[0]), str(start[1]))
    finish = (str(finish[0]), str(finish[1]))
    required = required or {}
    full_mask = (1 << _req_total(required)) - 1

    reason = validate_poses(route, start, finish)
    if reason is not None:
        return GradeResult(
            route_id=route.route_id, arm_span=arm_span, start=start, finish=finish,
            gradable=False, steps=None, path=(), final_pose=None,
            reachable_states=0, reason=reason,
        )

    # The start pose itself must fit the arm span.
    if manhattan(route.get(start[0]), route.get(start[1])) > arm_span:
        return GradeResult(
            route_id=route.route_id, arm_span=arm_span, start=start, finish=finish,
            gradable=False, steps=None, path=(), final_pose=start,
            reachable_states=1, reason="start_exceeds_arm_span",
        )

    start_mask = _req_mask_for(required, LEFT, start[0], 0)
    start_mask = _req_mask_for(required, RIGHT, start[1], start_mask)
    start_state = (start, start_mask)

    parent: dict[tuple[tuple[str, str], int], tuple[tuple[tuple[str, str], int], Step] | None] = {
        start_state: None
    }
    depth: dict[tuple[tuple[str, str], int], int] = {start_state: 0}
    queue: deque[tuple[tuple[str, str], int]] = deque([start_state])

    def goal(state_mask: int, pose: tuple[str, str]) -> bool:
        return pose == finish and state_mask == full_mask

    found = goal(start_mask, start)

    while queue and not found:
        pose, mask = queue.popleft()
        d = depth[(pose, mask)]
        for hand, dest, new_pose in _neighbours(route, pose, arm_span):
            new_mask = _req_mask_for(required, hand, dest, mask)
            new_state = (new_pose, new_mask)
            if new_state in parent:
                continue
            parent[new_state] = ((pose, mask), Step(hand, pose[0] if hand == LEFT else pose[1], dest, new_pose))
            depth[new_state] = d + 1
            if goal(new_mask, new_pose):
                found = True
                break
            queue.append(new_state)

    reachable = len(parent)
    goal_state = (finish, full_mask)

    if found:
        path: list[Step] = []
        node = goal_state
        while parent[node] is not None:
            prev, step = parent[node]
            path.append(step)
            node = prev
        path.reverse()
        return GradeResult(
            route_id=route.route_id, arm_span=arm_span, start=start, finish=finish,
            gradable=True, steps=depth[goal_state], path=tuple(path),
            final_pose=finish, reachable_states=reachable, reason=None,
        )

    furthest = _furthest_pose(route, finish, {p for (p, _m) in parent})
    return GradeResult(
        route_id=route.route_id, arm_span=arm_span, start=start, finish=finish,
        gradable=False, steps=None, path=(), final_pose=furthest,
        reachable_states=reachable, reason="no_path",
    )


def _furthest_pose(
    route: Route,
    finish: tuple[str, str],
    parent: dict[tuple[str, str], tuple[tuple[str, str], Step] | None],
) -> tuple[str, str]:
    """Deterministically pick the "last pose you can reach".

    Order of preference: highest combined height, then highest lower hand,
    then closest (Manhattan) to the finish pose, then smallest hold numbers.
    """

    gl, gr = finish
    best: tuple[tuple, tuple[str, str]] | None = None
    for pose in parent:
        lh, rh = route.get(pose[0]), route.get(pose[1])
        goal_dist = manhattan(lh, route.get(gl)) + manhattan(rh, route.get(gr))
        key = (
            lh.y + rh.y,                 # maximise
            min(lh.y, rh.y),             # maximise
            -goal_dist,                  # minimise distance
        )
        # For the final tie-break we want smallest ids, so invert by lexicographic
        # comparison done manually below.
        if best is None or key > best[0] or (
            key == best[0] and pose < best[1]
        ):
            best = (key, pose)
    assert best is not None
    return best[1]


def min_steps(
    route: Route,
    start: tuple[str, str],
    finish: tuple[str, str],
    arm_span: int,
    *,
    removed: Iterable[str] = (),
    required: RequiredVisits | None = None,
) -> int | None:
    result = grade(route, start, finish, arm_span, removed=removed, required=required)
    return result.steps if result.gradable else None


@dataclass(frozen=True)
class ClimbPath:
    """One concrete minimum-length climb.

    ``poses[i]`` is the hand pose after ``i`` moves; ``poses[0]`` is the start
    pose and ``poses[-1]`` the finish pose.  ``len(poses) == steps + 1``.
    """

    steps: tuple[Step, ...]
    poses: tuple[tuple[str, str], ...]

    def grips_at(self, beat: int) -> tuple[str, str]:
        """Hands gripped at ``beat`` (clamped to the final pose once done)."""

        if beat >= len(self.poses):
            return self.poses[-1]
        return self.poses[beat]


def _state_graph(
    route: Route,
    start: tuple[str, str],
    finish: tuple[str, str],
    arm_span: int,
    required: RequiredVisits,
):
    full_mask = (1 << _req_total(required)) - 1
    start_mask = _req_mask_for(required, LEFT, start[0], 0)
    start_mask = _req_mask_for(required, RIGHT, start[1], start_mask)
    start_state = (start, start_mask)
    goal_state = (finish, full_mask)

    edges: dict[tuple[tuple[str, str], int], list[tuple[str, str, tuple[tuple[str, str], int]]]] = {}
    fdist: dict[tuple[tuple[str, str], int], int] = {start_state: 0}
    queue: deque[tuple[tuple[str, str], int]] = deque([start_state])
    while queue:
        state = queue.popleft()
        pose, mask = state
        d = fdist[state]
        outs: list[tuple[str, str, tuple[tuple[str, str], int]]] = []
        for hand, dest, new_pose in _neighbours(route, pose, arm_span):
            new_mask = _req_mask_for(required, hand, dest, mask)
            nxt = (new_pose, new_mask)
            outs.append((hand, dest, nxt))
            if nxt not in fdist:
                fdist[nxt] = d + 1
                queue.append(nxt)
        edges[state] = outs
    return start_state, goal_state, edges, fdist


def enumerate_min_paths(
    route: Route,
    start: tuple[str, str],
    finish: tuple[str, str],
    arm_span: int,
    *,
    removed: Iterable[str] = (),
    required: RequiredVisits | None = None,
    limit: int = 200_000,
) -> list[ClimbPath]:
    """Every distinct minimum-length legal climb, as :class:`ClimbPath`.

    Returns ``[]`` when the finish is unreachable.  Used by the concurrency
    analysis, which must quantify over *all* paths a self-selecting climber
    may take (never just one "paper" path).
    """

    if removed:
        route = route.restricted(removed)
    start = (str(start[0]), str(start[1]))
    finish = (str(finish[0]), str(finish[1]))
    required = required or {}
    if validate_poses(route, start, finish) is not None:
        return []
    if manhattan(route.get(start[0]), route.get(start[1])) > arm_span:
        return []

    start_state, goal_state, edges, fdist = _state_graph(
        route, start, finish, arm_span, required
    )
    if goal_state not in fdist:
        return []
    L = fdist[goal_state]

    # Backward distances on the reversed graph.
    reverse: dict[tuple[tuple[str, str], int], list[tuple[tuple[str, str], int]]] = {}
    for s, outs in edges.items():
        for _hand, _dest, nxt in outs:
            if fdist.get(nxt, 10**9) != fdist[s] + 1:
                continue
            reverse.setdefault(nxt, []).append(s)
    bdist: dict[tuple[tuple[str, str], int], int] = {goal_state: 0}
    bq: deque[tuple[tuple[str, str], int]] = deque([goal_state])
    while bq:
        s = bq.popleft()
        for prev in reverse.get(s, []):
            if prev not in bdist:
                bdist[prev] = bdist[s] + 1
                bq.append(prev)

    on_path = {
        s for s in fdist
        if s in bdist and fdist[s] + bdist[s] == L
    }

    results: list[ClimbPath] = []

    def dfs(state, steps: list[Step]):
        if len(results) >= limit:
            return
        if state == goal_state:
            poses = [start] + [st.pose for st in steps]
            results.append(ClimbPath(tuple(steps), tuple(poses)))
            return
        for hand, dest, nxt in edges.get(state, []):
            if nxt in on_path and fdist[nxt] == fdist[state] + 1:
                steps.append(
                    Step(hand, state[0][0] if hand == LEFT else state[0][1], dest, nxt[0])
                )
                dfs(nxt, steps)
                steps.pop()

    dfs(start_state, [])
    return results
