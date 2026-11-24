"""Bridge: build gradable routes from the existing *case-file* product.

The case-file product is the ``citation-product/1`` JSONL produced by
``citation_system``.  This module re-reads such a product (a dict per
citation, as exported) and interprets it as bouldering routes:

* a **RANGE** citation opens the route's holds -- "区间所列每页开放一组有
  位置、颜色和允许左右手的岩点".  Each page identity in the range is one
  stably-numbered hold.
* a **PINNED** citation lists the route's start/finish *signs* -- "固定引用列
  的是本线起终点签".  The ordered start/finish poses are declared by the route
  spec; the PINNED citation must actually contain those sign pages.
* "端页反转则挂起": if the RANGE is ``needs_attention`` (reversed endpoints or
  an inactive endpoint) the route is **suspended** and cannot be graded.

Hold identity is the ``(volume_id, page_id)`` identity, never the colour name:
"同名色不能代替稳定的岩点号".  Geometry (position / colour / allowed hands)
lives in a separate *hold sheet* keyed by the page identity, so the existing
page schema does not have to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import Route, make_hold, LEFT, RIGHT

SUSPENDED = "suspended"
USABLE = "usable"


@dataclass(frozen=True)
class HoldGeometry:
    """Geometry/colour/hand permissions for one hold (one page identity)."""

    volume_id: str
    page_id: str
    x: int
    y: int
    color: str = ""
    hands: frozenset[str] = frozenset({LEFT, RIGHT})

    @property
    def key(self) -> tuple[str, str]:
        return (self.volume_id, self.page_id)

    def as_hold(self) -> Any:
        return make_hold(
            f"{self.volume_id}/{self.page_id}",
            self.x,
            self.y,
            color=self.color,
            hands=self.hands,
            page_id=self.page_id,
        )


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    name: str
    range_citation_id: str
    pinned_citation_id: str
    #: Ordered start/finish poses given as ``(volume_id, page_id)`` pairs.
    start: tuple[tuple[str, str], tuple[str, str]]
    finish: tuple[tuple[str, str], tuple[str, str]]
    arm_span: int

    def start_hold_ids(self) -> tuple[str, str]:
        return (f"{self.start[0][0]}/{self.start[0][1]}",
                f"{self.start[1][0]}/{self.start[1][1]}")

    def finish_hold_ids(self) -> tuple[str, str]:
        return (f"{self.finish[0][0]}/{self.finish[0][1]}",
                f"{self.finish[1][0]}/{self.finish[1][1]}")


@dataclass
class BuiltRoute:
    spec: RouteSpec
    status: str
    route: Route | None
    start: tuple[str, str] | None
    finish: tuple[str, str] | None
    open_page_ids: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None

    @property
    def gradable(self) -> bool:
        return self.status == USABLE and self.route is not None


def _index_products(products: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["citation_id"]: p for p in products}


def _geometry_lookup(sheet: Iterable[HoldGeometry]) -> dict[tuple[str, str], HoldGeometry]:
    return {g.key: g for g in sheet}


def build_route(
    spec: RouteSpec,
    products: dict[str, dict[str, Any]],
    geometry: dict[tuple[str, str], HoldGeometry],
) -> BuiltRoute:
    """Build one gradable route from citation products + a hold sheet.

    A route is **suspended** (and cannot be graded) when either the RANGE or
    the PINNED citation is not usable, e.g. because its endpoints reversed in
    the physical page order.
    """

    rng = products.get(spec.range_citation_id)
    pin = products.get(spec.pinned_citation_id)
    if rng is None:
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason="range_missing")
    if pin is None:
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason="pinned_missing")

    if rng.get("kind") != "RANGE":
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason="range_wrong_kind")
    if pin.get("kind") != "PINNED":
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason="pinned_wrong_kind")

    if rng.get("status") != "usable":
        # Endpoint reversed or inactive -> the whole route is suspended.
        reason = rng.get("reason") or "range_not_usable"
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason=reason)
    if pin.get("status") != "usable":
        reason = pin.get("reason") or "pinned_not_usable"
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason=reason)

    open_keys = [(p["volume_id"], p["page_id"]) for p in rng["pages"]]
    open_set = set(open_keys)

    # Every start/finish sign must be pinned and open.
    sign_keys = set(spec.start) | set(spec.finish)
    pinned_keys = {(p["volume_id"], p["page_id"]) for p in pin["pages"]}
    missing_pin = sign_keys - pinned_keys
    if missing_pin:
        return BuiltRoute(spec, SUSPENDED, None, None, None,
                          reason=f"sign_not_pinned:{_fmt(missing_pin)}")
    missing_open = sign_keys - open_set
    if missing_open:
        # A sign page moved out of the open range: no such hold on this line.
        return BuiltRoute(spec, SUSPENDED, None, None, None,
                          reason=f"sign_not_open:{_fmt(missing_open)}")

    holds = []
    for key in open_keys:
        geo = geometry.get(key)
        if geo is None:
            # A page open in the range but with no geometry cannot be a hold.
            continue
        holds.append(geo.as_hold())
    if not holds:
        return BuiltRoute(spec, SUSPENDED, None, None, None, reason="no_holds")

    route = Route.from_holds(spec.route_id, holds, name=spec.name)
    start, finish = spec.start_hold_ids(), spec.finish_hold_ids()
    return BuiltRoute(
        spec=spec, status=USABLE, route=route, start=start, finish=finish,
        open_page_ids=open_keys, reason=None,
    )


def _fmt(keys: Iterable[tuple[str, str]]) -> str:
    return ",".join(f"{v}/{p}" for v, p in sorted(keys))
