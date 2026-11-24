"""End-to-end demonstrations of the route-grading layer.

Run with ``python3 scripts/demo_routes.py``.  Prints, for each scenario in the
task, the computed grade, the safe scheduling decision, and the reasoning.
Nothing here reads PDFs or images.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from route_grading.model import (  # noqa: E402
    grade, enumerate_min_paths, make_hold as H, Route,
)
from route_grading.examples import (  # noqa: E402
    build_jia_route, JIA_START, JIA_FINISH,
    build_yi_route, build_yi_route_private_start, YI_START, YI_FINISH,
    YI_PRIVATE_START, build_relay_pair, arm_span_grading_example,
)
from route_grading.scheduling import (  # noqa: E402
    Slot, find_cover_plan, relay_schedule, slots_safe_at_beats,
    round_shared_holds, required_gap, validate_delivery,
)
from route_grading.assignment import (  # noqa: E402
    Group, RouteEntry, assign_round, LaneLock,
)

SCENARIOS: list[dict] = []


def show(title: str, payload: dict) -> None:
    SCENARIOS.append({"title": title, "payload": payload})
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def path_summary(paths):
    return [
        {"steps": [(s.hand, s.from_id, s.to_id) for s in p.steps]}
        for p in paths
    ]


def main() -> int:
    # 1. 甲/乙 true lines: each two steps, either hand first.
    jia = build_jia_route()
    res = grade(jia, JIA_START, JIA_FINISH, 2)
    show("甲线分级（臂展2）", {
        "grade": res.steps,
        "paths": path_summary(enumerate_min_paths(jia, JIA_START, JIA_FINISH, 2)),
    })

    # 2. Removing the open right top page -> 甲 has no route.
    removed = grade(jia, JIA_START, JIA_FINISH, 2, removed=["jia.Rf"])
    show("甲线移出开放右顶点页", {
        "gradable": removed.gradable,
        "reason": removed.reason,
    })

    # 3. Shared left start blocks a same round; private start is fair.
    g = [Group("A", 2, 1), Group("B", 2, 2)]
    yi_shared = build_yi_route()
    blocked = assign_round(
        g,
        [RouteEntry("甲", jia, JIA_START, JIA_FINISH),
         RouteEntry("乙", yi_shared, YI_START, YI_FINISH)],
    )
    yi_priv = build_yi_route_private_start()
    fair = assign_round(
        g,
        [RouteEntry("甲", jia, JIA_START, JIA_FINISH),
         RouteEntry("乙", yi_priv, YI_PRIVATE_START, YI_FINISH)],
    )
    show("甲/乙同轮：共用左起点 vs 私有左起点", {
        "shared_start": blocked.as_dict(),
        "private_start": fair.as_dict(),
    })

    # 4. Relay pair: observation constraint, same-beat unsafe, covers, relay.
    r1, r2, meta = build_relay_pair()
    req = {"L": [meta["observation"]]}
    bare = grade(r1, meta["start1"], meta["finish1"], 3)
    strict = grade(r1, meta["start1"], meta["finish1"], 3, required=req)
    s1 = Slot("LEFT", r1, meta["start1"], meta["finish1"], 3, req)
    s2 = Slot("RIGHT", r2, meta["start2"], meta["finish2"], 3, req)
    same_beat = slots_safe_at_beats(s1, 0, s2, 0)
    cover = find_cover_plan([s1, s2], forced={"LEFT": ["obs.yi"], "RIGHT": ["obs.jia"]})
    broken = find_cover_plan(
        [s1, s2], forced={"LEFT": ["obs.yi"], "RIGHT": ["obs.jia", "obs.yi"]}
    )
    sched = relay_schedule([s1, s2], [0, 2])
    show("中继观察签等分线", {
        "grades": {"bare_shortcut": bare.steps, "with_observation": strict.steps},
        "same_beat_safe": same_beat[0],
        "required_gap": required_gap(s1, s2),
        "cover_feasible": cover.as_dict() if cover else None,
        "cover_after_removing_right_yi": broken.as_dict() if broken else None,
        "relay_two_usable_beats": sched.as_dict(),
    })

    # 5. Delayed second number cannot backfill: G1's number arrives at 0 and
    #    it really moves at 1 (valid); G2's number is pushed to 5 but G2 had
    #    actually already moved at 1 together with G1.  The late number cannot
    #    retroactively separate them.
    delayed = validate_delivery({"G1": 0, "G2": 5}, {"G1": 1, "G2": 1})
    show("第二号改迟至同时出手后", delayed.as_dict())

    # 6. Arm-span-dependent grading.
    demo = arm_span_grading_example()
    spans = {
        span: grade(demo["route"], demo["start"], demo["finish"], span).steps
        for span in demo["spans"]
    }
    show("同线不同臂展的分级", {"grades_by_span": spans})

    out = ROOT / "exports" / "routes" / "demo.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(SCENARIOS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
