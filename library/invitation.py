"""柬房：联名柬配词、印寄、素封直寄与不可推断的交接。

两个岗位、两种证据视角
----------------------
- 礼房（li_fang）：只查自己经手的封面与准许公开的回执；持有 li_fang 证言与
  交接日志里**实际收到**的资料。礼房看不到访谈员保管的原谱与实名证言。
- 访谈员（interviewer）：持钥，保管原谱；敏感封面只供其核对，封缄直寄时
  礼房只收到素封编号与准许公开的寄件状态。

两种办理方式
------------
- local（开放时段离线印寄）：礼房看得到经手封面，普通印台即可；敏感封面
  在保密约定下不得走此路（见到专称即等于看到受保护身份）。
- sealed（有钥实印位封缄直寄）：访谈员在有钥印位打印敏感封面并套编号素封，
  礼房只见编号/状态/班次。每个有钥印位一班只能印寄一封，普通印台不能代用。

隐私闸（PrivacyGate）
---------------------
家属要求隐去父系或母系身份时启用。礼房能读到的全部交接材料、准寄与退回
记录（含旧回执）合起来，仍不能排除已登记的两种受保护身份中的任何一种：
- 配词/封面类观察要求**强不可区分**：两种身份下可安全配出的词完全一致；
- 单项属性资料（联名先后、字盘号）要求**单项放行**：放行后两侧仍各有世界；
  顺序与盘号各自单项通过、同时放行会泄密时，第二次放行被拒；
- 编号、退回时点、合批结果在两种身份下由系统做成同一投影，礼房无法据此推知。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .errors import Rejection
from .kinship import PROTECTED_SIDES, KinshipService, SIDE_CN

MAX_SIGNERS = 8
MAX_PENDING_CARDS = 6


class InvitationService:
    def __init__(self, conn: sqlite3.Connection, kinship: KinshipService) -> None:
        self.conn = conn
        self.kinship = kinship

    # ================================================================ 字模/字盘

    def set_mold(self, term: str, available: bool, op_id: str) -> dict[str, Any]:
        """印坊上架/撤换字模。撤去某词后，该词不再可用于配词。"""
        payload = {"term": term, "available": available}
        fp = db.fingerprint("set_mold", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "set_mold", fp)
            if prior is not None:
                conn.commit()
                return prior
            conn.execute(
                "INSERT INTO inv_molds(term, available) VALUES(?, ?) "
                "ON CONFLICT(term) DO UPDATE SET available=excluded.available",
                (term, 1 if available else 0),
            )
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "set_mold", fp)
            db.add_receipt(conn, "set_mold", "ok", result, op_id)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    def available_terms(self) -> list[str]:
        return [
            r["term"]
            for r in self.conn.execute(
                "SELECT term FROM inv_molds WHERE available=1 ORDER BY term"
            )
        ]

    def add_tray(self, tray_id: str) -> dict[str, Any]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO inv_trays(tray_id) VALUES(?)", (tray_id,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "ok", "tray_id": tray_id}

    def list_trays(self) -> list[str]:
        return [r["tray_id"] for r in self.conn.execute(
            "SELECT tray_id FROM inv_trays ORDER BY tray_id")]

    # ================================================================ 印位班次

    def register_position(self, position_id: str, keyed: bool) -> dict[str, Any]:
        """登记印位。印位是否有钥是物理事实；普通印台 keyed=False，不能代打敏感封面。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 有钥位通过班次占用表的 keyed 标记体现；普通印台不建占用行
            if keyed:
                conn.execute(
                    "INSERT OR IGNORE INTO inv_position_shifts"
                    "(position_id, shift, keyed) VALUES(?, ?, 1)",
                    (position_id, "__capacity__"),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "ok", "position_id": position_id, "keyed": keyed}

    def keyed_positions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT position_id FROM inv_position_shifts WHERE keyed=1 "
            "ORDER BY position_id"
        ).fetchall()
        return [r["position_id"] for r in rows]

    def position_remaining(self, position_id: str, shift: str) -> int:
        """有钥印位一班一封：返回该位在该班次还能接几封（0/1）。"""
        row = self.conn.execute(
            "SELECT 1 FROM inv_position_shifts WHERE position_id=? AND keyed=1",
            (position_id,),
        ).fetchone()
        if row is None:
            raise Rejection("not_a_keyed_position", position=position_id)
        used = self.conn.execute(
            "SELECT 1 FROM inv_position_shifts WHERE position_id=? AND shift=? "
            "AND card_id IS NOT NULL",
            (position_id, shift),
        ).fetchone()
        return 0 if used else 1

    def keyed_capacity(self, shift: str) -> list[dict[str, Any]]:
        return [
            {"position_id": pid, "remaining": self.position_remaining(pid, shift)}
            for pid in self.keyed_positions()
        ]

    def assign_keyed_position(self, card_id: str, position_id: str, shift: str) -> None:
        """把唯一钥位预占给某签单（占印位）；已占给别的柬即拒绝。"""
        if self.position_remaining(position_id, shift) == 0:
            raise Rejection(
                "keyed_position_shift_taken", position=position_id, shift=shift
            )
        self.conn.execute(
            "INSERT INTO inv_position_shifts(position_id, shift, keyed, card_id) "
            "VALUES(?, ?, 1, ?) "
            "ON CONFLICT(position_id, shift) DO UPDATE SET card_id=excluded.card_id",
            (position_id, shift, card_id),
        )

    # ================================================================ 柬单

    def register_card(
        self,
        card_id: str,
        recipient: str,
        signers: list[str],
        copy_id: str,
        deadline_shift: str,
        op_id: str,
        sensitive: bool | None = None,
        must_joint: list[str] | None = None,
    ) -> dict[str, Any]:
        """登记一张待发柬（单号即 card_id，可反复办理新柬、重开按单号续办）。

        规则：
        - 同一张柬只用一行称呼；落款 1..8 人，待发柬至多 6 张；
        - 收件人各须收一次；must_joint 约定的两人必须出现在给同一收件人的同一张柬；
        - sensitive 缺省时由“两侧可能世界能否共用同一安全词”自动判定；
        - 词只能从印坊给的备选词（字模）里挑，配词在印寄时按当时证据现算。
        """
        if not signers or len(signers) > MAX_SIGNERS:
            raise Rejection("bad_signer_count", card=card_id, limit=MAX_SIGNERS)
        if len(set(signers)) != len(signers):
            raise Rejection("duplicate_signer", card=card_id)
        payload = {
            "card_id": card_id,
            "recipient": recipient,
            "signers": list(signers),
            "copy_id": copy_id,
            "deadline_shift": deadline_shift,
            "must_joint": sorted(must_joint) if must_joint else None,
        }
        fp = db.fingerprint("register_card", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_card", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM inv_cards WHERE card_id=?", (card_id,)
            ).fetchone():
                raise Rejection("card_exists", card=card_id)
            # 柬面自身规则先于库内状态校验
            if must_joint:
                if len(must_joint) != 2 or not all(s in signers for s in must_joint):
                    raise Rejection(
                        "must_joint_violation", card=card_id, pair=must_joint
                    )
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM inv_cards WHERE status='registered'"
            ).fetchone()["n"]
            if pending >= MAX_PENDING_CARDS:
                raise Rejection("too_many_pending_cards", limit=MAX_PENDING_CARDS)
            # 收件人各须收一次：同一收件人已有未结柬则拒绝
            dup = conn.execute(
                "SELECT 1 FROM inv_cards WHERE recipient=? AND status IN "
                "('registered','mailed')",
                (recipient,),
            ).fetchone()
            if dup:
                raise Rejection("recipient_already_served", recipient=recipient)
            self.kinship._require_known_copy(copy_id)
            if sensitive is None:
                sensitive = self._is_sensitive(card_id, recipient, signers, copy_id)
            conn.execute(
                "INSERT INTO inv_cards(card_id, recipient, signers, copy_id, "
                "deadline_shift, sensitive, must_joint, status, op_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)",
                (
                    card_id, recipient, json.dumps(signers, ensure_ascii=False),
                    copy_id, deadline_shift, 1 if sensitive else 0,
                    json.dumps(must_joint, ensure_ascii=False) if must_joint else None,
                    op_id, db.now_iso(),
                ),
            )
            result = {"status": "ok", **payload, "sensitive": sensitive}
            db.record_operation(conn, op_id, "register_card", fp)
            db.add_receipt(conn, "register_card", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_card", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_card(self, card_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM inv_cards WHERE card_id=?", (card_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_card", card=card_id)
        return self._card_row(row)

    def list_cards(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM inv_cards WHERE status=? ORDER BY card_id", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM inv_cards ORDER BY card_id").fetchall()
        return [self._card_row(r) for r in rows]

    def hold_card(self, card_id: str, reason: str, op_id: str) -> dict[str, Any]:
        """标记某封只能暂存（留痕，不删除柬单）。"""
        payload = {"card_id": card_id, "reason": reason}
        fp = db.fingerprint("hold_card", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "hold_card", fp)
            if prior is not None:
                conn.commit()
                return prior
            card = self.get_card(card_id)
            if card["status"] != "registered":
                raise Rejection("card_not_open", card=card_id, status=card["status"])
            conn.execute(
                "UPDATE inv_cards SET status='held' WHERE card_id=?", (card_id,)
            )
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "hold_card", fp)
            db.add_receipt(conn, "hold_card", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("hold_card", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 配词计算

    def safe_terms_for_card(self, card_id: str, holder: str = "li_fang") -> dict[str, Any]:
        """按 holder 当时有权掌握的证据，计算该柬每个落款人在每份仍成立
        家系下语义正确的词，并给出对**全部落款人 × 全部家系**都正确的安全词。

        词必须同时在印坊上架字模与该副本词条内。无安全词时柬不能寄。
        """
        card = self.get_card(card_id)
        molds = set(self.available_terms())
        per_signer: dict[str, dict[str, list[str]]] = {}
        for signer in card["signers"]:
            viable = self.kinship.viable_terms(
                card["copy_id"], signer, card["recipient"], holder
            )
            per_signer[signer] = {
                tree: [t for t in terms if t in molds]
                for tree, terms in viable.items()
            }
        trees = self.kinship.tenable_trees(holder)
        safe: set[str] = set(molds)
        for tree in trees:
            for signer in card["signers"]:
                safe &= set(per_signer[signer].get(tree["tree_id"], []))
        return {
            "card_id": card_id,
            "holder": holder,
            "tenable_trees": [t["tree_id"] for t in trees],
            "per_signer": per_signer,
            "safe_terms": sorted(safe),
        }

    def card_is_sensitive(self, card: dict[str, Any]) -> bool:
        """动态判定：保密约定下，按**当前在架字模**两受保护身份无共用安全词即敏感。

        中性字模（如“长辈”）在架时敏感柬会降为普通柬；撤去中性字模后重新敏感。
        登记时的 stored sensitive 仅作初始快照，决策一律以本方法现算为准。
        """
        if not self.confidentiality_active():
            return False
        side_sets = self._common_safe_terms_by_side(card)
        if side_sets is None:
            # 某一身份已无成立世界：保密承诺已被破坏，不能按普通柬处理
            return True
        # 两侧无共用安全词，即任何在架词都会区分身份 -> 敏感
        return not (side_sets[PROTECTED_SIDES[0]] & side_sets[PROTECTED_SIDES[1]])

    def _common_safe_terms_by_side(self, card: dict[str, Any]) -> dict[str, set[str]] | None:
        """返回两身份各自安全词集合；任一身份无成立世界则返回 None。"""
        molds = set(self.available_terms())
        constraints = self._li_fang_attr_constraints()
        side_sets: dict[str, set[str]] = {}
        for side in PROTECTED_SIDES:
            trees = self.kinship.tenable_trees(
                "li_fang", extra_constraints={**constraints, "side": side}
            )
            if not trees:
                return None
            common = set(molds)
            for tree in trees:
                for signer in card["signers"]:
                    ok = {
                        term for term in molds
                        if self.kinship.term_valid_in_tree(
                            card["copy_id"], term, signer,
                            card["recipient"], tree["tree_id"]
                        )
                    }
                    common &= ok
            side_sets[side] = common
        return side_sets

    def _is_sensitive(self, card_id: str, recipient: str,
                      signers: list[str], copy_id: str) -> bool:
        """登记时的初始敏感性快照（决策仍以 card_is_sensitive 动态现算）。"""
        card = {"card_id": card_id, "recipient": recipient,
                "signers": signers, "copy_id": copy_id}
        return self.card_is_sensitive(card)

    # ================================================================ 保密约定

    def confidentiality_active(self) -> bool:
        """已登记带受保护 side 的家系即视为家属提出了父系/母系保密承诺。"""
        row = self.conn.execute(
            "SELECT 1 FROM kin_trees WHERE side IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def _li_fang_attr_constraints(self) -> dict[str, Any]:
        """礼房交接日志中实际收到的全部属性资料（含旧回执，累积证据）。"""
        out: dict[str, Any] = {}
        rows = self.conn.execute(
            "SELECT payload FROM inv_handover WHERE field_kind='attr' ORDER BY handover_id"
        ).fetchall()
        for r in rows:
            out.update(json.loads(r["payload"]))
        return out

    def gate_check_attr(
        self, field: str, value: Any, hypothetical: bool = False
    ) -> dict[str, Any]:
        """单项资料放行检查：放行后两种受保护身份仍各有成立世界才准。

        顺序（甲先/乙先）与盘号（东盘/西盘）各自单项可过；合起来若只剩
        一种身份，第二次放行拒绝——单项检查都过，同时放行即泄密。
        hypothetical=True 时只评估这一项本身（不带已收资料），用于回答
        “若只交盘号，单项检查能否通过”，不改变礼房的累积证据。
        """
        if not self.confidentiality_active():
            return {"status": "ok", "protected": False}
        base = {} if hypothetical else self._li_fang_attr_constraints()
        constraints = {**base, field: value}
        counts = {
            side: len(self.kinship.tenable_trees(
                "li_fang", extra_constraints={**constraints, "side": side}
            ))
            for side in PROTECTED_SIDES
        }
        if any(n == 0 for n in counts.values()):
            raise Rejection(
                "privacy_leak_single_item",
                field=field,
                value=value,
                remaining_worlds=counts,
            )
        return {"status": "ok", "protected": True, "remaining_worlds": counts}

    def release_attr(self, field: str, value: Any, op_id: str) -> dict[str, Any]:
        """礼房接收一项可公开的单项办事资料（先过隐私闸，再入交接日志）。"""
        payload = {"field": field, "value": value}
        fp = db.fingerprint("release_attr", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "release_attr", fp)
            if prior is not None:
                conn.commit()
                return prior
            check = self.gate_check_attr(field, value)
            conn.execute(
                "INSERT INTO inv_handover(field_kind, payload, op_id, created_at) "
                "VALUES('attr', ?, ?, ?)",
                (json.dumps({field: value}, ensure_ascii=False), op_id, db.now_iso()),
            )
            result = {"status": "ok", **payload, "gate": check}
            db.record_operation(conn, op_id, "release_attr", fp)
            db.add_receipt(conn, "release_attr", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("release_attr", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def release_term_credential(self, card_id: str, op_id: str) -> dict[str, Any]:
        """礼房接收配词凭据：凭据只含安全词；保密约定下要求强不可区分。"""
        payload = {"card_id": card_id}
        fp = db.fingerprint("release_term_credential", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "release_term_credential", fp)
            if prior is not None:
                conn.commit()
                return prior
            card = self.get_card(card_id)
            if card["status"] != "registered":
                raise Rejection("card_not_open", card=card_id, status=card["status"])
            # 保密约定下先做强不可区分检查：哪怕没有安全词，
            # “两身份可用词不同”这件事本身也不能经凭据交给礼房
            if self.confidentiality_active():
                self._assert_indistinguishable_terms(card)
            safe = self.safe_terms_for_card(card_id)["safe_terms"]
            if not safe:
                raise Rejection("no_safe_term", card=card_id)
            conn.execute(
                "INSERT INTO inv_handover(field_kind, payload, op_id, created_at) "
                "VALUES('term_credential', ?, ?, ?)",
                (json.dumps({"card_id": card_id, "safe_terms": safe},
                            ensure_ascii=False), op_id, db.now_iso()),
            )
            result = {"status": "ok", "card_id": card_id, "safe_terms": safe}
            db.record_operation(conn, op_id, "release_term_credential", fp)
            db.add_receipt(conn, "release_term_credential", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("release_term_credential", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def _assert_indistinguishable_terms(self, card: dict[str, Any]) -> None:
        """强不可区分：两种身份下安全词集合必须完全一致，否则拒绝交接。"""
        side_sets = self._common_safe_terms_by_side(card)
        if side_sets is None:
            raise Rejection(
                "privacy_identity_excluded", card=card["card_id"]
            )
        if side_sets[PROTECTED_SIDES[0]] != side_sets[PROTECTED_SIDES[1]]:
            raise Rejection(
                "privacy_leak_term_distinguishes_identity",
                card=card["card_id"],
                patrilineal_terms=sorted(side_sets[PROTECTED_SIDES[0]]),
                matrilineal_terms=sorted(side_sets[PROTECTED_SIDES[1]]),
            )

    def check_compatibility(self, card_id: str) -> dict[str, Any]:
        """先判断要寄的联名柬是否与保密承诺相容。

        相容：无保密约定，或两种身份下都存在且共用同一安全词（普通柬）；
        敏感柬只能走封缄直寄，且直寄投影本身仍须通过不可区分检查。
        """
        card = self.get_card(card_id)
        if not self.confidentiality_active():
            return {
                "card_id": card_id,
                "compatible": True,
                "mode": "local",
                "reason": "no_confidentiality_commitment",
            }
        safe = self.safe_terms_for_card(card_id)["safe_terms"]
        if not self.card_is_sensitive(card):
            return {"card_id": card_id, "compatible": True,
                    "mode": "local", "safe_terms": safe}
        # 无共用安全词（或中性字模已撤）：敏感柬。直寄路径的编号/状态投影由
        # 系统做成两侧同形，故相容性取决于“是否还存在有钥容量”，占用在印寄时再判。
        if self.keyed_positions():
            return {"card_id": card_id, "compatible": True,
                    "mode": "sealed", "reason": "sensitive_cover_sealed_only"}
        return {"card_id": card_id, "compatible": False,
                "mode": None, "reason": "sensitive_without_keyed_position"}

    # ================================================================ 印寄

    def print_local(
        self,
        op_id: str,
        card_id: str,
        term: str,
        shift: str,
        tray_id: str | None = None,
    ) -> dict[str, Any]:
        """方式一：礼房在开放时段离线印寄（看得到经手封面，普通印台）。

        敏感封面在保密约定下禁止走此路；用词必须是该柬对全部落款人、在礼房
        每份仍不能排除的家系里都语义正确的安全词，且字模在架。
        """
        payload = {"card_id": card_id, "term": term, "shift": shift,
                   "tray_id": tray_id, "mode": "local"}
        fp = db.fingerprint("print_local", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "print_local", fp)
            if prior is not None:
                conn.commit()
                return prior
            card = self._require_open_card(card_id)
            if shift != card["deadline_shift"]:
                raise Rejection(
                    "shift_before_deadline_required",
                    shift=shift, deadline=card["deadline_shift"],
                )
            if term not in self.available_terms():
                raise Rejection("mold_unavailable", term=term)
            if self.confidentiality_active() and self.card_is_sensitive(card):
                raise Rejection(
                    "sensitive_cover_requires_sealed", card=card_id
                )
            safe = self.safe_terms_for_card(card_id)["safe_terms"]
            if term not in safe:
                raise Rejection(
                    "term_not_safe_across_all_trees",
                    card=card_id, term=term, safe_terms=safe,
                )
            if tray_id is not None and not conn.execute(
                "SELECT 1 FROM inv_trays WHERE tray_id=?", (tray_id,)
            ).fetchone():
                raise Rejection("unknown_tray", tray=tray_id)
            envelope = self._next_envelope_no()
            conn.execute(
                "INSERT INTO inv_mailings(envelope_no, card_id, shift, mode, "
                "position_id, term, tray_id, sealed, public_status, op_id, created_at) "
                "VALUES(?, ?, ?, 'local', NULL, ?, ?, 0, 'accepted', ?, ?)",
                (envelope, card_id, shift, term, tray_id, op_id, db.now_iso()),
            )
            conn.execute(
                "UPDATE inv_cards SET status='mailed' WHERE card_id=?", (card_id,)
            )
            self._log_mail_status(envelope, card_id, shift, "accepted", op_id)
            result = {"status": "ok", "envelope_no": envelope, "card_id": card_id,
                      "mode": "local", "term": term, "shift": shift, "tray_id": tray_id}
            db.record_operation(conn, op_id, "print_local", fp)
            db.add_receipt(conn, "print_local", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("print_local", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def print_sealed(
        self,
        op_id: str,
        card_id: str,
        term: str,
        position_id: str,
        shift: str,
        tray_id: str | None = None,
        envelope_no: str | None = None,
    ) -> dict[str, Any]:
        """方式二：访谈员在有钥实印位打印敏感封面、套编号素封后封缄直寄。

        - 必须是有钥印位，且该位本班未占（一班一封）；普通印台不能代用；
        - 用词由访谈员按实际家系核对（持钥可见全证据），但写入素封的投影
          不含词、不含落款，仅编号 + 公开状态 + 班次；
        - 保密约定下，素封投影在两种身份下必须同形（强不可区分检查）。
        """
        payload = {"card_id": card_id, "term": term, "position_id": position_id,
                   "shift": shift, "tray_id": tray_id, "mode": "sealed",
                   "envelope_no": envelope_no}
        fp = db.fingerprint("print_sealed", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "print_sealed", fp)
            if prior is not None:
                conn.commit()
                return prior
            card = self._require_open_card(card_id)
            if shift != card["deadline_shift"]:
                raise Rejection(
                    "shift_before_deadline_required",
                    shift=shift, deadline=card["deadline_shift"],
                )
            if term not in self.available_terms():
                raise Rejection("mold_unavailable", term=term)
            if self.position_remaining(position_id, shift) == 0:
                raise Rejection(
                    "keyed_position_shift_taken",
                    position=position_id, shift=shift,
                )
            # 访谈员视角：实际家系下用词必须对全部落款人语义正确
            iv_safe = self.safe_terms_for_card(card_id, holder="interviewer")
            if term not in iv_safe["safe_terms"]:
                raise Rejection(
                    "term_not_valid_under_actual_tree",
                    card=card_id, term=term,
                    interviewer_safe_terms=iv_safe["safe_terms"],
                )
            if self.confidentiality_active():
                # 礼房视角仍须同时保留两种身份，且直寄投影不携带任何区分信息
                self._assert_both_sides_remain(card)
            envelope = envelope_no or self._next_envelope_no()
            if conn.execute(
                "SELECT 1 FROM inv_mailings WHERE envelope_no=?", (envelope,)
            ).fetchone():
                raise Rejection("envelope_no_taken", envelope=envelope)
            conn.execute(
                "INSERT INTO inv_position_shifts(position_id, shift, keyed, card_id) "
                "VALUES(?, ?, 1, ?) "
                "ON CONFLICT(position_id, shift) DO UPDATE SET card_id=excluded.card_id",
                (position_id, shift, card_id),
            )
            # 敏感封面全量落库供持钥岗位核对；sealed=1 的行对礼房查询做投影过滤
            conn.execute(
                "INSERT INTO inv_mailings(envelope_no, card_id, shift, mode, "
                "position_id, term, tray_id, sealed, public_status, op_id, created_at) "
                "VALUES(?, ?, ?, 'sealed', ?, ?, ?, 1, 'accepted', ?, ?)",
                (envelope, card_id, shift, position_id, term, tray_id,
                 op_id, db.now_iso()),
            )
            conn.execute(
                "UPDATE inv_cards SET status='mailed' WHERE card_id=?", (card_id,)
            )
            self._log_mail_status(envelope, card_id, shift, "accepted", op_id)
            result = {"status": "ok", "envelope_no": envelope, "card_id": card_id,
                      "mode": "sealed", "position_id": position_id, "shift": shift,
                      "term": term, "tray_id": tray_id,
                      "li_fang_view": {"envelope_no": envelope,
                                       "public_status": "accepted", "shift": shift}}
            db.record_operation(conn, op_id, "print_sealed", fp)
            db.add_receipt(conn, "print_sealed", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("print_sealed", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def return_envelope(self, op_id: str, envelope_no: str) -> dict[str, Any]:
        """退回记录：礼房可见公开状态；退回时点由调用班次给定，系统不把
        家系信息编进时点（两身份下同编号同状态）。"""
        payload = {"envelope_no": envelope_no}
        fp = db.fingerprint("return_envelope", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "return_envelope", fp)
            if prior is not None:
                conn.commit()
                return prior
            row = conn.execute(
                "SELECT * FROM inv_mailings WHERE envelope_no=?", (envelope_no,)
            ).fetchone()
            if row is None:
                raise Rejection("unknown_envelope", envelope=envelope_no)
            conn.execute(
                "UPDATE inv_mailings SET public_status='returned' WHERE envelope_no=?",
                (envelope_no,),
            )
            conn.execute(
                "UPDATE inv_cards SET status='returned' WHERE card_id=?",
                (row["card_id"],),
            )
            self._log_mail_status(envelope_no, row["card_id"], row["shift"],
                                  "returned", op_id)
            result = {"status": "ok", "envelope_no": envelope_no,
                      "public_status": "returned", "card_id": row["card_id"]}
            db.record_operation(conn, op_id, "return_envelope", fp)
            db.add_receipt(conn, "return_envelope", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("return_envelope", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def run_batch(self, shift: str, op_prefix: str) -> dict[str, Any]:
        """以截止前能安全寄出的最多具名信件交单。

        普通柬走普通印台（不限量），敏感柬占用有钥印位（一班一封/位）；
        礼房可见整批回执在两种身份下同形：被暂存的敏感柬只报“暂存/容量”，
        不暴露是哪一个专称、哪一种身份。
        """
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            cards = [
                self._card_row(r)
                for r in conn.execute(
                    "SELECT * FROM inv_cards WHERE status='registered' "
                    "AND deadline_shift=? ORDER BY card_id",
                    (shift,),
                )
            ]
            mailed: list[dict[str, Any]] = []
            held: list[dict[str, Any]] = []
            for card in cards:
                op_id = f"{op_prefix}-{card['card_id']}"
                if self.card_is_sensitive(card):
                    pos = self._free_keyed_position(shift)
                    if pos is None:
                        held.append({"card_id": card["card_id"],
                                     "reason": "no_keyed_capacity_sealed_only"})
                        conn.execute(
                            "UPDATE inv_cards SET status='held' WHERE card_id=?",
                            (card["card_id"],),
                        )
                        db.add_receipt(conn, "run_batch", "rejected", {
                            "status": "rejected",
                            "error": {"reason": "no_keyed_capacity_sealed_only",
                                      "card": card["card_id"]},
                        }, op_id)
                        continue
                    iv_safe = self.safe_terms_for_card(
                        card["card_id"], holder="interviewer")
                    if not iv_safe["safe_terms"]:
                        held.append({"card_id": card["card_id"],
                                     "reason": "no_safe_term_actual_tree"})
                        continue
                    term = iv_safe["safe_terms"][0]
                    envelope = self._next_envelope_no()
                    conn.execute(
                        "INSERT INTO inv_position_shifts"
                        "(position_id, shift, keyed, card_id) "
                        "VALUES(?, ?, 1, ?) "
                        "ON CONFLICT(position_id, shift) "
                        "DO UPDATE SET card_id=excluded.card_id",
                        (pos, shift, card["card_id"]),
                    )
                    conn.execute(
                        "INSERT INTO inv_mailings(envelope_no, card_id, shift, mode, "
                        "position_id, term, sealed, public_status, op_id, created_at) "
                        "VALUES(?, ?, ?, 'sealed', ?, ?, 1, 'accepted', ?, ?)",
                        (envelope, card["card_id"], shift, pos, term,
                         op_id, db.now_iso()),
                    )
                    conn.execute(
                        "UPDATE inv_cards SET status='mailed' WHERE card_id=?",
                        (card["card_id"],),
                    )
                    self._log_mail_status(envelope, card["card_id"], shift,
                                          "accepted", op_id)
                    mailed.append({"card_id": card["card_id"], "mode": "sealed",
                                   "envelope_no": envelope, "position_id": pos})
                    db.add_receipt(conn, "run_batch", "ok", {
                        "status": "ok", "card_id": card["card_id"],
                        "mode": "sealed", "envelope_no": envelope,
                    }, op_id)
                else:
                    safe = self.safe_terms_for_card(card["card_id"])["safe_terms"]
                    if not safe:
                        held.append({"card_id": card["card_id"],
                                     "reason": "no_safe_term"})
                        conn.execute(
                            "UPDATE inv_cards SET status='held' WHERE card_id=?",
                            (card["card_id"],),
                        )
                        db.add_receipt(conn, "run_batch", "rejected", {
                            "status": "rejected",
                            "error": {"reason": "no_safe_term",
                                      "card": card["card_id"]},
                        }, op_id)
                        continue
                    term = safe[0]
                    envelope = self._next_envelope_no()
                    conn.execute(
                        "INSERT INTO inv_mailings(envelope_no, card_id, shift, mode, "
                        "position_id, term, sealed, public_status, op_id, created_at) "
                        "VALUES(?, ?, ?, 'local', NULL, ?, 0, 'accepted', ?, ?)",
                        (envelope, card["card_id"], shift, term, op_id, db.now_iso()),
                    )
                    conn.execute(
                        "UPDATE inv_cards SET status='mailed' WHERE card_id=?",
                        (card["card_id"],),
                    )
                    self._log_mail_status(envelope, card["card_id"], shift,
                                          "accepted", op_id)
                    mailed.append({"card_id": card["card_id"], "mode": "local",
                                   "envelope_no": envelope, "term": term})
                    db.add_receipt(conn, "run_batch", "ok", {
                        "status": "ok", "card_id": card["card_id"],
                        "mode": "local", "envelope_no": envelope, "term": term,
                    }, op_id)
            result = {
                "status": "ok",
                "shift": shift,
                "mailed": mailed,
                "held": held,
                "keyed_remaining": self.keyed_capacity(shift),
                "li_fang_view": self.li_fang_batch_view(shift),
            }
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    # ================================================================ 角色化查询

    def interviewer_mailings(self) -> list[dict[str, Any]]:
        """持钥访谈员：逐封落款、收件人与称呼（封面全量核对）。"""
        rows = self.conn.execute(
            "SELECT m.*, c.recipient, c.signers FROM inv_mailings m "
            "JOIN inv_cards c ON c.card_id=m.card_id ORDER BY m.envelope_no"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "envelope_no": r["envelope_no"],
                "card_id": r["card_id"],
                "recipient": r["recipient"],
                "signers": json.loads(r["signers"]),
                "term": r["term"],
                "mode": r["mode"],
                "position_id": r["position_id"],
                "shift": r["shift"],
                "tray_id": r["tray_id"],
                "public_status": r["public_status"],
            })
        return out

    def li_fang_mailings(self) -> list[dict[str, Any]]:
        """礼房：自己经手（local）的封面可见；sealed 只见素封编号与公开状态。"""
        rows = self.conn.execute(
            "SELECT * FROM inv_mailings ORDER BY envelope_no"
        ).fetchall()
        out = []
        for r in rows:
            if r["sealed"]:
                out.append({
                    "envelope_no": r["envelope_no"],
                    "sealed": True,
                    "public_status": r["public_status"],
                    "shift": r["shift"],
                })
            else:
                card = self.get_card(r["card_id"])
                out.append({
                    "envelope_no": r["envelope_no"],
                    "sealed": False,
                    "card_id": r["card_id"],
                    "recipient": card["recipient"],
                    "signers": card["signers"],
                    "term": r["term"],
                    "tray_id": r["tray_id"],
                    "public_status": r["public_status"],
                    "shift": r["shift"],
                })
        return out

    def li_fang_batch_view(self, shift: str | None = None) -> dict[str, Any]:
        """礼房在一批之后能看到的全部资料：本人封面 + 素封投影 + 暂存（不带专称）。"""
        held = [
            {"card_id": c["card_id"], "status": c["status"]}
            for c in self.list_cards() if c["status"] == "held"
        ]
        capacity: dict[str, int] = {}
        if shift is not None:
            capacity = {
                r["position_id"]: r["remaining"]
                for r in self.keyed_capacity(shift)
            }
        return {
            "mailings": self.li_fang_mailings(),
            "held_cards": held,
            "keyed_remaining": capacity,
        }

    # 礼房可直接读的柬房操作种类；封缄寄件的封面内容（称呼/落款）不在其列
    LI_FANG_RECEIPT_KINDS = frozenset({
        "register_copy_terms", "register_tree", "register_registration",
        "set_mold", "register_card", "hold_card", "release_attr",
        "release_term_credential", "return_envelope",
    })

    def li_fang_receipts(self) -> list[dict[str, Any]]:
        """礼房专用回执视图：封缄寄件只保留素封编号与准许公开的状态。

        全局 receipts 表供持钥/审计岗位全量核对；本视图按礼房“只查自己经手的
        封面与准许公开的回执”做投影，避免它经原始回执读到敏感封面称呼。
        """
        rows = self.conn.execute(
            "SELECT receipt_id, op_id, kind, status, result, created_at "
            "FROM receipts ORDER BY receipt_id"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            result = json.loads(r["result"])
            if r["kind"] == "print_sealed":
                if r["status"] == "ok":
                    out.append({
                        "receipt_id": r["receipt_id"], "op_id": r["op_id"],
                        "kind": r["kind"], "status": r["status"],
                        "result": {
                            "status": "ok",
                            "envelope_no": result.get("envelope_no"),
                            "mode": "sealed",
                            "li_fang_view": result.get("li_fang_view"),
                        },
                        "created_at": r["created_at"],
                    })
                else:
                    # 拒绝回执只留原因码与柬号，剥离可能的专称字段
                    out.append({
                        "receipt_id": r["receipt_id"], "op_id": r["op_id"],
                        "kind": r["kind"], "status": r["status"],
                        "result": {"status": "rejected",
                                   "error": {"reason": result.get("error", {})
                                             .get("reason")}},
                        "created_at": r["created_at"],
                    })
            elif r["kind"] in self.LI_FANG_RECEIPT_KINDS:
                out.append({
                    "receipt_id": r["receipt_id"], "op_id": r["op_id"],
                    "kind": r["kind"], "status": r["status"],
                    "result": result, "created_at": r["created_at"],
                })
            # print_local 封面礼房经 li_fang_mailings 可见，原始回执不直接外推；
            # 借书系统原有的 catalog/ledger 回执不属于柬房岗位视图
        return out

    def public_mail_status(self, envelope_no: str) -> dict[str, Any]:
        """准许公开的寄件状态（任何岗位可调，不含封面内容）。"""
        row = self.conn.execute(
            "SELECT envelope_no, public_status, shift, sealed FROM inv_mailings "
            "WHERE envelope_no=?",
            (envelope_no,),
        ).fetchone()
        if row is None:
            raise Rejection("unknown_envelope", envelope=envelope_no)
        return {"envelope_no": row["envelope_no"],
                "public_status": row["public_status"],
                "shift": row["shift"], "sealed": bool(row["sealed"])}

    def li_fang_handover(self) -> list[dict[str, Any]]:
        """礼房只见自己已收到的资料（交接日志全量，含旧回执）。"""
        rows = self.conn.execute(
            "SELECT handover_id, field_kind, payload, op_id, created_at "
            "FROM inv_handover ORDER BY handover_id"
        ).fetchall()
        return [
            {"handover_id": r["handover_id"], "field_kind": r["field_kind"],
             "payload": json.loads(r["payload"]), "op_id": r["op_id"],
             "created_at": r["created_at"]}
            for r in rows
        ]

    def identity_remaining_possible(self) -> dict[str, int]:
        """礼房视角下两种受保护身份各自仍有多少成立世界（隐私承诺自检）。"""
        constraints = self._li_fang_attr_constraints()
        return {
            side: len(self.kinship.tenable_trees(
                "li_fang", extra_constraints={**constraints, "side": side}
            ))
            for side in PROTECTED_SIDES
        }

    # ================================================================ 内部

    def _assert_both_sides_remain(self, card: dict[str, Any]) -> None:
        counts = self.identity_remaining_possible()
        if any(n == 0 for n in counts.values()):
            raise Rejection(
                "privacy_identity_excluded",
                card=card["card_id"], remaining_worlds=counts,
            )

    def _free_keyed_position(self, shift: str) -> str | None:
        for pid in self.keyed_positions():
            if self.position_remaining(pid, shift) == 1:
                return pid
        return None

    def _next_envelope_no(self) -> str:
        nxt = int(db.get_meta(self.conn, "envelope_counter", "0")) + 1
        db.set_meta(self.conn, "envelope_counter", str(nxt))
        # 素封编号不编码家系：纯顺序号，两种身份下同批同序同号
        return f"E{nxt:04d}"

    def _log_mail_status(self, envelope: str, card_id: str,
                         shift: str, status: str, op_id: str) -> None:
        self.conn.execute(
            "INSERT INTO inv_handover(field_kind, payload, op_id, created_at) "
            "VALUES('mail_status', ?, ?, ?)",
            (json.dumps({"envelope_no": envelope, "card_id": card_id,
                         "shift": shift, "public_status": status},
                        ensure_ascii=False),
             op_id, db.now_iso()),
        )

    def _require_open_card(self, card_id: str) -> dict[str, Any]:
        card = self.get_card(card_id)
        if card["status"] != "registered":
            raise Rejection("card_not_open", card=card_id, status=card["status"])
        return card

    @staticmethod
    def _card_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "card_id": row["card_id"],
            "recipient": row["recipient"],
            "signers": json.loads(row["signers"]),
            "copy_id": row["copy_id"],
            "deadline_shift": row["deadline_shift"],
            "sensitive": bool(row["sensitive"]),
            "must_joint": json.loads(row["must_joint"]) if row["must_joint"] else None,
            "status": row["status"],
        }

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
