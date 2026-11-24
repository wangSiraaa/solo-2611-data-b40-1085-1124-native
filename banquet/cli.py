"""Command-line interface for the banquet reconstruction layer.

Subcommands:

* ``seat``  -- from citation products + a ceremony script, list every compliant
  seating chart (or the reviewable basis that there is none), or suspend.
* ``sheet`` -- on a chosen chart, simulate the night under a control (none /
  screen / swap) and emit the interpretation sheet, including breaches and
  venue diagnostics.

The CLI never reads the citation database: it consumes exported
``citation-product/1`` JSONL exactly like the read-only client.  A *script*
supplies the ritual meaning of each page and the guest directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .control import (
    Night,
    NoControl,
    ScreenControl,
    SeatSwap,
    SwapControl,
    can_install_screen,
)
from .decode import Script, decode_clause, decode_ordinance
from .model import (
    Conversation,
    Edge,
    PageOrdinance,
    Round,
    SecretGroup,
    SeatChart,
    build_ordinance,
)
from .products import ProductError, load_product_files
from .seating import seat_session
from .sheet import build_sheet


def _load_products(paths: list[str]) -> Any:
    return load_product_files(*paths)


def _night_from_payload(payload: dict, script: Script, control_kind: str) -> Night:
    chart = SeatChart(tuple(payload["chart"]))
    rounds = []
    for r in payload.get("rounds", []):
        convs = []
        for c in r.get("conversations", []):
            convs.append(
                Conversation(
                    conv_id=c["conv_id"],
                    speaker_a=c["speaker_a"],
                    speaker_b=c["speaker_b"],
                    language=c.get("language", ""),
                    secret=c.get("secret", ""),
                    authorized=frozenset(c.get("authorized", [])),
                    incidental=bool(c.get("incidental", False)),
                )
            )
        rounds.append(Round(r["round_id"], tuple(convs)))
    prior = []
    for p in payload.get("prior", []):
        pr = p["round"]
        convs = [
            Conversation(
                conv_id=c["conv_id"], speaker_a=c["speaker_a"],
                speaker_b=c["speaker_b"], language=c.get("language", ""),
                secret=c.get("secret", ""),
                authorized=frozenset(c.get("authorized", [])),
                incidental=bool(c.get("incidental", False)),
            )
            for c in pr.get("conversations", [])
        ]
        prior.append((SeatChart(tuple(p["chart"])), Round(pr["round_id"], tuple(convs))))
    pairs = [frozenset(p) for p in payload.get("designated_pairs", [])]
    groups = [
        SecretGroup(g["group_id"], frozenset(g["secrets"]))
        for g in payload.get("secret_groups", [])
    ]
    quota = tuple(payload.get("swap_quota", []))
    # The ordinance for a night simulation comes from the script's pages, in
    # insertion order; the chart is supplied explicitly in the payload.
    pages = [
        PageOrdinance(pid, decode_clause(pid, spec))
        for pid, spec in script.ordinances.items()
    ]
    return Night(
        guests=dict(script.guests),
        base_chart=chart,
        rounds=tuple(rounds),
        ordinance=build_ordinance(pages),
        designated_pairs=tuple(pairs),
        secret_groups=tuple(groups),
        swap_quota=quota,
        prior=tuple(prior),
    )


def _control_from_args(args, night: Night):
    if args.control == "screen":
        seam = Edge(*sorted(args.seam.split("-")))
        check = can_install_screen(night, seam, before_round=args.before_round)
        if not check.feasible:
            return None, check.reason
        return ScreenControl(seam=seam, install_round=check.install_round), None
    if args.control == "swap":
        swaps = []
        for spec in args.swap or []:
            # format: round_index:guest:target_index
            ri, guest, target = spec.split(":")
            swaps.append(
                SeatSwap(round_index=int(ri), guest_id=guest, target_index=int(target))
            )
        return SwapControl(swaps=tuple(swaps)), None
    return NoControl(), None


def cmd_seat(args: argparse.Namespace) -> int:
    products = _load_products(args.products)
    script = Script.load(args.script)
    decoded = decode_ordinance(
        products, script, range_citation_id=args.range_citation
    )
    product_reasons = list(products.suspend_reasons())
    if not decoded.clean or product_reasons:
        problems = list(dict.fromkeys(product_reasons + list(decoded.problems)))
        print(json.dumps({"status": "suspended", "problems": problems,
                          "catalog_revision": (
                              products.single_revision()
                              if len(products.revisions) == 1 else None)},
                         ensure_ascii=False, indent=2))
        return 0
    result = seat_session(
        products,
        decoded.ordinance_pages,
        guests=decoded.guests.values(),
        identity_of=decoded.identity_of,
        max_results=args.max_results,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    # The sheet simulates a night on a chosen chart; it needs the guest
    # directory from the script, not the RANGE decode (the chart and rounds
    # are supplied explicitly in the night payload).  A script may still carry
    # ordinance pages, but undescribed RANGE pages must not block simulation.
    script = Script.load(args.script)
    with open(args.night, encoding="utf-8") as fh:
        payload = json.load(fh)
    night = _night_from_payload(payload, script, args.control)
    control, reason = _control_from_args(args, night)
    if control is None:
        print(json.dumps({"status": "control_unavailable", "reason": reason},
                         ensure_ascii=False, indent=2))
        return 0
    sheet = build_sheet(night, control)
    print(json.dumps(sheet.as_dict(), ensure_ascii=False, indent=2))
    return 0 if sheet.signed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="banquet")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seat", help="enumerate compliant seat charts")
    p.add_argument("--products", nargs="+", required=True,
                   help="citation-product/1 JSONL files (usable + needs_attention)")
    p.add_argument("--script", required=True, help="ceremony script JSON")
    p.add_argument("--range-citation", default=None)
    p.add_argument("--max-results", type=int, default=2000)
    p.set_defaults(func=cmd_seat)

    p = sub.add_parser("sheet", help="emit the interpretation sheet")
    p.add_argument("--products", nargs="*", default=[],
                   help="ignored for simulation; accepted for symmetry")
    p.add_argument("--script", required=True)
    p.add_argument("--night", required=True, help="night payload JSON")
    p.add_argument("--range-citation", default=None)
    p.add_argument("--control", choices=["none", "screen", "swap"], default="none")
    p.add_argument("--seam", default=None, help="for screen: 'A-B'")
    p.add_argument("--before-round", type=int, default=0)
    p.add_argument("--swap", action="append",
                   help="for swap: 'round:guest:target_index' (repeatable)")
    p.set_defaults(func=cmd_sheet)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ProductError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
