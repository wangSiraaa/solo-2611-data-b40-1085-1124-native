"""End-to-end demonstration of the banquet reconstruction layer.

Runs on the *unmodified* citation products under ``exports/scenario/`` and on
hand-built night payloads that reproduce every concrete case in the task
specification.  Each step's interpretation sheet is written under
``exports/banquet/`` so old and new sheets can be compared at any time.

Nothing here parses PDFs/images or performs speech recognition: the four-chair
and red/blue scenarios are pure symbolic seat-language puzzles, at most eight
people and three rounds.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from banquet.decode import Script, decode_ordinance  # noqa: E402
from banquet.control import (  # noqa: E402
    NoControl,
    Night,
    ScreenControl,
    SeatSwap,
    SwapControl,
    can_install_screen,
    find_swap_seats,
)
from banquet.model import (  # noqa: E402
    Conversation,
    Edge,
    Guest,
    OrderClause,
    PageOrdinance,
    RevocationClause,
    Round,
    SeatChart,
    SecretGroup,
    build_ordinance,
    enumerate_compliant_charts,
)
from banquet.relay import plan_round  # noqa: E402
from banquet.products import load_product_files  # noqa: E402
from banquet.seating import seat_session  # noqa: E402
from banquet.sheet import build_sheet  # noqa: E402

MATERIALS = ROOT / "materials" / "726"
EXPORTS = ROOT / "exports" / "banquet"
SCENARIO = ROOT / "exports" / "scenario"

A = Guest("甲", frozenset({"red"}))
B = Guest("乙", frozenset({"blue"}))
C = Guest("丙", frozenset({"red", "blue"}))
D = Guest("丁", frozenset({"blue", "green"}))
WU = Guest("戊", frozenset({"blue", "green"}))
FOUR = {g.guest_id: g for g in (A, B, C, D)}
FIVE = {g.guest_id: g for g in (A, B, C, WU, D)}


def write(name: str, data: dict) -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    (EXPORTS / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n=== {name} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1400])


def four_chair_cases() -> dict:
    """The three four-chair verdicts from the spec."""
    talk = Conversation("AB", "甲", "乙", "red", "s_ab", authorized=frozenset({"丙"}))
    out: dict = {"guests": {g: sorted(FOUR[g].languages) for g in FOUR}}

    orig = build_ordinance(
        [PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁")))]
    )
    enum = enumerate_compliant_charts(orig, ["甲", "乙", "丙", "丁"])
    chart = enum.charts[0]
    plan = plan_round(chart, Round("r1", (talk,)), FOUR)
    out["original_order"] = {
        "chart": list(chart.seats),
        "feasible": plan.ok,
        "path": list(plan.routes[0].path) if plan.ok else None,
        "languages": list(plan.routes[0].languages) if plan.ok else None,
        "interpreter": sorted(plan.routes[0].interpreters) if plan.ok else None,
    }

    errata = build_ordinance(
        [
            PageOrdinance("op1", OrderClause("op1", ("甲", "丙", "乙", "丁"))),
            PageOrdinance("op2", RevocationClause("op2", "op1")),
            PageOrdinance("op3", OrderClause("op3", ("甲", "乙", "丙", "丁"))),
        ]
    )
    enum2 = enumerate_compliant_charts(errata, ["甲", "乙", "丙", "丁"])
    plan2 = plan_round(enum2.charts[0], Round("r1", (talk,)), FOUR)
    out["errata_甲_乙_丙_丁"] = {
        "chart": list(enum2.charts[0].seats),
        "feasible": plan2.ok,
        "failure": plan2.failures[0].reason if not plan2.ok else None,
    }

    no_perm = Conversation("AB", "甲", "乙", "red", "s_ab", authorized=frozenset())
    plan3 = plan_round(chart, Round("r1", (no_perm,)), FOUR)
    out["restore_order_revoke_c_permission"] = {
        "chart": list(chart.seats),
        "feasible": plan3.ok,
        "failure": plan3.failures[0].reason if not plan3.ok else None,
    }
    return out


def red_blue_scenario() -> dict:
    """Red spoken earlier by 丙; blue conveyed by 戊; 丙 sits adjacent 戊."""
    red = Conversation("R1", "甲", "乙", "red", "red", authorized=frozenset({"丙"}))
    blue = Conversation("R2", "戊", "丁", "blue", "blue", incidental=True)
    prior_chart = SeatChart(("甲", "丙", "乙", "丁", "戊", None))
    base_chart = SeatChart(("甲", "丙", "戊", "丁", "乙", None))
    seam = Edge("丙", "戊")
    groups = [SecretGroup("red_vs_blue", frozenset({"red", "blue"}))]

    def night(rounds):
        return Night(
            guests=FIVE,
            base_chart=base_chart,
            rounds=tuple(rounds),
            ordinance=build_ordinance([]),
            secret_groups=tuple(groups),
            prior=((prior_chart, Round("R1r", (red,))),),
        )

    out: dict = {"base_chart": list(base_chart.seats), "seam": "丙-戊"}

    # (a) no control: 丙 incidentally hears blue -> single-interpreter breach.
    n = night([Round("R2r", (blue,))])
    sheet = build_sheet(n, NoControl())
    out["no_control"] = {
        "status": sheet.result.status,
        "breaches": sheet.as_dict()["breaches"],
        "diagnostics": list(sheet.diagnostics),
    }

    # (b) screen installed in an empty round -> seals 丙's leak.
    n2 = night([Round("empty", ()), Round("R2r", (blue,))])
    check = can_install_screen(n2, seam, before_round=1)
    sheet2 = build_sheet(n2, ScreenControl(seam=seam, install_round=check.install_round))
    out["screen_with_empty_round"] = {
        "install_round": check.install_round,
        "status": sheet2.result.status,
        "signed": sheet2.signed,
        "diagnostics": list(sheet2.diagnostics),
    }

    # (c) only the speaking round remains -> no empty round; cannot install.
    n3 = night([Round("R2r", (blue,))])
    check3 = can_install_screen(n3, seam, before_round=0)
    out["screen_no_empty_round"] = {
        "feasible": check3.feasible,
        "reason": check3.reason,
        "note": "cannot rescue the sheet by rewriting the record",
    }

    # (d) swap 丙 to a non-adjacent compliant unlocked chair.
    n4 = Night(
        guests=FIVE,
        base_chart=base_chart,
        rounds=(Round("R2r", (blue,)),),
        ordinance=build_ordinance([]),
        secret_groups=tuple(groups),
        swap_quota=(1,),
        prior=((prior_chart, Round("R1r", (red,))),),
    )
    seats = find_swap_seats(n4, "丙", round_index=0, avoid_adjacent_to=["戊"])
    chosen = seats[0]
    n4b = Night(
        guests=FIVE,
        base_chart=base_chart,
        rounds=(Round("R2r", (blue,)),),
        ordinance=build_ordinance([]),
        secret_groups=tuple(groups),
        swap_quota=(1,),
        prior=((prior_chart, Round("R1r", (red,))),),
    )
    sheet4 = build_sheet(
        n4b,
        SwapControl(swaps=(SeatSwap(0, "丙", chosen.target_index),)),
    )
    out["swap_c_to_nonadjacent"] = {
        "target_chair": chosen.target_index,
        "resulting_chart": list(chosen.resulting_chart.seats),
        "status": sheet4.result.status,
        "signed": sheet4.signed,
    }

    # (e) final twist: 戊 is 丙's registered mutual communicator.
    n5 = Night(
        guests=FIVE,
        base_chart=base_chart,
        rounds=(Round("empty", ()), Round("R2r", (blue,))),
        ordinance=build_ordinance([]),
        secret_groups=tuple(groups),
        designated_pairs=(frozenset({"丙", "戊"}),),
        prior=((prior_chart, Round("R1r", (red,))),),
    )
    sheet5 = build_sheet(n5, ScreenControl(seam=seam, install_round=0))
    out["designated_pair_breaks"] = {
        "status": sheet5.result.status,
        "signed": sheet5.signed,
        "breaches": sheet5.as_dict()["breaches"],
    }
    return out


def real_products_flow() -> dict:
    """Use the *unmodified* materials exports as the citation backend."""
    usable = SCENARIO / "00_initial" / "usable.jsonl"
    attn = SCENARIO / "00_initial" / "needs_attention.jsonl"
    products = load_product_files(usable, attn)
    script = Script.from_dict(
        {
            "guests": {"甲": ["red"], "乙": ["blue"], "丙": ["red", "blue"]},
            "identities": {"V1/p2": "甲", "V1/p3": "乙"},
            "ordinances": [
                {"page_id": "p2", "kind": "order", "sequence": ["甲", "丙", "乙"]},
                {"page_id": "p3", "kind": "adjacency", "pairs": [["甲", "乙"]]},
                {"page_id": "p4", "kind": "revoke", "target_page_id": "p2"},
            ],
        }
    )
    decoded = decode_ordinance(products, script)
    result = None
    if decoded.clean:
        result = seat_session(
            products, decoded.ordinance_pages,
            guests=decoded.guests.values(),
            identity_of=decoded.identity_of,
        )
    return {
        "suspend_reasons": products.suspend_reasons(),
        "catalog_revision": products.single_revision(),
        "signin_pages": [f"{v}/{p}" for v, p in products.usable_pinned()[0].page_keys],
        "ordinance_pages": [f"{v}/{p}" for v, p in products.usable_ranges()[0].page_keys],
        "decoded_clean": decoded.clean,
        "decoded_problems": list(decoded.problems),
        "seating_status": result.status if result else "suspended",
        "chart_count": len(result.charts) if result else 0,
        "basis": list(result.basis) if result else [],
    }


def main() -> int:
    if EXPORTS.exists():
        shutil.rmtree(EXPORTS)
    EXPORTS.mkdir(parents=True, exist_ok=True)

    index = {
        "four_chair": four_chair_cases(),
        "red_blue": red_blue_scenario(),
        "real_products_flow": real_products_flow(),
    }
    write("index", index)
    print(f"\nWrote {EXPORTS / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
