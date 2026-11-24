"""版师服务：从原借还台账读取真实取件回执，按取件当时的册版出缝边记录。

边界（对应题面）：
- 版师只读 library.ledger 的 reservations/tickets/receipts，从不写台账；
- 图解册版不可变，勘误/再版只追加新版本并切换 knit_chart_head 指针；
- 缝边记录锁定取件回执当时的 revision 与 edition，并整块保存边谱快照，
  之后更正同书勘误或目录，重开仍能按 J5 的册版找回那一针和实际余弧；
  只有更正之后新兑现的借件才会取到更正版；
- 同名作品原书目（book_id）与本册已画记号（knit_copy_marks）都原样保留；
- 二并一的两个父圈、加针的两个子圈在 seam_loops 里都追得回去；
- 边针何时从两端收掉由册版的 edge_dec_end 决定，标记随锚圈被收而消失。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from library import db
from library.errors import Rejection

from . import engine
from .engine import EdgeSpectrum


class PatternService:
    def __init__(self, conn: sqlite3.Connection, ledger: Any) -> None:
        self.conn = conn
        self.ledger = ledger  # library.ledger.LedgerService，只读使用

    # ============================================================ 册版登记

    def register_edition(self, chart_payload: dict[str, Any], op_id: str) -> dict[str, Any]:
        """登记一个不可变图解册版（首次登记自动成为该图解的生效册版）。"""
        spec = engine.build_spec(chart_payload)  # 形状不合法直接抛 Rejection
        payload = {"chart_id": spec.chart_id, "edition": spec.edition}
        fp = db.fingerprint("knit_register_edition", {**payload, "spec": chart_payload})
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_register_edition", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM knit_editions WHERE chart_id=? AND edition=?",
                (spec.chart_id, spec.edition),
            ).fetchone():
                raise Rejection(
                    "edition_exists", chart=spec.chart_id, edition=spec.edition
                )
            conn.execute(
                "INSERT INTO knit_editions(chart_id, edition, book_id, chart_json, created_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (
                    spec.chart_id,
                    spec.edition,
                    chart_payload["book_id"],
                    json.dumps(chart_payload, ensure_ascii=False, sort_keys=True),
                    db.now_iso(),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO knit_chart_head(chart_id, edition) VALUES(?, ?)",
                (spec.chart_id, spec.edition),
            )
            db.record_operation(conn, op_id, "knit_register_edition", fp)
            result = {"status": "ok", **payload, "book_id": chart_payload["book_id"]}
            db.add_receipt(conn, "knit_register_edition", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_register_edition", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def activate_edition(self, chart_id: str, edition: str, op_id: str) -> dict[str, Any]:
        """勘误/再版后切换生效册版。旧缝边记录已锁版本，不受影响。"""
        payload = {"chart_id": chart_id, "edition": edition}
        fp = db.fingerprint("knit_activate_edition", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_activate_edition", fp)
            if prior is not None:
                conn.commit()
                return prior
            if not conn.execute(
                "SELECT 1 FROM knit_editions WHERE chart_id=? AND edition=?",
                (chart_id, edition),
            ).fetchone():
                raise Rejection("unknown_edition", **payload)
            conn.execute(
                "INSERT INTO knit_chart_head(chart_id, edition) VALUES(?, ?) "
                "ON CONFLICT(chart_id) DO UPDATE SET edition=excluded.edition",
                (chart_id, edition),
            )
            db.record_operation(conn, op_id, "knit_activate_edition", fp)
            result = {"status": "ok", **payload}
            db.add_receipt(conn, "knit_activate_edition", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_activate_edition", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def head_edition(self, chart_id: str) -> str:
        row = self.conn.execute(
            "SELECT edition FROM knit_chart_head WHERE chart_id=?", (chart_id,)
        ).fetchone()
        if row is None:
            raise Rejection("chart_not_registered", chart=chart_id)
        return row["edition"]

    def get_edition_spec(self, chart_id: str, edition: str) -> engine.ChartSpec:
        row = self.conn.execute(
            "SELECT chart_json FROM knit_editions WHERE chart_id=? AND edition=?",
            (chart_id, edition),
        ).fetchone()
        if row is None:
            raise Rejection("unknown_edition", chart=chart_id, edition=edition)
        return engine.build_spec(json.loads(row["chart_json"]))

    # ============================================================ 本册记号

    def add_copy_mark(
        self,
        mark_id: str,
        receipt_id: str,
        anchor: str,
        label: str,
        orientation: str,
        op_id: str,
    ) -> dict[str, Any]:
        """在某次取件的样本上画记号；记号挂具名线圈，随该件缝边记录走。"""
        payload = {
            "mark_id": mark_id,
            "receipt_id": receipt_id,
            "anchor": anchor,
            "label": label,
            "orientation": orientation,
        }
        fp = db.fingerprint("knit_copy_mark", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_copy_mark", fp)
            if prior is not None:
                conn.commit()
                return prior
            if orientation not in ("up", "down"):
                raise Rejection("bad_marker_orientation", marker=mark_id)
            if conn.execute(
                "SELECT 1 FROM knit_copy_marks WHERE mark_id=?", (mark_id,)
            ).fetchone():
                raise Rejection("mark_exists", mark=mark_id)
            conn.execute(
                "INSERT INTO knit_copy_marks(mark_id, receipt_id, anchor, label, orientation, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (mark_id, receipt_id, anchor, label, orientation, db.now_iso()),
            )
            db.record_operation(conn, op_id, "knit_copy_mark", fp)
            result = {"status": "ok", **payload}
            db.add_receipt(conn, "knit_copy_mark", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_copy_mark", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ============================================================ 缝边记录

    def create_seam_record(
        self,
        seam_id: str,
        receipt_id: str,
        chart_id: str,
        piece_kind: str,
        op_id: str,
        edition: str | None = None,
    ) -> dict[str, Any]:
        """按已兑现票号取实际样本，出带具名线圈和边弧的缝边记录。

        - 从台账 reservations 读真实取件回执（必须 fulfilled），锁定其 revision；
        - edition 缺省取该图解当前生效册版（即“取件当时的册版”）；
        - 合并该回执下本册已画记号一起织；快照整块落库，之后更正不影响本记录。
        """
        payload = {
            "seam_id": seam_id,
            "receipt_id": receipt_id,
            "chart_id": chart_id,
            "piece_kind": piece_kind,
            "edition": edition,
        }
        fp = db.fingerprint("knit_seam_record", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_seam_record", fp)
            if prior is not None:
                conn.commit()
                return prior
            if piece_kind not in ("sleeve", "body"):
                raise Rejection("bad_piece_kind", kind=piece_kind)
            if conn.execute(
                "SELECT 1 FROM seam_records WHERE seam_id=?", (seam_id,)
            ).fetchone():
                raise Rejection("seam_record_exists", seam=seam_id)
            hold = self._require_fulfilled_receipt(receipt_id)
            edition = edition or self.head_edition(chart_id)
            spec = self.get_edition_spec(chart_id, edition)
            marks = [
                {
                    "id": r["mark_id"],
                    "anchor": r["anchor"],
                    "orientation": r["orientation"],
                }
                for r in conn.execute(
                    "SELECT mark_id, anchor, orientation FROM knit_copy_marks "
                    "WHERE receipt_id=? ORDER BY mark_id",
                    (receipt_id,),
                )
            ]
            spectrum = engine.knit(spec, extra_markers=marks)
            self._persist_spectrum(
                seam_id, receipt_id, hold, chart_id, edition, piece_kind, spectrum
            )
            db.record_operation(conn, op_id, "knit_seam_record", fp)
            result = {
                "status": "ok",
                "seam_id": seam_id,
                "receipt_id": receipt_id,
                "revision": hold["revision"],
                "chart_id": chart_id,
                "edition": edition,
                "piece_kind": piece_kind,
                "copy_id": hold["copy_id"],
                "ticket_id": hold["ticket_id"],
                "reader_id": hold["reader_id"],
                "edge_loops": spectrum.edge_order,
                "arc_count": len(spectrum.arcs),
                "markers": [m.as_dict() for m in spectrum.markers.values()],
            }
            db.add_receipt(conn, "knit_seam_record", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_seam_record", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def _require_fulfilled_receipt(self, receipt_id: str) -> sqlite3.Row:
        """台账只读：必须是已兑现（实际取到样本）的保留，且能追到票号与读者。"""
        hold = self.conn.execute(
            "SELECT * FROM reservations WHERE hold_id=?", (receipt_id,)
        ).fetchone()
        if hold is None:
            raise Rejection("unknown_receipt", receipt=receipt_id)
        if hold["status"] != "fulfilled":
            raise Rejection(
                "receipt_not_fulfilled", receipt=receipt_id, status=hold["status"]
            )
        return hold

    def _persist_spectrum(
        self,
        seam_id: str,
        receipt_id: str,
        hold: sqlite3.Row,
        chart_id: str,
        edition: str,
        piece_kind: str,
        spectrum: EdgeSpectrum,
    ) -> None:
        conn = self.conn
        conn.execute(
            "INSERT INTO seam_records(seam_id, receipt_id, revision, chart_id, edition, "
            "piece_kind, copy_id, ticket_id, reader_id, snapshot_json, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seam_id,
                receipt_id,
                hold["revision"],
                chart_id,
                edition,
                piece_kind,
                hold["copy_id"],
                hold["ticket_id"],
                hold["reader_id"],
                json.dumps(spectrum.snapshot(), ensure_ascii=False, sort_keys=True),
                db.now_iso(),
            ),
        )
        conn.executemany(
            "INSERT INTO seam_loops(seam_id, loop_id, row_no, position, alive, parents, children)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    seam_id,
                    l.loop_id,
                    l.row_no,
                    l.position,
                    1 if l.alive else 0,
                    json.dumps(l.parents),
                    json.dumps(l.children),
                )
                for l in spectrum.loops.values()
            ],
        )
        conn.executemany(
            "INSERT INTO seam_markers(seam_id, marker_id, anchor_loop, orientation, alive)"
            " VALUES(?, ?, ?, ?, ?)",
            [
                (seam_id, m.marker_id, m.anchor_loop, m.orientation, 1 if m.alive else 0)
                for m in spectrum.markers.values()
            ],
        )
        conn.executemany(
            "INSERT INTO seam_arcs(seam_id, arc_id, seq, owner_loop, region)"
            " VALUES(?, ?, ?, ?, ?)",
            [
                (seam_id, a.arc_id, a.seq, a.owner_loop, a.region)
                for a in spectrum.arcs
            ],
        )

    # ============================================================ 单独查询

    def get_seam_record(self, seam_id: str) -> dict[str, Any]:
        """缝边记录头 + 整块快照（重开后据此找回那一针和实际余弧）。"""
        row = self.conn.execute(
            "SELECT * FROM seam_records WHERE seam_id=?", (seam_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_seam_record", seam=seam_id)
        return {
            "seam_id": row["seam_id"],
            "receipt_id": row["receipt_id"],
            "revision": row["revision"],
            "chart_id": row["chart_id"],
            "edition": row["edition"],
            "piece_kind": row["piece_kind"],
            "copy_id": row["copy_id"],
            "ticket_id": row["ticket_id"],
            "reader_id": row["reader_id"],
            "snapshot": json.loads(row["snapshot_json"]),
        }

    def load_spectrum(self, seam_id: str) -> EdgeSpectrum:
        return engine.spectrum_from_snapshot(self.get_seam_record(seam_id)["snapshot"])

    def edge_loop_ledger(self, seam_id: str) -> list[dict[str, Any]]:
        """版师的边圈谱：每个具名线圈的行位、存活与父子谱系，可单独查。"""
        self.get_seam_record(seam_id)  # 存在性校验
        rows = self.conn.execute(
            "SELECT loop_id, row_no, position, alive, parents, children "
            "FROM seam_loops WHERE seam_id=? ORDER BY row_no, position",
            (seam_id,),
        ).fetchall()
        return [
            {
                "loop_id": r["loop_id"],
                "row_no": r["row_no"],
                "position": r["position"],
                "alive": bool(r["alive"]),
                "parents": json.loads(r["parents"]),
                "children": json.loads(r["children"]),
            }
            for r in rows
        ]

    def edge_arcs(self, seam_id: str) -> list[dict[str, Any]]:
        self.get_seam_record(seam_id)
        rows = self.conn.execute(
            "SELECT arc_id, seq, owner_loop, region FROM seam_arcs "
            "WHERE seam_id=? ORDER BY seq",
            (seam_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def markers(self, seam_id: str) -> list[dict[str, Any]]:
        self.get_seam_record(seam_id)
        rows = self.conn.execute(
            "SELECT marker_id, anchor_loop, orientation, alive "
            "FROM seam_markers WHERE seam_id=? ORDER BY marker_id",
            (seam_id,),
        ).fetchall()
        return [
            {
                "marker_id": r["marker_id"],
                "anchor_loop": r["anchor_loop"],
                "orientation": r["orientation"],
                "alive": bool(r["alive"]),
            }
            for r in rows
        ]

    def arcs_between(self, seam_id: str, start_marker: str, end_marker: str) -> list[dict[str, Any]]:
        """制样师的本地短行：两条缝线标记之间还剩几段边弧。"""
        return self.load_spectrum(seam_id).arcs_between(start_marker, end_marker)

    def marker_alive(self, seam_id: str, marker_id: str) -> bool:
        """制样师的本地短行：某标记是否已随收针消失。"""
        return self.load_spectrum(seam_id).marker_alive(marker_id)

    # ============================================================ 内部

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict[str, Any]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, kind, fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(conn, kind, "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
