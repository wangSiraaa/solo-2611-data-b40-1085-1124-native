#!/usr/bin/env python3
"""End-to-end demo: stenography proof desk + morning/evening press room.

Run from the repository root::

    python3 demo/run_press_demo.py

It builds a fresh SQLite source holding real practice cards, samples them
with the original weighted sampler into a real dataset directory, then walks
through every scenario from the task:

1. cards ``uv``: stroke u deletes, v inserts 丙 -> prints 甲丙;
2. re-define u as "back" (a card body revision, new source version) -> 甲丙乙;
3. restore the evidence and extend the locked prefix -> first stroke stops;
4. a ligature with two cuts reaching the same final text, one crossing the
   locked prefix mid-run;
5. the late continuation with two legal readings, under the shared-form and
   reserve-form press processes, including the box with only one 甲 sort;
6. retract one sampled practice card and re-issue the sample by refresh.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wrsource.dataset import build_dataset, refresh_dataset
from wrsource.printshop import (TypeBox, analyze_safety, plan_shared_column,
                                press_query, simulate)
from wrsource.proofdesk import (MANUAL_FILE, cards_from_dataset,
                                induce_from_dataset, ingest_cards,
                                load_manual, retract_and_refresh,
                                revise_card_body, save_manual)
from wrsource.source import SourceService
from wrsource.steno import (BACK, DELETE, INSERT, Card, Command, Manual,
                            apply_semantics, induce_semantics, proofread)

SEED = bytes(range(32))
QUOTA = 10


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def show(title, obj):
    print(f"-- {title}")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(obj)


def build_manual() -> Manual:
    # Exactly six short strokes; the lexicon lists every legal cut.
    # u = delete, v = insert 丙 (the printed proof evidence), w = back,
    # x = insert 丁, a = insert 甲, b = insert 乙 (the late continuation).
    return Manual({
        "u": [("u",)],
        "v": [("v",)],
        "w": [("w",)],
        "x": [("x",)],
        "a": [("a",)],
        "b": [("b",)],
        # Two cuts; without a lock both end at 甲丁.  The long cut removes
        # the locked 甲 and re-casts it, the short cut only appends 丁.
        "y": [("u", "a", "x"), ("x",)],
        # The late ligature: keep 甲 then append 乙, or delete 甲 and re-cast
        # 甲乙.  Both free runs end at the same title 甲乙.
        "n": [("b",), ("u", "a", "b")],
    })


def build_cards():
    ins = lambda ch: Command(INSERT, ch)
    return [
        # The three cards fixing the original meanings of u, v, w, x.
        Card("card-001", "uv", (Command(DELETE), ins("丙"))),
        Card("card-002", "vw", (ins("丙"), Command(BACK))),
        Card("card-003", "vx", (ins("丙"), ins("丁"))),
        # No complete cut supports this printed string: the only cut of "wu"
        # has two strokes against three printed commands.
        Card("card-004", "wu", (Command(DELETE), ins("丙"), ins("丁"))),
        # Unused distractor cards so a retraction has a true next-ranked row.
        # Every card keeps the manual-wide one-meaning rule: u=delete,
        # v=insert 丙, w=back, x=insert 丁.
        Card("card-005", "wx", (Command(BACK), ins("丁"))),
        Card("card-006", "xv", (ins("丁"), ins("丙"))),
        Card("card-007", "vv", (ins("丙"), ins("丙"))),
        Card("card-008", "ww", (Command(BACK), Command(BACK))),
        Card("card-009", "uw", (Command(DELETE), Command(BACK))),
        Card("card-010", "xw", (ins("丁"), Command(BACK))),
        # Fixes the meaning of stroke a (re-cast 甲) used by the long cut of y.
        Card("card-011", "au", (ins("甲"), Command(DELETE))),
        # Extra supported distractor: after retracting one sampled card this
        # is the true next-ranked row the refresh pulls in.
        Card("card-012", "vu", (ins("丙"), Command(DELETE))),
        # Fixes stroke b = insert 乙 used by the late ligature n.
        Card("card-013", "bx", (ins("乙"), ins("丁"))),
    ]


def main():
    work = os.path.join(HERE, "_press_work")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    db = os.path.join(work, "source.sqlite")
    manual_path = os.path.join(work, MANUAL_FILE)

    banner("1. Ingest practice cards and sample them with the original sampler")
    svc = SourceService(db)
    svc.init()
    cards = build_cards()
    v1 = ingest_cards(svc, cards)
    manual = build_manual()
    save_manual(manual, manual_path)
    ds1 = os.path.join(work, "ds_v1")
    manifest, records = build_dataset(
        svc, v1, SEED, {"practice": QUOTA}, ds1)
    sampled_ids = [r["row_id"] for r in records]
    show("source version", v1)
    show("sampled row ids (exact sampler output)", sampled_ids)

    banner("2. Induce the one-command-per-stroke table from the sampled cards")
    report = induce_from_dataset(ds1, manual)
    apply_semantics(manual, report)
    save_manual(manual, manual_path)
    show("semantics report", report.as_dict())
    # card-004 never makes the quota; check its rejection through the pure
    # domain API instead (a suffix-only / wrong-length reading is not support).
    direct = induce_semantics(cards, build_manual())
    assert direct.unsupported_cards == ["card-004"]
    assert report.unsupported_cards == []
    assert report.conflicts == {}
    assert manual.meaning("u") == Command(DELETE)
    assert manual.meaning("v") == Command(INSERT, "丙")

    banner("3. New manuscript 'uv': complete cuts only, prints 甲丙")
    result = proofread(manual, "uv")
    show("readings of uv", result.as_dict())
    assert len(result.readings) == 1
    outcome = simulate("甲乙", result.readings[0].commands, TypeBox({"丙": 1}))
    show("run on the empty-locked stick 甲乙 (cursor at end)", outcome)
    assert outcome["text"] == "甲丙" and not outcome["stopped"]

    banner("4. Re-define u as 'back' by revising the sampled card (new version)")
    v2 = revise_card_body(svc, "card-001", "uv",
                          (Command(BACK), Command(INSERT, "丙")))
    ds2 = os.path.join(work, "ds_v2")
    refresh_dataset(svc, ds1, new_version=v2, out_dir=ds2)
    manual2 = load_manual(manual_path)
    report2 = induce_from_dataset(ds2, manual2)
    show("induced semantics after card revision", report2.as_dict())
    # The other sampled cards still witness u=delete, so induction reports a
    # manual-wide conflict instead of silently guessing.  The desk applies the
    # revised card's evidence explicitly (one meaning per stroke, replaced).
    assert set(report2.conflicts["u"]["tokens"]) == {"back", "delete"}
    manual2.define("u", Command(BACK), replace=True)
    save_manual(manual2, manual_path)
    assert manual2.meaning("u") == Command(BACK)
    outcome2 = simulate("甲乙", proofread(manual2, "uv").readings[0].commands,
                        TypeBox({"丙": 1}))
    show("uv now runs as back then insert:丙", outcome2)
    assert outcome2["text"] == "甲丙乙"

    banner("5. Restore the evidence; extend the lock to 甲乙 -> first stroke stops")
    v3 = revise_card_body(svc, "card-001", "uv",
                          (Command(DELETE), Command(INSERT, "丙")))
    ds3 = os.path.join(work, "ds_v3")
    refresh_dataset(svc, ds2, new_version=v3, out_dir=ds3)
    manual3 = load_manual(manual_path)
    apply_semantics(manual3, induce_from_dataset(ds3, manual3))
    assert manual3.meaning("u") == Command(DELETE)
    stopped = simulate("甲乙",
                       proofread(manual3, "uv").readings[0].commands,
                       TypeBox({"丙": 1}), locked=2)
    show("uv against the fully locked stick 甲乙", stopped)
    assert stopped["stopped"] and stopped["events"][0]["status"] == "refused"
    assert stopped["events"][0]["detail"] == \
        "delete would touch locked prefix"

    banner("6. Ligature 'y': two cuts, same free final text, one crosses lock")
    # y = u+a+x = delete 甲, re-cast 甲, append 丁 (crosses the locked prefix)
    #   or x     = append 丁 (safe); the free runs both end at 甲丁.
    y_result = proofread(manual3, "y")
    show("readings of y", y_result.as_dict())
    assert len(y_result.readings) == 2
    free = [simulate("甲", r.commands, TypeBox({"甲": 2, "丁": 2}))
            for r in y_result.readings]
    show("free runs (no lock)", [f["text"] for f in free])
    assert all(f["text"] == "甲丁" for f in free)
    safety = analyze_safety("甲", y_result.readings, "甲丁",
                            locked=1, box=TypeBox({"甲": 2, "丁": 2}))
    show("safety against the locked prefix 甲", safety.as_dict())
    assert not safety.all_safe
    assert safety.earliest_violation["stopped_step"] == 0
    assert safety.earliest_violation["detail"] == \
        "delete would touch locked prefix"
    assert safety.opposite_reading is not None
    assert safety.opposite_reading["tokens"] == ["insert:丁"]

    banner("7. Late ligature 'n': shared-form vs reserve-form press process")
    n_result = proofread(manual3, "n")
    show("readings of n", n_result.as_dict())
    # n = b = insert 乙 (keep the morning 甲),
    # or u+a+b = delete 甲, re-cast 甲, insert 乙; both free runs end at 甲乙.
    # The morning deadline precedes this ligature (deadline_ligatures=0), so
    # the morning sheet must not assume which continuation arrives.
    answer = press_query("甲", n_result.readings, "甲乙",
                         deadline_ligatures=0,
                         box_sorts={"甲": 2, "乙": 1})
    show("full press query (box: two 甲, one 乙)", answer)
    shared = answer["shared"]
    # Locking the morning 甲 would make the delete-reading cross the lock;
    # the longest prefix every continuation safely accepts is therefore zero.
    assert shared["longest_safe_length"] == 0
    assert shared["morning_blocker"]["reading_index"] == 1
    # With no locked morning sorts the same stick can still run both at night.
    assert shared["evening_possible"]
    assert all(e["delivered"] for e in shared["evening_readings"])
    reserve = answer["reserve"]
    assert reserve["feasible"] and reserve["all_delivered"]
    assert {e["form"] for e in reserve["evening_readings"]} == \
        {"main", "reserve"}
    assert answer["box_initial"] == {"甲": 2, "乙": 1}

    banner("8. Withdraw one 甲 sort: the reserve form can no longer be cast")
    answer_one = press_query("甲", n_result.readings, "甲乙",
                             deadline_ligatures=0,
                             box_sorts={"甲": 1, "乙": 1})
    show("reserve feasibility with a single 甲",
         {"reserve": answer_one["reserve"],
          "shared": answer_one["shared"]})
    assert not answer_one["reserve"]["feasible"]
    assert "missing" in answer_one["reserve"]["reason"]
    # The morning sheet's sorts cannot be reclaimed for the night run, and
    # even the single-stick shared form still cannot lock anything up front.
    shared_one = plan_shared_column(
        "甲", n_result.readings, "甲乙", 0, TypeBox({"甲": 1, "乙": 1}))
    assert shared_one.longest_safe_length == 0
    assert shared_one.evening_possible

    banner("9. Retract one sampled practice card and re-issue the sample")
    before = cards_from_dataset(ds3)
    target = "card-005" if "card-005" in sampled_ids else sampled_ids[-1]
    outcome = retract_and_refresh(svc, ds3, target,
                                  out_dir=os.path.join(work, "ds_v4"))
    after = cards_from_dataset(os.path.join(work, "ds_v4"))
    show("retraction + refresh report",
         {"report": outcome["refresh"], "new_version": outcome["new_version"]})
    show("sampled card ids before/after",
         {"before": [c.card_id for c in before],
          "after": [c.card_id for c in after]})
    assert target not in [c.card_id for c in after]
    assert len(after) == QUOTA
    # The replacement row is the true next-ranked row: a full rebuild at the
    # new version must give the identical sample.
    full_dir = os.path.join(work, "ds_v4_full")
    build_dataset(svc, outcome["new_version"], SEED, {"practice": QUOTA},
                  full_dir)
    from wrsource.dataset import compare_datasets
    cmp = compare_datasets(os.path.join(work, "ds_v4"), full_dir)
    show("refresh == full rebuild", cmp)
    assert cmp["equal"]

    banner("Replay with the CLI")
    print(f"""
python3 -m wrsource.proofdesk induce    --dataset {ds3} --manual {manual_path} --apply
python3 -m wrsource.proofdesk proofread --dataset {ds3} --manual {manual_path} --gestures y
python3 -m wrsource.proofdesk press     --manual {manual_path} --dataset {ds3} --gestures n \\
        --initial-text 甲 --title 甲乙 --deadline-ligatures 0 \\
        --sorts '{{"甲":2,"乙":1}}'
python3 -m wrsource.proofdesk retract   --db {db} --dataset {ds3} --row-id {target}
""")
    print("work directory:", work)


if __name__ == "__main__":
    main()
