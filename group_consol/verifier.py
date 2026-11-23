"""独立核验：按原始分录重新累加单体余额，按明示公式手算抵销。

刻意不调用 EntityLedger.gl()、ConsolidationService._reconcile() 等被核验系统自己的
汇总/核对函数；这里直接打开 SQLite 读 journal_lines / internal_* / stock_items 原始表，
独立重算并与已发布底稿逐项比对。任何不符都抛 AssertionError。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction

from .accounts import ACCOUNTS
from .jsoncodec import json_to_frac, loads


def _raw_balances(conn: sqlite3.Connection, entity: str, prefix: int) -> dict:
    """核验者自己的余额累加器：只认 journal_lines 原始表。"""
    bal = {a: 0 for a in ACCOUNTS}
    rows = conn.execute(
        """
        SELECT jl.account, jl.debit, jl.credit
        FROM journal_lines jl
        JOIN journals j ON jl.entity = j.entity AND jl.seq = j.seq
        WHERE jl.entity = ? AND j.seq <= ?
        ORDER BY j.seq, jl.line_no
        """,
        (entity, prefix),
    ).fetchall()
    for account, debit, credit in rows:
        bal[account] += debit - credit
    return bal


def _index_invoices(conn, heads):
    invoices: dict[str, dict] = {}
    for entity in ("P", "S"):
        for row in conn.execute(
            "SELECT * FROM internal_invoices WHERE entity = ? AND journal_seq <= ? ORDER BY invoice_id",
            (entity, heads[entity]),
        ).fetchall():
            invoices.setdefault(row["invoice_id"], {})[entity] = dict(row)
    return invoices


def _index_payments(conn, heads):
    paid: dict[tuple[str, str], int] = {}
    pay_rows: dict[tuple[str, str], list] = {}
    for entity in ("P", "S"):
        for row in conn.execute(
            "SELECT * FROM internal_payments WHERE entity = ? AND journal_seq <= ?",
            (entity, heads[entity]),
        ).fetchall():
            key = (entity, row["invoice_id"])
            paid[key] = paid.get(key, 0) + row["amount"]
            pay_rows.setdefault(key, []).append(dict(row))
    return paid, pay_rows


def verify_worksheet(conn: sqlite3.Connection, input_id: str) -> dict:
    """独立核验已发布底稿，返回核验报告；失败抛 AssertionError。"""
    ws = conn.execute(
        "SELECT * FROM worksheets WHERE input_id = ?", (input_id,)
    ).fetchone()
    assert ws is not None, f"底稿 {input_id} 不存在"
    assert ws["status"] == "published", f"底稿 {input_id} 状态为 {ws['status']}，不可核验"
    stmt = loads(ws["statement_json"])
    heads = stmt["snapshot"]["heads"]
    version = stmt["snapshot"]["stock_version"]
    assert set(heads) == {"P", "S"}

    # ---- 1. 独立重算单体原始合计 ----------------------------------------
    raw = {e: _raw_balances(conn, e, heads[e]) for e in ("P", "S")}
    for entity in ("P", "S"):
        assert sum(raw[entity].values()) == 0, f"{entity} 单体余额借贷不平"
        assert raw[entity] == stmt["raw"][entity], (
            f"{entity} 原始余额与底稿不符：独立重算 {raw[entity]} vs 底稿 {stmt['raw'][entity]}"
        )
    combined = {a: raw["P"][a] + raw["S"][a] for a in ACCOUNTS}
    assert combined == stmt["raw"]["combined"], "单体合计与底稿 combined 不符"

    # ---- 2. 独立重建发票/收付/声明索引 -----------------------------------
    invoices = _index_invoices(conn, heads)
    paid, pay_rows = _index_payments(conn, heads)
    stock_rows = conn.execute(
        "SELECT invoice_id, r FROM stock_items WHERE version = ? ORDER BY invoice_id",
        (version,),
    ).fetchall()
    stock = {r["invoice_id"]: r["r"] for r in stock_rows}
    sv = conn.execute(
        "SELECT canonical FROM stock_versions WHERE version = ?", (version,)
    ).fetchone()
    assert sv is not None and loads(sv["canonical"])["items"] == stock

    matched = {iid: pair for iid, pair in invoices.items() if len(pair) == 2}
    assert set(stock) == set(matched), "声明覆盖的发票集合与双侧镜像集合不一致"

    # ---- 3. 按明示公式手算每张发票的三类抵销 ------------------------------
    expected_entries: list[dict] = []
    elim_totals = {a: Fraction(0) for a in ACCOUNTS}
    for iid in sorted(matched):
        p_row, s_row = matched[iid]["P"], matched[iid]["S"]
        assert p_row["side"] != s_row["side"], f"{iid} 两侧 side 相同"
        for field in ("seller", "buyer", "sku", "q", "T", "C"):
            assert p_row[field] == s_row[field], f"{iid} 字段 {field} 两侧异文"
        q, t, c = p_row["q"], p_row["T"], p_row["C"]
        assert iid in stock and 0 <= stock[iid] <= q, f"{iid} 声明 r 越界"
        r = stock[iid]
        paid_p = paid.get(("P", iid), 0)
        paid_s = paid.get(("S", iid), 0)
        assert paid_p == paid_s, f"{iid} 两侧累计收付不一致"
        unsettled = t - paid_p
        # 公式：未实现利润 = (T-C) × r/q（Fraction 自动约分；可为负，不截零）
        urp = Fraction(t - c, q) * r

        if unsettled > 0:
            expected_entries.append(
                {"kind": "intercompany_balance", "invoice_id": iid,
                 "lines": {"payable": Fraction(unsettled), "receivable": Fraction(-unsettled)}}
            )
        expected_entries.append(
            {"kind": "intercompany_sale", "invoice_id": iid,
             "lines": {"revenue": Fraction(t), "cost": Fraction(-t)}}
        )
        if urp != 0:
            expected_entries.append(
                {"kind": "unrealized_profit", "invoice_id": iid,
                 "lines": {"cost": urp, "inventory": -urp}}
            )

    # ---- 4. 与底稿抵销分录逐条比对（顺序、方向、金额） ---------------------
    actual_entries = stmt["eliminations"]
    assert len(actual_entries) == len(expected_entries), (
        f"抵销分录条数不符：期望 {len(expected_entries)}，底稿 {len(actual_entries)}"
    )
    for actual, expected in zip(actual_entries, expected_entries):
        assert actual["kind"] == expected["kind"], "抵销类型顺序不符"
        assert actual["invoice_id"] == expected["invoice_id"], "抵销发票顺序不符"
        signed = {
            ln["account"]: json_to_frac(ln["debit"]) - json_to_frac(ln["credit"])
            for ln in actual["lines"]
        }
        assert signed == expected["lines"], (
            f"分录 {actual['entry_id']} 金额/方向与手算不符: {signed} vs {expected['lines']}"
        )
        # 每条分录自身借贷平衡
        assert sum(json_to_frac(ln["debit"]) for ln in actual["lines"]) == sum(
            json_to_frac(ln["credit"]) for ln in actual["lines"]
        ), f"分录 {actual['entry_id']} 自身不平衡"
        # 来源引用必须能落到真实凭证/声明
        for ref in actual["sources"]:
            if ref["type"] == "journal":
                exists = conn.execute(
                    "SELECT 1 FROM journals WHERE entity = ? AND seq = ?",
                    (ref["entity"], ref["seq"]),
                ).fetchone()
                assert exists, f"分录 {actual['entry_id']} 来源凭证不存在: {ref}"
            elif ref["type"] == "stock":
                assert stock.get(ref["invoice_id"]) is not None
        for account, value in signed.items():
            elim_totals[account] += value

    assert {a: json_to_frac(v) for a, v in stmt["elimination_totals"].items()} == elim_totals
    assert sum(elim_totals.values(), Fraction(0)) == 0, "抵销总额不守恒"

    # ---- 5. 最终余额 = 原始合计 + 抵销；总借贷仍平衡 -----------------------
    for account in ACCOUNTS:
        want = Fraction(combined[account]) + elim_totals[account]
        got = json_to_frac(stmt["final"][account])
        assert got == want, f"最终余额 {account} 不符: {got} vs {want}"
    assert sum(json_to_frac(v) for v in stmt["final"].values()) == 0, "最终总借贷不平衡"

    # ---- 6. 外部凭证保留性：底稿涉及前缀内的每张凭证都仍在原始表，且未被改写 ----
    for entity in ("P", "S"):
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM journals WHERE entity = ? AND seq <= ?",
            (entity, heads[entity]),
        ).fetchone()["n"]
        assert n == heads[entity], f"{entity} 前缀 {heads[entity]} 与实际凭证数 {n} 不一致"

    return {
        "input_id": input_id,
        "verified": True,
        "heads": heads,
        "stock_version": version,
        "invoices_checked": len(matched),
        "elimination_entries": len(actual_entries),
        "raw_combined": combined,
        "final": stmt["final"],
        "independent_note": "余额由 journal_lines 原始表独立累加，抵销按 (T-C)*r/q 手算",
    }
