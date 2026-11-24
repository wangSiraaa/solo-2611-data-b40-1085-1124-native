#!/usr/bin/env python3
"""End-to-end tasting-cabinet demonstration on top of the sampling warehouse.

Run from the repository root::

    python3 demo/run_shop_demo.py [--work demo/_shop]

It exercises the two acceptance sessions described in the task:

* session A -- ordinary cabinet (no reservations, no restock rights):
  original tickets fully honoured; correcting the selected ticket to 乙
  makes the second buyer miss out; restoring it and removing the 甲 pack
  from the sealed boxes also breaks the promise;
* a merged ring that each single ticket supports but the desk-wide graph
  rejects;
* retracting the sampled receipt lets the real next-ranked row enter the
  sample while the historical dataset stays replayable;
* session B -- the same two buyers under both selectable opening modes,
  including a late erratum / late restock that cannot undo a handed-out
  pack and is logged as a contradiction.

The counter opens one order against the actually sampled tickets, processes
the first buyer's pickup, refreshes the sample, then continues.  Reopening an
order id independently returns the tickets used then and the packs handed
out.  All selected records and dispensing lists are written as local files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wrsource.cabinet import (Box, Buyer, CabinetConfig, CabinetEngine,
                              MODE_PLAIN, MODE_RESERVE, MODE_RESTOCK,
                              Reservation, RestockRight)
from wrsource.dataset import build_dataset, load_dataset, refresh_dataset
from wrsource.ledger import OrderLedger, Pickup
from wrsource.receipts import (PreferenceCycleError, PreferenceDesk,
                               make_receipt_body)
from wrsource.source import SourceService

SEED = bytes(range(32))
TEA_A, TEA_B = "甲", "乙"
LIN, QI = "小林", "小齐"


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def add_ticket(row_id, buyer, visible, picked, group, weight=10,
               shard="s0"):
    return {"op": "add", "row_id": row_id, "group": group, "weight": weight,
            "body": make_receipt_body(buyer, visible, picked), "shard": shard}


def desk_possible_picks(desk: PreferenceDesk):
    """Cabinet hook: only preference-compatible top choices remain open."""
    def hook(buyer, options):
        return desk.possible_picks(buyer, options)
    return hook


def print_report(title, report):
    banner(title)
    cheapest = report.cheapest
    if cheapest is None:
        print("no plan can guarantee every gift.")
    else:
        print(f"cheapest fully-honouring plan: boxes={list(cheapest.boxes)} "
              f"total={cheapest.total_cost} "
              f"(box={cheapest.box_cost}, reserve_fee="
              f"{cheapest.reservation_fee}, order_fee="
              f"{cheapest.restock_order_fee}, exec_fee="
              f"{cheapest.restock_execute_fee})")
    for p in report.infeasible:
        first = p.first_failed_buyer
        print(f"- affordable but failing plan boxes={list(p.boxes)} "
              f"cost={p.total_cost}: first missed buyer = {first}")
        for e in p.witness:
            line = (f"    step {e.step} {e.buyer}: shelf={e.shelf} "
                    f"options={e.options} picked={e.picked} "
                    f"pack={e.pack_id} ({e.source})")
            if e.called_right:
                line += f" called={e.called_right}"
            if e.note:
                line += f" | {e.note}"
            print(line)


def plan_with_boxes(report, case_ids):
    wanted = tuple(case_ids)
    for p in report.infeasible:
        if p.boxes == wanted:
            return p
    raise AssertionError(f"no failing plan with boxes {wanted}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.path.join(HERE, "_shop"))
    args = ap.parse_args()
    if os.path.isdir(args.work):
        shutil.rmtree(args.work)
    os.makedirs(args.work)

    db = os.path.join(args.work, "shop.sqlite")
    svc = SourceService(db)
    svc.init()
    ledger = OrderLedger(os.path.join(args.work, "ledger.sqlite"))
    ledger.init()

    # ------------------------------------------------------------------
    # Session A: ordinary cabinet, two buyers, two teas, one each.
    # ------------------------------------------------------------------
    banner("1. Ingest tasting receipts into the weighted source")
    rows = [
        # 小林 group: two rows, quota 1 -> the sampler picks exactly one.
        add_ticket("t-lin-1", LIN, [TEA_A, TEA_B], TEA_A, "g-lin", weight=50),
        add_ticket("t-lin-2", LIN, [TEA_A, TEA_B], TEA_B, "g-lin", weight=5),
        # 小齐 group: only sees / accepts 乙.
        add_ticket("t-qi-1", QI, [TEA_B], TEA_B, "g-qi", weight=50),
        add_ticket("t-qi-2", QI, [TEA_B], TEA_B, "g-qi", weight=5),
    ]
    v1 = svc.apply_batch(rows, note="session A receipts").version
    quotas = {"g-lin": 1, "g-qi": 1}
    ds1 = os.path.join(args.work, "ds_A_v1")
    m1, recs1 = build_dataset(svc, v1, SEED, quotas, ds1)
    selected_ids = [r["row_id"] for r in recs1]
    print("source version:", v1, "| sampled receipts:", selected_ids)
    desk = PreferenceDesk.from_records(recs1)
    print("desk buyers:", desk.buyers)
    print(f"小林 must prefer {TEA_A} over {TEA_B}:",
          desk.must_precede(TEA_A, TEA_B, LIN))
    print("desk-wide relation 甲 vs 乙:", desk.relation(TEA_A, TEA_B))
    assert selected_ids == ["t-lin-1", "t-qi-1"]
    assert desk.must_precede(TEA_A, TEA_B, LIN)

    boxes = (Box("case-1", {TEA_A: 1, TEA_B: 1}, price=10),)
    buyers = (
        Buyer(LIN, 0, acceptable=(TEA_A, TEA_B),
              visible=(TEA_A, TEA_B)),
        Buyer(QI, 1, acceptable=(TEA_B,), visible=(TEA_B,)),
    )
    cfg = CabinetConfig(varieties=(TEA_A, TEA_B), boxes=boxes, buyers=buyers,
                        budget=100, mode=MODE_PLAIN)
    engine = CabinetEngine(cfg, desk_possible_picks(desk))

    banner("2. Ordinary opening: original sampled ticket fully honoured")
    report = engine.evaluate()
    print_report("ordinary cabinet, original tickets", report)
    assert report.cheapest is not None
    assert report.cheapest.boxes == ("case-1",)
    # Counter opens an order against this exact sampler result.
    order_a = ledger.open_order(
        MODE_PLAIN, ds1, v1, SEED.hex(), quotas, recs1,
        note="session A ordinary cabinet")
    print("opened order", order_a)

    banner("3. First buyer takes a pack, then the sample is refreshed")
    from wrsource.cabinet import build_plan
    plan1 = build_plan(cfg, boxes)
    orders = {LIN: (TEA_A, TEA_B), QI: (TEA_B, TEA_A)}
    handovers = engine.execute(plan1, orders)
    ledger.record_handovers(order_a, handovers)
    for h in handovers:
        print("handover:", h.as_dict())
    assert [h.buyer for h in handovers] == [LIN, QI]
    assert handovers[0].variety == TEA_A and handovers[1].variety == TEA_B
    write_json(os.path.join(args.work, "session_A_dispense.json"),
               [h.as_dict() for h in handovers])

    # ------------------------------------------------------------------
    # 4a. Correct the *selected* ticket: 小林 now actually took 乙.
    # ------------------------------------------------------------------
    banner("4. Erratum on the selected ticket: 小林's real pick was 乙")
    svc.apply_batch([
        {"op": "set_body", "row_id": "t-lin-1",
         "body": make_receipt_body(LIN, [TEA_A, TEA_B], TEA_B)},
    ], note="erratum: lin picked yi")
    v2 = svc.latest_version()
    ds2 = os.path.join(args.work, "ds_A_v2_corrected")
    m2, recs2, _ = refresh_dataset(svc, ds1, new_version=v2, out_dir=ds2)
    desk2 = PreferenceDesk.from_records(recs2)
    assert desk2.must_precede(TEA_B, TEA_A, LIN)
    engine2 = CabinetEngine(cfg, desk_possible_picks(desk2))
    report2 = engine2.evaluate()
    print_report("ordinary cabinet after erratum (lin -> 乙)", report2)
    assert report2.cheapest is None
    failing2 = plan_with_boxes(report2, ("case-1",))
    assert failing2.first_failed_buyer == QI
    order_a2 = ledger.open_order(
        MODE_PLAIN, ds2, v2, SEED.hex(), quotas, recs2,
        note="session A after erratum")
    ledger.record_failure(order_a2, QI, step=1,
                          witness=[e.as_dict() for e in failing2.witness])
    write_json(os.path.join(args.work, "session_A_erratum_report.json"),
               report2.as_dict())

    # ------------------------------------------------------------------
    # 4b. Restore the original ticket; remove the 甲 pack from the boxes.
    # ------------------------------------------------------------------
    banner("5. Restore the ticket, but seal boxes without the 甲 pack")
    svc.apply_batch([
        {"op": "set_body", "row_id": "t-lin-1",
         "body": make_receipt_body(LIN, [TEA_A, TEA_B], TEA_A)},
    ], note="restore lin ticket")
    v3 = svc.latest_version()
    ds3 = os.path.join(args.work, "ds_A_v3_restored")
    m3, recs3, _ = refresh_dataset(svc, ds2, new_version=v3, out_dir=ds3)
    desk3 = PreferenceDesk.from_records(recs3)
    assert desk3.must_precede(TEA_A, TEA_B, LIN)
    boxes_no_jia = (Box("case-no-jia", {TEA_B: 1}, price=8),)
    cfg3 = CabinetConfig(varieties=(TEA_A, TEA_B), boxes=boxes_no_jia,
                         buyers=buyers, budget=100, mode=MODE_PLAIN)
    report3 = CabinetEngine(cfg3, desk_possible_picks(desk3)).evaluate()
    print_report("ordinary cabinet with 甲 removed from sealed boxes", report3)
    assert report3.cheapest is None
    failing3 = plan_with_boxes(report3, ("case-no-jia",))
    # The only pack left is 乙. 小林's receipt only proves 甲>乙 and he still
    # accepts 乙 when 甲 is absent, so he takes it; 小齐 then arrives to an
    # empty shelf and is the first buyer whose gift is broken.
    assert failing3.first_failed_buyer == QI
    assert [e.picked for e in failing3.witness if e.picked] == [TEA_B]
    write_json(os.path.join(args.work, "session_A_no_jia_report.json"),
               report3.as_dict())

    # ------------------------------------------------------------------
    # 6. Merged ring: every single ticket is satisfiable, the merge rings.
    # ------------------------------------------------------------------
    banner("6. Per-buyer tickets are fine, but the merged graph is a ring")
    ring_rows = [
        add_ticket("ring-1", "b1", ["x", "y"], "x", "g-ring", weight=20),
        add_ticket("ring-2", "b2", ["y", "z"], "y", "g-ring", weight=20),
        add_ticket("ring-3", "b3", ["z", "x"], "z", "g-ring", weight=20),
    ]
    v4 = svc.apply_batch(ring_rows, note="ring receipts").version
    ds4 = os.path.join(args.work, "ds_ring")
    _m4, recs4 = build_dataset(svc, v4, SEED, {"g-ring": 3}, ds4)
    desk4 = PreferenceDesk.from_records(recs4)
    for t in desk4.tickets:
        assert desk4.find_cycle(t.buyer) is None
    cycle = desk4.find_cycle()
    evidence = desk4.cycle_evidence()
    print("merged cycle:", cycle)
    print("evidence:", json.dumps(evidence, ensure_ascii=False))
    try:
        desk4.relation("x", "y")
        raise AssertionError("relation must raise on a cyclic desk")
    except PreferenceCycleError as exc:
        print("relation() raised PreferenceCycleError:", exc)
    assert cycle is not None and len(cycle) >= 4
    write_json(os.path.join(args.work, "ring_evidence.json"),
               {"cycle": cycle, "evidence": evidence})

    # ------------------------------------------------------------------
    # 7. Retract the sampled ticket: the true next-ranked row enters.
    # ------------------------------------------------------------------
    banner("7. Retract a sampled receipt; the real next row enters the sample")
    svc.apply_batch([{"op": "retract", "row_id": "t-lin-1"}],
                    note="retract selected lin ticket")
    v5 = svc.latest_version()
    ds5 = os.path.join(args.work, "ds_A_v5_retracted")
    m5, recs5, refresh_report = refresh_dataset(
        svc, ds3, new_version=v5, out_dir=ds5)
    ids5 = [r["row_id"] for r in recs5]
    print("sample after retraction:", ids5)
    assert "t-lin-1" not in ids5 and "t-lin-2" in ids5
    # Historical version still replays byte-for-byte.
    hist_manifest, hist_records = load_dataset(ds1)
    assert [r["row_id"] for r in hist_records] == ["t-lin-1", "t-qi-1"]
    print("historical dataset ds_A_v1 still replays:",
          [r["row_id"] for r in hist_records])
    write_json(os.path.join(args.work, "retraction_refresh.json"),
               refresh_report)

    # ------------------------------------------------------------------
    # Session B: same two buyers, both selectable opening modes.
    # ------------------------------------------------------------------
    banner("8. Session B: reserve mode and restock mode")
    rows_b = [
        # A single-variety receipt carries no 甲 vs 乙 constraint, so 小林's
        # preference between the two teas stays unknown until more evidence.
        add_ticket("b-lin-unknown", LIN, [TEA_A], TEA_A, "g-lin",
                   weight=99),
        add_ticket("b-lin-constrained", LIN, [TEA_A, TEA_B], TEA_A, "g-lin",
                   weight=1),
        add_ticket("b-qi-yi", QI, [TEA_B], TEA_B, "g-qi", weight=50),
    ]
    vb = svc.apply_batch(rows_b, note="session B receipts").version
    dsb = os.path.join(args.work, "ds_B")
    _mb, recsb = build_dataset(svc, vb, SEED,
                               {"g-lin": 1, "g-qi": 1}, dsb)
    deskb = PreferenceDesk.from_records(recsb)
    print("session B sampled receipts:", [r["row_id"] for r in recsb])
    # The selected ticket only ever saw 甲, so the 甲/乙 order is unknown:
    # both directions remain compatible.
    assert deskb.relation(TEA_A, TEA_B, LIN) == "both_possible"
    assert deskb.possible_picks(LIN, [TEA_A, TEA_B]) == [TEA_A, TEA_B]
    boxes_b = (Box("B-case", {TEA_A: 1, TEA_B: 1}, price=10),)
    buyers_b = (
        Buyer(LIN, 0, acceptable=(TEA_A, TEA_B),
              visible=(TEA_A, TEA_B)),
        Buyer(QI, 1, acceptable=(TEA_B,), visible=(TEA_B,)),
    )

    # 8a. Reserve 乙 for 小齐: 小林 then only sees 甲.
    cfg_res = CabinetConfig(
        varieties=(TEA_A, TEA_B), boxes=boxes_b, buyers=buyers_b,
        budget=100, mode=MODE_RESERVE,
        reservations=(Reservation(QI, TEA_B, fee=2),))
    report_res = CabinetEngine(
        cfg_res, desk_possible_picks(deskb)).evaluate()
    print_report("reserve mode: 乙 split out for 小齐", report_res)
    cheapest_res = report_res.cheapest
    assert cheapest_res is not None
    assert cheapest_res.total_cost == 12  # 10 box + 2 splitting labour
    plan_res = build_plan(cfg_res, boxes_b)
    assert plan_res.public_counts() == {TEA_A: 1, TEA_B: 0}
    handovers_res = CabinetEngine(
        cfg_res, desk_possible_picks(deskb)).execute(
        plan_res, {LIN: (TEA_A, TEA_B), QI: (TEA_B, TEA_A)})
    order_b1 = ledger.open_order(
        MODE_RESERVE, dsb, vb, SEED.hex(), {"g-lin": 1, "g-qi": 1}, recsb,
        note="session B reserve mode")
    ledger.record_handovers(order_b1, handovers_res)
    assert [h.variety for h in handovers_res] == [TEA_A, TEA_B]
    assert handovers_res[1].source == "reserved"
    write_json(os.path.join(args.work, "session_B_reserve.json"),
               report_res.as_dict())

    # 8b. Public shelf: 小林 really took 乙; restock/erratum arrive too late.
    # The sampled desk leaves 小林's order partly unknown: both picks stay
    # compatible.  With two buyers any restock right called after 小林's
    # handover can only land after 小齐 has already picked, so no plan can
    # promise the gift on the adversarial branch.
    cfg_rest = CabinetConfig(
        varieties=(TEA_A, TEA_B), boxes=boxes_b, buyers=buyers_b,
        budget=100, mode=MODE_RESTOCK,
        restock_rights=(RestockRight("right-yi", TEA_B, order_fee=3,
                                     execute_fee=4),))
    report_rest = CabinetEngine(
        cfg_rest, desk_possible_picks(deskb)).evaluate()
    print_report("restock mode: 乙 taken first, right arrives after next pick",
                 report_rest)
    assert report_rest.cheapest is None
    failing = plan_with_boxes(report_rest, ("B-case",))
    assert failing.first_failed_buyer == QI
    # In the failure story 小林 really takes the only 乙 and 小齐 then faces
    # an empty shelf; no event hands the restock pack out in time.
    assert [e.picked for e in failing.witness if e.picked] == [TEA_B]
    assert all(e.source != "restock" for e in failing.witness)
    write_json(os.path.join(args.work, "session_B_restock.json"),
               report_rest.as_dict())

    # Concrete run with the adversarial order: failure is raised and recorded.
    plan_rest = build_plan(cfg_rest, boxes_b, cfg_rest.restock_rights)
    engine_rest = CabinetEngine(cfg_rest, desk_possible_picks(deskb))
    order_b2 = ledger.open_order(
        MODE_RESTOCK, dsb, vb, SEED.hex(), {"g-lin": 1, "g-qi": 1}, recsb,
        note="session B restock mode, late erratum scenario")
    try:
        engine_rest.execute(
            plan_rest, {LIN: (TEA_B, TEA_A), QI: (TEA_B, TEA_A)},
            restock_policy="none")
        raise AssertionError("the run must fail for 小齐")
    except Exception as exc:  # CabinetFailure
        from wrsource.cabinet import CabinetFailure
        assert isinstance(exc, CabinetFailure) and exc.buyer == QI
        ledger.record_handovers(order_b2, exc.handovers)
        ledger.record_failure(order_b2, QI, exc.step,
                              [h.as_dict() for h in exc.handovers])
        print("concrete run failed as expected for", exc.buyer,
              "after handovers", [h.as_dict() for h in exc.handovers])

    # 8c. The erratum contradicts an already handed-out pack: flag, no rewrite.
    lin_pickup = ledger.pickups_for(order_b2, LIN)[0]
    old_ticket = next(t for t in ledger.tickets(order_b2) if t.buyer == LIN)
    new_ticket = {**old_ticket.as_dict(), "picked": TEA_A}
    ledger.record_correction(
        order_b2, LIN, old_ticket.as_dict(), new_ticket,
        contradicts_pack=lin_pickup)
    reopened = ledger.reopen_order(order_b2)
    print("contradiction events:",
          json.dumps(reopened["contradictions"], ensure_ascii=False))
    assert reopened["contradictions"], "contradiction must be recorded"
    # The original pickup is still exactly the handed-out 乙 pack.
    assert ledger.pickups_for(order_b2, LIN) == [lin_pickup]
    assert lin_pickup["variety"] == TEA_B
    write_json(os.path.join(args.work, "session_B_reopen.json"), reopened)

    # ------------------------------------------------------------------
    # 9. Reopening yesterday's orders independently replays everything.
    # ------------------------------------------------------------------
    banner("9. Counter replay: reopen every order id")
    summary = []
    for oid in [order_a, order_a2, order_b1, order_b2]:
        data = ledger.reopen_order(oid)
        summary.append({
            "order_id": data["order_id"],
            "mode": data["mode"],
            "source_version": data["source_version"],
            "ticket_row_ids": [t["row_id"] for t in data["tickets"]],
            "pickups": data["pickups"],
            "contradictions": len(data["contradictions"]),
        })
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    write_json(os.path.join(args.work, "order_replay_summary.json"), summary)
    print("\nwork directory:", args.work)


if __name__ == "__main__":
    main()
