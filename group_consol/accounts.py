"""固定账户表与有符号余额约定。

约定：统一保存“借减贷”的有符号整数余额，即
    signed = sum(debit) - sum(credit)
前四个账户（cash/receivable/inventory/payable）为余额账户，借贷方向都可能出现；
capital、revenue 正常为贷方（正常余额为负）；cost 正常为借方（正常余额为正）。
任何一套平衡分录累加后，七个账户的有符号余额之和恒为 0。
"""

# 固定账户表，顺序也是输出顺序
ACCOUNTS = ("cash", "receivable", "inventory", "payable", "capital", "revenue", "cost")
ACCOUNT_SET = frozenset(ACCOUNTS)

# 正常借方余额的账户
NORMAL_DEBIT = frozenset(("cash", "receivable", "inventory", "cost"))
# 正常贷方余额的账户
NORMAL_CREDIT = frozenset(("payable", "capital", "revenue"))

ENTITIES = ("P", "S")
ENTITY_SET = frozenset(ENTITIES)


def zero_balances() -> dict:
    """七个账户的零余额字典。"""
    return {a: 0 for a in ACCOUNTS}


def apply_line(balances: dict, account: str, debit: int, credit: int) -> None:
    """把一行借贷金额按借减贷累加到余额字典（原地修改）。"""
    balances[account] = balances.get(account, 0) + debit - credit
