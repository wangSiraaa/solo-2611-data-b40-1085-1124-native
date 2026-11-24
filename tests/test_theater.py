"""Local theater model: real telber round-trips, rail kinematics and rules."""

from __future__ import annotations

import io

import pytest

from theater.admin import (
    Inventory,
    MoveRecord,
    identify_owner,
    inventory_witness_for_pair,
    possible_hooks_at_close,
    possible_pairings,
    recover_hook,
    steel_only_owners,
)
from theater.events import (
    HANG,
    INVENTORY,
    MOVE,
    TheaterStore,
    event,
    load_product,
    stream_bytes,
)
from theater.model import (
    Coat,
    Guest,
    HalfTag,
    Reservation,
    Ring,
    is_deliverable,
    run_rail,
    worst_case_genuine,
    worst_case_moving_guest,
)
from theater.scenarios import (
    CREDENTIAL_METHOD,
    check_bounds,
    run_base,
    run_counter_rotating_case,
    run_handover_case,
    run_opposed_axis_order,
    run_rubbing_case,
)
from telber.service import CompileService


# -- wire round trip ---------------------------------------------------------


def test_events_round_trip_through_repository():
    evs = [
        event(HANG, session=0, beat=0, guest=1, coat=1, tag_number=21,
              ring=0, slot=2, patterns=bytes([7]), counter=1, return_beat=8),
        event(MOVE, session=0, beat=2, coat=1, ring=0,
              from_ring=0, from_slot=2, to_ring=0, to_slot=3),
        event(INVENTORY, session=0, beat=8, ring=0, counted=1, counter=1,
              note="清点".encode()),
    ]
    with TheaterStore() as store:
        store.install(evs)
        assert store.publishable()
        facts = store.events()
    assert [f["kind"] for f in facts] == [HANG, MOVE, INVENTORY]
    assert facts[0]["patterns"] == bytes([7])
    assert facts[1]["to_slot"] == 3
    assert facts[2]["note"] == "清点".encode()
    assert [f["_ordinal"] for f in facts] == [0, 1, 2]


def test_stream_bytes_block_invariant():
    evs = [event(HANG, session=0, beat=0, guest=1, coat=1, tag_number=5,
                 ring=0, slot=2, patterns=bytes([1]), counter=1, return_beat=8)]
    data = stream_bytes(evs)
    product = load_product()
    service = CompileService()
    # decoding the same bytes at many base offsets must yield the same values
    for base in (0, 1, 2, 7, 100):
        rec = service.decode(product, data, base=base)
        vals = {v.path: v.value for v in rec.value_map().values() if v.present}
        assert vals["tag_number"] == 5 and vals["slot"] == 2


