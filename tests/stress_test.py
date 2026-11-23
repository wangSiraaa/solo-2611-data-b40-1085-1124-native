"""容量与边界压力测试：40 张内部发票、每实体 200 张凭证、20 份存货声明。

数量/金额在 1..100000 内随机生成（固定种子可复现），全程断言：
- 连续 seq、全部凭证平衡、重开可复读；
- 双侧齐套后发布成功，独立核验通过；有理未实现利润保留分数不取整；
- 缺一侧镜像时发布待核；
- 规模与题目输入族上限一致（允许完整重算与串行事务）。
"""

from __future__ import annotations

import os
import random
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol import EntityLedger, ConsolidationService, PendingError  # noqa: E402
from group_consol.verifier import verify_worksheet  # noqa: E402

N_INVOICES = 40
N_JOURNALS = 200
N_VERSIONS = 20
LIMIT = 100_000


def main() -> None:
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "state", "stress.db")
    if os.path.exists(db):
        os.remove(db)
    rng = random.Random(20261117)
    ledger = EntityLedger(db)
    svc = ConsolidationService(ledger)

    invoices = []
    for i in range(N_INVOICES):
        q = rng.randint(1, 100)
        t = rng.randint(1, LIMIT)
        c = rng.randint(1, LIMIT)
        r = rng.randint(0, q)
        invoices.append((f"inv{i:02d}", q, t, c, r))

    def sale_lines_seller(t, c):
        return [["receivable", t, 0], ["revenue", 0, t], ["cost", c, 0], ["inventory", 0, c]]

    def sale_lines_buyer(t):
        return [["inventory", t, 0], ["payable", 0, t]]

    # 先把每实体普通外部凭证补齐到 200 张（期初注资 + 外部现金业务）
    for entity in ("P", "S"):
        ledger.post("opening", entity, [["cash", LIMIT, 0], ["capital", 0, LIMIT]])
    external_each = N_JOURNALS - N_INVOICES - 1  # 剩余名额给外部凭证
    for k in range(external_each):
        amt = rng.randint(1, LIMIT)
        ledger.post(f"ext-p-{k}", "P", [["cash", amt, 0], ["revenue", 0, amt]])
        ledger.post(f"ext-s-{k}", "S", [["cash", amt, 0], ["revenue", 0, amt]])
    assert ledger.head("P") == external_each + 1
    assert ledger.head("S") == external_each + 1

    # 逐发票双侧镜像；S 侧故意最后缺一张，验证待核
    for idx, (iid, q, t, c, _r) in enumerate(invoices):
        doc_s = {"type": "internal_sale", "side": "seller", "invoice_id": iid,
                 "seller": "P", "buyer": "S", "sku": f"SKU{i}", "q": q, "T": t, "C": c}
        doc_b = dict(doc_s, side="buyer")
        ledger.post(f"sell-{iid}", "P", sale_lines_seller(t, c), doc_s)
        if idx < N_INVOICES - 1:
            ledger.post(f"buy-{iid}", "S", sale_lines_buyer(t), doc_b)

    assert ledger.head("P") == N_JOURNALS
    assert ledger.head("S") == N_JOURNALS - 1

    # 20 份声明版本：前 19 份只覆盖已齐套发票，最后一份覆盖全部
    full_items = {iid: r for iid, _q, _t, _c, r in invoices}
    for v in range(1, N_VERSIONS):
        items = dict(list(full_items.items())[: N_INVOICES - 1])
        ledger.register_stock(f"stress-v{v}", items)
    ledger.register_stock(f"stress-v{N_VERSIONS}", full_items)

    # 缺侧待核
    try:
        svc.publish("stress-pending", {"P": N_JOURNALS, "S": N_JOURNALS - 1},
                    f"stress-v{N_VERSIONS}")
        raise SystemExit("缺镜像时应当待核")
    except PendingError as exc:
        assert any(i["issue_type"] == "missing_side" for i in exc.details["issues"])
        print("缺侧待核明细数：", len(exc.details["issues"]))

    # 补齐最后一张镜像后发布
    iid, q, t, c, _r = invoices[-1]
    doc_b = {"type": "internal_sale", "side": "buyer", "invoice_id": iid,
             "seller": "P", "buyer": "S", "sku": f"SKU{N_INVOICES - 1}", "q": q, "T": t, "C": c}
    ledger.post(f"buy-{iid}", "S", sale_lines_buyer(t), doc_b)
    assert ledger.head("S") == N_JOURNALS

    started = time.time()
    stmt = svc.publish("stress-full", {"P": N_JOURNALS, "S": N_JOURNALS},
                       f"stress-v{N_VERSIONS}")
    report = verify_worksheet(ledger.conn, "stress-full")
    elapsed = time.time() - started

    # 手算抽查一张发票的未实现利润（不依赖合并服务）
    iid0, q0, t0, c0, r0 = invoices[0]
    expect_urp = Fraction(t0 - c0, q0) * r0
    actual = next(d for d in stmt["invoice_details"] if d["invoice_id"] == iid0)
    n, d = expect_urp.numerator, expect_urp.denominator
    expect_json = n if d == 1 else {"num": n, "den": d}
    assert actual["unrealized_profit"] == expect_json

    # 重开复读
    ledger.close()
    ledger = EntityLedger(db)
    svc = ConsolidationService(ledger)
    assert svc.statement("stress-full")["input_id"] == "stress-full"
    verify_worksheet(ledger.conn, "stress-full")

    print(f"压力测试通过：发票 {N_INVOICES}，每实体凭证 {N_JOURNALS}，声明 {N_VERSIONS} 份")
    print(f"抵销分录 {report['elimination_entries']} 条，发布+核验耗时 {elapsed:.2f}s")
    ledger.close()


if __name__ == "__main__":
    main()
