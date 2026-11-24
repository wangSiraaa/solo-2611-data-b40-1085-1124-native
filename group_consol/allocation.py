"""股册与分派：普通份余款、优先份整类转普通或按顺序取定额、稳定选择筛选。

合成口径：
  * 普通份参与余款，按份数精确分；转股类连同普通持有人按份数一起分。
  * 可转优先份两类选项：
        cash    -> 按登记顺序取定额；不足时该类及后类按份数比例承受短缺
        convert -> 整类转普通（等价份数 = 份数 × ratio），加入普通池
  * 一个"选择"为每个可转类指定 cash/convert。若某类单独反转成 convert 而不增加
    自己的所得（即在每个稳定选择里，把它改回 convert 都不会让它更多），则该类
    反转不稳。只有所有类都稳定的选择才可正式宣告。
  * 稳定选择集合给出每人的金额区间（只用于判断能否作保底承诺：承诺须 ≤ 最小值）。
  * 剩余货物对应的内部毛利不进入可分余额（见 book.py）。
"""

from __future__ import annotations

from fractions import Fraction
from itertools import product
from typing import Optional


class AllocError(Exception):
    code = "alloc_error"

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _err(code: str, message: str, **details) -> AllocError:
    e = AllocError(message, details)
    e.code = code
    return e


class Preference:
    """一类可转优先份。"""

    def __init__(self, name: str, shares: Fraction, ratio: Fraction, fixed: Fraction,
                 order: int, convertible: bool = True):
        if shares <= 0:
            raise _err("bad_preference", f"{name} 份数必须为正")
        if ratio <= 0:
            raise _err("bad_preference", f"{name} 转股比例必须为正")
        if fixed < 0:
            raise _err("bad_preference", f"{name} 定额不能为负")
        self.name = name
        self.shares = Fraction(shares)
        self.ratio = Fraction(ratio)
        self.fixed = Fraction(fixed)
        self.order = order
        self.convertible = convertible

    def __repr__(self):
        return (f"Preference({self.name!r}, shares={self.shares}, ratio={self.ratio}, "
                f"fixed={self.fixed}, order={self.order})")


