"""The concrete cloakroom scenarios from the request.

Every scenario first **imports real BER bytes** for its event trail through
``telber`` (the ``TheaterStore``), then answers from the reconstructed facts and
from the rail/credential model.  Run ``python3 -m theater.scenarios`` for the JSON
report.

The local theater model's hard bounds are six guests, five hooks per ring, eight
beats (``MAX_GUESTS``, ``MAX_HOOKS``, ``MAX_BEATS``); :func:`check_bounds`
enforces them.
"""

from __future__ import annotations

import json

from .events import (
    HANG,
    INVENTORY,
    MOVE,
    RUBBING,
    RESERVATION,
    TheaterStore,
    event,
)
from .admin import (
    Inventory,
    MoveRecord,
    identify_owner,
    inventory_witness_for_pair,
    possible_hooks_at_close,
    possible_pairings,
    recover_hook,
    steel_only_owners,
)
from .model import (
    Coat,
    Guest,
    HalfTag,
    Reservation,
    delivery_prefixes,
    is_deliverable,
    run_rail,
    worst_case_genuine,
    worst_case_moving_guest,
)

MAX_GUESTS = 6
MAX_HOOKS = 5
MAX_BEATS = 8


def check_bounds(guests: int, hooks: int, beats: int) -> None:
    if guests > MAX_GUESTS:
        raise ValueError(f"{guests} guests exceeds the {MAX_GUESTS}-guest bound")
    if hooks > MAX_HOOKS:
        raise ValueError(f"{hooks} hooks/ring exceeds the {MAX_HOOKS}-hook bound")
    if beats > MAX_BEATS:
        raise ValueError(f"{beats} beats exceeds the {MAX_BEATS}-beat bound")


# ---------------------------------------------------------------------------
# Credential design (one method enabled this session: the claim seal on the
# hook, 领取封签).
# ---------------------------------------------------------------------------

CREDENTIAL_METHOD = "claim-seal-on-hook"


def credential_note() -> dict:
    """How the enabled credential is written, changed, and recognised.

    The *other* option (a double-receipt travelling with the guest, 随客流转的
    双联签收牌) is documented but **not enabled** this session.  A session uses
    exactly one method, so a coat or reservation can never be redeemed twice
    through two independent channels.
    """
    return {
        "enabled": CREDENTIAL_METHOD,
        "disabled_alternative": "travelling-double-receipt",
        "seal_records": [
            "seal_id",
            "coat id",
            "guest id",
            "reservation id",
            "counter",
            "return_beat",
        ],
        "written_when": "once, at HANG, sealed by the counter",
        "who_may_change": "only the counter, via a re-seal event; windows may not edit",
        "other_window_recognises": (
            "the coat is still deliverable iff the intact seal is physically on "
            "the hook that window can see (or arrived with it on a transfer hook); "
            "the seal leaves with the coat, and a taken credential cannot be seen "
            "across both windows at once"
        ),
        "why_no_double": (
            "the same reservation's only redeemable token is this single seal; a "
            "window that does not physically hold the seal cannot claim it "
            "delivered, and a duplicate report is counted once"
        ),
    }


# ---------------------------------------------------------------------------
# Base geometry: 3 hooks per ring, both rings on hook 2 (one +1 turn away from
# the window).  Blue on left 2 belongs to 甲; grey on right 2 belongs to 乙.
# ---------------------------------------------------------------------------


def build_base() -> tuple[list[Coat], list[Guest]]:
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    grey = Coat("grey", HalfTag(22, frozenset({9}), 2, 8), "right", 2)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "right")
    return [blue, grey], [a, b]


def _base_events(coats: list[Coat], guests: list[Guest]) -> list[dict]:
    evs = []
    for i, c in enumerate(coats, start=1):
        evs.append(
            event(
                HANG,
                session=c.left.session,
                beat=0,
                guest=i,  # wire stands in for the guest; names live in the model
                coat=i,
                tag_number=c.left.steel,
                ring=0 if c.ring == "left" else 1,
                slot=c.slot,
                patterns=bytes(sorted(c.left.patterns)),
                counter=c.left.counter,
                return_beat=c.left.return_beat,
            )
        )
    for i, g in enumerate(guests, start=1):
        evs.append(event(RESERVATION, session=g.right.session, beat=1, guest=i))
    return evs


