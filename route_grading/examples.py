"""Concrete example routes used throughout the task and the test-suite.

These mirror the worked examples in the assignment:

* ``build_jia_route`` / ``build_yi_route`` -- the two "真线" (甲/乙) that are
  each individually fair and each solvable in two moves from either leading
  hand, but whose right-hold grid cell / shared start make a fair *same round*
  impossible unless the collision is removed.
* ``build_relay_pair`` -- the equal-grade relay pair with a shared observation
  sign (甲/乙) that the left hand must touch before its finish.
* ``arm_span_grading_example`` -- a route whose minimum hand changes depend on
  the climber's arm span (so the same route gets a different grade for
  different groups).
"""

from __future__ import annotations

from .model import Route, make_hold as H


# ---------------------------------------------------------------------------
# 甲 / 乙 : the "true lines" from the worked example.
# ---------------------------------------------------------------------------
def build_jia_route() -> Route:
    """甲线.

    Left start (0,0), right start (1,0); left finish (0,1), right finish (1,1).
    Every hold is restricted to exactly one hand.  Arm span 2 lets either hand
    lead first and finish in two hand changes.
    """

    return Route.from_holds(
        "jia",
        [
            H("jia.Ls", 0, 0, color="chalk", hands="L"),
            H("jia.Rs", 1, 0, color="chalk", hands="R"),
            H("jia.Lf", 0, 1, color="tape", hands="L"),
            H("jia.Rf", 1, 1, color="tape", hands="R"),
        ],
        name="甲线",
    )


JIA_START = ("jia.Ls", "jia.Rs")
JIA_FINISH = ("jia.Lf", "jia.Rf")


def build_yi_route() -> Route:
    """乙线.

    A second four-hold line on the **same local grid** as 甲.  Its four holds
    ("四个另外的具名岩点") sit on the same cells as 甲's but carry their own
    stable hold numbers -- "一格可装两枚不同编号的手点", so two climbs can
    share a grid cell without sharing a hold.  The left start is optionally
    the very same physical hold as 甲's left start (``jia.Ls``).
    """

    return Route.from_holds(
        "yi",
        [
            # left start (0,0) -- the *same hold id* as 甲's left start
            H("jia.Ls", 0, 0, color="chalk", hands="L"),
            H("yi.Rs", 1, 0, color="chalk", hands="R"),
            H("yi.Lf", 0, 1, color="tape", hands="L"),
            H("yi.Rf", 1, 1, color="tape", hands="R"),
        ],
        name="乙线",
    )


YI_START = ("jia.Ls", "yi.Rs")
YI_FINISH = ("yi.Lf", "yi.Rf")


def build_yi_route_private_start() -> Route:
    """乙线 with its *own* left start (no shared hold with 甲)."""

    return Route.from_holds(
        "yi",
        [
            H("yi.Ls", 0, 0, color="chalk", hands="L"),
            H("yi.Rs", 1, 0, color="chalk", hands="R"),
            H("yi.Lf", 0, 1, color="tape", hands="L"),
            H("yi.Rf", 1, 1, color="tape", hands="R"),
        ],
        name="乙线",
    )


YI_PRIVATE_START = ("yi.Ls", "yi.Rs")


