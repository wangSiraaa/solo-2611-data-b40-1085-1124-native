"""缝台服务：读版师的缝边记录与已落针位置，给余下能走的针序。

缝合规则（对应题面，全部在服务层强制）：
- 按腋下起点向两侧合拢，袖山与衣身的定位标都不可翻面，方向（orientation）须相配；
- 一次缝合取两侧各一段（1 对 1），或仅在准许吃势的具名区用同一针缝两段袖弧
  对一段身弧（2 对 1）；不得跨标记跳缝；
- 吃势额度按具名区声明（ease_regions），每区至多一次；
- 左右袖同时交货；共用的一段装饰标线只够缝一只（部分唯一索引 + 服务校验）；
- 临缝用专用线，一次占用到本次交货前不能再借给另一袖；
- 定针缝入的袖圈即使同名也不能拆后当作原圈复用：permanent 针锁定其袖弧归属圈，
  后续针序只从尚未落定针的弧继续，断点可具体报出。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from library import db
from library.errors import Rejection

from .pattern import PatternService

BASTE, PERMANENT, DECORATIVE = "baste", "permanent", "decorative"
MAX_SLEEVES = 2


def _raise_from_plan(error: dict[str, Any]) -> None:
    """把 plan_needles 的错误字典还原成业务拒绝（reason 不重复传参）。"""
    reason = error["reason"]
    raise Rejection(reason, **{k: v for k, v in error.items() if k != "reason"})


class SewingService:
    def __init__(self, conn: sqlite3.Connection, pattern: PatternService) -> None:
        self.conn = conn
        self.pattern = pattern

    # ============================================================ 线轴

    def register_spool(
        self, spool_id: str, kind: str, capacity: int, op_id: str
    ) -> dict[str, Any]:
        payload = {"spool_id": spool_id, "kind": kind, "capacity": capacity}
        fp = db.fingerprint("knit_spool", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_spool", fp)
            if prior is not None:
                conn.commit()
                return prior
            if kind not in (BASTE, PERMANENT, DECORATIVE):
                raise Rejection("bad_thread_kind", kind=kind)
            if capacity <= 0:
                raise Rejection("bad_capacity", capacity=capacity)
            if conn.execute(
                "SELECT 1 FROM thread_spools WHERE spool_id=?", (spool_id,)
            ).fetchone():
                raise Rejection("spool_exists", spool=spool_id)
            conn.execute(
                "INSERT INTO thread_spools(spool_id, kind, capacity, created_at)"
                " VALUES(?, ?, ?, ?)",
                (spool_id, kind, capacity, db.now_iso()),
            )
            db.record_operation(conn, op_id, "knit_spool", fp)
            result = {"status": "ok", **payload}
            db.add_receipt(conn, "knit_spool", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_spool", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def _open_units(self, spool_id: str) -> int:
        row = self.conn.execute(
            "SELECT capacity FROM thread_spools WHERE spool_id=?", (spool_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_spool", spool=spool_id)
        used = self.conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS u FROM thread_allocations "
            "WHERE spool_id=? AND released=0",
            (spool_id,),
        ).fetchone()["u"]
        return row["capacity"] - used

    def _consume(
        self,
        alloc_id: str,
        spool_id: str,
        kind: str,
        units: int,
        seam_id: str | None,
        sleeve_id: str | None,
    ) -> None:
        # 装饰线同一轴同时只能挂一只袖（库内部分唯一索引兜底，这里给可读拒绝）
        if kind == DECORATIVE:
            clash = self.conn.execute(
                "SELECT 1 FROM thread_allocations WHERE spool_id=? AND released=0 "
                "AND sleeve_id IS NOT ?",
                (spool_id, sleeve_id),
            ).fetchone()
            if clash:
                raise Rejection("decorative_thread_one_sleeve_only", spool=spool_id)
        # 临缝专用线：未释放占用不得再借给另一袖
        if kind == BASTE:
            clash = self.conn.execute(
                "SELECT sleeve_id FROM thread_allocations WHERE spool_id=? "
                "AND released=0 AND sleeve_id IS NOT NULL AND sleeve_id IS NOT ?",
                (spool_id, sleeve_id),
            ).fetchone()
            if clash:
                raise Rejection(
                    "baste_thread_reserved_until_delivery",
                    spool=spool_id,
                    held_by=clash["sleeve_id"],
                )
        if self._open_units(spool_id) < units:
            raise Rejection(
                "thread_exhausted",
                spool=spool_id,
                requested=units,
                remaining=self._open_units(spool_id),
            )
        self.conn.execute(
            "INSERT INTO thread_allocations(alloc_id, spool_id, seam_id, sleeve_id, "
            "kind, units, released, created_at) VALUES(?, ?, ?, ?, ?, ?, 0, ?)",
            (alloc_id, spool_id, seam_id, sleeve_id, kind, units, db.now_iso()),
        )

    def release_allocation(self, alloc_id: str, op_id: str) -> dict[str, Any]:
        """试衣后拆临缝：只释放临缝线占用；定针占用不可释放，缝入袖圈不复用。"""
        payload = {"alloc_id": alloc_id}
        fp = db.fingerprint("knit_release_thread", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_release_thread", fp)
            if prior is not None:
                conn.commit()
                return prior
            row = conn.execute(
                "SELECT * FROM thread_allocations WHERE alloc_id=?", (alloc_id,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_allocation", alloc=alloc_id)
            if row["released"]:
                raise Rejection("allocation_already_released", alloc=alloc_id)
            if row["kind"] != BASTE:
                raise Rejection("only_baste_thread_releasable", kind=row["kind"])
            conn.execute(
                "UPDATE thread_allocations SET released=1 WHERE alloc_id=?", (alloc_id,)
            )
            db.record_operation(conn, op_id, "knit_release_thread", fp)
            result = {"status": "ok", **payload}
            db.add_receipt(conn, "knit_release_thread", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_release_thread", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ============================================================ 落针

    def baste_sleeve(
        self,
        seam_group: str,
        sleeve_id: str,
        seam_id_s: str,
        seam_id_b: str,
        spool_id: str,
        ease_regions: list[str],
        op_id: str,
    ) -> dict[str, Any]:
        """接单方式一：整袖先上临时疏缝，试衣后可拆临缝再落定针。

        按完整可行针序预缝整袖，消耗临缝线；落 baste 实针但不锁袖圈
        （拆临缝后原圈仍可用于定针）。
        """
        payload = {
            "seam_group": seam_group, "sleeve_id": sleeve_id,
            "seam_id_s": seam_id_s, "seam_id_b": seam_id_b, "spool_id": spool_id,
            "ease_regions": sorted(ease_regions),
        }
        fp = db.fingerprint("knit_baste", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_baste", fp)
            if prior is not None:
                conn.commit()
                return prior
            self._assert_sleeve_cap(seam_group, sleeve_id)
            plan = self.plan_needles(
                seam_id_s, seam_id_b, ease_regions,
                sleeve_id=sleeve_id, seam_group=seam_group,
            )
            if plan["status"] == "rejected":
                _raise_from_plan(plan["error"])
            needles = plan["needles"]
            self._consume(
                f"alloc-{op_id}", spool_id, BASTE, len(needles), seam_id_s, sleeve_id
            )
            for nd in needles:
                self._insert_needle(
                    seam_group, sleeve_id, seam_id_s, seam_id_b, BASTE, nd
                )
            db.record_operation(conn, op_id, "knit_baste", fp)
            result = {
                "status": "ok", "seam_group": seam_group, "sleeve_id": sleeve_id,
                "mode": BASTE, "needles": len(needles), "spool": spool_id,
                "remaining_thread": self._open_units(spool_id),
            }
            db.add_receipt(conn, "knit_baste", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_baste", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def place_permanent(
        self,
        seam_group: str,
        sleeve_id: str,
        seam_id_s: str,
        seam_id_b: str,
        spool_id: str,
        ease_regions: list[str],
        op_id: str,
        stop_after: int | None = None,
    ) -> dict[str, Any]:
        """接单方式二：从腋下直接落定针。

        stop_after 给定时只缝规划针序的前 N 针，在自己判为仍可改的区段留活口；
        定针缝入的袖圈锁定，后续只能从断点继续，同名圈不得拆后复用。
        """
        payload = {
            "seam_group": seam_group, "sleeve_id": sleeve_id,
            "seam_id_s": seam_id_s, "seam_id_b": seam_id_b, "spool_id": spool_id,
            "ease_regions": sorted(ease_regions), "stop_after": stop_after,
        }
        fp = db.fingerprint("knit_permanent", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_permanent", fp)
            if prior is not None:
                conn.commit()
                return prior
            self._assert_sleeve_cap(seam_group, sleeve_id)
            plan = self.plan_needles(
                seam_id_s, seam_id_b, ease_regions,
                sleeve_id=sleeve_id, seam_group=seam_group,
            )
            if plan["status"] == "rejected":
                _raise_from_plan(plan["error"])
            needles = plan["needles"]
            if stop_after is not None and not (0 < stop_after <= len(needles)):
                raise Rejection(
                    "bad_stop_after", stop_after=stop_after, total=len(needles)
                )
            todo = needles if stop_after is None else needles[:stop_after]
            self._consume(
                f"alloc-{op_id}", spool_id, PERMANENT, len(todo), seam_id_s, sleeve_id
            )
            for nd in todo:
                self._insert_needle(
                    seam_group, sleeve_id, seam_id_s, seam_id_b, PERMANENT, nd
                )
            db.record_operation(conn, op_id, "knit_permanent", fp)
            result = {
                "status": "ok", "seam_group": seam_group, "sleeve_id": sleeve_id,
                "mode": PERMANENT, "needles_placed": len(todo),
                "total_planned": len(needles), "spool": spool_id,
                "remaining_thread": self._open_units(spool_id),
                "open_tail": stop_after is not None and stop_after < len(needles),
            }
            db.add_receipt(conn, "knit_permanent", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_permanent", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def allocate_decorative(
        self, seam_group: str, sleeve_id: str, spool_id: str, op_id: str
    ) -> dict[str, Any]:
        """共用的一段装饰标线：整单只够一只袖，先到先占。"""
        payload = {
            "seam_group": seam_group, "sleeve_id": sleeve_id, "spool_id": spool_id
        }
        fp = db.fingerprint("knit_decorative", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "knit_decorative", fp)
            if prior is not None:
                conn.commit()
                return prior
            self._assert_sleeve_cap(seam_group, sleeve_id)
            self._consume(f"alloc-{op_id}", spool_id, DECORATIVE, 1, None, sleeve_id)
            db.record_operation(conn, op_id, "knit_decorative", fp)
            result = {"status": "ok", **payload}
            db.add_receipt(conn, "knit_decorative", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("knit_decorative", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ============================================================ 针序规划

    def plan_needles(
        self,
        seam_id_s: str,
        seam_id_b: str,
        ease_regions: list[str],
        sleeve_id: str | None = None,
        seam_group: str | None = None,
    ) -> dict[str, Any]:
        """读缝边记录和已落针位置，给余下能走的针序（或具体断口）。

        定针锁定的弧从余量里剔除；临缝不锁弧（拆后可重新定针）。
        返回 {"status":"ok","needles":[...]} 或 {"status":"rejected","error":{...}}，
        纯规划不落库、不占线。
        """
        try:
            spec_s = self.pattern.load_spectrum(seam_id_s)
            spec_b = self.pattern.load_spectrum(seam_id_b)
            self._check_marker_orientation(spec_s, spec_b)
            sections_s, sections_b = self._sections(spec_s), self._sections(spec_b)
            if list(sections_s) != list(sections_b):
                raise Rejection(
                    "marker_sequence_mismatch",
                    sleeve_markers=list(sections_s),
                    body_markers=list(sections_b),
                )
            used_s, used_b, used_ease = self._consumed_arcs(seam_group, sleeve_id)
            ease_set = set(ease_regions)
            needles: list[dict[str, Any]] = []
            for region in sections_s:
                arcs_s = [
                    a for a in sections_s[region] if a["arc_id"] not in used_s
                ]
                arcs_b = [
                    a for a in sections_b[region] if a["arc_id"] not in used_b
                ]
                needles.extend(
                    self._plan_region(
                        region, arcs_s, arcs_b,
                        ease_available=region in ease_set,
                        used_ease=used_ease,
                    )
                )
            return {"status": "ok", "needles": needles}
        except Rejection as rej:
            return {"status": "rejected", "error": rej.as_dict()}

    def _plan_region(
        self,
        region: str,
        arcs_s: list[dict[str, Any]],
        arcs_b: list[dict[str, Any]],
        ease_available: bool,
        used_ease: set[str],
    ) -> list[dict[str, Any]]:
        """一个标记区间内从腋下侧顺序消费：2 对 1 只在吃势区且每区一次。

        按区间总量决定：袖弧比身弧恰好多 1 且吃势准许 -> 首针 2 对 1，其余 1 对 1；
        总量相等 -> 全部 1 对 1；其余（如 4 对 2，差 2；或不准吃势）报 cannot_align。
        """
        total_s, total_b = len(arcs_s), len(arcs_b)
        if total_s == total_b:
            return [
                self._needle(region, [arcs_s[k]], [arcs_b[k]], 0)
                for k in range(total_b)
            ]
        if total_s == total_b + 1 and ease_available and region not in used_ease:
            needles = [self._needle(region, arcs_s[0:2], [arcs_b[0]], 1)]
            used_ease.add(region)
            needles.extend(
                self._needle(region, [arcs_s[k + 1]], [arcs_b[k]], 0)
                for k in range(1, total_b)
            )
            return needles
        raise Rejection(
            "cannot_align",
            region=region,
            sleeve_left=total_s,
            body_left=total_b,
            ease_allowed=ease_available,
            ease_used=region in used_ease,
        )

    @staticmethod
    def _needle(
        region: str, sleeve_arcs: list[dict], body_arcs: list[dict], ease: int
    ) -> dict[str, Any]:
        return {
            "region": region,
            "ease": ease,
            "sleeve_arcs": [a["arc_id"] for a in sleeve_arcs],
            "body_arcs": [a["arc_id"] for a in body_arcs],
            "sleeve_loops": [a["owner_loop"] for a in sleeve_arcs],
            "body_loops": [a["owner_loop"] for a in body_arcs],
        }

    # ============================================================ 改肩宽

    def evaluate_shoulder_change(
        self,
        seam_group: str,
        sleeve_id: str,
        seam_id_s: str,
        ease_regions: list[str],
        pre_marker: str,
        op_id: str | None = None,
    ) -> dict[str, Any]:
        """试衣前把肩标前的末弧由一段改两段：只有留在肩标前的吃势位可吸收。

        - 定针已缝住该末弧 -> 改不动，报那一针（缝入袖圈同名也不能拆）；
        - 肩标前没有未用吃势位 -> 改不动，报被耗掉/未声明的吃势区；
        - 否则可提前承诺：在该区间插一针 2 对 1 即吸收新增的一段。
        只评估不落针；op_id 给定时落回执留痕。
        """
        spec_s = self.pattern.load_spectrum(seam_id_s)
        marker = spec_s.marker(pre_marker)
        if not marker.alive:
            raise Rejection("marker_gone", marker=pre_marker)
        edge = spec_s.edge_order
        anchor_pos = edge.index(marker.anchor_loop)
        if anchor_pos == 0:
            raise Rejection("no_arc_before_marker", marker=pre_marker)
        last_arc = next(
            a for a in spec_s.arcs if a.owner_loop == edge[anchor_pos - 1]
        )
        perm_rows, _ = self._needle_modes(seam_group, sleeve_id)
        # 断口一：定针已经缝住肩标前末弧，缝入袖圈同名也不能拆
        blocking = next(
            (r for r in perm_rows
             if last_arc.arc_id in json.loads(r["sleeve_arcs"])),
            None,
        )
        if blocking is not None:
            result = {
                "status": "rejected",
                "reason": "permanent_needle_blocks_change",
                "sleeve_id": sleeve_id,
                "blocking_needle_seq": blocking["needle_seq"],
                "blocking_loop": last_arc.owner_loop,
            }
        else:
            region = last_arc.region
            # 断口二：肩标前没有“留在”的吃势位——未声明，或本袖已把该区吃势用掉
            ease_used_regions = {r["region"] for r in perm_rows if r["ease"] == 1}
            if region not in set(ease_regions):
                result = {
                    "status": "rejected",
                    "reason": "ease_unavailable_before_marker",
                    "sleeve_id": sleeve_id,
                    "region": region,
                    "ease_declared": False,
                    "ease_consumed": region in ease_used_regions,
                }
            elif region in ease_used_regions:
                used_needle = next(
                    r for r in perm_rows if r["ease"] == 1 and r["region"] == region
                )
                result = {
                    "status": "rejected",
                    "reason": "ease_already_consumed",
                    "sleeve_id": sleeve_id,
                    "region": region,
                    "consumed_by_needle_seq": used_needle["needle_seq"],
                }
            else:
                result = {
                    "status": "ok",
                    "sleeve_id": sleeve_id,
                    "absorb_region": region,
                    "extra_sleeve_arc_on": last_arc.owner_loop,
                    "plan": "insert_one_ease_needle_before_marker",
                }
        if op_id is not None:
            self._record_eval(op_id, "knit_shoulder_change", result)
        return result

    # ============================================================ 交货

    def delivery_report(self, seam_group: str) -> dict[str, Any]:
        """报能按时完成的袖对、已占用的线；不能交一对时指出耗掉的线或那一针。"""
        sleeves = sorted({
            r["sleeve_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT sleeve_id FROM sewing_needles WHERE seam_group=?",
                (seam_group,),
            )
        } | {
            r["sleeve_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT a.sleeve_id FROM thread_allocations a "
                "JOIN thread_spools s ON s.spool_id=a.spool_id "
                "WHERE a.released=0 AND s.kind=? AND a.sleeve_id IS NOT NULL",
                (DECORATIVE,),
            )
        })
        per_sleeve: dict[str, dict[str, Any]] = {}
        for sid in sleeves:
            perm_rows, baste_rows = self._needle_modes(seam_group, sid)
            allocs = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT alloc_id, spool_id, kind, units FROM thread_allocations "
                    "WHERE sleeve_id=? AND released=0 ORDER BY alloc_id",
                    (sid,),
                )
            ]
            per_sleeve[sid] = {
                "permanent_needles": len(perm_rows),
                "baste_needles": len(baste_rows),
                "open_thread": allocs,
                "blocking_for_pair": self._pair_blockers(
                    seam_group, sid, perm_rows, baste_rows
                ),
            }
        fully_sewn = {
            sid for sid, v in per_sleeve.items() if v["permanent_needles"] > 0
        }
        return {
            "seam_group": seam_group,
            "sleeves": per_sleeve,
            "pair_deliverable": len(fully_sewn) == MAX_SLEEVES,
            "deliverable_sleeves": sorted(fully_sewn),
        }

    def _pair_blockers(
        self,
        seam_group: str,
        sleeve_id: str,
        perm_rows: list[sqlite3.Row],
        baste_rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        """该袖导致“一对交不齐”的具体原因：耗掉的临缝线，或越过可改区的定针。"""
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(
            "SELECT a.alloc_id, a.spool_id, a.units FROM thread_allocations a "
            "JOIN thread_spools s ON s.spool_id=a.spool_id "
            "WHERE a.sleeve_id=? AND a.released=0 AND s.kind=?",
            (sleeve_id, BASTE),
        ):
            out.append({
                "kind": "baste_thread_held",
                "alloc_id": r["alloc_id"],
                "spool": r["spool_id"],
                "units": r["units"],
            })
        if perm_rows:
            out.append({
                "kind": "permanent_needle_locks_loop",
                "needle_seq": perm_rows[-1]["needle_seq"],
                "locked_sleeve_arcs": json.loads(perm_rows[-1]["sleeve_arcs"]),
            })
        return out

    # ============================================================ 实针/线查询

    def needle_log(
        self, seam_group: str, sleeve_id: str | None = None
    ) -> list[dict[str, Any]]:
        """缝台的实针记录：与版师边圈谱分开，可单独查。"""
        sql = (
            "SELECT needle_seq, seam_group, sleeve_id, region, mode, ease, "
            "sleeve_arcs, body_arcs FROM sewing_needles WHERE seam_group=?"
        )
        args: list[Any] = [seam_group]
        if sleeve_id is not None:
            sql += " AND sleeve_id=?"
            args.append(sleeve_id)
        sql += " ORDER BY needle_seq"
        return [
            {
                "needle_seq": r["needle_seq"],
                "seam_group": r["seam_group"],
                "sleeve_id": r["sleeve_id"],
                "region": r["region"],
                "mode": r["mode"],
                "ease": r["ease"],
                "sleeve_arcs": json.loads(r["sleeve_arcs"]),
                "body_arcs": json.loads(r["body_arcs"]),
            }
            for r in self.conn.execute(sql, args)
        ]

    def thread_status(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.spool_id, s.kind, s.capacity, "
            "COALESCE(SUM(CASE WHEN a.released=0 THEN a.units END), 0) AS used "
            "FROM thread_spools s LEFT JOIN thread_allocations a "
            "ON s.spool_id=a.spool_id GROUP BY s.spool_id ORDER BY s.spool_id"
        ).fetchall()
        return [
            {
                "spool_id": r["spool_id"],
                "kind": r["kind"],
                "capacity": r["capacity"],
                "used": r["used"],
                "remaining": r["capacity"] - r["used"],
            }
            for r in rows
        ]

    # ============================================================ 内部

    def _assert_sleeve_cap(self, seam_group: str, sleeve_id: str) -> None:
        """最多两袖：同一单内已出现过的袖号不重复计数，第三只袖整针拒绝。"""
        known = {
            r["sleeve_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT sleeve_id FROM sewing_needles WHERE seam_group=?",
                (seam_group,),
            )
        } | {
            r["sleeve_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT a.sleeve_id FROM thread_allocations a "
                "JOIN thread_spools s ON s.spool_id=a.spool_id "
                "WHERE a.released=0 AND s.kind=? AND a.sleeve_id IS NOT NULL",
                (DECORATIVE,),
            )
        }
        if sleeve_id not in known and len(known) >= MAX_SLEEVES:
            raise Rejection("too_many_sleeves", limit=MAX_SLEEVES, group=seam_group)

    def _insert_needle(
        self,
        seam_group: str,
        sleeve_id: str,
        seam_id_s: str,
        seam_id_b: str,
        mode: str,
        nd: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO sewing_needles(seam_group, sleeve_id, seam_id_s, seam_id_b, "
            "region, mode, ease, sleeve_arcs, body_arcs, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seam_group, sleeve_id, seam_id_s, seam_id_b, nd["region"], mode,
                nd["ease"],
                json.dumps(nd["sleeve_arcs"], ensure_ascii=False),
                json.dumps(nd["body_arcs"], ensure_ascii=False),
                db.now_iso(),
            ),
        )

    def _needle_modes(
        self, seam_group: str, sleeve_id: str
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        rows = self.conn.execute(
            "SELECT * FROM sewing_needles WHERE seam_group=? AND sleeve_id=? "
            "ORDER BY needle_seq",
            (seam_group, sleeve_id),
        ).fetchall()
        return (
            [r for r in rows if r["mode"] == PERMANENT],
            [r for r in rows if r["mode"] == BASTE],
        )

    def _consumed_arcs(
        self, seam_group: str | None, sleeve_id: str | None
    ) -> tuple[set[str], set[str], set[str]]:
        """已定针（permanent）锁定的袖弧、身弧与已用吃势区。临缝不计入锁定。"""
        if seam_group is None:
            return set(), set(), set()
        sql = (
            "SELECT * FROM sewing_needles WHERE seam_group=? AND mode='permanent'"
        )
        args: list[Any] = [seam_group]
        if sleeve_id is not None:
            sql += " AND sleeve_id=?"
            args.append(sleeve_id)
        used_s: set[str] = set()
        used_b: set[str] = set()
        used_ease: set[str] = set()
        for r in self.conn.execute(sql, args):
            used_s.update(json.loads(r["sleeve_arcs"]))
            used_b.update(json.loads(r["body_arcs"]))
            if r["ease"] == 1:
                used_ease.add(r["region"])
        return used_s, used_b, used_ease

    @staticmethod
    def _check_marker_orientation(spec_s: Any, spec_b: Any) -> None:
        """袖山与衣身的同名定位标都不可翻面：存活一致且方向相配。"""
        common = set(spec_s.markers) & set(spec_b.markers)
        for mid in sorted(common):
            ms, mb = spec_s.markers[mid], spec_b.markers[mid]
            if ms.alive != mb.alive:
                raise Rejection("marker_liveness_mismatch", marker=mid)
            if ms.alive and ms.orientation != mb.orientation:
                raise Rejection(
                    "marker_orientation_mismatch",
                    marker=mid,
                    sleeve=ms.orientation,
                    body=mb.orientation,
                )

    @staticmethod
    def _sections(spec: Any) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for arc in spec.arcs:
            out.setdefault(arc.region, []).append(arc.as_dict())
        return dict(sorted(out.items()))

    def _record_eval(self, op_id: str, kind: str, result: dict[str, Any]) -> None:
        """评估类操作的留痕（只读评估，独立短事务；同 op_id 重放不重复写）。"""
        fp = db.fingerprint(kind, result)
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                "SELECT 1 FROM operations WHERE op_id=?", (op_id,)
            ).fetchone():
                conn.commit()
                return
            db.record_operation(conn, op_id, kind, fp)
            db.add_receipt(conn, kind, result["status"], result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