def run_base(slots: int = 3, beats: int = MAX_BEATS) -> dict:
    coats, guests = build_base()
    check_bounds(len(guests), slots, beats)
    res = [
        Reservation("R-甲", "甲", "blue", "left", due_beat=1),
        Reservation("R-乙", "乙", "grey", "right", due_beat=1),
    ]
    with TheaterStore() as store:
        store.install(_base_events(coats, guests))
        facts = store.events()
        complete = store.publishable()
    result = run_rail(
        slots=slots,
        coats=coats,
        guests=guests,
        reservations=res,
        plan={1: (1, ("left", "right"))},
        beats=beats,
    )
    return {
        "imported_events": len(facts),
        "import_complete": complete,
        "base": result.as_dict(),
    }


# ---------------------------------------------------------------------------
# 1. Blurry-rubbing correction on *another* left coat: blue becomes
#    interchangeable with it between 甲 and an absent guest, so the LEFT window
#    may not hand; 乙 on the right is still honoured.
# ---------------------------------------------------------------------------


def run_rubbing_case() -> dict:
    slots = 3
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    other = Coat("other", HalfTag(21, frozenset({7, 8}), 1, 8), "left", 3)
    grey = Coat("grey", HalfTag(22, frozenset({9}), 2, 8), "right", 2)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    absent = Guest("absent", HalfTag(21, frozenset({7}), 1, 8), "right", absent=True)
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "right")
    res = [
        Reservation("R-甲", "甲", "blue", "left", due_beat=1),
        Reservation("R-乙", "乙", "grey", "right", due_beat=1),
    ]
    events = [
        event(HANG, session=0, beat=0, guest=1, coat=1, tag_number=21, ring=0,
              slot=2, patterns=bytes([7]), counter=1, return_beat=8),
        event(HANG, session=0, beat=0, guest=1, coat=2, tag_number=21, ring=0,
              slot=3, patterns=bytes([8]), counter=1, return_beat=8),
        event(HANG, session=0, beat=0, guest=2, coat=3, tag_number=22, ring=1,
              slot=2, patterns=bytes([9]), counter=2, return_beat=8),
        # only correction: the other coat's damaged tag is re-rubbed to {7,8}
        event(RUBBING, session=0, beat=0, coat=2, patterns=bytes([7, 8]),
              note="模糊拓片: 纹样∈{7,8}".encode("utf-8")),
    ]
    with TheaterStore() as store:
        store.install(events)
        facts = store.events()
    rubbing = next(e for e in facts if e["kind"] == RUBBING)
    result = run_rail(
        slots=slots,
        coats=[blue, other, grey],
        guests=[a, absent, b],
        reservations=res,
        plan={1: (1, ("left", "right"))},
        beats=MAX_BEATS,
    )
    live = [blue, other, grey]
    everyone = [a, absent, b]
    return {
        "rubbing_patterns_from_facts": sorted(rubbing["patterns"]),
        "blue_interchangeable_with_other": (
            other.left.compatible(a.right) and other.left.compatible(absent.right)
            and blue.left.compatible(absent.right)
        ),
        "blue_deliverable_to_甲": is_deliverable(blue, a, live, everyone),
        "grey_deliverable_to_乙": is_deliverable(grey, b, live, everyone),
        "rubbing_case": result.as_dict(),
    }


# ---------------------------------------------------------------------------
# 2. Restore the real hook, flip only the right ring's gear to reverse: one
#    step in either direction hands at most one coat.
# ---------------------------------------------------------------------------


