"""Order and pickup ledger for the tasting cabinet.

The ledger is an append-only SQLite store.  It sits in front of the weighted
source and the cabinet and makes every business day independently replayable:

* an *order* captures the exact sampler result the counter used -- dataset
  directory, source version, seed, quota and every sampled ticket (as
  parsed, with body digest);
* *events* are the only mutable timeline.  Pickup events name the concrete
  pack that changed hands (case pack id or restock id), correction events
  record a post-hoc ticket erratum, and a correction that contradicts a pack
  already handed out is logged as a contradiction event without ever
  rewriting the pickup record.

Reopening yesterday's order number therefore returns both the tickets that
were actually in sample at that time and the exact packs the buyers took,
with no dependency on the current source contents.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from .receipts import Ticket, tickets_from_records

SCHEMA_VERSION = 1

EVENT_PICKUP = "pickup"
EVENT_CORRECTION = "correction"
EVENT_CONTRADICTION = "contradiction"
EVENT_NOTE = "note"


class LedgerError(Exception):
    pass


@dataclass
class Pickup:
    step: int
    buyer: str
    variety: str
    source: str
    pack_id: str

    @classmethod
    def from_handover(cls, h) -> "Pickup":
        return cls(h.step, h.buyer, h.variety, h.source, h.pack_id)

    def as_dict(self) -> dict:
        return {"step": self.step, "buyer": self.buyer,
                "variety": self.variety, "source": self.source,
                "pack_id": self.pack_id}


class OrderLedger:
    """Append-only orders + pickup events."""

    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders(
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    mode TEXT NOT NULL,
                    dataset_dir TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    seed_hex TEXT NOT NULL,
                    quotas_json TEXT NOT NULL,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS order_tickets(
                    order_id INTEGER NOT NULL REFERENCES orders(order_id),
                    group_name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    row_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    ticket_json TEXT NOT NULL,
                    PRIMARY KEY(order_id, row_id)
                );
                CREATE TABLE IF NOT EXISTS events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(order_id),
                    seq INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    buyer TEXT,
                    payload_json TEXT NOT NULL,
                    UNIQUE(order_id, seq)
                );
                """)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),))
        finally:
            conn.close()

    def _ensure(self):
        if not os.path.exists(self.path):
            self.init()

    # -- orders ----------------------------------------------------------
    def open_order(self, mode: str, dataset_dir: str, source_version: int,
                   seed_hex: str, quotas: Mapping[str, int],
                   records: Sequence[Mapping], note: Optional[str] = None
                   ) -> int:
        """Register the exact sampled tickets this order is built on."""
        self._ensure()
        tickets = tickets_from_records(records)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO orders(created_at, mode, dataset_dir, "
                "source_version, seed_hex, quotas_json, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), mode, dataset_dir, int(source_version),
                 seed_hex, json.dumps(dict(quotas), sort_keys=True), note))
            order_id = int(cur.lastrowid)
            ranks: Dict[str, int] = {}
            for t in tickets:
                ranks.setdefault(t.buyer, 0)
                rank = ranks[t.buyer]
                ranks[t.buyer] += 1
                conn.execute(
                    "INSERT INTO order_tickets(order_id, group_name, rank, "
                    "row_id, body_sha256, ticket_json) VALUES (?,?,?,?,?,?)",
                    (order_id, t.buyer, rank, t.row_id, t.body_sha256,
                     json.dumps(t.as_dict(), ensure_ascii=False,
                                sort_keys=True)))
            conn.execute("COMMIT")
            return order_id
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def reopen_order(self, order_id: int) -> dict:
        """Independent replay: tickets then and the actual packs handed out."""
        self._ensure()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"unknown order_id: {order_id}")
            tickets = [
                json.loads(r["ticket_json"])
                for r in conn.execute(
                    "SELECT ticket_json FROM order_tickets "
                    "WHERE order_id=? ORDER BY group_name, rank, row_id",
                    (order_id,))]
            events = [
                {"event_id": r["event_id"], "seq": r["seq"],
                 "kind": r["kind"], "buyer": r["buyer"],
                 "created_at": r["created_at"],
                 "payload": json.loads(r["payload_json"])}
                for r in conn.execute(
                    "SELECT * FROM events WHERE order_id=? ORDER BY seq",
                    (order_id,))]
            return {
                "order_id": order_id,
                "created_at": row["created_at"],
                "mode": row["mode"],
                "dataset_dir": row["dataset_dir"],
                "source_version": row["source_version"],
                "seed_hex": row["seed_hex"],
                "quotas": json.loads(row["quotas_json"]),
                "note": row["note"],
                "tickets": tickets,
                "events": events,
                "pickups": [e["payload"] for e in events
                            if e["kind"] == EVENT_PICKUP],
                "contradictions": [e for e in events
                                   if e["kind"] == EVENT_CONTRADICTION],
            }
        finally:
            conn.close()

    def list_orders(self) -> List[dict]:
        self._ensure()
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT order_id, created_at, mode, dataset_dir, "
                "source_version, note FROM orders ORDER BY order_id")]
        finally:
            conn.close()

    def tickets(self, order_id: int) -> List[Ticket]:
        self._ensure()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ticket_json FROM order_tickets WHERE order_id=? "
                "ORDER BY group_name, rank, row_id", (order_id,)).fetchall()
            if not rows:
                raise LedgerError(f"unknown order_id: {order_id}")
            out = []
            for r in rows:
                d = json.loads(r["ticket_json"])
                out.append(Ticket(
                    row_id=d["row_id"], buyer=d["buyer"],
                    visible=tuple(d["visible"]), picked=d["picked"],
                    source_version=d["source_version"],
                    body_sha256=d["body_sha256"]))
            return out
        finally:
            conn.close()

    # -- events ----------------------------------------------------------
    def _next_seq(self, conn, order_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS s FROM events "
            "WHERE order_id=?", (order_id,)).fetchone()
        return int(row["s"])

    def _append(self, order_id: int, kind: str, buyer: Optional[str],
                payload: Mapping) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM orders WHERE order_id=?",
                            (order_id,)).fetchone() is None:
                raise LedgerError(f"unknown order_id: {order_id}")
            seq = self._next_seq(conn, order_id)
            conn.execute(
                "INSERT INTO events(order_id, seq, created_at, kind, buyer, "
                "payload_json) VALUES (?,?,?,?,?,?)",
                (order_id, seq, time.time(), kind, buyer,
                 json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)))
            conn.execute("COMMIT")
            return seq
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_pickup(self, order_id: int, pickup: Pickup) -> int:
        return self._append(order_id, EVENT_PICKUP, pickup.buyer,
                            pickup.as_dict())

    def record_handovers(self, order_id: int, handovers: Sequence) -> int:
        """Record concrete cabinet handovers in arrival order."""
        last = -1
        for h in handovers:
            last = self.record_pickup(order_id, Pickup.from_handover(h))
        return last

    def record_failure(self, order_id: int, buyer: str, step: int,
                       witness: Sequence[Mapping]) -> int:
        return self._append(order_id, "failure", buyer,
                            {"buyer": buyer, "step": step,
                             "witness": [dict(e) for e in witness]})

    def record_note(self, order_id: int, note: str,
                    buyer: Optional[str] = None) -> int:
        return self._append(order_id, EVENT_NOTE, buyer, {"note": note})

    def record_correction(self, order_id: int, buyer: str,
                          old_ticket: Mapping, new_ticket: Mapping,
                          contradicts_pack: Optional[Mapping] = None) -> int:
        """Log a post-hoc erratum.

        If the erratum contradicts a pack already handed to that buyer, an
        immutable contradiction event is appended and the pickup timeline is
        left untouched.
        """
        payload = {
            "buyer": buyer,
            "old_ticket": dict(old_ticket),
            "new_ticket": dict(new_ticket),
        }
        seq = self._append(order_id, EVENT_CORRECTION, buyer, payload)
        if contradicts_pack is not None:
            self._append(order_id, EVENT_CONTRADICTION, buyer, {
                "buyer": buyer,
                "correction_seq": seq,
                "handed_out": dict(contradicts_pack),
                "new_ticket": dict(new_ticket),
                "note": ("new evidence contradicts the handed-out pack; "
                         "the pickup record is not rewritten"),
            })
        return seq

    def pickups_for(self, order_id: int, buyer: Optional[str] = None
                    ) -> List[dict]:
        data = self.reopen_order(order_id)
        out = data["pickups"]
        if buyer is not None:
            out = [p for p in out if p["buyer"] == buyer]
        return out

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m wrsource.ledger",
        description="Tasting-cabinet order and pickup ledger (SQLite).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--db", required=True)
    p = sub.add_parser("orders")
    p.add_argument("--db", required=True)
    p = sub.add_parser("reopen")
    p.add_argument("--db", required=True)
    p.add_argument("--order-id", type=int, required=True)
    args = parser.parse_args(argv)
    ledger = OrderLedger(args.db)
    if args.cmd == "init":
        ledger.init()
        print(json.dumps({"schema": SCHEMA_VERSION, "db": args.db}))
    elif args.cmd == "orders":
        print(json.dumps(ledger.list_orders(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(ledger.reopen_order(args.order_id),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(main())
