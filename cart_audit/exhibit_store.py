"""展览保洁交接的冻结报告存储与独立复核。

与核查库同一套纪律：

- 一份交接报告冻结全部输入（分区/通路、轨迹快照、刷轮廓、初始粉、换套点、
  实物巾册与场外消费、场单、签收请求），旧版轨迹因此可反复查“每区受色的
  首次来源和套号”；
- 同 report_id 同内容复读返回原报告，异文抛 :class:`ExhibitRejected`；
- :func:`verify_exhibit` 只从冻结 payload 重跑 :func:`run_exhibit`，与库中
  结果做规范 JSON 逐字节比对，不依赖当前内存对象。

交接失败（路线不可交、签收不合格）是必须留痕的业务结论，因此与核查库
“失败不产生可通过报告”不同：这里会发布 ``ok=false`` 的报告作为失败证据，
但绝不把它呈现成可交接。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Dict, Optional, Sequence

from .exhibit import (
    BrushPlan,
    Desk,
    Hall,
    SessionSheet,
    Wipe,
    WipeSlot,
)
from .models import Profile, Trajectory
from .rational import qstr

__all__ = [
    "ExhibitRejected",
    "ExhibitLibrary",
    "build_payload",
    "run_exhibit",
    "verify_exhibit",
]


class ExhibitRejected(Exception):
    """report_id 已存在但冻结输入异文，拒绝覆盖。"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS exhibit_report (
    report_id    TEXT PRIMARY KEY,
    input_hash   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# payload 构造（对象 -> JSON 友好冻结资料）
# --------------------------------------------------------------------------- #
def build_payload(
    *,
    case_id: str,
    hall: Hall,
    horizon: tuple[Fraction, Fraction],
    trajectories: Dict[str, Trajectory],
    brush_profiles: Dict[str, Profile],
    brush_plans: Sequence[BrushPlan] = (),
    initial_zone_colors: Optional[Dict[str, Sequence[str]]] = None,
    inventory: Sequence[str] = (),
    sheets: Sequence[SessionSheet] = (),
    external_consumed: Sequence[Wipe] = (),
    actual_wipes: Sequence[Wipe] = (),
    signoffs: Sequence[dict] = (),
) -> dict:
    h0, h1 = horizon
    return {
        "case_id": case_id,
        "hall": hall.to_dict(),
        "horizon": [qstr(h0), qstr(h1)],
        "brush_profiles": {cid: p.to_dict() for cid, p in sorted(brush_profiles.items())},
        "trajectories": {
            cid: {
                "cart_id": traj.cart_id,
                "profile_id": traj.profile_id,
                "knots": [k.to_dict() for k in traj.knots],
            }
            for cid, traj in sorted(trajectories.items())
        },
        "brush_plans": [
            {
                "cart_id": p.cart_id,
                "initial_colors": sorted(p.initial_colors),
                "sleeve_changes": [qstr(t) for t in p.sleeve_changes],
            }
            for p in brush_plans
        ],
        "initial_zone_colors": {z: sorted(cs) for z, cs in sorted((initial_zone_colors or {}).items())},
        "inventory": sorted(inventory),
        "external_consumed": [
            {"wipe_id": w.wipe_id, "zone_id": w.zone_id, "t": qstr(w.t)} for w in external_consumed
        ],
        "actual_wipes": [
            {"wipe_id": w.wipe_id, "zone_id": w.zone_id, "t": qstr(w.t)} for w in actual_wipes
        ],
        "sheets": [_sheet_to_dict(s) for s in sheets],
        "signoffs": list(signoffs),
    }


def _sheet_to_dict(s: SessionSheet) -> dict:
    return {
        "session_id": s.session_id,
        "forbidden": s.forbidden,
        "open_t": qstr(s.open_t),
        "depart_t": qstr(s.depart_t),
        "entrance": s.entrance,
        "exhibit": s.exhibit,
        "waiting_zone": s.waiting_zone,
        "arrive_waiting": qstr(s.arrive_waiting) if s.arrive_waiting is not None else None,
        "handover": qstr(s.handover) if s.handover is not None else None,
        "old_route": list(s.old_route) if s.old_route else None,
        "new_route": list(s.new_route) if s.new_route else None,
        "slots": [{"zone_id": slot.zone_id, "t": qstr(slot.t)} for slot in s.slots],
    }


# --------------------------------------------------------------------------- #
# 从冻结 payload 重建并重算（存储复核与独立 verifier 共用）
# --------------------------------------------------------------------------- #
def _rebuild(payload: dict):
    hall = Hall.from_data(payload["hall"])
    h0 = Fraction(payload["horizon"][0])
    h1 = Fraction(payload["horizon"][1])
    horizon = (h0, h1)
    profiles = {cid: Profile.from_data(p) for cid, p in payload["brush_profiles"].items()}
    trajectories = {
        cid: Trajectory.from_data(item, horizon) for cid, item in payload["trajectories"].items()
    }
    plans = [
        BrushPlan(
            cart_id=p["cart_id"],
            initial_colors=frozenset(p.get("initial_colors", [])),
            sleeve_changes=tuple(Fraction(t) for t in p.get("sleeve_changes", [])),
        )
        for p in payload.get("brush_plans", [])
    ]
    initial = {z: list(cs) for z, cs in payload.get("initial_zone_colors", {}).items()}
    external = [
        Wipe(w["wipe_id"], w["zone_id"], Fraction(w["t"]))
        for w in payload.get("external_consumed", [])
    ]
    actual = [
        Wipe(w["wipe_id"], w["zone_id"], Fraction(w["t"]))
        for w in payload.get("actual_wipes", [])
    ]
    sheets = [_sheet_from_dict(d) for d in payload.get("sheets", [])]
    return hall, horizon, trajectories, profiles, plans, initial, external, actual, sheets