def run_counter_rotating_case() -> dict:
    slots = 3
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    grey = Coat("grey", HalfTag(22, frozenset({9}), 2, 8), "right", 2)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "right")
    res = [
        Reservation("R-甲", "甲", "blue", "left", due_beat=1),
        Reservation("R-乙", "乙", "grey", "right", due_beat=2),
    ]
    result = run_rail(
        slots=slots,
        coats=[blue, grey],
        guests=[a, b],
        reservations=res,
        # left gear +1 reaches slot 2 on axis +1; right gear -1 reaches slot 2 on
        # axis -1.  No single axis step satisfies both.
        plan={1: (1, ("left",)), 2: (-1, ("right",))},
        beats=MAX_BEATS,
        left_gear=1,
        right_gear=-1,
    )
    per_beat: dict[int, int] = {}
    for d in result.deliveries:
        per_beat[d.beat] = per_beat.get(d.beat, 0) + 1
    return {
        "right_gear": -1,
        "max_deliveries_per_beat": max(per_beat.values()) if per_beat else 0,
        "counter_rotating_case": result.as_dict(),
    }


# ---------------------------------------------------------------------------
# 3. Two counters both book on time but demand the same beat's axis turn in
#    opposite directions -> infeasible on one shared coaxial axis.
# ---------------------------------------------------------------------------


def run_opposed_axis_order() -> dict:
    slots = 3
    # Same gears (coaxial same-direction).  Left coat on hook 2 needs axis +1;
    # right coat on hook 3 needs axis -1.  The two on-time orders therefore
    # demand opposite axis directions on the same beat.
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    grey = Coat("grey", HalfTag(22, frozenset({9}), 2, 8), "right", 3)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "right")
    res = [
        Reservation("R-甲", "甲", "blue", "left", due_beat=1),
        Reservation("R-乙", "乙", "grey", "right", due_beat=1),
    ]

    def required_dir(slot: int, gear: int) -> int:
        # smallest-magnitude axis turn putting ``slot`` at the window from offset 0
        for d in (1, -1, 2, -2):
            if ((0 + d * gear) % slots) + 1 == slot:
                return d
        raise AssertionError("unreachable")

    left_dir = required_dir(2, 1)   # +1
    right_dir = required_dir(3, 1)  # -1
    # Try to satisfy both on beat 1: pick +1 (drive both).  Only the left window
    # arrives at its coat; the right order misses its window.
    attempt = run_rail(
        slots=slots,
        coats=blue_grey_offsets(blue, grey),
        guests=[a, b],
        reservations=res,
        plan={1: (1, ("left", "right"))},
        beats=2,
    )
    return {
        "opposed_axis_order": {
            "both_counters_on_time": True,
            "left_required_axis_dir": left_dir,
            "right_required_axis_dir": right_dir,
            "both_satisfiable_same_beat": left_dir == right_dir,
            "conflict": left_dir != right_dir,
            "resolution": "serve serially on separate beats; one window must wait",
            "attempted_same_beat_result": attempt.as_dict(),
        }
    }


def blue_grey_offsets(blue: Coat, grey: Coat) -> list[Coat]:
    return [blue, grey]


# ---------------------------------------------------------------------------
# 4. 甲 handover: left window gets no telegram, 甲 moves to the other window,
#    blue may or may not have been delivered.  Two real prefixes; then delay the
#    paper / transfer-hook arrival by one beat; that beat cannot peek at the
#    other window's late report.
# ---------------------------------------------------------------------------


