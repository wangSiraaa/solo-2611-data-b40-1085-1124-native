"""可追溯账簿与可分余额（合成口径，金额 Fraction，绝不取整）。

账簿侧：
  * 凭证只追加；稳定 id 复读、异文拒绝；可用 `supersedes` 以新版替换旧版，
    旧版保留可查但**不再计入余额**（因此旧版冲回/旧版费用不会被算两遍）。
  * 记账统一 `借减贷` 有符号余额，每张凭证自身借贷平衡。
  * 对外收入/成本只取"非内部"凭证；内部销货的 revenue/cost 被剔除，剩余货物对应的
    未实现内部毛利单独列示（尚留库，不进可分余额）。

可分余额（题内约定）：
      收入(revenue 贷) - 成本(cost 借)
      - 另册尚有效亏损(原亏损 - 已有效冲回)
      - 此前已宣告未撤销的红利
    再取 max(0, ·)。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Optional

from .jsoncodec import dumps, loads, frac_to_json, json_to_frac

_ACCOUNTS = frozenset(
    ["cash", "receivable", "inventory", "payable", "capital", "revenue", "cost"]
)


class BookError(Exception):
    code = "book_error"

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _err(code: str, message: str, **details) -> BookError:
    e = BookError(message, details)
    e.code = code
    return e


def _sum_fractions(amounts) -> Fraction:
    total = Fraction(0)
    for a in amounts:
        total += json_to_frac(loads(a))
    return total


def _parse_lines(lines) -> list[dict]:
    if not isinstance(lines, list) or not lines:
        raise _err("bad_lines", "lines 必须是非空数组")
    out, seen = [], set()
    total_d = total_c = 0
    for i, raw in enumerate(lines):
        if isinstance(raw, (list, tuple)):
            if len(raw) != 3:
                raise _err("bad_lines", f"第 {i} 行必须是 [account, debit, credit]")
            account, debit, credit = raw
            line_id = str(i)
        elif isinstance(raw, dict):
            account = raw.get("account")
            debit, credit = raw.get("debit"), raw.get("credit")
            line_id = str(raw.get("line_id", i))
        else:
            raise _err("bad_lines", f"第 {i} 行形态非法")
        if isinstance(debit, bool) or isinstance(credit, bool) \
                or not isinstance(debit, int) or not isinstance(credit, int) \
                or debit < 0 or credit < 0:
            raise _err("bad_lines", f"第 {i} 行金额必须是非 bool 非负整数")
        if not ((debit > 0) ^ (credit > 0)):
            raise _err("bad_lines", f"第 {i} 行必须恰一侧为正")
        if account not in _ACCOUNTS:
            raise _err("bad_lines", f"第 {i} 行未知账户 {account!r}")
        if line_id in seen:
            raise _err("bad_lines", f"重复行身份 {line_id!r}")
        seen.add(line_id)
        out.append({"line_id": line_id, "account": account,
                    "debit": debit, "credit": credit})
        total_d += debit
        total_c += credit
    if total_d != total_c or total_d == 0:
        raise _err("bad_lines", "凭证借贷必须平衡且金额为正")
    return out


class Ledger:
    """单本只追加账簿。"""

    def __init__(self, conn: sqlite3.Connection, book_id: str, kind: str):
        self.conn = conn
        self.book_id = book_id
        self.kind = kind
        self._schema()

    def _rollback(self) -> None:
        """只在事务仍打开时回滚，避免“已 COMMIT 后再次 ROLLBACK”报错。"""
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bk_vouchers (
                book_id     TEXT NOT NULL,
                journal_id  TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                canonical   TEXT NOT NULL,
                doc_type    TEXT,
                doc_json    TEXT,
                status      TEXT NOT NULL,      -- active / superseded
                supersedes  TEXT,
                PRIMARY KEY (book_id, journal_id),
                UNIQUE (book_id, seq)
            );
            CREATE TABLE IF NOT EXISTS bk_lines (
                book_id     TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                line_no     INTEGER NOT NULL,
                account     TEXT NOT NULL,
                debit       INTEGER NOT NULL,
                credit      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bk_losses (
                book_id     TEXT NOT NULL,
                loss_id     TEXT NOT NULL,
                amount      TEXT NOT NULL,
                PRIMARY KEY (book_id, loss_id)
            );
            CREATE TABLE IF NOT EXISTS bk_loss_recoveries (
                book_id     TEXT NOT NULL,
                reversal_id TEXT NOT NULL,
                loss_id     TEXT NOT NULL,
                amount      TEXT NOT NULL,
                status      TEXT NOT NULL,      -- active / superseded
                PRIMARY KEY (book_id, reversal_id)
            );
            CREATE TABLE IF NOT EXISTS bk_declarations (
                book_id     TEXT NOT NULL,
                div_id      TEXT NOT NULL,
                amount      TEXT NOT NULL,
                lot_id      TEXT NOT NULL,
                status      TEXT NOT NULL,      -- active / revoked / superseded
                supersedes  TEXT,
                plan_id     TEXT,
                PRIMARY KEY (book_id, div_id)
            );
            CREATE TABLE IF NOT EXISTS bk_stock (
                book_id     TEXT NOT NULL,
                version     TEXT NOT NULL,
                invoice_id  TEXT NOT NULL,
                r           INTEGER NOT NULL,
                PRIMARY KEY (book_id, version, invoice_id)
            );
            """
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------- vouchers
    def post(self, journal_id: str, lines: list, document: Optional[dict] = None,
             supersedes: Optional[str] = None) -> dict:
        canonical_lines = _parse_lines(lines)
        if document is not None and not isinstance(document, dict):
            raise _err("bad_document", "document 必须是对象或 null")
        doc_type = None
        if document is not None:
            doc_type = document.get("type")
            if doc_type is not None and not isinstance(doc_type, str):
                raise _err("bad_document", "document type 必须是字符串")
        canonical = dumps({"journal_id": journal_id, "lines": canonical_lines,
                           "document": document, "supersedes": supersedes})
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT canonical, status FROM bk_vouchers WHERE book_id=? AND journal_id=?",
                (self.book_id, journal_id),
            ).fetchone()
            if existing is not None:
                if existing["canonical"] != canonical:
                    raise _err("conflict", f"凭证号 {journal_id} 已用于不同内容")
                self.conn.execute("COMMIT")
                return {"status": "replayed", "journal_id": journal_id,
                        "book": self.book_id}
            if supersedes is not None:
                old = self.conn.execute(
                    "SELECT status FROM bk_vouchers WHERE book_id=? AND journal_id=?",
                    (self.book_id, supersedes),
                ).fetchone()
                if old is None:
                    raise _err("unknown_supersede", f"要替换的凭证 {supersedes} 不存在")
                if old["status"] != "active":
                    raise _err("double_supersede",
                               f"凭证 {supersedes} 已被替换，不能再次替换历史版本")
                self.conn.execute(
                    "UPDATE bk_vouchers SET status='superseded' WHERE book_id=? AND journal_id=?",
                    (self.book_id, supersedes),
                )
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM bk_vouchers WHERE book_id=?",
                (self.book_id,),
            ).fetchone()["m"] + 1
            self.conn.execute(
                "INSERT INTO bk_vouchers (book_id, journal_id, seq, canonical, doc_type, "
                "doc_json, status, supersedes) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (self.book_id, journal_id, seq, canonical, doc_type,
                 dumps(document) if document is not None else None, supersedes),
            )
            for line_no, ln in enumerate(canonical_lines):
                self.conn.execute(
                    "INSERT INTO bk_lines (book_id, seq, line_no, account, debit, credit) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.book_id, seq, line_no,
                     ln["account"], ln["debit"], ln["credit"]),
                )
            self.conn.execute("COMMIT")
            return {"status": "committed", "journal_id": journal_id, "seq": seq,
                    "book": self.book_id}
        except BookError:
            self._rollback()
            raise
        except Exception:
            self._rollback()
            raise

    def gl(self, prefix: Optional[int] = None) -> dict:
        """active 凭证的前缀余额（借减贷）。被替换旧版不计入。"""
        if prefix is None:
            prefix = self.head()
        bal = {a: 0 for a in _ACCOUNTS}
        rows = self.conn.execute(
            "SELECT account, debit, credit FROM bk_lines l "
            "JOIN bk_vouchers v ON l.book_id=v.book_id AND l.seq=v.seq "
            "WHERE l.book_id=? AND l.seq<=? AND v.status='active'",
            (self.book_id, prefix),
        ).fetchall()
        for account, debit, credit in rows:
            bal[account] += debit - credit
        if sum(bal.values()) != 0:
            raise _err("unbalanced", "账簿总借贷不平", balances=bal)
        return {"book": self.book_id, "prefix": prefix, "balances": bal}

    def head(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) AS m FROM bk_vouchers WHERE book_id=?",
            (self.book_id,),
        ).fetchone()
        return row["m"]

    def vouchers(self, prefix: Optional[int] = None) -> list[dict]:
        if prefix is None:
            prefix = self.head()
        out = []
        for v in self.conn.execute(
            "SELECT * FROM bk_vouchers WHERE book_id=? AND seq<=? ORDER BY seq",
            (self.book_id, prefix),
        ).fetchall():
            lines = [
                {"line_no": r["line_no"], "account": r["account"],
                 "debit": r["debit"], "credit": r["credit"]}
                for r in self.conn.execute(
                    "SELECT * FROM bk_lines WHERE book_id=? AND seq=? ORDER BY line_no",
                    (self.book_id, v["seq"]),
                ).fetchall()
            ]
            out.append({
                "journal_id": v["journal_id"], "seq": v["seq"],
                "status": v["status"], "supersedes": v["supersedes"],
                "document": loads(v["doc_json"]) if v["doc_json"] else None,
                "lines": lines,
            })
        return out

    def voucher(self, journal_id: str) -> dict:
        row = self.conn.execute(
            "SELECT seq FROM bk_vouchers WHERE book_id=? AND journal_id=?",
            (self.book_id, journal_id),
        ).fetchone()
        if row is None:
            raise _err("unknown_voucher", f"无凭证 {journal_id}")
        for v in self.vouchers(row["seq"]):
            if v["seq"] == row["seq"]:
                return v
        raise _err("unknown_voucher", f"无凭证 {journal_id}")

    # ------------------------------------------------------------ losses
    def register_loss(self, loss_id: str, amount: Fraction) -> dict:
        if amount < 0:
            raise _err("bad_loss", "亏损金额不能为负")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT amount FROM bk_losses WHERE book_id=? AND loss_id=?",
                (self.book_id, loss_id),
            ).fetchone()
            if row is not None:
                if json_to_frac(loads(row["amount"])) != amount:
                    raise _err("conflict", f"亏损 {loss_id} 已用于不同金额")
                self.conn.execute("COMMIT")
                return {"status": "replayed", "loss_id": loss_id}
            self.conn.execute(
                "INSERT INTO bk_losses (book_id, loss_id, amount) VALUES (?, ?, ?)",
                (self.book_id, loss_id, dumps(frac_to_json(amount))),
            )
            self.conn.execute("COMMIT")
            return {"status": "committed", "loss_id": loss_id,
                    "amount": frac_to_json(amount)}
        except BookError:
            self._rollback()
            raise
        except Exception:
            self._rollback()
            raise

    def recover_loss(self, reversal_id: str, loss_id: str, amount: Fraction,
                     supersedes: Optional[str] = None) -> dict:
        """正式冲回一部分原亏损；旧版冲回被替换后不得再次计入。"""
        if amount <= 0:
            raise _err("bad_reversal", "冲回金额必须为正")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT amount, status, loss_id FROM bk_loss_recoveries "
                "WHERE book_id=? AND reversal_id=?",
                (self.book_id, reversal_id),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "active" and \
                        json_to_frac(loads(existing["amount"])) == amount and \
                        existing["loss_id"] == loss_id:
                    self.conn.execute("COMMIT")
                    return {"status": "replayed", "reversal_id": reversal_id}
                if existing["status"] == "superseded":
                    self.conn.execute("COMMIT")
                    raise _err("stale_reversal",
                               "旧版冲回已被替换，不能把旧版再冲一遍",
                               reversal_id=reversal_id)
                raise _err("conflict", f"冲回号 {reversal_id} 已用于不同内容")
            loss = self.conn.execute(
                "SELECT amount FROM bk_losses WHERE book_id=? AND loss_id=?",
                (self.book_id, loss_id),
            ).fetchone()
            if loss is None:
                raise _err("unknown_loss", f"无亏损 {loss_id}")
            loss_amt = json_to_frac(loads(loss["amount"]))
            if supersedes is not None:
                old = self.conn.execute(
                    "SELECT status FROM bk_loss_recoveries WHERE book_id=? AND reversal_id=?",
                    (self.book_id, supersedes),
                ).fetchone()
                if old is None:
                    raise _err("unknown_supersede", f"要替换的冲回 {supersedes} 不存在")
                if old["status"] != "active":
                    raise _err("double_supersede",
                               f"冲回 {supersedes} 已失效，不能再次替换")
                active_old = json_to_frac(loads(self.conn.execute(
                    "SELECT amount FROM bk_loss_recoveries WHERE book_id=? AND reversal_id=?",
                    (self.book_id, supersedes)).fetchone()["amount"]))
                active_total = self._active_recovery_sum(loss_id) - active_old
                if active_total + amount > loss_amt:
                    raise _err("over_recovery",
                               "冲回累计超过原亏损", cap=frac_to_json(loss_amt),
                               active=frac_to_json(active_total),
                               this=frac_to_json(amount))
                self.conn.execute(
                    "UPDATE bk_loss_recoveries SET status='superseded' "
                    "WHERE book_id=? AND reversal_id=?",
                    (self.book_id, supersedes),
                )
            else:
                active_total = self._active_recovery_sum(loss_id)
                if active_total + amount > loss_amt:
                    raise _err("over_recovery",
                               "冲回累计超过原亏损", cap=frac_to_json(loss_amt),
                               active=frac_to_json(active_total),
                               this=frac_to_json(amount))
            self.conn.execute(
                "INSERT INTO bk_loss_recoveries (book_id, reversal_id, loss_id, amount, "
                "status) VALUES (?, ?, ?, ?, 'active')",
                (self.book_id, reversal_id, loss_id, dumps(frac_to_json(amount))),
            )
            self.conn.execute("COMMIT")
            return {"status": "committed", "reversal_id": reversal_id,
                    "loss_id": loss_id, "amount": frac_to_json(amount)}
        except BookError:
            self._rollback()
            raise
        except Exception:
            self._rollback()
            raise

    def _active_recovery_sum(self, loss_id: str) -> Fraction:
        rows = self.conn.execute(
            "SELECT amount FROM bk_loss_recoveries WHERE book_id=? AND loss_id=? "
            "AND status='active'",
            (self.book_id, loss_id),
        ).fetchall()
        return _sum_fractions([r["amount"] for r in rows])

    def effective_loss(self) -> Fraction:
        losses = self.conn.execute(
            "SELECT amount FROM bk_losses WHERE book_id=?", (self.book_id,)
        ).fetchall()
        total = _sum_fractions([r["amount"] for r in losses])
        recs = self.conn.execute(
            "SELECT amount FROM bk_loss_recoveries WHERE book_id=? AND status='active'",
            (self.book_id,),
        ).fetchall()
        recovered = _sum_fractions([r["amount"] for r in recs])
        return total - recovered

    # -------------------------------------------------------- declarations
    def declare(self, div_id: str, amount: Fraction, lot_id: str,
                plan_id: Optional[str] = None,
                supersedes: Optional[str] = None) -> dict:
        if amount < 0:
            raise _err("bad_declaration", "宣告金额不能为负")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT amount, status FROM bk_declarations WHERE book_id=? AND div_id=?",
                (self.book_id, div_id),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "active" and \
                        json_to_frac(loads(existing["amount"])) == amount:
                    self.conn.execute("COMMIT")
                    return {"status": "replayed", "div_id": div_id}
                raise _err("conflict", f"宣告号 {div_id} 已用于不同内容/状态")
            if supersedes is not None:
                old = self.conn.execute(
                    "SELECT status FROM bk_declarations WHERE book_id=? AND div_id=?",
                    (self.book_id, supersedes),
                ).fetchone()
                if old is None:
                    raise _err("unknown_supersede", f"要替换的宣告 {supersedes} 不存在")
                if old["status"] != "active":
                    raise _err("double_supersede", f"宣告 {supersedes} 已失效")
                self.conn.execute(
                    "UPDATE bk_declarations SET status='superseded' "
                    "WHERE book_id=? AND div_id=?",
                    (self.book_id, supersedes),
                )
            self.conn.execute(
                "INSERT INTO bk_declarations (book_id, div_id, amount, lot_id, status, "
                "supersedes, plan_id) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (self.book_id, div_id, dumps(frac_to_json(amount)), lot_id,
                 supersedes, plan_id),
            )
            self.conn.execute("COMMIT")
            return {"status": "committed", "div_id": div_id,
                    "amount": frac_to_json(amount)}
        except BookError:
            self._rollback()
            raise
        except Exception:
            self._rollback()
            raise

    def revoke(self, div_id: str) -> dict:
        row = self.conn.execute(
            "SELECT status FROM bk_declarations WHERE book_id=? AND div_id=?",
            (self.book_id, div_id),
        ).fetchone()
        if row is None:
            raise _err("unknown_declaration", f"无宣告 {div_id}")
        if row["status"] != "active":
            return {"div_id": div_id, "status": "already_inactive"}
        self.conn.execute(
            "UPDATE bk_declarations SET status='revoked' WHERE book_id=? AND div_id=?",
            (self.book_id, div_id),
        )
        return {"div_id": div_id, "status": "revoked"}

    def net_declared(self) -> Fraction:
        rows = self.conn.execute(
            "SELECT amount FROM bk_declarations WHERE book_id=? AND status='active'",
            (self.book_id,),
        ).fetchall()
        return _sum_fractions([r["amount"] for r in rows])

    # --------------------------------------------------------------- stock
    def declare_stock(self, version: str, items: dict) -> dict:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT COUNT(*) AS n FROM bk_stock WHERE book_id=? AND version=?",
                (self.book_id, version),
            ).fetchone()["n"]
            if existing:
                raise _err("conflict", f"存货声明 {version} 已存在")
            for iid, r in items.items():
                if isinstance(r, bool) or not isinstance(r, int) or r < 0:
                    raise _err("bad_stock", f"{iid} 的 r 必须是非负整数")
                self.conn.execute(
                    "INSERT INTO bk_stock (book_id, version, invoice_id, r) VALUES (?, ?, ?, ?)",
                    (self.book_id, version, iid, r),
                )
            self.conn.execute("COMMIT")
            return {"status": "committed", "version": version}
        except BookError:
            self._rollback()
            raise
        except Exception:
            self._rollback()
            raise

    def stock(self, version: str) -> dict:
        rows = self.conn.execute(
            "SELECT invoice_id, r FROM bk_stock WHERE book_id=? AND version=?",
            (self.book_id, version),
        ).fetchall()
        return {r["invoice_id"]: r["r"] for r in rows}