def test_partial_import_is_only_pending():
    evs = [
        event(HANG, session=0, beat=0, guest=1, coat=1, tag_number=21,
              ring=0, slot=2, patterns=bytes([7]), counter=1, return_beat=8),
        event(HANG, session=0, beat=0, guest=2, coat=2, tag_number=22,
              ring=1, slot=2, patterns=bytes([9]), counter=2, return_beat=8),
    ]
    data = stream_bytes(evs)
    with TheaterStore() as store:
        _, info = store.install_truncated(evs, keep=len(evs[0]) + len(data) // 2)
        assert info.status == "partial"
        from telber.errors import ImportStateError

        with pytest.raises(ImportStateError):
            store.repo.publish_import(store.import_id)


# -- half-tag matching -------------------------------------------------------


def test_half_tags_scope_by_session():
    a = HalfTag(21, frozenset({7}), 1, 8, session=0)
    b = HalfTag(21, frozenset({7}), 1, 8, session=1)
    assert not a.compatible(b)


def test_deliverable_requires_bijection():
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    other = Coat("other", HalfTag(21, frozenset({7, 8}), 1, 8), "left", 3)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    absent = Guest("absent", HalfTag(21, frozenset({7}), 1, 8), "right", absent=True)
    assert not is_deliverable(blue, a, [blue, other], [a, absent])
    # once the twin is gone (delivered), blue is isolated again for 甲
    assert is_deliverable(blue, a, [blue], [a])


# -- rail geometry -----------------------------------------------------------


def test_one_forward_turn_hands_two():
    res = run_base()
    dels = res["base"]["deliveries"]
    assert [(d["beat"], d["window"], d["coat"]) for d in dels] == [
        (1, "left", "blue"),
        (1, "right", "grey"),
    ]


def test_ring_gear_reverse_moves_opposite():
    r = Ring("right", 3, gear=-1)
    r.advance(+1, True)
    assert r.offset == 2  # (0 + 1*-1) % 3
    r2 = Ring("left", 3, gear=+1)
    r2.advance(+1, True)
    assert r2.offset == 1


def test_right_gear_reverse_caps_one_per_beat():
    out = run_counter_rotating_case()
    assert out["max_deliveries_per_beat"] == 1
    assert out["right_gear"] == -1
    dels = out["counter_rotating_case"]["deliveries"]
    assert {(d["beat"], d["coat"]) for d in dels} == {(1, "blue"), (2, "grey")}


def test_opposed_axis_orders_conflict():
    out = run_opposed_axis_order()["opposed_axis_order"]
    assert out["left_required_axis_dir"] != out["right_required_axis_dir"]
    assert out["conflict"] is True
    assert out["both_satisfiable_same_beat"] is False
    # the naive same-beat attempt honours only one of the two orders
    assert len(out["attempted_same_beat_result"]["deliveries"]) == 1


def test_transfer_hook_requires_empty_target():
    mover = Coat("y", HalfTag(31, frozenset({2}), 3, 8), "left", 1)
    occupied = Coat("x", HalfTag(30, frozenset({1}), 3, 8), "right", 3)
    res = run_rail(
        slots=3, coats=[mover, occupied], guests=[], reservations=[], plan={},
        beats=8, transfer_hooks=[{"beat": 1, "from_ring": "left", "from_slot": 1,
                                  "to_ring": "right", "to_slot": 3}],
    )
    assert res.transfer_log[0]["ok"] is False
    assert res.transfer_log[0]["reason"] == "target hook occupied"


# -- rubbing and handover ----------------------------------------------------


def test_rubbing_blocks_left_but_honours_right():
    out = run_rubbing_case()
    assert out["rubbing_patterns_from_facts"] == [7, 8]
    assert out["blue_deliverable_to_甲"] is False
    assert out["grey_deliverable_to_乙"] is True
    dels = out["rubbing_case"]["deliveries"]
    assert [(d["window"], d["coat"]) for d in dels] == [("right", "grey")]
    assert out["rubbing_case"]["over_window"][0]["reservation"] == "R-甲"


def test_handover_prefixes_and_worst_case():
    out = run_handover_case()
    p1, p2 = out["prefixes"]
    assert p1["prefix"].startswith("P1") and p1["continues"] == "R-乙"
    assert p1["cannot"] == "R-甲"
    assert p2["continues"] == "R-甲"
    worst = out["worst_case"]
    assert worst["guaranteed_count"] == 1
    assert worst["counted_once"] == ["R-甲"]
    # transfer lands one beat late, then the right window hands once
    assert out["handover_P2"]["transfers"][0]["ok"] is True
    assert out["handover_P2"]["transfers"][0]["beat"] == 2
    dels = out["handover_P2"]["deliveries"]
    assert [(d["beat"], d["window"], d["guest"], d["coat"]) for d in dels] == [
        (2, "right", "甲", "blue")
    ]


def test_duplicate_reports_count_once():
    worst = worst_case_genuine(
        [Reservation("R-甲", "甲", "blue", "left", 1)],
        {"R-甲": 1},
        max_late=2,
        exchange_beats=[2, 4, 6],
        local_receipts={"R-甲": "seal-torn"},
    )
    assert worst["guaranteed_count"] == 1
    assert worst["counted_once"] == ["R-甲"]


def test_moving_guest_worst_case_depends_on_credential():
    # with a travelling credential P2 is decidable at once -> guaranteed
    with_cred = worst_case_moving_guest(
        reservation="R-甲", delivered_at_origin=True, guest_leaves_at=2,
        telegram_arrival=2, credential_travels=True,
    )
    assert with_cred["worst_case_genuine"] == 1
    # telegram only, arriving after the guest left -> P2 lost, worst case 0
    without = worst_case_moving_guest(
        reservation="R-甲", delivered_at_origin=True, guest_leaves_at=2,
        telegram_arrival=3, credential_travels=False,
    )
    assert without["worst_case_genuine"] == 0
    # telegram only, arriving in time -> still guaranteed
    in_time = worst_case_moving_guest(
        reservation="R-甲", delivered_at_origin=True, guest_leaves_at=4,
        telegram_arrival=3, credential_travels=False,
    )
    assert in_time["worst_case_genuine"] == 1


# -- admin -------------------------------------------------------------------


def test_recover_hook_unknown_destination_uses_adjacent_hooks():
    # coat 8 last known on left 3; its own move has no destination recorded.
    # Only moves *from adjacent slots* (2 and 4) are legitimate candidates; a
    # far-away registration from slot 5 must not be considered.
    hang = {8: ("left", 3)}
    moves = [
        MoveRecord(8, "left", 3, "left", 0),
        MoveRecord(9, "left", 4, "left", 6),
        MoveRecord(10, "left", 2, "left", 7),
        MoveRecord(11, "left", 5, "left", 9),
    ]
    assert recover_hook(hang, moves, 8) == {("left", 6), ("left", 7)}


def test_recover_hook_from_move_not_number():
    hang = {2: ("left", 3)}
    moves = [MoveRecord(2, "left", 3, "left", 5)]
    assert recover_hook(hang, moves, 2) == {("left", 5)}
    # a coat on the same steel number must not steal the identity: recovery is
    # purely by the move log, and identify_owner uses half-tags only
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    twin = Coat("twin", HalfTag(21, frozenset({8}), 1, 8), "left", 5)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    assert identify_owner(blue, [a]) == ["甲"]
    assert identify_owner(twin, [a]) == []  # same steel, wrong pattern


def test_inventory_witness_excludes_pair():
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    twin = Coat("twin", HalfTag(21, frozenset({7, 8}), 1, 8), "left", 4)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    assert inventory_witness_for_pair(a, blue, twin, []) is None
    witness = inventory_witness_for_pair(
        a, blue, twin, [Inventory("left", counted=1, beat=8, event_key=4)]
    )
    assert witness is not None
    assert witness["counted"] == 1
    assert set(witness["coats"]) == {"blue", "twin"}


def test_possible_hooks_at_close_uses_recovery_sets():
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    twin = Coat("twin", HalfTag(21, frozenset({7, 8}), 1, 8), "left", 4)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    hooks = possible_hooks_at_close(
        [a], [blue, twin],
        coat_candidates={"blue": {("left", 2)}, "twin": {("left", 4)}},
    )
    assert hooks["甲"] == [["left", 2], ["left", 4]]


def test_possible_pairings_and_steel_shortcut_differ():
    blue = Coat("blue", HalfTag(21, frozenset({7}), 1, 8), "left", 2)
    twin = Coat("twin", HalfTag(21, frozenset({8}), 1, 8), "left", 3)
    a = Guest("甲", HalfTag(21, frozenset({7}), 1, 8), "left")
    pairs = possible_pairings([blue, twin], [a])
    assert pairs == [{"guest": "甲", "coat": "blue"}]
    shortcut = steel_only_owners([blue, twin], [a])
    # the shortcut wrongly suggests twin too; half-tags reject it
    assert {s["coat"] for s in shortcut} == {"blue", "twin"}
    assert {s["coat"] for s in shortcut if s["half_tags_combine"]} == {"blue"}


# -- bounds ------------------------------------------------------------------


def test_bounds_enforced():
    check_bounds(6, 5, 8)
    with pytest.raises(ValueError):
        check_bounds(7, 5, 8)
    with pytest.raises(ValueError):
        check_bounds(6, 6, 8)
    with pytest.raises(ValueError):
        check_bounds(6, 5, 9)