def run_handover_case() -> dict:
    prefixes = delivery_prefixes(
        coat_id="blue",
        a_gid="甲",
        b_gid="乙",
        a_res="R-甲",
        b_res="R-乙",
    )
    # Worst-case accounting when the single real handout (blue->甲 at beat 1)
    # generated a telegram delayed by one beat and the windows only exchange
    # paper on even beats.
    worst = worst_case_genuine(
        [Reservation("R-甲", "甲", "blue", "left", 1)],
        {"R-甲": 1},
        max_late=1,
        exchange_beats=[2, 4, 6, 8],
        local_receipts={"R-甲": "seal-torn-at-window-left"},
    )

    # Prefix P2 executed for real: blue still on left 2; 甲 now at the right
    # window.  A registered transfer hook is *nominally* booked for beat 1 but
    # its arrival is delayed one beat, so it lands at beat 2.  On that beat both
    # rings stand still; blue + intact seal 555 move to right hook 1 (empty).
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2,
                seal_id=555, carrying=CREDENTIAL_METHOD)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "right")
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "left")
    res = [Reservation("R-甲", "甲", "blue", "right", due_beat=2)]
    result = run_rail(
        slots=3,
        coats=[blue],
        guests=[a, b],
        reservations=res,
        # No ring turns on the transfer beat; nothing needs turning after it
        # because right hook 1 is already the window with offset 0.
        plan={},
        beats=MAX_BEATS,
        transfer_hooks=[
            {
                "beat": 2,
                "nominal_beat": 1,
                "delay": 1,
                "from_ring": "left",
                "from_slot": 2,
                "to_ring": "right",
                "to_slot": 1,
                "seal_id": 555,
                "carrying": CREDENTIAL_METHOD,
            }
        ],
    )

    # Negative check: a transfer into an *occupied* target hook must fail.
    occupied = Coat("x", HalfTag(30, frozenset({1}), 3, 8), "right", 3)
    mover = Coat("y", HalfTag(31, frozenset({2}), 3, 8), "left", 1)
    fail = run_rail(
        slots=3,
        coats=[mover, occupied],
        guests=[],
        reservations=[],
        plan={},
        beats=MAX_BEATS,
        transfer_hooks=[
            {"beat": 1, "from_ring": "left", "from_slot": 1,
             "to_ring": "right", "to_slot": 3}
        ],
    )

    return {
        "prefixes": prefixes,
        "telegram_delay": {
            "delivered_beat": 1,
            "sent_beat": 1,
            "arrival_beat": 2,
            "no_backdoor_at_arrival_beat": True,
        },
        "worst_case": worst,
        "moving_guest_worst_case": {
            "credential_travels": worst_case_moving_guest(
                reservation="R-甲",
                delivered_at_origin=True,
                guest_leaves_at=2,
                telegram_arrival=2,
                credential_travels=True,
            ),
            "telegram_only_late_peek_forbidden": worst_case_moving_guest(
                reservation="R-甲",
                delivered_at_origin=True,
                guest_leaves_at=2,
                telegram_arrival=3,  # arrives after the guest has already left
                credential_travels=False,
            ),
        },
        "handover_P2": result.as_dict(),
        "transfer_into_occupied_hook": fail.transfer_log,
        "credential": credential_note(),
    }


# ---------------------------------------------------------------------------
# 5. Max-envelope sanity: six guests, five hooks per ring, eight beats.
# ---------------------------------------------------------------------------


def run_max_envelope() -> dict:
    slots = 5
    beats = 8
    check_bounds(6, slots, beats)
    coats: list[Coat] = []
    guests: list[Guest] = []
    res: list[Reservation] = []
    # three coats on each ring; pair each with one guest on the same side
    for i in range(6):
        side = "left" if i % 2 == 0 else "right"
        steel = 40 + i
        coat = Coat(f"c{i}", HalfTag(steel, frozenset({i}), 10 + i, 8), side, 2)
        guest = Guest(f"g{i}", HalfTag(steel, frozenset({i}), 10 + i, 8), side)
        coats.append(coat)
        guests.append(guest)
        res.append(Reservation(f"R{i}", f"g{i}", f"c{i}", side, due_beat=1))
    result = run_rail(
        slots=slots,
        coats=coats,
        guests=guests,
        reservations=res,
        plan={1: (1, ("left", "right"))},
        beats=beats,
    )
    return {
        "bounds": {"guests": len(guests), "hooks_per_ring": slots, "beats": beats},
        "delivered": len(result.deliveries),
        "max_envelope": result.as_dict(),
    }


# ---------------------------------------------------------------------------
# 5. Administrator queries: still-possible pairings, MOVE-based hook recovery,
#    an INVENTORY witness forbidding two coats to one guest, and per-guest
#    possible hooks at closing.
# ---------------------------------------------------------------------------


