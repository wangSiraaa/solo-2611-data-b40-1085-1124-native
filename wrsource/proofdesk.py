"""Proof desk binding the steno domain to the sampled source repository.

The practice cards live in the *same* revisionable weighted source as every
other record (group ``practice`` by default).  A card body is JSON shaped by
:class:`wrsource.steno.Card`, so the existing machinery already covers every
required workflow:

* sampling selects the exact ``row_id`` set and the fixed source version;
* ``set_body`` revises a card's printed proof (e.g. re-defining what stroke
  ``u`` means) and publishes a new immutable version;
* ``retract`` withdraws a sampled card and the incremental refresh replaces it
  with the true next-ranked row rather than re-drawing the old sample.

This module only adds: loading the manual, reading cards out of a real
sampled dataset directory (the records written by :mod:`wrsource.dataset`),
inducing the one-command-per-stroke table, proofreading a newly delivered
manuscript and running a press query.  It also exposes a small CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .dataset import load_dataset
from .printshop import press_query
from .source import SourceService
from .steno import (CARD_BODY_KIND, Card, Command, Manual, ProofResult,
                    SemanticsReport, apply_semantics, induce_semantics,
                    proofread)

MANUAL_FILE = "manual.json"
CARD_GROUP = "practice"


# ---------------------------------------------------------------------------
# Manual + card loading
# ---------------------------------------------------------------------------

def load_manual(path: str) -> Manual:
    with open(path, encoding="utf-8") as fh:
        return Manual.from_json(fh.read())


def save_manual(manual: Manual, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manual.to_dict(), fh, ensure_ascii=False, sort_keys=True,
                  indent=2)
        fh.write("\n")


def cards_from_dataset(dataset_dir: str,
                       row_ids: Optional[Sequence[str]] = None
                       ) -> List[Card]:
    """Read practice cards from a real sampled directory's ``samples.jsonl``.

    Uses exactly the row ids and versioned bodies the original sampler put on
    disk; nothing is re-sampled here.
    """
    _, records = load_dataset(dataset_dir)
    wanted = set(row_ids) if row_ids is not None else None
    cards: List[Card] = []
    for rec in records:
        if wanted is not None and rec["row_id"] not in wanted:
            continue
        body = rec["body"]
        if isinstance(body, str):
            parsed = json.loads(body)
        else:
            parsed = body
        if not isinstance(parsed, dict) or parsed.get("kind") != CARD_BODY_KIND:
            continue
        cards.append(Card.from_row(rec))
    return cards


def cards_from_source(service: SourceService, version: int,
                      row_ids: Sequence[str]) -> List[Card]:
    """Read specific cards straight from a fixed source version."""
    rows = service.get_rows(version, row_ids)
    missing = [rid for rid in row_ids if rid not in rows]
    if missing:
        raise KeyError(f"row ids missing from version {version}: {missing}")
    return [Card.from_row(rows[rid]) for rid in row_ids]


# ---------------------------------------------------------------------------
# Desk workflows
# ---------------------------------------------------------------------------

def induce_from_dataset(dataset_dir: str, manual: Manual
                        ) -> SemanticsReport:
    cards = cards_from_dataset(dataset_dir)
    return induce_semantics(cards, manual)


def proofread_dataset(dataset_dir: str, manual: Manual, gestures: str
                      ) -> ProofResult:
    """Proofread an as-yet-untypeset manuscript against sampled evidence."""
    report = induce_from_dataset(dataset_dir, manual)
    apply_semantics(manual, report)
    result = proofread(manual, gestures)
    return result


def retract_and_refresh(service: SourceService, dataset_dir: str,
                        card_row_id: str, out_dir: Optional[str] = None
                        ) -> dict:
    """Withdraw one sampled card and re-issue the sample at the new version."""
    result = service.apply_batch(
        [{"op": "retract", "row_id": card_row_id}],
        note=f"retract practice card {card_row_id}")
    if not result.created:
        raise RuntimeError(
            f"retracting {card_row_id!r} produced no new source version")
    from .dataset import refresh_dataset
    _, records, refresh_report = refresh_dataset(
        service, dataset_dir, new_version=result.version, out_dir=out_dir)
    return {
        "retracted": card_row_id,
        "new_version": result.version,
        "refresh": refresh_report,
        "records": len(records),
    }


def revise_card_body(service: SourceService, card_row_id: str,
                     gestures: str, printed: Sequence[Command]) -> int:
    """Publish a new source version carrying a corrected practice card."""
    card = Card(card_row_id, gestures, tuple(printed))
    result = service.apply_batch(
        [{"op": "set_body", "row_id": card_row_id,
          "body": card.body_json()}],
        note=f"revise practice card {card_row_id}")
    if not result.created:
        raise RuntimeError("card revision changed nothing")
    return result.version


def ingest_cards(service: SourceService, cards: Sequence[Card],
                 group: str = CARD_GROUP, shard: str = "s0",
                 weight: int = 50) -> int:
    """Add practice cards as ordinary weighted source rows."""
    ops = [{
        "op": "add",
        "row_id": card.card_id,
        "group": group,
        "weight": weight,
        "body": card.body_json(),
        "shard": shard,
    } for card in cards]
    if not ops:
        return service.latest_version()
    return service.apply_batch(ops, note="ingest practice cards").version


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrsource.proofdesk",
        description="Stenography proof desk and press process query.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("induce", help="induce stroke semantics from a sample")
    p.add_argument("--dataset", required=True)
    p.add_argument("--manual", required=True)
    p.add_argument("--apply", action="store_true",
                   help="write the determined meanings back to the manual")

    p = sub.add_parser("proofread", help="list all legal readings")
    p.add_argument("--dataset", required=True)
    p.add_argument("--manual", required=True)
    p.add_argument("--gestures", required=True)

    p = sub.add_parser("press", help="shared/reserve press process query")
    p.add_argument("--manual", required=True)
    p.add_argument("--gestures", required=True)
    p.add_argument("--initial-text", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--deadline-ligatures", type=int, required=True)
    p.add_argument("--sorts", required=True,
                   help='JSON object of face -> count, e.g. {"甲":2,"乙":1}')
    p.add_argument("--dataset",
                   help="sampled dataset whose cards induce stroke meanings")
    p.add_argument("--semantics",
                   help="optional JSON {stroke: token} overriding the manual")

    p = sub.add_parser("retract", help="retract a sampled card and refresh")
    p.add_argument("--db", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--row-id", required=True)
    p.add_argument("--out")

    p = sub.add_parser("schedule",
                       help="run a whole delivery-order press schedule")
    p.add_argument("--manual", required=True)
    p.add_argument("--plan", required=True,
                   help="JSON file: {box:{...}, process, manuscripts:[...]}")
    p.add_argument("--dataset",
                   help="sampled dataset whose cards induce stroke meanings")

    args = parser.parse_args(argv)

    if args.cmd == "induce":
        manual = load_manual(args.manual)
        report = induce_from_dataset(args.dataset, manual)
        if args.apply:
            apply_semantics(manual, report)
            save_manual(manual, args.manual)
        _print(report.as_dict())
    elif args.cmd == "proofread":
        manual = load_manual(args.manual)
        result = proofread_dataset(args.dataset, manual, args.gestures)
        _print(result.as_dict())
    elif args.cmd == "press":
        manual = load_manual(args.manual)
        if args.dataset:
            apply_semantics(manual, induce_from_dataset(args.dataset, manual))
        if args.semantics:
            for stroke, token in json.loads(args.semantics).items():
                manual.define(stroke, Command.parse(token), replace=True)
        result = proofread(manual, args.gestures)
        if not result.readings:
            _print({"error": "no legal readings", "stuck": result.stuck,
                    "unknown_strokes": result.unknown_strokes})
            return 2
        answer = press_query(args.initial_text, result.readings, args.title,
                             args.deadline_ligatures,
                             json.loads(args.sorts))
        _print(answer)
    elif args.cmd == "retract":
        service = SourceService(args.db)
        service._ensure()
        _print(retract_and_refresh(service, args.dataset, args.row_id,
                                   out_dir=args.out))
    elif args.cmd == "schedule":
        from .printshop import ManuscriptOrder, run_press_schedule
        manual = load_manual(args.manual)
        if args.dataset:
            apply_semantics(manual, induce_from_dataset(args.dataset, manual))
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        order = []
        for item in plan["manuscripts"]:
            result = proofread(manual, item["gestures"])
            if not result.readings:
                raise SystemExit(
                    f"manuscript {item.get('name')!r} has no legal readings: "
                    f"{result.stuck} unknown={result.unknown_strokes}")
            order.append(ManuscriptOrder(
                name=item["name"],
                initial_text=item["initial_text"],
                readings=result.readings,
                target_title=item["title"],
                morning_ligatures=item.get("morning_ligatures", 0)))
        answer = run_press_schedule(plan["box"], order, plan["process"])
        _print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