# ---------------------------------------------------------------------------
# Relay observation pair (中继观察签)
# ---------------------------------------------------------------------------
def build_relay_pair() -> tuple[Route, Route, dict]:
    """Two equal-grade routes with a shared observation sign.

    Layout (local grid, ``y`` up)::

        y=2     Lf1(0,2)        Lf2(0,2)       different hold numbers
        y=1     JIA(-1,1)  R(0,1)  YI(1,1)     R = each route's own
                                               start/finish hold
        y=0   Ls1(-1,0)         Ls2(1,0)       named starts

    Right hand stays on the route's own (0,1) hold.  Left hand must touch
    either shared observation point (甲 at (-1,1) or 乙 at (1,1)) and only then
    touch its finish at (0,2).  Arm span 3.  Both required touches are scored,
    so the minimum is 2 hand changes (via 甲 or via 乙); a direct 1-move jump
    to the finish is **not** accepted because the observation point was missed.
    """

    shared = [
        H("obs.jia", -1, 1, color="observe", hands="L", page_id="obs-jia"),
        H("obs.yi", 1, 1, color="observe", hands="L", page_id="obs-yi"),
    ]
    route1 = Route.from_holds(
        "relay1",
        [
            H("r1.R", 0, 1, color="anchor", hands="R"),
            H("r1.Ls", -1, 0, color="start", hands="L"),
            H("r1.Lf", 0, 2, color="finish", hands="L"),
            *shared,
        ],
        name="等分线一",
    )
    route2 = Route.from_holds(
        "relay2",
        [
            H("r2.R", 0, 1, color="anchor", hands="R"),
            H("r2.Ls", 1, 0, color="start", hands="L"),
            H("r2.Lf", 0, 2, color="finish", hands="L"),
            *shared,
        ],
        name="等分线二",
    )
    meta = {
        "start1": ("r1.Ls", "r1.R"),
        "start2": ("r2.Ls", "r2.R"),
        "finish1": ("r1.Lf", "r1.R"),
        "finish2": ("r2.Lf", "r2.R"),
        "observation": frozenset({"obs.jia", "obs.yi"}),
        "shared": frozenset({"obs.jia", "obs.yi"}),
        "arm_span": 3,
    }
    return route1, route2, meta


# ---------------------------------------------------------------------------
# A small league instance: 4 lines, <= 10 holds, for 3 groups.
# ---------------------------------------------------------------------------
def build_league() -> dict:
    """Four lines over at most ten holds on a 2-wide grid.

    Layout (``y`` up), two holds per upper cell with *different* numbers::

        y=2     g  h        (cell (0,2),(1,2))
        y=1     c  d        (cell (0,1),(1,1))  -- two numbers c,d
                e  f        (same cells, other numbers e,f)
        y=0     a  b

    ``L1 = {a,b,c,d}``, ``L2 = {a,b,e,f}``, ``L3 = {c,d,g,h}``,
    ``L4 = {e,f,g,h}``.  Each line is a two-step climb with arm span 2.
    ``L1`` and ``L4`` are hold-disjoint (so they form a fair same round) even
    though ``L2`` and ``L3`` reuse cells of ``L1``/``L4`` under other names --
    two lines sharing a *cell* but not a *hold number* still may not share the
    round unless a warm-up overlap is declared.
    """

    a = H("a", 0, 0, color="s0", hands="L")
    b = H("b", 1, 0, color="s0", hands="R")
    c = H("c", 0, 1, color="t0", hands="L")
    d = H("d", 1, 1, color="t0", hands="R")
    e = H("e", 0, 1, color="t1", hands="L")
    f = H("f", 1, 1, color="t1", hands="R")
    g = H("g", 0, 2, color="t2", hands="L")
    h = H("h", 1, 2, color="t2", hands="R")
    return {
        "L1": (Route.from_holds("L1", [a, b, c, d]), ("a", "b"), ("c", "d")),
        "L2": (Route.from_holds("L2", [a, b, e, f]), ("a", "b"), ("e", "f")),
        "L3": (Route.from_holds("L3", [c, d, g, h]), ("c", "d"), ("g", "h")),
        "L4": (Route.from_holds("L4", [e, f, g, h]), ("e", "f"), ("g", "h")),
    }


# ---------------------------------------------------------------------------
# Arm-span-sensitive grading
# ---------------------------------------------------------------------------
def arm_span_grading_example() -> dict:
    """One route, two groups, different grades because arm span differs.

    The line needs a wide reach between the two hands' holds.  With arm span 1
    it cannot even hold the start pose; with arm span 2 it is solvable in two
    hand changes; with a larger span a one-move "shortcut" appears.  Hence the
    *same* route is graded differently for groups of different reach.
    """

    route = Route.from_holds(
        "span-demo",
        [
            H("s.Ls", 0, 0, color="a", hands="L"),
            H("s.Rs", 2, 0, color="a", hands="R"),
            H("s.Lf", 0, 1, color="b", hands="L"),
            H("s.Rf", 2, 1, color="b", hands="R"),
            H("s.Mid", 1, 1, color="c", hands="B"),
        ],
        name="臂展演示线",
    )
    return {
        "route": route,
        "start": ("s.Ls", "s.Rs"),
        "finish": ("s.Lf", "s.Rf"),
        "spans": [1, 2, 3, 4, 5],
    }
