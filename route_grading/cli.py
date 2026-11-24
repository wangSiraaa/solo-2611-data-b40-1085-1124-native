"""Command-line interface for the bouldering route-grading layer.

The route layer is a **consumer** of the existing case-file product: it reads
the ``citation-product/1`` JSONL that ``citation_system`` exports, plus a
*hold sheet* describing each page's geometry/colour/hands, and produces grades
and scheduling decisions.

Typical use::

    # Build a catalog, export the product (existing CLI).
    python3 -m citation_system init --db work/routes.db --pages route_pages.json
    python3 -m citation_system add-references --db work/routes.db --references route_refs.jsonl
    python3 -m citation_system export --db work/routes.db \
        --usable work/u.jsonl --needs-attention work/a.jsonl

    # Grade one route from that product.
    python3 -m route_grading grade --products work/u.jsonl work/a.jsonl \
        --holds route_holds.json --spec route_spec.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .model import grade
from .product import HoldGeometry, RouteSpec, build_route


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_geometry(doc: dict[str, Any]) -> dict[tuple[str, str], HoldGeometry]:
    out: dict[tuple[str, str], HoldGeometry] = {}
    for h in doc.get("holds", []):
        hands = h.get("hands", "LR")
        handset = frozenset(
            {"L"} if hands == "L" else {"R"} if hands == "R" else {"L", "R"}
        )
        geo = HoldGeometry(
            volume_id=h["volume_id"], page_id=h["page_id"],
            x=int(h["x"]), y=int(h["y"]), color=h.get("color", ""), hands=handset,
        )
        out[geo.key] = geo
    return out


def _load_spec(doc: dict[str, Any]) -> RouteSpec:
    def pair(items: list[dict[str, str]]) -> tuple[tuple[str, str], tuple[str, str]]:
        return ((items[0]["volume_id"], items[0]["page_id"]),
                (items[1]["volume_id"], items[1]["page_id"]))

    return RouteSpec(
        route_id=doc["route_id"], name=doc.get("name", doc["route_id"]),
        range_citation_id=doc["range_citation_id"],
        pinned_citation_id=doc["pinned_citation_id"],
        start=pair(doc["start"]), finish=pair(doc["finish"]),
        arm_span=int(doc["arm_span"]),
    )


def cmd_grade(args: argparse.Namespace) -> int:
    products: dict[str, dict[str, Any]] = {}
    for path in args.products:
        for p in _read_jsonl(path):
            products[p["citation_id"]] = p
    geometry = _load_geometry(_read_json(args.holds))
    spec = _load_spec(_read_json(args.spec))
    built = build_route(spec, products, geometry)
    if not built.gradable:
        print(json.dumps({"status": built.status, "reason": built.reason},
                          ensure_ascii=False, indent=2))
        return 2
    result = grade(built.route, built.start, built.finish, spec.arm_span)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="route_grading")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("grade", help="grade one route from citation products")
    p.add_argument("--products", nargs="+", required=True)
    p.add_argument("--holds", required=True)
    p.add_argument("--spec", required=True)
    p.set_defaults(func=cmd_grade)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - single structured failure
        print(json.dumps({"error": exc.__class__.__name__, "message": str(exc)},
                          ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
