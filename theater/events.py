"""Local theater cloakroom model built on :mod:`telber`.

The cloakroom's paper trail is a stream of BER records.  Every event family is
encoded with the strict ``theater.event`` schema and imported through the real
``telber`` repository; all later reasoning is done from the *facts the
repository reconstructed*, not from the in-memory Python objects that produced
them.  That keeps the two halves honest: the codec validates the bytes and the
model reads only what survived the round trip.

Event families (the ``kind`` field selects one; the rest are optional):

===============  ====  =====================================================
kind             int   meaning
===============  ====  =====================================================
HANG               1   guest checks a coat; coat gets a left half, guest a
                       right half (steel no, cut pattern set, counter,
                       return beat); records ring + hook slot.
MOVE               2   coat carried from one hook to another (换钩登记).
TRANSFER_RIGHT     3   a right half is handed from one guest to another
                       (客间移交右牌); the recipient gains the claim.
RUBBING            4   a damaged tag's rubbing is registered as a *set* of
                       possible cut patterns (污损拓片).
INVENTORY          5   session-end count on one ring (结场清点).
RESERVATION       10   a guest reserves a coat at a window.
DELIVERY           6   a window physically hands a coat over (local action).
TELEGRAM           7   a counter telegram about a delivery, sent at one beat,
                       arriving at another (may be late).
TRANSFER_HOOK      8   a registered transfer hook moves a coat left->right at a
                       beat during which neither ring turns.
CLUTCH             9   which side(s) the coaxial clutch engaged on a beat.
===============  ====  =====================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from telber.codec import encode_record
from telber.repository import Repository
from telber.schema import Product, compile_product

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "theater_v1.json"

HANG = 1
MOVE = 2
TRANSFER_RIGHT = 3
RUBBING = 4
INVENTORY = 5
DELIVERY = 6
TELEGRAM = 7
TRANSFER_HOOK = 8
CLUTCH = 9
RESERVATION = 10

KIND_NAMES = {
    HANG: "HANG",
    MOVE: "MOVE",
    TRANSFER_RIGHT: "TRANSFER_RIGHT",
    RUBBING: "RUBBING",
    INVENTORY: "INVENTORY",
    DELIVERY: "DELIVERY",
    TELEGRAM: "TELEGRAM",
    TRANSFER_HOOK: "TRANSFER_HOOK",
    CLUTCH: "CLUTCH",
    RESERVATION: "RESERVATION",
}

_UNIVERSAL = {"INTEGER": "INTEGER", "OCTET STRING": "OCTET STRING"}


def load_product() -> Product:
    return compile_product(SCHEMA_PATH.read_text(encoding="utf-8"))


def event(kind: int, **fields: Any) -> dict:
    """Build one event mapping (``kind`` plus named fields)."""
    if kind not in KIND_NAMES:
        raise ValueError(f"unknown event kind {kind!r}")
    out: dict[str, Any] = {"kind": kind}
    out.update(fields)
    return out


def event_bytes(ev: Mapping[str, Any], product: Product | None = None) -> bytes:
    """Encode a single event as one top-level BER record."""
    product = product or load_product()
    return encode_record(ev, product)


def stream_bytes(events: Iterable[Mapping[str, Any]], product: Product | None = None) -> bytes:
    product = product or load_product()
    return b"".join(event_bytes(e, product) for e in events)


@dataclass
class TheaterStore:
    """A telber repository that holds the cloakroom's reconstructed facts."""

    db_path: str = ":memory:"
    product: Product = field(default_factory=load_product)
    repo: Repository | None = None
    import_id: int | None = None

    def __post_init__(self) -> None:
        self.repo = Repository(self.db_path)
        self.repo.publish_product(self.product.schema_dict())

    def install(self, events: Iterable[Mapping[str, Any]]) -> int:
        """Encode and ingest ``events``; return the import id."""
        data = stream_bytes(events, self.product)
        self.import_id = self.repo.create_import(
            self.product.product_id, source_name="theater/events.bin"
        )
        self.repo.ingest_bytes(self.import_id, data, block_size=7)
        return self.import_id

    def install_truncated(self, events: Iterable[Mapping[str, Any]], keep: int) -> tuple[int, Any]:
        """Ingest a *partial* stream (for 待查 facts only).

        Returns ``(import_id, ImportInfo)``.  A partial import is recorded as
        ``partial`` and may only be used for pending investigation.
        """
        data = stream_bytes(events, self.product)
        truncated = data[:keep]
        self.import_id = self.repo.create_import(
            self.product.product_id, source_name="theater/partial.bin"
        )
        try:
            self.repo.ingest_bytes(self.import_id, truncated, block_size=5)
        except Exception:
            pass
        return self.import_id, self.repo.import_info(self.import_id)

    def events(self) -> list[dict]:
        """Reconstruct events from the stored relation rows (repository truth).

        Each returned mapping is exactly what the codec *decoded* from the
        bytes; ``_ordinal`` is the record's position in the imported stream.
        """
        if self.import_id is None:
            return []
        rows = self.repo.relation_rows(self.import_id)
        by_record: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not row["present"]:
                continue
            value = row["bytes_value"] if row["type"] == "OCTET STRING" else row["int_value"]
            by_record.setdefault(row["record_ordinal"], {})[row["path"]] = value
        out = []
        for ordinal in sorted(by_record):
            data = by_record[ordinal]
            data["_ordinal"] = ordinal
            out.append(data)
        return out

    def publishable(self) -> bool:
        return self.repo.publish_import(self.import_id).status == "complete"  # type: ignore[union-attr]

    def close(self) -> None:
        if self.repo is not None:
            self.repo.close()

    def __enter__(self) -> "TheaterStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def dump_events(events: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps(list(events), indent=2, default=lambda b: b.hex())