class DividendHouse:
    """两本账：main（集团当期收入/费用）+ memo（另册亏损），共享一个 SQLite。"""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.main = Ledger(self.conn, "main", "main")
        self.memo = Ledger(self.conn, "memo", "memo")

    def close(self):
        self.conn.close()

    def distributable_from_worksheet(self, statement: dict) -> dict:
        """直接读已成功的集团合并底稿，算可分余额（题面要求"在集团底稿上"）。

        底稿最终余额里：revenue 为贷方（负）、cost 为借方（正）。
        当期对外经营成果 = -(revenue_signed) - cost_signed
                          = 对外收入 − 对外成本 + 内部已实现毛利
        （底稿已把内部销货 revenue/cost 抵销、并按 (T−C)×r/q 调掉未实现毛利）。
        再减另册有效亏损与此前已宣告未撤销，最低取零。
        """
        final = statement.get("final", {})
        revenue_signed = json_to_frac(final["revenue"])
        cost_signed = json_to_frac(final["cost"])
        income = -revenue_signed - cost_signed
        eff_loss = self.memo.effective_loss()
        declared = self.main.net_declared()
        raw = income - eff_loss - declared
        return {
            "source": "consolidation_worksheet",
            "input_id": statement.get("input_id"),
            "loss_from_revenue": revenue_signed,
            "expense_from_cost": cost_signed,
            "traceable_income": income,
            "memo_loss": eff_loss,
            "net_declared": declared,
            "raw_before_floor": raw,
            "distributable": max(Fraction(0), raw),
        }

    # ------------------------------------------------- traceable income
    def traceable_income(self, prefix: Optional[int] = None,
                         stock_version: Optional[str] = None) -> dict:
        """按题内合成口径算集团当期收入：对外收入 - 对外成本 + 内部已实现毛利。

        内部销货的 revenue/cost 不入对外收入/成本；内部毛利中尚留库的部分
        （= (T-C)*r/q，r 来自存货声明）不算已实现，因此不进当期收入。
        等价于合并底稿：收入(内部抵销后) - 成本(内部抵销 + 未实现调整后)。
        """
        vouchers = self.main.vouchers(prefix)
        revenue = Fraction(0)  # 对外收入
        cost = Fraction(0)     # 对外成本
        gross_internal = Fraction(0)  # Σ(T-C)
        internal_rows = []
        for v in vouchers:
            if v["status"] != "active":
                continue
            doc = v["document"] or {}
            dtype = doc.get("type")
            if dtype == "internal_sale":
                if doc.get("side") == "seller":
                    t, c = doc["T"], doc["C"]
                    gross_internal += Fraction(t - c)
                    internal_rows.append({"journal_id": v["journal_id"],
                                          "invoice_id": doc.get("invoice_id"),
                                          "q": doc.get("q"), "T": t, "C": c,
                                          "gross_profit": Fraction(t - c)})
                continue
            for ln in v["lines"]:
                if ln["account"] == "revenue":
                    revenue += ln["credit"] - ln["debit"]
                elif ln["account"] == "cost":
                    cost += ln["debit"] - ln["credit"]
        urp = Fraction(0)
        urp_items = []
        if stock_version is not None:
            stock = self.main.stock(stock_version)
            for row in internal_rows:
                iid = row["invoice_id"]
                r = stock.get(iid, 0)
                amt = Fraction(row["T"] - row["C"], row["q"]) * r
                urp += amt
                urp_items.append({"invoice_id": iid, "q": row["q"], "T": row["T"],
                                  "C": row["C"], "r": r, "unrealized_profit": amt})
        realized_internal = gross_internal - urp
        income = revenue - cost + realized_internal
        return {
            "external_revenue": revenue,
            "external_cost": cost,
            "internal_gross_profit": gross_internal,
            "internal_realized_profit": realized_internal,
            "internal_unrealized_profit": urp,
            "internal_rows": internal_rows,
            "unrealized_items": urp_items,
            "income": income,
        }

    def undistributed_inventory_profit(self, version: str) -> dict:
        """内部毛利中尚留库（未实现）的部分 = (T-C)*r/q，逐发票列示。"""
        inc = self.traceable_income(stock_version=version)
        return {"total": inc["internal_unrealized_profit"],
                "items": inc["unrealized_items"]}

    def distributable(self, stock_version: Optional[str] = None) -> dict:
        inc = self.traceable_income(stock_version=stock_version)
        eff_loss = self.memo.effective_loss()
        declared = self.main.net_declared()
        raw = inc["income"] - eff_loss - declared
        ceiling = max(Fraction(0), raw)
        inventory = (self.undistributed_inventory_profit(stock_version)
                     if stock_version else None)
        return {
            "book": "main",
            "traceable_revenue": inc["external_revenue"],
            "traceable_cost": inc["external_cost"],
            "internal_gross_profit": inc["internal_gross_profit"],
            "internal_realized_profit": inc["internal_realized_profit"],
            "traceable_income": inc["income"],
            "internal_sale_profit_total": inc["internal_gross_profit"],
            "memo_loss": eff_loss,
            "net_declared": declared,
            "raw_before_floor": raw,
            "distributable": ceiling,
            "inventory_kept_profit": inventory,
        }
