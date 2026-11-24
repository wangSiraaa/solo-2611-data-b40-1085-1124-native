"""End-to-end rerun of the lacquer four-cell examples.

Run with::

    python3 scripts/demo_lacquer.py

It reproduces, from the materials alone:

1. the two still-possible middle-box configurations
   ``(bottom notch 0 / top lug 1)`` and ``(bottom notch 2 / top lug 3)``;
2. the robust base/lid choice under the original gauge board;
3. the same-name page revision ``1+3 -> 2+2`` after which neither the
   notch-1 lid nor the notch-3 lid can be carried away;
4. the closed-loop failure journal;
5. the three work-order variants (two-beat gauge, one-beat bite with and
   without an authorized named spare tongue);
6. sampling restricted to usable pages of an old citation export.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lacquer.model import VesselConfiguration
from lacquer.notebook import JournalBook, CLOSURE_FAILURE
from lacquer.orders import load_pieces, load_work_order
from lacquer.packing import (
    PackingProblem,
    enumerate_worlds,
    find_robust_sheets,
    find_seatings_for_world,
)
from lacquer.planner import evaluate_all_worlds
from lacquer.sampling import reconstruct_from_export

MATERIALS = ROOT / "materials" / "lacquer"


def load_book(*, revised: bool) -> JournalBook:
    book = JournalBook.from_file(MATERIALS / "notebooks.json")
    if revised:
        pieces = json.loads((MATERIALS / "pieces.json").read_text(encoding="utf-8"))
        for change in pieces["revisions"]:
            book.revise_page(
                change["journal_id"],
                change["page_id"],
                step=change["step"],
                feature=change["feature"],
            )
    return book


def middle_configurations(book: JournalBook) -> list[VesselConfiguration]:
    configurations, reports = book.reconstruct_vessel(
        "M", bottom_journal="M.bottom", top_journal="M.top"
    )
    for report in reports:
        assert not report.conflicts, report
    return configurations


def build_problem(
    configurations: list[VesselConfiguration],
    *,
    lid_ids: tuple[str, ...] = ("L1", "L3"),
    base_ids: tuple[str, ...] = ("B0", "B2"),
) -> PackingProblem:
    pieces = json.loads((MATERIALS / "pieces.json").read_text(encoding="utf-8"))
    _, bases, lids, stack_order = load_pieces(pieces)
    bases = [base for base in bases if base.item_id in base_ids]
    lids = [lid for lid in lids if lid.item_id in lid_ids]
    return PackingProblem(
        bases=bases,
        vessel_options={"M": configurations},
        lids=lids,
        stack_order=stack_order,
        # The middle box must be seated upright: zero whole-cell turns.
        pinned_vessel_rotations={"M": 0},
    )


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    # 1) original gauge board ------------------------------------------------
    section("1. original gauge board: two possible middle-box configurations")
    book = load_book(revised=False)
    configurations = middle_configurations(book)
    for configuration in configurations:
        print(
            f"origin {configuration.origin}: bottom notches "
            f"{configuration.bottom.notches}, top lugs {configuration.top.lugs}"
        )

    section("2. seatings per world under the original board (no pre-commitment)")
    # The four-cell example has lugs only on base cell 0.  The two worlds seat
    # with different lid choices, so no single fully-fixed seating survives
    # both and the sheet must wait for an observation.
    problem = build_problem(configurations)
    for world in enumerate_worlds(problem):
        seatings = find_seatings_for_world(problem, world)
        for seating in seatings:
            print(
                f"world o{world['M'].origin}: base {seating['base'].item_id} "
                f"lid {seating['lid'].item_id} vessel rotation "
                f"{[seat.rotation for seat in seating['seats']]}"
            )
    robust = find_robust_sheets(problem)
    print("sheets releasable before any observation:", robust)
    assert not robust, "the two worlds need different seatings; nothing is pre-committable"
    # The originally described real world (origin 0) does lock at zero turns.
    origin_zero = enumerate_worlds(problem)[0]
    zero_turn = [
        seating
        for seating in find_seatings_for_world(problem, origin_zero)
        if seating["base"].item_id == "B0"
        and seating["lid"].item_id == "L1"
        and all(seat.rotation == 0 for seat in seating["seats"])
    ]
    assert zero_turn, "the origin-0 middle box must lock at rotation 0 with B0 + L1"

    # 2) closed-loop failure -------------------------------------------------
    section("3. closed-loop failure journal X")
    failed = book.reconstruct("X.top")
    print("conflicts:", failed.conflicts, "rings:", failed.rings)
    assert failed.conflicts == (CLOSURE_FAILURE,)

    # 3) same-name page revision 1+3 -> 2+2 ----------------------------------
    section("4. same-name page revision 1+3 -> 2+2 (replay both revisions)")
    revised_book = load_book(revised=True)
    revised = middle_configurations(revised_book)
    for configuration in revised:
        print(
            f"revised origin {configuration.origin}: bottom notches "
            f"{configuration.bottom.notches}, top lugs {configuration.top.lugs}"
        )
    replayed = revised_book.reconstruct(
        "M.top", revision_pins={"mt1": 1, "mt2": 1}
    )
    print("replayed revision-1 top lugs:", [ring.lugs for ring in replayed.rings])

    section("5. revised board: neither lid L1 nor lid L3 can be carried away")
    for lid_id in ("L1", "L3"):
        revised_problem = build_problem(revised, lid_ids=(lid_id,))
        seatings = {
            f"o{world['M'].origin}": find_seatings_for_world(revised_problem, world)
            for world in enumerate_worlds(revised_problem)
        }
        print(lid_id, "seatings per world:", {k: len(v) for k, v in seatings.items()})
        assert not find_robust_sheets(revised_problem)
    print("no robust sheet exists with either lid -> cannot lift the stack")

    # 4) workbench / work orders ---------------------------------------------
    section("6. work orders: gauge vs trial bite, beats and spare tongues")
    orders_doc = json.loads((MATERIALS / "work_orders.json").read_text(encoding="utf-8"))
    orders = [load_work_order(entry) for entry in orders_doc["orders"]]
    # The single base plus two lids admit no pre-observation sheet; the bench
    # must distinguish the worlds before it can sign.
    choice_problem = build_problem(configurations)
    assert not find_robust_sheets(choice_problem), "order must not be pre-committable"
    for order in orders:
        print(f"\n-- {order.order_id} (budget {order.beat_budget}) --")
        for result in evaluate_all_worlds(order, choice_problem):
            sheet = result["committed_sheet"]
            sheet_label = (
                f"{sheet['base_id']}+{sheet['lid_id']}" if sheet is not None else "-"
            )
            print(
                f"   real {result['real_world']}: committable={result['committable']} "
                f"sheet={sheet_label} beats={result['beats_used']} "
                f"consumed={result['consumed_tongues']} spares={result['used_spares']} "
                f"reason={result['reason']}"
            )

    # 5) sampling only from usable old-export pages --------------------------
    section("7. sampling restricted to usable citation-export pages")
    # The export pages live in their own volume; alias the rim page ids onto a
    # synthetic volume through a temporary usable product file.
    rows = []
    for journal_id in ("M.bottom", "M.top"):
        page_ids = [page.page_id for page in book.journal(journal_id).pages]
        rows.append(
            {
                "format": "citation-product/1",
                "catalog_revision": 1,
                "citation_id": f"SEG_{journal_id}",
                "text": journal_id,
                "kind": "PINNED",
                "status": "usable",
                "source": {"volume_id": "VLACQ", "page_ids": page_ids},
                "pages": [
                    {
                        "volume_id": "VLACQ",
                        "page_id": page_id,
                        "section_id": "rim",
                        "label": page_id,
                        "active": True,
                    }
                    for page_id in page_ids
                ],
                "reason": None,
            }
        )
    with tempfile.TemporaryDirectory() as tmp:
        alias_path = Path(tmp) / "usable_alias.jsonl"
        alias_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )
        sampled = reconstruct_from_export(
            book, "M.top", [alias_path], volume_id="VLACQ"
        )
        print("sampled pages:", sampled.sampled_pages, "rings:", len(sampled.rings))
        unsampled = reconstruct_from_export(
            book, "X.top", [alias_path], volume_id="VLACQ"
        )
    print("X.top over the same export:", unsampled.conflicts, unsampled.rings)
    assert unsampled.conflicts == ("no_usable_pages",)
    print("\nall lacquer reruns completed")


if __name__ == "__main__":
    main()
