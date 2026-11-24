"""Work-order loading for the lacquer workshop.

A work order is the single source of truth for what the shift may do: the beat
budget, the stations and the exact questions each station may ask, the named
original tongues that must ship, and which named spare tongues (if any) may be
substituted.  Nothing here is guessed from defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import INVALID_ORDER, LacquerError
from .model import Base, Lid, ring_from_features
from .workbench import StationOffer, WorkOrder


def load_pieces(document: dict[str, Any]):
    """Build bases and lids from a ``lacquer-pieces/1`` document."""

    if document.get("format") != "lacquer-pieces/1":
        raise LacquerError(
            INVALID_ORDER,
            "unsupported pieces document",
            details={"format": document.get("format")},
        )
    divisions = int(document["divisions"])
    bases = [
        Base(
            str(entry["item_id"]),
            ring_from_features(
                divisions, lugs=entry.get("lugs", []), name=str(entry["item_id"])
            ),
        )
        for entry in document.get("bases", [])
    ]
    lids = [
        Lid(
            str(entry["item_id"]),
            ring_from_features(
                divisions, notches=entry.get("notches", []), name=str(entry["item_id"])
            ),
            pinned_rotation=(
                int(entry["pinned_rotation"])
                if entry.get("pinned_rotation") is not None
                else None
            ),
        )
        for entry in document.get("lids", [])
    ]
    return divisions, bases, lids, list(document.get("stack_order", []))


def load_work_order(document: dict[str, Any]) -> WorkOrder:
    # Orders may be embedded in a wrapper document; the wrapper carries the
    # format and each order carries its own order_id.
    if document.get("format") not in ("lacquer-work-order/1",):
        raise LacquerError(
            INVALID_ORDER,
            "unsupported work order format",
            details={"format": document.get("format")},
        )

    def tongue_set(key: str) -> dict[str, frozenset[str]]:
        return {
            str(vessel_id): frozenset(str(t) for t in tongues)
            for vessel_id, tongues in document.get(key, {}).items()
        }

    offers: list[StationOffer] = []
    for entry in document.get("stations", []):
        offers.append(
            StationOffer(
                station_id=str(entry["station_id"]),
                vessel_id=str(entry["vessel_id"]),
                gauge_cells=tuple(
                    (str(item["rim"]), int(item["cell"]))
                    for item in entry.get("gauge", [])
                ),
                trial_bites=tuple(
                    (
                        str(item["rim"]),
                        int(item["cell"]),
                        str(item["tongue_id"]),
                    )
                    for item in entry.get("trial_bites", [])
                ),
            )
        )

    return WorkOrder(
        order_id=str(document["order_id"]),
        beat_budget=int(document["beat_budget"]),
        required_original_tongues=tongue_set("required_original_tongues"),
        allowed_spare_tongues=tongue_set("allowed_spare_tongues"),
        original_tongues=tongue_set("original_tongues"),
        offers=tuple(offers),
        pressed_vessels=frozenset(str(v) for v in document.get("pressed_vessels", [])),
        spare_sheet_authorized=bool(document.get("spare_sheet_authorized", True)),
    )


def load_work_order_file(path: str | Path) -> WorkOrder:
    with open(path, encoding="utf-8") as fh:
        return load_work_order(json.load(fh))