def _sheet_from_dict(d: dict) -> SessionSheet:
    return SessionSheet(
        session_id=d["session_id"],
        forbidden=d["forbidden"],
        open_t=Fraction(d["open_t"]),
        depart_t=Fraction(d["depart_t"]),
        entrance=d.get("entrance"),
        exhibit=d.get("exhibit"),
        waiting_zone=d.get("waiting_zone"),
        arrive_waiting=Fraction(d["arrive_waiting"]) if d.get("arrive_waiting") is not None else None,
        handover=Fraction(d["handover"]) if d.get("handover") is not None else None,
        old_route=tuple(d["old_route"]) if d.get("old_route") else None,
        new_route=tuple(d["new_route"]) if d.get("new_route") else None,
        slots=tuple(WipeSlot(s["zone_id"], Fraction(s["t"])) for s in d.get("slots", [])),
    )


def run_exhibit(payload: dict) -> dict:
    """按冻结 payload 完整重跑：三场调度、签收、最终来源与时间线。"""
    (
        hall, horizon, trajectories, profiles, plans, initial, external, actual, sheets,
    ) = _rebuild(payload)
    desk = Desk(
        hall=hall,
        horizon=horizon,
        trajectories=trajectories,
        brush_profiles=profiles,
        brush_plans=plans,
        initial_zone_colors=initial,
        inventory=payload.get("inventory", []),
        external_consumed=external,
        actual_wipes=actual,
    )
    decisions = desk.run_all(sheets)
    signoffs = []
    for req in payload.get("signoffs", []):
        signoffs.append(
            desk.sign_off(
                req["session_id"],
                req["leader"],
                Fraction(req["at"]) if req.get("at") is not None else None,
            )
        )
    sim = desk.simulation()
    return {
        "case_id": payload.get("case_id"),
        "ok": all(d["ok"] for d in decisions) and all(s["signed"] for s in signoffs),
        "decisions": decisions,
        "signoffs": signoffs,
        "remaining_wipe_ids": desk.free_ids(),
        "provenance": sim.provenance,
        "events": sim.events,
        "zone_timeline": {
            zid: [[qstr(t), sorted(colors)] for t, colors in timeline]
            for zid, timeline in sorted(sim.zone_timeline.items())
        },
    }


# --------------------------------------------------------------------------- #
# SQLite 报告库
# --------------------------------------------------------------------------- #
class ExhibitLibrary:
    def __init__(self, db_path: Optional[str] = None, *, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn if conn is not None else sqlite3.connect(db_path)
        self._owns_conn = conn is None
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    def publish(self, report_id: str, payload: dict) -> dict:
        if not isinstance(report_id, str) or not report_id:
            raise ExhibitRejected("report_id 必须是非空字符串")
        payload_json = _canonical(payload)
        input_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT input_hash, result_json FROM exhibit_report WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is not None:
            if row[0] != input_hash:
                raise ExhibitRejected(f"report_id {report_id!r} 已存在且冻结资料异文，拒绝覆盖")
            return json.loads(row[1])
        result = run_exhibit(payload)
        with self.conn:
            self.conn.execute(
                "INSERT INTO exhibit_report(report_id, input_hash, payload_json, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (report_id, input_hash, payload_json, _canonical(result), _now()),
            )
        return result

    def read(self, report_id: str) -> dict:
        row = self.conn.execute(
            "SELECT result_json FROM exhibit_report WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise ExhibitRejected(f"展览交接报告 {report_id!r} 不存在")
        return json.loads(row[0])

    def read_payload(self, report_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload_json FROM exhibit_report WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise ExhibitRejected(f"展览交接报告 {report_id!r} 不存在")
        return json.loads(row[0])

    def provenance(self, report_id: str) -> dict:
        """清扫岗旧版轨迹查询：每区每色的首次来源与套号。"""
        return self.read(report_id)["provenance"]

    def list_reports(self) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT report_id FROM exhibit_report ORDER BY rowid"
        ).fetchall()]


def verify_exhibit(library: ExhibitLibrary, report_id: str) -> dict:
    """从冻结 payload 独立重算并与库中结果逐字节比对。"""
    row = library.conn.execute(
        "SELECT input_hash, payload_json, result_json FROM exhibit_report WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        raise ExhibitRejected(f"展览交接报告 {report_id!r} 不存在")
    input_hash, payload_json, stored_result_json = row
    payload = json.loads(payload_json)
    actual_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    recomputed = run_exhibit(payload)
    recomputed_json = _canonical(recomputed)
    return {
        "report_id": report_id,
        "verified": actual_hash == input_hash and recomputed_json == stored_result_json,
        "payload_hash_match": actual_hash == input_hash,
        "result_match": recomputed_json == stored_result_json,
        "ok": recomputed["ok"],
    }