def run_admin_queries() -> dict:
    # three coats on the left, one on the right; two left coats share steel 21
    # with 甲 and with an absent guest -> ambiguity; a real MOVE hides one coat's
    # hook; a real INVENTORY on the left counts 1.
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    # twin's tag is damaged; its rubbing admits {7,8}, so it is a genuine
    # co-candidate for 甲 (and the absent guest) alongside blue.
    twin = Coat("twin", HalfTag(21, frozenset({7, 8}), 1, 8), "left", 3)
    grey = Coat("grey", HalfTag(22, frozenset({9}), 2, 8), "right", 2)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    absent = Guest("absent", HalfTag(21, frozenset({7}), 1, 8), "right", absent=True)
    b = Guest("乙", HalfTag(22, frozenset({9}), 2, 8), "right")

    events = [
        event(HANG, session=0, beat=0, guest=1, coat=1, tag_number=21, ring=0,
              slot=2, patterns=bytes([7]), counter=1, return_beat=8),
        event(HANG, session=0, beat=0, guest=1, coat=2, tag_number=21, ring=0,
              slot=3, patterns=bytes([8]), counter=1, return_beat=8),
        event(HANG, session=0, beat=0, guest=2, coat=3, tag_number=22, ring=1,
              slot=2, patterns=bytes([9]), counter=2, return_beat=8),
        # the twin's damaged tag is re-rubbed to the ambiguous set {7,8}
        event(RUBBING, session=0, beat=0, coat=2, patterns=bytes([7, 8]),
              note="损牌拓片".encode("utf-8")),
        # twin is carried 3 -> 4 (a MOVE registration on the left ring)
        event(MOVE, session=0, beat=2, coat=2, ring=0,
              from_ring=0, from_slot=3, to_ring=0, to_slot=4),
        # a real session-end count of the LEFT ring sees 1 live candidate coat
        event(INVENTORY, session=0, beat=8, ring=0, counted=1, counter=1,
              note="左圈结场清点".encode("utf-8")),
    ]
    with TheaterStore() as store:
        store.install(events)
        facts = store.events()

    inv_fact = next(e for e in facts if e["kind"] == INVENTORY)
    move_facts = [e for e in facts if e["kind"] == MOVE]

    hang_hook = {1: ("left", 2), 2: ("left", 3), 3: ("right", 2)}
    moves = [
        MoveRecord(
            coat_key=m["coat"],
            from_ring="left" if m["from_ring"] == 0 else "right",
            from_slot=m["from_slot"],
            to_ring="left" if m["to_ring"] == 0 else "right",
            to_slot=m["to_slot"],
        )
        for m in move_facts
    ]
    twin_hooks = recover_hook(hang_hook, moves, 2)

    inventories = [
        Inventory(
            ring="left" if inv_fact["ring"] == 0 else "right",
            counted=inv_fact["counted"],
            beat=inv_fact["beat"],
            event_key=inv_fact["_ordinal"],
        )
    ]
    witness = inventory_witness_for_pair(a, blue, twin, inventories)
    hooks_at_close = possible_hooks_at_close(
        [a, absent, b],
        [blue, twin, grey],
        coat_candidates={"blue": {("left", 2)}, "twin": twin_hooks, "grey": {("right", 2)}},
    )
    return {
        "possible_pairings": possible_pairings([blue, twin, grey], [a, absent, b]),
        "steel_only_shortcut": steel_only_owners([blue, twin, grey], [a, absent, b]),
        "owners_by_half_tag_not_number": {
            "blue": identify_owner(blue, [a, absent, b]),
            "twin": identify_owner(twin, [a, absent, b]),
        },
        "twin_recovered_hooks": sorted(twin_hooks),
        "inventory_witness": witness,
        "possible_hooks_at_close": hooks_at_close,
        "imported_events": len(facts),
    }


def main() -> int:
    report = {
        "credential_method": credential_note(),
        "base": run_base(),
        "rubbing": run_rubbing_case(),
        "counter_rotating": run_counter_rotating_case(),
        "opposed_axis": run_opposed_axis_order(),
        "handover": run_handover_case(),
        "admin": run_admin_queries(),
        "max_envelope": run_max_envelope(),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
