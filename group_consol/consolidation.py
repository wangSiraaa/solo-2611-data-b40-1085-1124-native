"""集团合并服务：冻结切面、错时待核、逐发票抵销、不可变工作底稿。

核心保证：
- 成功底稿以 {heads(P/S 各自已提交前缀), stock_version} 为不可变输入切面，同 input_id
  同切面重试复读，异文拒绝；新单体提交、新声明都不影响旧底稿。
- 发布前逐发票核对两侧镜像（身份/主体/q/T/C）与累计收付；缺侧、异文、不一致均产出
  结构化待核明细并抛 PendingError（同时落盘，便于同切面复读待核结论），绝不按金额凑平、
  静默丢文档或改写单体账。
- 抵销三类分开列示：内部往来（应收/应付）、内部收入/成本、未实现存货利润；货款结清只
  影响第一类，存货利润独立计算。T<C 时未实现利润为负，反向表达，不截零。
- 金额用 fractions.Fraction 约分有理数；单体账整数余额原样进入合计，绝不为取整改账。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Any, Optional

from .accounts import ACCOUNTS, zero_balances
from .errors import PendingError, SnapshotConflictError, ValidationError
from .jsoncodec import dumps, loads, frac_to_json, json_to_frac
from .ledger import EntityLedger


class ConsolidationService:
    def __init__(self, ledger: EntityLedger):
        self.ledger = ledger
        self.conn = ledger.conn
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS worksheets (
                input_id      TEXT PRIMARY KEY,
                status        TEXT NOT NULL,          -- published / pending
                heads_json    TEXT NOT NULL,
                stock_version TEXT NOT NULL,
                snapshot_key  TEXT NOT NULL,          -- 切面规范文本
                statement_json TEXT
            );
            CREATE TABLE IF NOT EXISTS ws_pending (
                input_id   TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ws_entries (
                input_id   TEXT NOT NULL,
                entry_id   TEXT NOT NULL,
                kind       TEXT NOT NULL,
                invoice_id TEXT,
                ord        INTEGER NOT NULL,
                PRIMARY KEY (input_id, entry_id)
            );
            """
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # ============================================================ publish
    def publish(self, input_id: str, heads: dict, stock_version: str) -> dict:
        if not isinstance(input_id, str) or not input_id:
            raise ValidationError("input_id 必须是非空字符串")
        if not isinstance(heads, dict) or set(heads) != {"P", "S"}:
            raise ValidationError("heads 必须恰好包含 P/S 两个前缀")
        for entity in ("P", "S"):
            self.ledger._check_prefix(entity, heads[entity])
        stock = self.ledger.stock(stock_version)
        if stock is None:
            raise ValidationError(f"存货声明版本 {stock_version} 不存在")

        snapshot_key = dumps({"heads": heads, "stock_version": stock_version})

        cur = self.conn.cursor()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = cur.execute(
                "SELECT status, snapshot_key FROM worksheets WHERE input_id = ?", (input_id,)
            ).fetchone()
            if existing is not None:
                if existing["snapshot_key"] != snapshot_key:
                    raise SnapshotConflictError(
                        f"input_id {input_id} 已绑定不同切面，异文拒绝",
                        {"input_id": input_id},
                    )
                # 同切面复读：重新取持久化结论（published -> statement，pending -> 再抛）
                result = self._replay(input_id)
                self.conn.execute("COMMIT")
                return result
            statement = self._build(cur, input_id, heads, stock_version, stock, snapshot_key)
            self.conn.execute("COMMIT")
            return statement
        except SnapshotConflictError:
            self.conn.execute("ROLLBACK")
            raise
        except PendingError:
            # 新建待核：待核明细已随 _build 落盘，需要提交；复读待核：前面已提交
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")
            raise
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _replay(self, input_id: str) -> dict:
        row = self.conn.execute(
            "SELECT status, statement_json FROM worksheets WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row["status"] == "published":
            return loads(row["statement_json"])
        issues = [
            {"issue_type": r["issue_type"], "details": loads(r["details_json"])}
            for r in self.conn.execute(
                "SELECT * FROM ws_pending WHERE input_id = ? ORDER BY rowid", (input_id,)
            ).fetchall()
        ]
        raise PendingError(
            f"切面仍有待核项目（复读）：{input_id}", {"input_id": input_id, "issues": issues}
        )

    # ------------------------------------------------------------- reconcile
    def _reconcile(self, heads: dict, stock: dict) -> tuple[dict, dict, list[dict]]:
        """返回 (发票索引, 累计收付索引, 待核问题清单)。只读取账簿前缀，不写任何单体数据。"""
        issues: list[dict] = []
        invoices: dict[str, dict] = {}   # invoice_id -> {"P": row, "S": row}
        payments: dict[tuple[str, str], int] = {}

        for entity in ("P", "S"):
            for inv in self.ledger.internal_invoices(entity, heads[entity]):
                invoices.setdefault(inv["invoice_id"], {})[entity] = inv
            for pay in self.ledger.internal_payments(entity, heads[entity]):
                key = (entity, pay["invoice_id"])
                payments[key] = payments.get(key, 0) + pay["amount"]

        for invoice_id in sorted(invoices):
            pair = invoices[invoice_id]
            if "P" not in pair or "S" not in pair:
                issues.append(
                    {
                        "issue_type": "missing_side",
                        "details": {
                            "invoice_id": invoice_id,
                            "present": sorted(pair),
                            "missing": [e for e in ("P", "S") if e not in pair],
                        },
                    }
                )
                continue
            p_row, s_row = pair["P"], pair["S"]
            # 两侧必须一卖一买，且主体与文档声明一致
            if p_row["side"] == s_row["side"]:
                issues.append(
                    {
                        "issue_type": "invoice_mismatch",
                        "details": {
                            "invoice_id": invoice_id,
                            "field": "side",
                            "P": p_row["side"],
                            "S": s_row["side"],
                        },
                    }
                )
            diffs = {}
            for field in ("seller", "buyer", "sku", "q", "T", "C"):
                if p_row[field] != s_row[field]:
                    diffs[field] = {"P": p_row[field], "S": s_row[field]}
            if diffs:
                issues.append(
                    {"issue_type": "invoice_mismatch",
                     "details": {"invoice_id": invoice_id, "fields": diffs}}
                )
            paid_p = payments.get(("P", invoice_id), 0)
            paid_s = payments.get(("S", invoice_id), 0)
            if paid_p != paid_s:
                issues.append(
                    {
                        "issue_type": "payment_mismatch",
                        "details": {
                            "invoice_id": invoice_id,
                            "paid_P": paid_p,
                            "paid_S": paid_s,
                        },
                    }
                )

        # 余额守卫：应收/付总账余额必须等于逐发票未清偿额，防止 document=null 的
        # 结算凭证把货款结清却在逐发票索引里“隐形”，导致按金额错误凑平。
        for entity in ("P", "S"):
            gl = self.ledger.gl(entity, heads[entity])["balances"]
            exp_rec = exp_pay = 0
            for invoice_id, pair in invoices.items():
                if entity not in pair:
                    continue
                row = pair[entity]
                unsettled = row["T"] - payments.get((entity, invoice_id), 0)
                if row["side"] == "seller":
                    exp_rec += unsettled
                else:
                    exp_pay += unsettled
            if gl["receivable"] != exp_rec:
                issues.append(
                    {
                        "issue_type": "balance_guard",
                        "details": {
                            "entity": entity,
                            "account": "receivable",
                            "gl_signed": gl["receivable"],
                            "tracked_unsettled": exp_rec,
                        },
                    }
                )
            if -gl["payable"] != exp_pay:
                issues.append(
                    {
                        "issue_type": "balance_guard",
                        "details": {
                            "entity": entity,
                            "account": "payable",
                            "gl_signed": gl["payable"],
                            "tracked_unsettled_signed": -exp_pay,
                        },
                    }
                )

        # 存货声明覆盖：含全部需要声明的内部发票，r=0 也不能省略；不得多出、不得越界
        declared = stock["items"]
        matched_ids = {iid for iid, pair in invoices.items() if len(pair) == 2}
        for iid in sorted(matched_ids):
            if iid not in declared:
                issues.append(
                    {"issue_type": "stock_missing", "details": {"invoice_id": iid}}
                )
            else:
                q = invoices[iid]["P"]["q"]
                if declared[iid] > q:
                    issues.append(
                        {
                            "issue_type": "stock_out_of_range",
                            "details": {
                                "invoice_id": iid,
                                "r": declared[iid],
                                "q": q,
                            },
                        }
                    )
        for iid in sorted(set(declared) - set(invoices)):
            issues.append(
                {"issue_type": "stock_extra", "details": {"invoice_id": iid}}
            )
        return invoices, payments, issues

    # ---------------------------------------------------------------- build
    def _build(self, cur, input_id, heads, stock_version, stock, snapshot_key) -> dict:
        invoices, payments, issues = self._reconcile(heads, stock)
        if issues:
            cur.execute(
                "INSERT INTO worksheets (input_id, status, heads_json, stock_version, snapshot_key, "
                "statement_json) VALUES (?, 'pending', ?, ?, ?, NULL)",
                (input_id, dumps(heads), stock_version, snapshot_key),
            )
            cur.executemany(
                "INSERT INTO ws_pending (input_id, issue_type, details_json) VALUES (?, ?, ?)",
                [(input_id, i["issue_type"], dumps(i["details"])) for i in issues],
            )
            raise PendingError(
                f"工作底稿 {input_id} 存在 {len(issues)} 项待核，未发布",
                {"input_id": input_id, "issues": issues},
            )

        # 1) 单体原始合计（整数余额，绝不改写单体账）
        raw = {e: self.ledger.gl(e, heads[e])["balances"] for e in ("P", "S")}
        combined = zero_balances()
        for entity in ("P", "S"):
            for account in ACCOUNTS:
                combined[account] += raw[entity][account]

        # 2) 逐发票抵销
        eliminations: list[dict] = []
        elim_totals = {a: Fraction(0) for a in ACCOUNTS}
        invoice_details: list[dict] = []
        ord_no = 0
        for invoice_id in sorted(invoices):
            pair = invoices[invoice_id]
            p_row, s_row = pair["P"], pair["S"]
            q, t, c = p_row["q"], p_row["T"], p_row["C"]
            r = stock["items"][invoice_id]
            paid = payments.get(("P", invoice_id), 0)
            unsettled = t - paid
            urp = Fraction(t - c, q) * r
            skipped: list[str] = []

            inv_sources = [
                {"type": "journal", "entity": "P", "seq": p_row["journal_seq"]},
                {"type": "journal", "entity": "S", "seq": s_row["journal_seq"]},
            ]
            pay_sources = [
                {"type": "journal", "entity": e, "seq": p["journal_seq"],
                 "journal_id": p["journal_id"]}
                for e in ("P", "S")
                for p in self.ledger.internal_payments(e, heads[e])
                if p["invoice_id"] == invoice_id
            ]

            # (a) 内部往来抵销：借 payable / 贷 receivable（仅未清偿部分）
            if unsettled > 0:
                ord_no += 1
                entry = {
                    "entry_id": f"E{ord_no:03d}",
                    "kind": "intercompany_balance",
                    "invoice_id": invoice_id,
                    "lines": [
                        {"account": "payable", "debit": Fraction(unsettled), "credit": Fraction(0)},
                        {"account": "receivable", "debit": Fraction(0), "credit": Fraction(unsettled)},
                    ],
                    "sources": inv_sources + pay_sources,
                }
                eliminations.append(entry)
                elim_totals["payable"] += Fraction(unsettled)
                elim_totals["receivable"] -= Fraction(unsettled)
            else:
                skipped.append("intercompany_balance:already_settled")

            # (b) 内部收入与对应内部成本抵销：借 revenue T / 贷 cost T
            ord_no += 1
            sale_entry = {
                "entry_id": f"E{ord_no:03d}",
                "kind": "intercompany_sale",
                "invoice_id": invoice_id,
                "lines": [
                    {"account": "revenue", "debit": Fraction(t), "credit": Fraction(0)},
                    {"account": "cost", "debit": Fraction(0), "credit": Fraction(t)},
                ],
                "sources": inv_sources,
            }
            eliminations.append(sale_entry)
            elim_totals["revenue"] += Fraction(t)
            elim_totals["cost"] -= Fraction(t)

            # (c) 未实现存货利润：(T-C)*r/q。允许负值，反向表达，不截零
            if urp != 0:
                ord_no += 1
                if urp > 0:
                    urp_lines = [
                        {"account": "cost", "debit": urp, "credit": Fraction(0)},
                        {"account": "inventory", "debit": Fraction(0), "credit": urp},
                    ]
                else:
                    urp_lines = [
                        {"account": "inventory", "debit": -urp, "credit": Fraction(0)},
                        {"account": "cost", "debit": Fraction(0), "credit": -urp},
                    ]
                eliminations.append(
                    {
                        "entry_id": f"E{ord_no:03d}",
                        "kind": "unrealized_profit",
                        "invoice_id": invoice_id,
                        "lines": urp_lines,
                        "sources": inv_sources + [
                            {"type": "stock", "version": stock_version, "invoice_id": invoice_id}
                        ],
                    }
                )
                for ln in urp_lines:
                    elim_totals[ln["account"]] += ln["debit"] - ln["credit"]
            else:
                skipped.append("unrealized_profit:zero_not_clamped")

            invoice_details.append(
                {
                    "invoice_id": invoice_id,
                    "q": q, "T": t, "C": c, "r": r,
                    "paid": paid, "unsettled": unsettled,
                    "unrealized_profit": frac_to_json(urp),
                    "skipped": skipped,
                }
            )

        # 3) 最终余额与自检
        final_balances = {
            account: Fraction(combined[account]) + elim_totals[account] for account in ACCOUNTS
        }
        for entry in eliminations:
            d = sum((ln["debit"] for ln in entry["lines"]), Fraction(0))
            c = sum((ln["credit"] for ln in entry["lines"]), Fraction(0))
            if d != c:
                raise RuntimeError(f"抵销分录 {entry['entry_id']} 自身不平衡")
        if sum(elim_totals.values(), Fraction(0)) != 0:
            raise RuntimeError("抵销总额借贷不平衡")
        if sum(final_balances.values(), Fraction(0)) != 0:
            raise RuntimeError("最终总借贷不平衡")

        statement = {
            "input_id": input_id,
            "status": "published",
            "snapshot": {"heads": heads, "stock_version": stock_version},
            "raw": {
                "P": raw["P"],
                "S": raw["S"],
                "combined": combined,
            },
            "eliminations": [_entry_to_json(e) for e in eliminations],
            "elimination_totals": {a: frac_to_json(v) for a, v in elim_totals.items()},
            "final": {a: frac_to_json(v) for a, v in final_balances.items()},
            "invoice_details": invoice_details,
            "checks": {
                "all_entries_balanced": True,
                "elimination_total_zero": True,
                "final_total_zero": True,
            },
        }

        cur.execute(
            "INSERT INTO worksheets (input_id, status, heads_json, stock_version, snapshot_key, "
            "statement_json) VALUES (?, 'published', ?, ?, ?, ?)",
            (input_id, dumps(heads), stock_version, snapshot_key, dumps(statement)),
        )
        cur.executemany(
            "INSERT INTO ws_entries (input_id, entry_id, kind, invoice_id, ord) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (input_id, e["entry_id"], e["kind"], e["invoice_id"], i)
                for i, e in enumerate(eliminations)
            ],
        )
        return statement

    # ============================================================ reads
    def statement(self, input_id: str) -> dict:
        """读取已发布底稿；待核 input_id 抛 PendingError；未知 id 抛 ValidationError。"""
        row = self.conn.execute(
            "SELECT status FROM worksheets WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"未知工作底稿 {input_id}")
        return self._replay(input_id)

    def pending(self, input_id: str) -> dict:
        """读取待核明细（无论之前是否已发布尝试）。未知 id 报错。"""
        row = self.conn.execute(
            "SELECT status FROM worksheets WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"未知工作底稿 {input_id}")
        issues = [
            {"issue_type": r["issue_type"], "details": loads(r["details_json"])}
            for r in self.conn.execute(
                "SELECT * FROM ws_pending WHERE input_id = ? ORDER BY rowid", (input_id,)
            ).fetchall()
        ]
        return {"input_id": input_id, "status": row["status"], "issues": issues}

    def sources(self, input_id: str, entry_id: Optional[str] = None) -> Any:
        """逐项来源查询：返回抵销分录及其解析后的原始凭证/声明条目，可追到原始凭证。"""
        statement = self.statement(input_id)
        if entry_id is None:
            return [self._resolve(input_id, e) for e in statement["eliminations"]]
        for entry in statement["eliminations"]:
            if entry["entry_id"] == entry_id:
                return self._resolve(input_id, entry)
        raise ValidationError(f"底稿 {input_id} 无抵销分录 {entry_id}")

    def _resolve(self, input_id: str, entry_json: dict) -> dict:
        resolved = []
        for ref in entry_json["sources"]:
            if ref["type"] == "journal":
                resolved.append(
                    {"ref": ref, "voucher": self.ledger.voucher(ref["entity"], ref["seq"])}
                )
            elif ref["type"] == "stock":
                stock = self.ledger.stock(ref["version"])
                resolved.append(
                    {
                        "ref": ref,
                        "stock_item": {
                            "version": ref["version"],
                            "invoice_id": ref["invoice_id"],
                            "r": stock["items"][ref["invoice_id"]],
                        },
                    }
                )
        return {"entry": entry_json, "resolved_sources": resolved}


def _entry_to_json(entry: dict) -> dict:
    return {
        "entry_id": entry["entry_id"],
        "kind": entry["kind"],
        "invoice_id": entry["invoice_id"],
        "lines": [
            {
                "account": ln["account"],
                "debit": frac_to_json(ln["debit"]),
                "credit": frac_to_json(ln["credit"]),
            }
            for ln in entry["lines"]
        ],
        "sources": entry["sources"],
    }
