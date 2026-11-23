"""复现随题材料 case.json：证明系统按固定合成口径逐字受理样例凭证。

样例结算凭证 document=null（材料规定的普通凭证形态）。脚本给出两个切面：
  A. 不投放结算凭证：发布成功，往来抵销 80，未实现利润 (80-40)*2/4 = 20；
  B. 投放 document=null 结算凭证：余额守卫识别出“货款已在总账结清、逐发票索引未登记”，
     发布返回明确待核（balance_guard），而不是静默凑平；
     改用带 invoice_id 引用的内部收付款凭证后发布成功，往来抵销为 0、存货利润仍为 20。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol import EntityLedger, ConsolidationService, PendingError  # noqa: E402
from group_consol.verifier import verify_worksheet  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _post(ledger: EntityLedger, voucher: dict) -> None:
    ledger.post(voucher["journal_id"], voucher["entity"], voucher["lines"],
                voucher.get("document"))


def post_case(ledger: EntityLedger, case: dict, include_null_settlements: bool) -> None:
    # opening 在样例里是凭证数组；其余键是单个凭证对象
    for voucher in case["opening"]:
        _post(ledger, voucher)
    _post(ledger, case["seller_journal"])
    _post(ledger, case["buyer_journal"])
    _post(ledger, case["external_sale"])
    if include_null_settlements:
        for settlement in case["settlements"]:
            _post(ledger, settlement)


def main() -> None:
    with open(os.path.join(HERE, "case.json"), encoding="utf-8") as fh:
        case = json.load(fh)
    db_path = os.path.join(HERE, "state", "case.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    ledger = EntityLedger(db_path)
    svc = ConsolidationService(ledger)

    # 切面 A：样例前四张凭证（不含结算）
    post_case(ledger, case, include_null_settlements=False)
    assert ledger.heads() == {"P": 2, "S": 2}
    ledger.register_stock("case-stock", {"i1": case["remaining_stock"]["i1"]})
    stmt_a = svc.publish("case-a", {"P": 2, "S": 2}, "case-stock")
    print("== 切面 A（未结算）==")
    print(json.dumps(stmt_a["eliminations"], ensure_ascii=False, indent=1))
    verify_worksheet(ledger.conn, "case-a")

    # 切面 B1：样例 document=null 结算凭证 -> 余额守卫待核，不静默凑平
    post_case(ledger, case, include_null_settlements=True)
    assert ledger.heads() == {"P": 3, "S": 3}
    try:
        svc.publish("case-b1", {"P": 3, "S": 3}, "case-stock")
        raise SystemExit("切面 B1 应当待核却发布成功")
    except PendingError as exc:
        print("== 切面 B1（document=null 结算）待核 ==")
        print(json.dumps(exc.details, ensure_ascii=False, indent=1))

    # 切面 B2：同额同身份改用类型化内部收付款（引用 invoice_id）-> 发布成功
    db2 = os.path.join(HERE, "state", "case-typed.db")
    if os.path.exists(db2):
        os.remove(db2)
    ledger2 = EntityLedger(db2)
    svc2 = ConsolidationService(ledger2)
    post_case(ledger2, case, include_null_settlements=False)
    ledger2.post("receive-i1", "P", [["cash", 80, 0], ["receivable", 0, 80]],
                 {"type": "internal_payment", "side": "receipt", "invoice_id": "i1"})
    ledger2.post("pay-i1", "S", [["payable", 80, 0], ["cash", 0, 80]],
                 {"type": "internal_payment", "side": "settlement", "invoice_id": "i1"})
    ledger2.register_stock("case-stock", {"i1": 2})
    stmt_b2 = svc2.publish("case-b2", {"P": 3, "S": 3}, "case-stock")
    print("== 切面 B2（类型化结算，货款结清）==")
    print(json.dumps(stmt_b2["eliminations"], ensure_ascii=False, indent=1))
    report = verify_worksheet(ledger2.conn, "case-b2")
    kinds = [e["kind"] for e in stmt_b2["eliminations"]]
    assert "intercompany_balance" not in kinds, "货款已结清，不应再有往来抵销"
    urp = next(d for d in stmt_b2["invoice_details"] if d["invoice_id"] == "i1")
    assert urp["unrealized_profit"] == 20, urp
    print("独立核验通过：", report)
    ledger.close()
    ledger2.close()


if __name__ == "__main__":
    main()
