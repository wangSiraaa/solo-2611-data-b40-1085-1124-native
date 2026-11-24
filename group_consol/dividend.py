"""红利宣告端到端门面：把可分余额、股东册分派、稳定筛选、宣告、兑领串起来。

一个 `Lot` 是一个期间：
  1) 入一套可追溯凭证（对外收入/费用/内部销货）→ 算可分余额；
  2) 股东会按股东册筛选稳定选择，作保底承诺（须 ≤ 每个稳定选择下所得）；
  3) 从稳定方案里挑一个经正式宣告；只有这次宣告可兑领；
  4) 宣告后开红利权（claim），可再拆转，按渠道兑领。

不改旧宣告：宣告入册后新入的单体账只影响后续批次。
"""

from __future__ import annotations

import os
from fractions import Fraction
from typing import Optional

from .allocation import ShareBook
from .book import DividendHouse
from .custody import ClaimBook


class DividendError(Exception):
    code = "dividend_error"

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _err(code, message, **details):
    e = DividendError(message, details)
    e.code = code
    return e


class DividendWorkspace:
    """一本书 + 一个 ClaimBook；批次号 lot_id 区分期间。"""

    def __init__(self, path: str):
        self.path = path
        self.house = DividendHouse(path)
        self.conn = self.house.conn
        self.main = self.house.main
        self.memo = self.house.memo
        self.claims = ClaimBook(self.conn)
        self._declarations: dict[str, dict] = {}

    def close(self):
        self.house.close()

    # ---------------------------------------------------------------- ledger
    def main_post(self, journal_id, lines, document=None, supersedes=None):
        return self.house.main.post(journal_id, lines, document, supersedes)

    def memo_post(self, journal_id, lines, document=None, supersedes=None):
        return self.house.memo.post(journal_id, lines, document, supersedes)

    def memo_loss(self, loss_id, amount):
        return self.house.memo.register_loss(loss_id, Fraction(amount))

    def memo_recover(self, reversal_id, loss_id, amount, supersedes=None):
        return self.house.memo.recover_loss(reversal_id, loss_id, Fraction(amount),
                                             supersedes)

    def declare_stock(self, version, items):
        return self.house.main.declare_stock(version, items)

    def balance(self, stock_version=None):
        return self.house.distributable(stock_version)

    def balance_from_worksheet(self, statement: dict) -> dict:
        """在已成功的集团合并底稿上算可分余额（题面要求）。"""
        return self.house.distributable_from_worksheet(statement)

    # ------------------------------------------------------------ evaluation
    def evaluate(self, classes, holdings, total: Fraction) -> dict:
        book = ShareBook(classes, holdings)
        ev = book.evaluate_all(Fraction(total))
        ev["_book"] = book
        return ev

    @staticmethod
    def choose_stable(ev: dict, index: int) -> dict:
        stable = ev["stable_options"]
        if not stable:
            raise _err("no_stable_option", "无稳定选择，不能宣告")
        if index < 0 or index >= len(stable):
            raise _err("bad_stable_index", "稳定选择序号越界",
                       count=len(stable))
        return stable[index]

    @staticmethod
    def promise(ev: dict, holder: str, amount: Fraction) -> dict:
        stable = ev["stable_options"]
        if not stable:
            raise _err("no_stable_option", "无稳定选择，不能作保底承诺")
        if holder not in ev["ranges"]:
            raise _err("unknown_holder", f"无此人 {holder}", holder=holder)
        mn = ev["ranges"][holder]["min"]
        if Fraction(amount) > mn:
            raise _err("promise_not_funded",
                       "承诺超过某些稳定选择下的所得，付不足",
                       holder=holder, promised=str(amount), guaranteed_min=str(mn))
        return {"holder": holder, "promised": str(amount), "guaranteed_min": str(mn),
                "funded": True}

    # ------------------------------------------------------------- declare
    def declare(self, lot_id: str, amount: Fraction, plan: dict,
                stock_version: Optional[str] = None) -> dict:
        """正式宣告：只有这次宣告的红利权可兑领；同时落宣告会计分录。"""
        if lot_id in self._declarations:
            raise _err("duplicate_lot", f"批次 {lot_id} 已宣告")
        # 1) 可分余额校验
        bal = self.house.distributable(stock_version)
        if Fraction(amount) > bal["distributable"]:
            raise _err("over_distributable",
                       "宣告金额超过可分余额",
                       amount=str(amount), distributable=str(bal["distributable"]))
        # 2) 稳定方案校验：必须是经核稳定的选择之一
        ev = plan["evaluation"]
        choice = plan["choice"]
        stable_choices = [o["choice"] for o in ev["stable_options"]]
        if choice not in stable_choices:
            raise _err("not_stable_choice", "宣告的方案不是经核稳定选择",
                       choice=choice,
                       stable=stable_choices,
                       rejected_deviations=ev["rejected_deviations"])
        chosen = next(o for o in ev["stable_options"] if o["choice"] == choice)
        if Fraction(amount) != Fraction(ev["total"]):
            # 宣告金额必须等于该稳定方案下的可分总额（这里按方案总额分派）
            raise _err("amount_mismatch",
                       "宣告金额须等于所选稳定方案的分派总额",
                       declared=str(amount), plan_total=str(ev["total"]))
        # 3) 宣告入册（bk_declarations，Fraction 原样保存）：这笔"已宣告未撤销"从此
        #    在可分余额里被扣除，且后加的单体凭证只改变后续批次，不改这笔宣告。
        self.house.main.declare(lot_id, Fraction(amount), lot_id, plan_id=choice.__repr__())
        plan_total = Fraction(ev["total"])
        alloc = chosen["amounts"]
        claims = self.claims.open_claims(
            {h: Fraction(v) for h, v in alloc.items()},
            plan.get("channels", {}), len(self._declarations) + 1)
        rec = {
            "lot_id": lot_id,
            "amount": str(Fraction(amount)),
            "choice": choice,
            "plan_total": str(plan_total),
            "allocations": {h: str(Fraction(v)) for h, v in alloc.items()},
            "claims": claims,
            "stable_options_count": len(ev["stable_options"]),
        }
        self._declarations[lot_id] = rec
        return rec

    def declaration(self, lot_id: str) -> dict:
        if lot_id not in self._declarations:
            raise _err("unknown_lot", f"未宣告批次 {lot_id}")
        return self._declarations[lot_id]

    # --------------------------------------------------------------- custody
    def claim_by_holder(self, lot_id: str, holder: str) -> dict:
        rec = self.declaration(lot_id)
        for c in rec["claims"]:
            if c["holder"] == holder:
                return c
        raise _err("no_claim", f"{holder} 在批次 {lot_id} 无红利权")

    def claim_of(self, claim_id: str) -> dict:
        return self.claims.claim(claim_id)

    # delegated custody ops
    def transfer(self, *a, **k):
        return self.claims.apply_transfer(*a, **k)

    def register_receipt(self, *a, **k):
        return self.claims.register_transfer_receipt(*a, **k)

    def issue_ticket(self, *a, **k):
        return self.claims.issue_ticket(*a, **k)

    def split_ticket(self, *a, **k):
        return self.claims.split_ticket(*a, **k)

    def deliver_ticket(self, *a, **k):
        return self.claims.deliver_ticket(*a, **k)

    def pay_ticket(self, *a, **k):
        return self.claims.pay_ticket(*a, **k)

    def pay_claim(self, *a, **k):
        return self.claims.pay_claim(*a, **k)

    def receipt(self, *a, **k):
        return self.claims.receipt(*a, **k)

    def check_ticket(self, *a, **k):
        return self.claims.check_ticket(*a, **k)


def fresh(path: str) -> DividendWorkspace:
    if os.path.exists(path):
        os.remove(path)
    return DividendWorkspace(path)