class ShareBook:
    """股东册：限四类权益（普通 + 至多三类优先）。"""

    def __init__(self, classes: dict, holdings: dict):
        """classes: {class_name: {kind:'common'|'preference', ratio, fixed, order, payable}}

        holdings: {holder: {class_name: shares}}
        """
        self.classes = classes
        self.holdings = {}
        if len(classes) > 4:
            raise _err("too_many_classes", "份类总数不得超过四类")
        self.common_names = []
        self.preferences: list[Preference] = []
        for name, spec in classes.items():
            kind = spec.get("kind", "common")
            if kind == "common":
                self.common_names.append(name)
            elif kind == "preference":
                self.preferences.append(Preference(
                    name, spec["shares"], spec.get("ratio", 1),
                    spec.get("fixed", 0), spec.get("order", len(self.preferences)),
                    spec.get("convertible", True),
                ))
            else:
                raise _err("bad_class", f"未知份类 {kind!r}")
        if not self.common_names:
            raise _err("no_common", "至少需要一类普通份")
        for holder, row in holdings.items():
            total = sum((Fraction(v) for v in row.values()), Fraction(0))
            if total <= 0:
                raise _err("bad_holding", f"{holder} 份额必须为正")
            for cls, shares in row.items():
                if cls not in classes:
                    raise _err("unknown_class", f"{holder} 持有未知类 {cls}")
            self.holdings[holder] = {k: Fraction(v) for k, v in row.items()}
        # 各类总份数须与声明一致
        for pref in self.preferences:
            held = sum((h.get(pref.name, Fraction(0)) for h in self.holdings.values()),
                       Fraction(0))
            if held != pref.shares:
                raise _err("share_count_mismatch",
                           f"类 {pref.name} 持有合计 {held} != 声明 {pref.shares}")

    # ------------------------------------------------------------------ pools
    def _convertible(self):
        return [p for p in self.preferences if p.convertible]

    def _choose_options(self) -> list[dict]:
        """枚举所有选择：每个可转类 cash/convert 的笛卡尔积。"""
        conv = self._convertible()
        if not conv:
            return [{"choice": {}}]
        options = []
        for bits in product(("cash", "convert"), repeat=len(conv)):
            options.append({"choice": {p.name: b for p, b in zip(conv, bits)}})
        return options

    # -------------------------------------------------------------- watering
    def allocate(self, total: Fraction, choice: dict) -> dict:
        """按一个具体选择分派；返回每人金额（Fraction）与该类总所得。

        相冲情形的合成处理：
          * 可转类若选 convert -> 整类转普通（等价份数 = 份数 × ratio）进入普通池；
            也可仍保留其"先取定额"权（本实现按题给可转类两选项互斥处理，convert
            即不再另取定额）。
          * 现金类和不可转类按登记顺序取定额；不足时"该类及后类"按份数比例分
            剩余的可分额（后到的类承受短缺）。
        """
        if total < 0:
            raise _err("bad_total", "可分总额不能为负")
        conv = self._convertible()
        chosen = {}
        for p in conv:
            opt = choice.get(p.name)
            if opt not in ("cash", "convert"):
                raise _err("bad_choice", f"类 {p.name} 选择必须是 cash/convert")
            chosen[p.name] = opt

        class_amounts = {p.name: Fraction(0) for p in self.preferences}
        cash_classes = [p for p in self.preferences
                        if not p.convertible or chosen.get(p.name) == "cash"]
        cash_classes.sort(key=lambda x: x.order)

        # --- 优先份定额：按登记顺序；不足时"该类及后类"按份数比例承受短缺 ---
        remaining = Fraction(total)
        for pos, p in enumerate(cash_classes):
            need = p.fixed * p.shares
            if remaining >= need:
                class_amounts[p.name] = need
                remaining -= need
            else:
                # 本类及所有后类按份数比例分摊剩余额度（后类承受短缺）
                tail = cash_classes[pos:]
                pool = sum((q.shares for q in tail), Fraction(0))
                for q in tail:
                    class_amounts[q.name] = remaining * q.shares / pool
                remaining = Fraction(0)
                break

        # --- 余款给普通池：普通份 + 转股等价份 ---
        common_pool_shares = Fraction(0)
        for h, row in self.holdings.items():
            for c in self.common_names:
                common_pool_shares += row.get(c, Fraction(0))
        convert_pool_shares = Fraction(0)
        for p in self.preferences:
            if p.convertible and chosen.get(p.name) == "convert":
                convert_pool_shares += p.shares * p.ratio
        pool = common_pool_shares + convert_pool_shares
        residual = remaining
        if pool == 0 and residual > 0:
            raise _err("no_residual_recipient",
                       "还有余款却无普通份/转股份可承接", residual=str(residual))
        per_share = residual / pool if pool > 0 else Fraction(0)

        out = {h: Fraction(0) for h in self.holdings}
        # 现金类：按份数比例分得该类定额
        for p in cash_classes:
            if p.shares == 0:
                continue
            for h, row in self.holdings.items():
                sh = row.get(p.name, Fraction(0))
                if sh:
                    out[h] += class_amounts[p.name] * sh / p.shares
        # 转股类：连同普通按份数精确分余款
        for p in self.preferences:
            if p.convertible and chosen.get(p.name) == "convert":
                class_amounts[p.name] = per_share * p.shares * p.ratio
                for h, row in self.holdings.items():
                    sh = row.get(p.name, Fraction(0))
                    if sh:
                        out[h] += per_share * sh * p.ratio
        # 普通份余款
        for h, row in self.holdings.items():
            for c in self.common_names:
                out[h] += per_share * row.get(c, Fraction(0))
        return {
            "total": total,
            "choice": chosen,
            "preferred_paid": {p.name: class_amounts[p.name] for p in self.preferences},
            "residual": residual,
            "per_share": per_share,
            "amounts": out,
            "class_amounts": class_amounts,
        }

    # --------------------------------------------------------------- stability
    def evaluate_all(self, total: Fraction) -> dict:
        """枚举全部选择，判定稳定选择并给出每人金额区间。"""
        options = self._choose_options()
        results = []
        conv = self._convertible()
        for opt in options:
            alloc = self.allocate(total, opt["choice"])
            results.append({"choice": opt["choice"], "amounts": alloc})
        # 稳定判定：对每个可转类，单独把该类的选择翻转，若重新分派后自己的所得
        # 严格增加，则该类"反转获益"，当前选择不稳定。
        stable = []
        for r in results:
            ok = True
            unstable_detail = []
            for p in conv:
                flipped = dict(r["choice"])
                flipped[p.name] = "convert" if r["choice"][p.name] == "cash" else "cash"
                alt = self.allocate(total, flipped)
                own = r["amounts"]["class_amounts"].get(p.name, Fraction(0))
                alt_own = alt["class_amounts"].get(p.name, Fraction(0))
                if alt_own > own:
                    ok = False
                    unstable_detail.append({
                        "class": p.name,
                        "from": r["choice"][p.name],
                        "to": flipped[p.name],
                        "own_now": own,
                        "own_flipped": alt_own,
                        "gain": alt_own - own,
                    })
            r["stable"] = ok
            r["unstable_by"] = unstable_detail
            if ok:
                stable.append(r)
        # 每人区间：只在稳定选择上取 min/max（区间只用于判断能否作保底承诺）
        holders = sorted(self.holdings)
        ranges = {}
        if stable:
            for h in holders:
                vals = [r["amounts"]["amounts"][h] for r in stable]
                ranges[h] = {"min": min(vals), "max": max(vals)}
        return {
            "total": total,
            "all_options": [
                {"choice": r["choice"], "amounts": r["amounts"]["amounts"],
                 "stable": r["stable"], "unstable_by": r["unstable_by"]}
                for r in results
            ],
            "stable_options": [
                {"choice": r["choice"], "amounts": r["amounts"]["amounts"]}
                for r in stable
            ],
            "ranges": ranges,
            "all_unstable": len(stable) == 0,
            # 无稳定选择时，每个被否选择交出一条获益偏离（一次单独反转即可获益）
            "rejected_deviations": [
                {"choice": r["choice"], "deviation": r["unstable_by"][0]}
                for r in results if not r["stable"]
            ] if not stable else [],
        }

    # ------------------------------------------------------------------ ipsum
    def guarded_promise(self, evaluation: dict, holder: str, promised: Fraction,
                        which_stable: Optional[int] = None) -> dict:
        """拟承诺必须 ≤ 每个稳定选择下该人所得，才"付得足"。"""
        stable = evaluation["stable_options"]
        if not stable:
            raise _err("no_stable_option", "无稳定选择，不能作保底承诺")
        mins = evaluation["ranges"][holder]["min"]
        if promised > mins:
            raise _err("promise_not_funded",
                       "承诺金额超过某些稳定选择下该人所得，付不足",
                       holder=holder, promised=promised, guaranteed_min=mins,
                       max_fundable=mins)
        if which_stable is not None:
            if which_stable < 0 or which_stable >= len(stable):
                raise _err("bad_stable_index", "稳定选择序号越界")
            chosen = stable[which_stable]
            amt = chosen["amounts"][holder]
            if promised > amt:
                raise _err("promise_not_funded", "在指定稳定方案下付不足",
                           holder=holder, promised=promised, that=amt)
            return {"holder": holder, "promised": promised, "stable_index": which_stable,
                    "stable_amount": amt, "funded": True}
        return {"holder": holder, "promised": promised, "guaranteed_min": mins,
                "funded": True}
