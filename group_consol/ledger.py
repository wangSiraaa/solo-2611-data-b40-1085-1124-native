"""独立实体账簿（P/S 各一本，物理上同库分表，逻辑上完全独立）。

设计要点：
1. 只追加（append-only）：按实体分别分配连续提交序号 seq，任何合并操作都不修改单体账。
2. 原子入账：post 在单个 SQLite 事务内完成全部校验与写入，任一校验失败整笔回滚，
   不留半截凭证、不跳号。
3. 稳定身份与复读：(entity, journal_id) 唯一；规范内容哈希持久化，同规范重投复读原
   seq，异文拒绝。数组行顺序有意义，纳入规范哈希。
4. 局部校验：内部销货/采购、内部收付款只校验本实体文档与本实体分录，不要求对侧已提交，
   绝不为集团合并拒绝合法的单边先入账。
5. 真实可重开：状态全部在 SQLite 落盘，重新打开连接即可读取全部历史前缀。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .accounts import ACCOUNTS, ACCOUNT_SET, ENTITY_SET, zero_balances, apply_line
from .errors import ConflictError, ValidationError
from .jsoncodec import dumps, loads

# 内部文档允许的字段（未知字段整笔拒绝，避免静默吞掉异文）
_SALE_FIELDS = frozenset(("type", "side", "invoice_id", "seller", "buyer", "sku", "q", "T", "C"))
_PAYMENT_FIELDS = frozenset(("type", "invoice_id", "side"))


class EntityLedger:
    """实体账簿门面。一个实例绑定一个 SQLite 文件，可被合并服务与单体客户共用。"""

    def __init__(self, path: str):
        # isolation_level=None：关闭驱动隐式事务，事务边界完全由显式
        # BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免嵌套事务错误
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # ------------------------------------------------------------------ schema
    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS journals (
                entity      TEXT NOT NULL,
                journal_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                canonical   TEXT NOT NULL,          -- 规范内容哈希输入（JSON 文本）
                doc_type    TEXT,                   -- NULL / internal_sale / internal_payment
                PRIMARY KEY (entity, journal_id),
                UNIQUE (entity, seq)
            );

            CREATE TABLE IF NOT EXISTS journal_lines (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity      TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                line_no     INTEGER NOT NULL,       -- 数组行顺序，有意义
                line_id     TEXT NOT NULL,          -- 行身份：显式 id 或位置序号
                account     TEXT NOT NULL,
                debit       INTEGER NOT NULL,
                credit      INTEGER NOT NULL,
                UNIQUE (entity, seq, line_id),
                FOREIGN KEY (entity, seq) REFERENCES journals (entity, seq)
            );

            CREATE TABLE IF NOT EXISTS internal_invoices (
                entity      TEXT NOT NULL,
                invoice_id  TEXT NOT NULL,
                side        TEXT NOT NULL,          -- seller / buyer
                seller      TEXT NOT NULL,
                buyer       TEXT NOT NULL,
                sku         TEXT NOT NULL,
                q           INTEGER NOT NULL,
                T           INTEGER NOT NULL,
                C           INTEGER NOT NULL,
                journal_seq INTEGER NOT NULL,
                PRIMARY KEY (entity, invoice_id)
            );

            CREATE TABLE IF NOT EXISTS internal_payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity      TEXT NOT NULL,
                invoice_id  TEXT NOT NULL,
                side        TEXT NOT NULL,          -- receipt（卖方收）/ settlement（买方付）
                amount      INTEGER NOT NULL,
                journal_seq INTEGER NOT NULL,
                journal_id  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_versions (
                version     TEXT PRIMARY KEY,
                canonical   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_items (
                version     TEXT NOT NULL,
                invoice_id  TEXT NOT NULL,
                r           INTEGER NOT NULL,
                PRIMARY KEY (version, invoice_id)
            );
            """
        )
        # executescript 在 autocommit 模式下可能遗留打开的事务，显式收尾
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ line parsing
    @staticmethod
    def _parse_lines(raw_lines: Any) -> list[dict]:
        """把输入行解析为规范行字典，并完成行级与整笔校验。

        支持两种行形态：
        - 列表行 [account, debit, credit]：行身份取位置序号（数组顺序有意义）；
        - 字典行 {"line_id"?: ..., "account", "debit", "credit"}：显式行身份不可重复。
        金额必须是非 bool 的非负整数，且每行恰一侧为正；总借必须等于总贷且大于 0。
        """
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ValidationError("lines 必须是非空数组")
        canonical_lines: list[dict] = []
        seen_ids: set[str] = set()
        total_d = total_c = 0
        for idx, raw in enumerate(raw_lines):
            if isinstance(raw, (list, tuple)):
                if len(raw) != 3:
                    raise ValidationError(f"第 {idx} 行：列表行必须是 [account, debit, credit] 三元组")
                account, debit, credit = raw
                line_id = str(idx)
            elif isinstance(raw, dict):
                if "account" not in raw or "debit" not in raw or "credit" not in raw:
                    raise ValidationError(f"第 {idx} 行：字典行必须含 account/debit/credit")
                account = raw["account"]
                debit, credit = raw["debit"], raw["credit"]
                line_id = str(raw.get("line_id", idx))
            else:
                raise ValidationError(f"第 {idx} 行：行形态非法")

            # bool 是 int 的子类，按题意显式拒绝
            if isinstance(debit, bool) or isinstance(credit, bool) or not isinstance(debit, int) \
                    or not isinstance(credit, int) or debit < 0 or credit < 0:
                raise ValidationError(f"第 {idx} 行：金额必须是非 bool 非负整数")
            if not ((debit > 0) ^ (credit > 0)):
                raise ValidationError(f"第 {idx} 行：每行必须恰一侧大于 0、另一侧为 0")
            if account not in ACCOUNT_SET:
                raise ValidationError(f"第 {idx} 行：未知账户 {account!r}")
            if line_id in seen_ids:
                raise ValidationError(f"重复行身份 {line_id!r}")
            seen_ids.add(line_id)

            canonical_lines.append(
                {"line_id": line_id, "account": account, "debit": debit, "credit": credit}
            )
            total_d += debit
            total_c += credit

        if total_d != total_c or total_d == 0:
            raise ValidationError(f"总借 {total_d} 必须等于总贷 {total_c} 且金额为正")
        return canonical_lines

    @staticmethod
    def _normalize_document(document: Any) -> Optional[dict]:
        """校验文档外壳并归一化；普通凭证 document 为 None。"""
        if document is None:
            return None
        if not isinstance(document, dict):
            raise ValidationError("document 必须是对象或 null")
        doc_type = document.get("type")
        if doc_type is None and "side" in document and "invoice_id" in document:
            # 兼容随题材料：内部销货/采购文档省略 type，由 side 识别
            doc_type = "internal_sale"
        if doc_type not in ("internal_sale", "internal_payment"):
            raise ValidationError(f"未知文档类型 {doc_type!r}")
        known = _SALE_FIELDS if doc_type == "internal_sale" else _PAYMENT_FIELDS
        unknown = set(document) - known
        if unknown:
            raise ValidationError(f"文档含未知字段 {sorted(unknown)}")
        normalized = dict(document)
        normalized["type"] = doc_type
        return normalized

    # -------------------------------------------------------- document checks
    def _check_sale_document(self, entity: str, doc: dict, lines: list[dict]) -> None:
        """内部销货/采购文档的本实体局部校验（不查对侧账簿）。"""
        for field in ("side", "invoice_id", "seller", "buyer", "sku", "q", "T", "C"):
            if field not in doc:
                raise ValidationError(f"内部销货文档缺字段 {field}")
        side = doc["side"]
        if side not in ("seller", "buyer"):
            raise ValidationError("内部销货文档 side 必须是 seller/buyer")
        for field in ("q", "T", "C"):
            val = doc[field]
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                raise ValidationError(f"内部销货文档 {field} 必须是非 bool 正整数")
        seller, buyer = doc["seller"], doc["buyer"]
        if seller not in ENTITY_SET or buyer not in ENTITY_SET or seller == buyer:
            raise ValidationError("seller/buyer 必须是 P/S 两个不同实体")
        if not isinstance(doc["invoice_id"], str) or not doc["invoice_id"]:
            raise ValidationError("invoice_id 必须是非空字符串")

        q, t, c = doc["q"], doc["T"], doc["C"]
        if side == "seller":
            if entity != seller:
                raise ValidationError("卖方分录只能由 seller 实体提交")
            expected = [
                ("receivable", t, 0),
                ("revenue", 0, t),
                ("cost", c, 0),
                ("inventory", 0, c),
            ]
        else:
            if entity != buyer:
                raise ValidationError("买方分录只能由 buyer 实体提交")
            expected = [
                ("inventory", t, 0),
                ("payable", 0, t),
            ]
        actual = [(ln["account"], ln["debit"], ln["credit"]) for ln in lines]
        if actual != expected:
            raise ValidationError(
                "内部销货/采购分录与文档不匹配",
                {"expected": expected, "actual": actual, "side": side},
            )

        # 同一实体一张内部发票只可出现一次
        row = self.conn.execute(
            "SELECT 1 FROM internal_invoices WHERE entity = ? AND invoice_id = ?",
            (entity, doc["invoice_id"]),
        ).fetchone()
        if row is not None:
            raise ConflictError(
                f"实体 {entity} 已存在内部发票 {doc['invoice_id']}，不可重复提交",
                {"entity": entity, "invoice_id": doc["invoice_id"]},
            )

    def _check_payment_document(
        self, entity: str, doc: dict, lines: list[dict]
    ) -> int:
        """内部收付款文档校验，返回本笔金额。累计不超该侧原始 T。"""
        invoice_id = doc.get("invoice_id")
        side = doc.get("side")
        if not invoice_id or side not in ("receipt", "settlement"):
            raise ValidationError("内部收付款文档需要 invoice_id 与 side(receipt/settlement)")
        inv = self.conn.execute(
            "SELECT * FROM internal_invoices WHERE entity = ? AND invoice_id = ?",
            (entity, invoice_id),
        ).fetchone()
        if inv is None:
            # 源端只保留本实体文档：未见过本侧发票就无法核定 T，拒绝但不影响对侧
            raise ValidationError(
                f"实体 {entity} 尚未提交发票 {invoice_id} 的本侧文档，不能登记内部收付款",
                {"entity": entity, "invoice_id": invoice_id},
            )
        if side == "receipt" and inv["side"] != "seller":
            raise ValidationError("receipt 只能由该发票的卖方实体登记")
        if side == "settlement" and inv["side"] != "buyer":
            raise ValidationError("settlement 只能由该发票的买方实体登记")

        if side == "receipt":
            expected_accounts = ["cash", "receivable"]
        else:
            expected_accounts = ["payable", "cash"]
        if len(lines) != 2 or [ln["account"] for ln in lines] != expected_accounts:
            raise ValidationError(
                "内部收付款分录必须恰为 借cash/贷receivable(receipt) 或 借payable/贷cash(settlement)"
            )
        self._strict_payment_lines(side, lines)
        # 两行严格同额，取借方行金额即可
        amount = lines[0]["debit"]

        paid = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM internal_payments "
            "WHERE entity = ? AND invoice_id = ?",
            (entity, invoice_id),
        ).fetchone()["s"]
        if paid + amount > inv["T"]:
            raise ValidationError(
                f"内部收付款累计 {paid + amount} 超过发票原始 T={inv['T']}",
                {"invoice_id": invoice_id, "cap": inv["T"], "already": paid, "this": amount},
            )
        return amount

    @staticmethod
    def _strict_payment_lines(side: str, lines: list[dict]) -> None:
        if side == "receipt":
            ok = (
                len(lines) == 2
                and lines[0]["account"] == "cash"
                and lines[0]["credit"] == 0
                and lines[1]["account"] == "receivable"
                and lines[1]["debit"] == 0
                and lines[0]["debit"] == lines[1]["credit"]
                and lines[0]["debit"] > 0
            )
        else:
            ok = (
                len(lines) == 2
                and lines[0]["account"] == "payable"
                and lines[0]["credit"] == 0
                and lines[1]["account"] == "cash"
                and lines[1]["debit"] == 0
                and lines[0]["debit"] == lines[1]["credit"]
                and lines[0]["debit"] > 0
            )
        if not ok:
            raise ValidationError("内部收付款分录必须恰为 借cash/贷receivable 或 借payable/贷cash")

    # ----------------------------------------------------------------- posting
    def post(
        self,
        journal_id: str,
        entity: str,
        lines: list,
        document: Optional[dict] = None,
    ) -> dict:
        """原子提交一张凭证，返回 {"status": committed/replayed, "entity", "journal_id", "seq"}。

        同 (entity, journal_id) 同规范内容重投 -> 复读原 seq；异文 -> ConflictError。
        任何校验失败 -> 整笔回滚，seq 不跳号、无残留行。
        """
        if not isinstance(journal_id, str) or not journal_id:
            raise ValidationError("journal_id 必须是非空字符串")
        if entity not in ENTITY_SET:
            raise ValidationError(f"未知实体 {entity!r}，只支持 P/S")

        # 纯函数式解析，尚未写库
        canonical_lines = self._parse_lines(lines)
        normalized_doc = self._normalize_document(document)
        canonical = dumps(
            {
                "journal_id": journal_id,
                "entity": entity,
                "lines": canonical_lines,
                "document": normalized_doc,
            }
        )

        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = cur.execute(
                "SELECT seq, canonical FROM journals WHERE entity = ? AND journal_id = ?",
                (entity, journal_id),
            ).fetchone()
            if existing is not None:
                if existing["canonical"] == canonical:
                    self.conn.execute("COMMIT")
                    return {
                        "status": "replayed",
                        "entity": entity,
                        "journal_id": journal_id,
                        "seq": existing["seq"],
                    }
                raise ConflictError(
                    f"实体 {entity} 的凭证号 {journal_id} 已用于不同内容，异文拒绝",
                    {"entity": entity, "journal_id": journal_id},
                )

            # 文档局部校验（内部发票唯一性、收付款累计限额等均在事务内读取最新状态）
            payment_amount = None
            if normalized_doc is not None:
                if normalized_doc["type"] == "internal_sale":
                    self._check_sale_document(entity, normalized_doc, canonical_lines)
                else:
                    payment_amount = self._check_payment_document(
                        entity, normalized_doc, canonical_lines
                    )

            row = cur.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM journals WHERE entity = ?",
                (entity,),
            ).fetchone()
            seq = row["m"] + 1

            cur.execute(
                "INSERT INTO journals (entity, journal_id, seq, canonical, doc_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entity,
                    journal_id,
                    seq,
                    canonical,
                    normalized_doc["type"] if normalized_doc else None,
                ),
            )
            for line_no, ln in enumerate(canonical_lines):
                cur.execute(
                    "INSERT INTO journal_lines (entity, seq, line_no, line_id, account, debit, credit) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity,
                        seq,
                        line_no,
                        ln["line_id"],
                        ln["account"],
                        ln["debit"],
                        ln["credit"],
                    ),
                )

            if normalized_doc is not None and normalized_doc["type"] == "internal_sale":
                cur.execute(
                    "INSERT INTO internal_invoices (entity, invoice_id, side, seller, buyer, sku, "
                    "q, T, C, journal_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity,
                        normalized_doc["invoice_id"],
                        normalized_doc["side"],
                        normalized_doc["seller"],
                        normalized_doc["buyer"],
                        normalized_doc["sku"],
                        normalized_doc["q"],
                        normalized_doc["T"],
                        normalized_doc["C"],
                        seq,
                    ),
                )
            elif normalized_doc is not None and normalized_doc["type"] == "internal_payment":
                cur.execute(
                    "INSERT INTO internal_payments (entity, invoice_id, side, amount, journal_seq, journal_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entity,
                        normalized_doc["invoice_id"],
                        normalized_doc["side"],
                        payment_amount,
                        seq,
                        journal_id,
                    ),
                )

            self.conn.execute("COMMIT")
            return {
                "status": "committed",
                "entity": entity,
                "journal_id": journal_id,
                "seq": seq,
            }
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------- stock input
    def register_stock(self, version: str, items: dict) -> dict:
        """登记一份期末存货声明（稳定版本、不可变）。

        items: {invoice_id: r}。即使 r=0 也必须显式列出。此处只做 r>=0 的形态校验，
        r<=q 与发票覆盖完整性在发布底稿时按切面核对并给出待核明细。
        """
        if not isinstance(version, str) or not version:
            raise ValidationError("存货声明版本号必须是非空字符串")
        if not isinstance(items, dict) or not items:
            raise ValidationError("存货声明必须是非空映射 {invoice_id: r}")
        norm: dict[str, int] = {}
        for invoice_id, r in items.items():
            if not isinstance(invoice_id, str) or not invoice_id:
                raise ValidationError("存货声明的 invoice_id 必须是非空字符串")
            if isinstance(r, bool) or not isinstance(r, int) or r < 0:
                raise ValidationError(f"存货声明 {invoice_id} 的 r 必须是非 bool 非负整数")
            norm[invoice_id] = r
        canonical = dumps({"version": version, "items": norm})

        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = cur.execute(
                "SELECT canonical FROM stock_versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                if existing["canonical"] == canonical:
                    self.conn.execute("COMMIT")
                    return {"status": "replayed", "version": version}
                raise ConflictError(f"存货声明版本 {version} 已用于不同内容")
            cur.execute(
                "INSERT INTO stock_versions (version, canonical) VALUES (?, ?)",
                (version, canonical),
            )
            cur.executemany(
                "INSERT INTO stock_items (version, invoice_id, r) VALUES (?, ?, ?)",
                [(version, iid, r) for iid, r in sorted(norm.items())],
            )
            self.conn.execute("COMMIT")
            return {"status": "committed", "version": version}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------------- queries
    def head(self, entity: str) -> int:
        """实体当前连续提交序号（无提交时为 0）。"""
        if entity not in ENTITY_SET:
            raise ValidationError(f"未知实体 {entity!r}")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM journals WHERE entity = ?", (entity,)
        ).fetchone()
        return row["m"]

    def heads(self) -> dict:
        return {e: self.head(e) for e in ("P", "S")}

    def _check_prefix(self, entity: str, prefix: Optional[int]) -> int:
        current = self.head(entity)
        if prefix is None:
            return current
        if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 0:
            raise ValidationError("前缀必须是非负整数")
        if prefix > current:
            raise ValidationError(
                f"请求前缀 {prefix} 超过实体 {entity} 已提交序号 {current}",
                {"entity": entity, "requested": prefix, "head": current},
            )
        return prefix

    def gl(self, entity: str, prefix: Optional[int] = None) -> dict:
        """任意已提交前缀的总账：七个账户的借减贷有符号整数余额。"""
        prefix = self._check_prefix(entity, prefix)
        balances = zero_balances()
        rows = self.conn.execute(
            "SELECT account, debit, credit FROM journal_lines "
            "WHERE entity = ? AND seq <= ? ORDER BY seq, line_no",
            (entity, prefix),
        ).fetchall()
        for row in rows:
            apply_line(balances, row["account"], row["debit"], row["credit"])
        return {"entity": entity, "prefix": prefix, "balances": balances}

    def journals(self, entity: str, prefix: Optional[int] = None) -> list[dict]:
        """返回前缀内的全部原始凭证（含分录与文档），按 seq 排序。"""
        prefix = self._check_prefix(entity, prefix)
        out: list[dict] = []
        jrows = self.conn.execute(
            "SELECT * FROM journals WHERE entity = ? AND seq <= ? ORDER BY seq",
            (entity, prefix),
        ).fetchall()
        for jr in jrows:
            lines = [
                {
                    "line_no": r["line_no"],
                    "line_id": r["line_id"],
                    "account": r["account"],
                    "debit": r["debit"],
                    "credit": r["credit"],
                }
                for r in self.conn.execute(
                    "SELECT * FROM journal_lines WHERE entity = ? AND seq = ? ORDER BY line_no",
                    (entity, jr["seq"]),
                ).fetchall()
            ]
            out.append(
                {
                    "entity": entity,
                    "journal_id": jr["journal_id"],
                    "seq": jr["seq"],
                    "doc_type": jr["doc_type"],
                    "document": loads(jr["canonical"])["document"],
                    "lines": lines,
                }
            )
        return out

    def voucher(self, entity: str, seq: int) -> dict:
        """按提交序号取原始凭证；也接受 journal_id 字符串。"""
        if isinstance(seq, str):
            row = self.conn.execute(
                "SELECT seq FROM journals WHERE entity = ? AND journal_id = ?", (entity, seq)
            ).fetchone()
            if row is None:
                raise ValidationError(f"实体 {entity} 不存在凭证 {seq}")
            seq = row["seq"]
        prefix = self._check_prefix(entity, seq)
        journals = self.journals(entity, prefix)
        for j in journals:
            if j["seq"] == seq:
                return j
        raise ValidationError(f"实体 {entity} 序号 {seq} 不存在")

    # ------------------------------------------------- snapshot index for merge
    def internal_invoices(self, entity: str, prefix: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM internal_invoices WHERE entity = ? AND journal_seq <= ? ORDER BY invoice_id",
            (entity, prefix),
        ).fetchall()
        return [dict(r) for r in rows]

    def internal_payments(self, entity: str, prefix: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM internal_payments WHERE entity = ? AND journal_seq <= ? "
            "ORDER BY invoice_id, journal_seq",
            (entity, prefix),
        ).fetchall()
        return [dict(r) for r in rows]

    def stock(self, version: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT canonical FROM stock_versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            return None
        parsed = loads(row["canonical"])
        return {"version": version, "items": parsed["items"]}
