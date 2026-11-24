"""红利权保管与兑领：claim、实体息票、登记处柜面、巡回付款台。

合成口径要点（不实现现实证券/银行制度）：
- 红利权是一张 claim（余额、归属人、兑领渠道）。拆分转让立即从原 claim 扣减，
  受让方得到新 claim；同一余额上能成功锁定的转让总额不超过原余额。
- 登记处柜面（facade）每日一班：受理过户、转息入册、领款。转入方 claim 可要求
  "转让回执已入册"才付款；回执未入册时明确告知回何处核、等到哪一步。
- 巡回付款台（circuit）用可拆转的具名实体息票：只能凭真实、未拆、未领的票领款；
  拆票必须收旧票再交子票，子票只携带"拆前旧票号码"；领过的真票被收走，不能退回。
  巡回台只凭票面真伪与本地票号簿核验，不联查登记处。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Optional

from .jsoncodec import dumps, loads, frac_to_json, json_to_frac

FACADE = "facade"
CIRCUIT = "circuit"
DESK_CIRCUIT = "desk:circuit"
DESK_FACADE = "desk:facade"


class CustodyError(Exception):
    code = "custody_error"

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _fail(code: str, message: str, **details):
    err = CustodyError(message, details)
    err.code = code
    return err


class ClaimBook:
    """权益锁定与兑领状态机（与红利簿共用同一 SQLite 连接）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._seq = 0
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS claims (
                claim_id      TEXT PRIMARY KEY,
                instrument_id TEXT NOT NULL,
                owner         TEXT NOT NULL,
                amount        TEXT NOT NULL,          -- Fraction JSON，绝不取整
                channel       TEXT NOT NULL,          -- facade / circuit
                origin        TEXT NOT NULL,          -- original / transfer
                receipt_id    TEXT,                   -- 转入 claim 对应的过户回执
                status        TEXT NOT NULL           -- active / paid / consumed
            );
            CREATE TABLE IF NOT EXISTS claim_transfers (
                transfer_id   TEXT PRIMARY KEY,
                source_claim  TEXT NOT NULL,
                new_claim     TEXT NOT NULL,
                from_owner    TEXT NOT NULL,
                to_owner      TEXT NOT NULL,
                amount        TEXT NOT NULL,
                receipt_id    TEXT NOT NULL,
                at_step       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transfer_receipts (
                receipt_id    TEXT PRIMARY KEY,
                transfer_id   TEXT NOT NULL,
                registered    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS transfer_applications (
                application_id TEXT PRIMARY KEY,
                claim_id       TEXT NOT NULL,
                from_owner     TEXT NOT NULL,
                to_owner       TEXT NOT NULL,
                amount         TEXT NOT NULL,
                receipt_id     TEXT NOT NULL,
                at_step        INTEGER NOT NULL,
                status         TEXT NOT NULL   -- locked / rejected
            );
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id     TEXT PRIMARY KEY,
                claim_id      TEXT NOT NULL,
                owner         TEXT NOT NULL,
                amount        TEXT NOT NULL,
                status        TEXT NOT NULL,   -- active / split / paid
                origin        TEXT NOT NULL,   -- original / split
                parent_ticket TEXT,
                old_no        TEXT,            -- 拆前旧票号码（子票只带这个）
                signature     TEXT NOT NULL,   -- 票面真伪标记
                presented_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS ticket_moves (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id     TEXT NOT NULL,
                from_owner    TEXT NOT NULL,
                to_owner      TEXT NOT NULL,
                reason        TEXT NOT NULL,
                at_step       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claim_payments (
                payment_id    TEXT PRIMARY KEY,
                claim_id      TEXT NOT NULL,
                ticket_id     TEXT,
                beneficiary   TEXT NOT NULL,  -- 实际领款人（息票持有人或权利归属人）
                amount        TEXT NOT NULL,
                channel       TEXT NOT NULL,
                at_step       INTEGER NOT NULL,
                receipt_id    TEXT
            );
            """
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # --------------------------------------------------------------- handlers
    @staticmethod
    def _register_channels() -> dict:
        return {
            FACADE: {"name": "登记处柜面", "daily_sittings": 1, "real_voucher": False},
            CIRCUIT: {"name": "巡回付款台", "daily_sittings": 2, "real_voucher": True},
        }

    # ----------------------------------------------------------------- helper
    def _row(self, table: str, key: str, value: str):
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {key} = ?", (value,)
        ).fetchone()

    def _claim_row(self, cid: str):
        row = self._row("claims", "claim_id", cid)
        if row is None:
            raise _fail("unknown_claim", f"未知红利权 {cid}", claim_id=cid)
        return row

    def _ticket_row(self, tid: str):
        row = self._row("tickets", "ticket_id", tid)
        if row is None:
            raise _fail("unknown_ticket", f"票号簿无此票 {tid}", ticket_id=tid)
        return row

    # ---------------------------------------------------------------- minting
    def open_claims(self, grouped: dict, channel_by_holder: dict, step: int) -> list[dict]:
        """按人数开 claim：{holder: Fraction}；同一人可指定 facade/circuit。"""
        out = []
        for holder in sorted(grouped):
            amount = Fraction(grouped[holder])
            if amount <= 0:
                continue
            cid = f"CLM-{len(out)+1:03d}-{holder}"
            channel = channel_by_holder.get(holder, FACADE)
            if channel not in (FACADE, CIRCUIT):
                raise _fail("bad_channel", f"未知兑领渠道 {channel}", holder=holder)
            self.conn.execute(
                "INSERT INTO claims (claim_id, instrument_id, owner, amount, channel, origin, "
                "receipt_id, status) VALUES (?, ?, ?, ?, ?, 'original', NULL, 'active')",
                (cid, f"lot-{step}", holder, dumps(frac_to_json(amount)), channel),
            )
            out.append({"claim_id": cid, "holder": holder,
                        "amount": frac_to_json(amount),
                        "channel": channel})
        return out

    def claim(self, cid: str) -> dict:
        row = self._claim_row(cid)
        return {
            "claim_id": cid,
            "owner": row["owner"],
            "amount": _decode(row["amount"]),
            "channel": row["channel"],
            "origin": row["origin"],
            "receipt_id": row["receipt_id"],
            "status": row["status"],
        }

    def list_claims(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM claims ORDER BY claim_id").fetchall()
        out = []
        for r in rows:
            out.append({
                "claim_id": r["claim_id"],
                "owner": r["owner"],
                "amount": _decode(r["amount"]),
                "channel": r["channel"],
                "origin": r["origin"],
                "status": r["status"],
            })
        return out

    # ---------------------------------------------------------------- transfer
    def apply_transfer(self, application_id: str, claim_id: str, from_owner: str,
                       to_owner: str, amount: Fraction, at_step: int, at: str) -> dict:
        """申请拆分转让一份权益：只有先到且余额够的申请能锁定额度。

        锁定额度立即从原 claim 扣减；后到申请若余额不足整笔拒绝并留下 rejected 记录。
        回执与原 claim 保持可查。
        """
        if amount <= 0:
            raise _fail("bad_amount", "转让金额必须为正", amount=frac_to_json(amount))
        receipt_id = f"RCPT-{application_id}"
        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self._row("transfer_applications", "application_id", application_id)
            if existing is not None:
                self.conn.execute("COMMIT")
                if existing["status"] == "locked":
                    return {"application_id": application_id, "status": "replayed",
                            "receipt_id": existing["receipt_id"]}
                raise _fail("prior_rejected", "同一申请号已被拒，不能复用",
                            application_id=application_id)
            claim = self._claim_row(claim_id)
            if claim["owner"] != from_owner:
                raise _fail("not_holder", "申请人不是当前持有人",
                            expected=claim["owner"], got=from_owner)
            if claim["status"] != "active":
                raise _fail("claim_inactive", "该权益已领讫或已耗尽，不能再转",
                            claim_id=claim_id, status=claim["status"])
            bal = _decode(claim["amount"])
            if amount > bal:
                self.conn.execute(
                    "INSERT INTO transfer_applications (application_id, claim_id, from_owner, "
                    "to_owner, amount, receipt_id, at_step, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected')",
                    (application_id, claim_id, from_owner, to_owner,
                     dumps(frac_to_json(amount)), receipt_id, at_step),
                )
                self.conn.execute("COMMIT")
                raise _fail("insufficient_balance", "余额不足以锁定该转让",
                            requested=frac_to_json(amount),
                            available=frac_to_json(bal), locked_by=None)
            remaining = bal - amount
            new_cid = f"{claim_id}+{application_id}"
            if remaining == 0:
                self.conn.execute(
                    "UPDATE claims SET amount=?, status='consumed' WHERE claim_id=?",
                    (dumps(frac_to_json(Fraction(0))), claim_id),
                )
            else:
                self.conn.execute(
                    "UPDATE claims SET amount=? WHERE claim_id=?",
                    (dumps(frac_to_json(remaining)), claim_id),
                )
            # 受让方 claim 继承渠道；facade 渠道另需回执入册方付款
            self.conn.execute(
                "INSERT INTO claims (claim_id, instrument_id, owner, amount, channel, origin, "
                "receipt_id, status) VALUES (?, ?, ?, ?, ?, 'transfer', ?, 'active')",
                (new_cid, claim["instrument_id"], to_owner,
                 dumps(frac_to_json(amount)), claim["channel"], receipt_id),
            )
            self.conn.execute(
                "INSERT INTO claim_transfers (transfer_id, source_claim, new_claim, from_owner, "
                "to_owner, amount, receipt_id, at_step) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (application_id, claim_id, new_cid, from_owner, to_owner,
                 dumps(frac_to_json(amount)), receipt_id, at_step),
            )
            self.conn.execute(
                "INSERT INTO transfer_receipts (receipt_id, transfer_id, registered) "
                "VALUES (?, ?, 0)",
                (receipt_id, application_id),
            )
            self.conn.execute(
                "INSERT INTO transfer_applications (application_id, claim_id, from_owner, "
                "to_owner, amount, receipt_id, at_step, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'locked')",
                (application_id, claim_id, from_owner, to_owner,
                 dumps(frac_to_json(amount)), receipt_id, at_step),
            )
            self.conn.execute("COMMIT")
        except CustodyError:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")
            raise
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "application_id": application_id,
            "status": "locked",
            "at": at,
            "receipt_id": receipt_id,
            "new_claim_id": new_cid,
            "source_remaining": frac_to_json(remaining),
            "receipt_registered": False,
        }

    def register_transfer_receipt(self, receipt_id: str, at_step: int) -> dict:
        row = self._row("transfer_receipts", "receipt_id", receipt_id)
        if row is None:
            raise _fail("unknown_receipt", f"登记处无此回执 {receipt_id}", receipt_id=receipt_id)
        if row["registered"]:
            return {"receipt_id": receipt_id, "status": "already_registered"}
        self.conn.execute(
            "UPDATE transfer_receipts SET registered=1 WHERE receipt_id=?", (receipt_id,)
        )
        return {"receipt_id": receipt_id, "status": "registered", "at_step": at_step}

    def receipt(self, receipt_id: str) -> dict:
        r = self._row("transfer_receipts", "receipt_id", receipt_id)
        if r is None:
            raise _fail("unknown_receipt", f"无此回执 {receipt_id}")
        t = self._row("claim_transfers", "transfer_id", r["transfer_id"])
        return {"receipt_id": receipt_id, "transfer": dict(t),
                "registered": bool(r["registered"]),
                "claim_id": t["new_claim"]}

    # ----------------------------------------------------------------- tickets
    def issue_ticket(self, claim_id: str, amount: Optional[Fraction] = None,
                     at_step: int = 0) -> dict:
        """为 circuit 渠道 claim 签发一张具名实体息票（默认全额）。"""
        claim = self._claim_row(claim_id)
        if claim["channel"] != CIRCUIT:
            raise _fail("wrong_channel", "只有巡回台渠道的权益才签发息票", claim_id=claim_id)
        if claim["status"] != "active":
            raise _fail("claim_inactive", "权益已领讫/已耗尽", claim_id=claim_id)
        if amount is None:
            amount = _decode(claim["amount"])
        if amount <= 0:
            raise _fail("bad_amount", "票面金额必须为正")
        active = self.conn.execute(
            "SELECT COALESCE(amount,0) FROM tickets WHERE claim_id=? AND status='active'",
            (claim_id,),
        ).fetchall()
        held = sum((_decode(a[0]) for a in active), Fraction(0))
        if held + amount > _decode(claim["amount"]):
            raise _fail("ticket_over_claim", "息票总额超过权益余额",
                        held=frac_to_json(held), claim=claim["amount"])
        tid = f"TK-{self._next_ticket_seq():03d}"
        self.conn.execute(
            "INSERT INTO tickets (ticket_id, claim_id, owner, amount, status, origin, "
            "parent_ticket, old_no, signature, presented_at) "
            "VALUES (?, ?, ?, ?, 'active', 'original', NULL, NULL, ?, NULL)",
            (tid, claim_id, claim["owner"], dumps(frac_to_json(amount)),
             f"sig:{tid}:{claim_id}"),
        )
        return {"ticket_id": tid, "claim_id": claim_id, "owner": claim["owner"],
                "amount": frac_to_json(amount), "old_no": tid}

    def _next_ticket_seq(self) -> int:
        n = self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        return n + 1

    def ticket(self, tid: str) -> dict:
        r = self._ticket_row(tid)
        return {
            "ticket_id": tid, "claim_id": r["claim_id"], "owner": r["owner"],
            "amount": _decode(r["amount"]), "status": r["status"],
            "origin": r["origin"], "parent_ticket": r["parent_ticket"],
            "old_no": r["old_no"],
        }

    def split_ticket(self, ticket_id: str, from_owner: str,
                     children: list[tuple[str, Fraction]], at_step: int,
                     at: str = CIRCUIT) -> dict:
        """拆票：必须收旧票，再交子票；子票只携带拆前旧票号码。"""
        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._ticket_row(ticket_id)
            if r["owner"] != from_owner:
                raise _fail("not_ticket_holder", "交票人不是票面持有人",
                            expected=r["owner"], got=from_owner)
            if r["status"] != "active":
                raise _fail("ticket_not_active",
                            "旧票已拆/已领，不能再次拆分", ticket_id=ticket_id,
                            status=r["status"])
            if not children or any(a <= 0 for _, a in children):
                raise _fail("bad_split", "子票金额必须全为正")
            total = sum((a for _, a in children), Fraction(0))
            face = _decode(r["amount"])
            if total != face:
                raise _fail("split_not_exact", "子票合计必须等于旧票面额",
                            children=frac_to_json(total), face=frac_to_json(face))
            old_no = r["ticket_id"] if r["old_no"] is None else r["old_no"]
            self.conn.execute("UPDATE tickets SET status='split' WHERE ticket_id=?",
                              (ticket_id,))
            kids = []
            for i, (recipient, amt) in enumerate(children, 1):
                kid = f"{ticket_id}.{i}"
                self.conn.execute(
                    "INSERT INTO tickets (ticket_id, claim_id, owner, amount, status, origin, "
                    "parent_ticket, old_no, signature, presented_at) "
                    "VALUES (?, ?, ?, ?, 'active', 'split', ?, ?, ?, NULL)",
                    (kid, r["claim_id"], recipient, dumps(frac_to_json(amt)),
                     ticket_id, old_no, f"sig:{kid}:{r['claim_id']}:{old_no}"),
                )
                self.conn.execute(
                    "INSERT INTO ticket_moves (ticket_id, from_owner, to_owner, reason, at_step) "
                    "VALUES (?, ?, ?, 'split_child', ?)",
                    (kid, ticket_id, recipient, at_step),
                )
                kids.append({"ticket_id": kid, "owner": recipient,
                             "amount": frac_to_json(amt), "old_no": old_no})
            self.conn.execute("COMMIT")
        except CustodyError:
            self.conn.execute("COMMIT")
            raise
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"parent": ticket_id, "parent_old_no": old_no, "children": kids,
                "old_ticket_surrendered": True}

    # ------------------------------------------------------------- circuit ops
    def _signature_ok(self, tid: str, r) -> bool:
        """票面真伪：原始票 sig:{tid}:{claim}；子票 sig:{tid}:{claim}:{旧号}。"""
        base = f"sig:{tid}:{r['claim_id']}"
        return r["signature"] == base or (
            r["origin"] == "split" and r["signature"] == f"{base}:{r['old_no']}")

    def check_ticket(self, ticket_id: str) -> dict:
        """巡回台本地核验：凭票号簿与票面真伪标记，不联查登记处。"""
        r = self._row("tickets", "ticket_id", ticket_id)
        if r is None:
            return {"ticket_id": ticket_id, "genuine": False, "reason": "not_in_local_book"}
        genuine = self._signature_ok(ticket_id, r)
        return {"ticket_id": ticket_id, "genuine": genuine,
                "status": r["status"], "owner": r["owner"],
                "amount": _decode(r["amount"])}

    def deliver_ticket(self, ticket_id: str, from_owner: str, at_step: int,
                       at: str = CIRCUIT) -> dict:
        """把真实、未拆票交到付款台（持有人离手）。"""
        r = self._ticket_row(ticket_id)
        if r["status"] != "active":
            raise _fail("ticket_not_payable",
                        "只有真实、未拆、未领的票可交收（领过/已拆的票不得退还）",
                        ticket_id=ticket_id, status=r["status"])
        if r["owner"] != from_owner:
            raise _fail("not_ticket_holder", "交票人不是票面持有人",
                        expected=r["owner"], got=from_owner)
        if not self._signature_ok(ticket_id, r):
            raise _fail("ticket_not_payable", "票面真伪标记不符",
                        ticket_id=ticket_id, status="forged")
        dest = DESK_CIRCUIT if at == CIRCUIT else DESK_FACADE
        self.conn.execute("UPDATE tickets SET owner=? WHERE ticket_id=?", (dest, ticket_id))
        self.conn.execute(
            "INSERT INTO ticket_moves (ticket_id, from_owner, to_owner, reason, at_step) "
            "VALUES (?, ?, ?, 'deliver', ?)",
            (ticket_id, from_owner, dest, at_step),
        )
        return {"ticket_id": ticket_id, "delivered_to": dest, "status": "active",
                "old_no": r["old_no"] or ticket_id}

    def pay_ticket(self, ticket_id: str, payment_id: str, at_step: int,
                   at: str = CIRCUIT) -> dict:
        """付款台兑付一张已交收的真票；付后票被收走，不能退回。"""
        r = self._ticket_row(ticket_id)
        if r["owner"] != DESK_CIRCUIT:
            raise _fail("ticket_not_delivered", "票未交到巡回台，不能付款",
                        owner=r["owner"])
        if r["status"] != "active":
            raise _fail("ticket_already_paid", "票已拆/已领，不得重复付款",
                        ticket_id=ticket_id, status=r["status"])
        if not self._signature_ok(ticket_id, r):
            raise _fail("ticket_not_payable", "票面真伪标记不符", ticket_id=ticket_id)
        amount = _decode(r["amount"])
        beneficiary = r["owner"] if r["owner"] != DESK_CIRCUIT else self._ticket_bearer(ticket_id, r)
        self.conn.execute("UPDATE tickets SET status='paid' WHERE ticket_id=?", (ticket_id,))
        self.conn.execute(
            "INSERT INTO claim_payments (payment_id, claim_id, ticket_id, beneficiary, amount, "
            "channel, at_step, receipt_id) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (payment_id, r["claim_id"], ticket_id, beneficiary, dumps(frac_to_json(amount)),
             CIRCUIT, at_step),
        )
        self._maybe_close_claim(r["claim_id"])
        return {"payment_id": payment_id, "ticket_id": ticket_id,
                "amount": frac_to_json(amount), "ticket_surrendered": True,
                "beneficiary": beneficiary, "claim_id": r["claim_id"]}

    def _ticket_bearer(self, ticket_id: str, r) -> str:
        """巡回台付款的领款人：取交票前的票面持有人（由最近一次 deliver 记录定位）。"""
        row = self.conn.execute(
            "SELECT from_owner FROM ticket_moves WHERE ticket_id=? AND reason='deliver' "
            "ORDER BY id DESC LIMIT 1", (ticket_id,)
        ).fetchone()
        if row is not None:
            return row["from_owner"]
        return r["owner"]

    def _maybe_close_claim(self, claim_id: str) -> None:
        n = self.conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE claim_id=? AND status!='paid'",
            (claim_id,),
        ).fetchone()[0]
        if n == 0:
            self.conn.execute("UPDATE claims SET status='paid' WHERE claim_id=?", (claim_id,))

    # -------------------------------------------------------------- facade ops
    def pay_claim(self, claim_id: str, payment_id: str, at_step: int,
                  at: str = FACADE) -> dict:
        """登记处柜面付款：查自己的应收份额与（转入方）回执入册后签付。"""
        claim = self._claim_row(claim_id)
        if claim["channel"] != FACADE:
            raise _fail("wrong_registry", "该权益不在登记处柜面兑领，应去巡回台",
                        claim_id=claim_id, channel=claim["channel"])
        if claim["status"] != "active":
            raise _fail("claim_inactive", "该权益已领讫/已耗尽", claim_id=claim_id,
                        status=claim["status"])
        if claim["origin"] == "transfer":
            rc = self._row("transfer_receipts", "receipt_id", claim["receipt_id"])
            if rc is None or not rc["registered"]:
                raise _fail(
                    "receipt_not_registered",
                    "转让回执尚未入册，登记处柜面不能签付",
                    receipt_id=claim["receipt_id"],
                    go_to="登记处过户窗口核对转让回执",
                    wait_until="回执入册（register_transfer_receipt）之后再来领款",
                )
        paid = self.conn.execute(
            "SELECT COUNT(*) FROM claim_payments WHERE claim_id=?", (claim_id,)
        ).fetchone()[0]
        if paid:
            raise _fail("claim_already_paid", "该权益已付过，不得重复付款",
                        claim_id=claim_id)
        amount = _decode(claim["amount"])
        self.conn.execute(
            "INSERT INTO claim_payments (payment_id, claim_id, ticket_id, beneficiary, amount, "
            "channel, at_step, receipt_id) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
            (payment_id, claim_id, claim["owner"], dumps(frac_to_json(amount)), FACADE,
             at_step, claim["receipt_id"]),
        )
        self.conn.execute("UPDATE claims SET status='paid' WHERE claim_id=?", (claim_id,))
        return {"payment_id": payment_id, "claim_id": claim_id,
                "amount": frac_to_json(amount), "channel": FACADE,
                "beneficiary": claim["owner"], "receipt_checked": claim["receipt_id"]}

    def paid_total(self, holder: str) -> Fraction:
        rows = self.conn.execute(
            "SELECT amount FROM claim_payments WHERE beneficiary=?", (holder,)
        ).fetchall()
        return sum((_decode(r[0]) for r in rows), Fraction(0))

    def payments(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM claim_payments ORDER BY at_step, payment_id"
        ).fetchall()
        return [{"payment_id": r["payment_id"], "claim_id": r["claim_id"],
                 "beneficiary": r["beneficiary"], "amount": _decode(r["amount"]),
                 "ticket_id": r["ticket_id"], "channel": r["channel"]} for r in rows]

    # -------------------------------------------------------- contested view
    def claim_state(self, claim_id: str) -> dict:
        """返回权益当前剩余与所有竞争申请（含被拒原因），便于说明归属锁定。"""
        claim = self._claim_row(claim_id)
        apps = self.conn.execute(
            "SELECT * FROM transfer_applications WHERE claim_id=? ORDER BY at_step",
            (claim_id,),
        ).fetchall()
        return {
            "claim_id": claim_id,
            "remaining": _decode(claim["amount"]),
            "holder": claim["owner"],
            "status": claim["status"],
            "applications": [
                {"application_id": a["application_id"], "from": a["from_owner"],
                 "to": a["to_owner"], "amount": _decode(a["amount"]),
                 "status": a["status"], "receipt": a["receipt_id"]}
                for a in apps
            ],
        }


# ------------------------------------------------------------------- decoding
def _decode(value):
    """Fraction JSON -> Fraction；兼容整数与 num/den 的 repr 文本。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, str):
        try:
            return json_to_frac(_load_amount(value))
        except Exception:
            return Fraction(value)
    return json_to_frac(value)


def _load_amount(text: str):
    """金额列存的是 Fraction JSON 文本；按 jsoncodec 还原。"""
    return loads(text)


