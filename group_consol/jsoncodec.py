"""JSON 序列化辅助。

单体账金额全部是非负整数分；合并底稿使用 fractions.Fraction（约分有理数），
分数以 {"num": 分子, "den": 分母} 形式持久化，整数原样存整数，绝不取整。
"""

import json
from fractions import Fraction
from typing import Any


def frac_to_json(value: Fraction) -> Any:
    """Fraction -> JSON 标量：整数转 int，否则转 num/den 对象。"""
    if value.denominator == 1:
        return value.numerator
    return {"num": value.numerator, "den": value.denominator}


def json_to_frac(value: Any) -> Fraction:
    """JSON 标量 -> Fraction。"""
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, dict) and "num" in value and "den" in value:
        return Fraction(value["num"], value["den"])
    raise ValueError(f"无法解释为有理数的 JSON 值: {value!r}")


def dumps(obj: Any) -> str:
    """稳定 JSON 文本（sort_keys、不转义非 ASCII）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)
