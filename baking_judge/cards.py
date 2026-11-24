"""原公式快照 / 留样卡：精确线性方程 + 整数盒域。

数据模型
========

* :class:`VarSpec` —— 一个底分变量。``name`` 即**永久格号**：同一物理席位在多张卡上
  留同一个底分时，各卡记录引用**同一个**变量名，天然连接，不会复制出两份。
  每个底分限制在题定整数区间 ``[lo, hi]``（含端点）。
* :class:`Record` —— 读卡岗编入快照的一条原公式记录：行合计 / 列合计 / 已看清的
  单格 / 换桌互证。形状为 ``Σ coef·var = rhs``，``source`` 记录它来自哪些卡/行/签认，
  矛盾时用来回答“哪几张记录不能同时使用”。

求解复用旧 ``budget_solver`` 的精确有理数 RREF（``rref_augmented``，带行溯源）：
不引入任何浮点；矛���证书是原记录的 λ 组合；有自由度时保留参数表达式与可代回的
整桌联合补卡。
"""

from fractions import Fraction
from itertools import product

from budget_solver.rationals import parse_rational, qstr
from budget_solver.solver import rref_augmented


class CardError(Exception):
    """卡系统结构或数值非法。"""


class VarSpec:
    __slots__ = ("name", "lo", "hi")

    def __init__(self, name, lo, hi):
        if not isinstance(name, str) or not name:
            raise CardError("变量名必须是非空字符串")
        if isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, int) or not isinstance(hi, int):
            raise CardError(f"变量 {name} 的上下界必须是整数")
        if lo > hi:
            raise CardError(f"变量 {name} 的下界 {lo} 大于上界 {hi}")
        self.name = name
        self.lo = lo
        self.hi = hi

    def contains(self, value):
        return isinstance(value, int) and self.lo <= value <= self.hi

    def to_json(self):
        return {"name": self.name, "lo": self.lo, "hi": self.hi, "permanent_cell": self.name}


class Record:
    __slots__ = ("record_id", "terms", "rhs", "source", "kind")

    def __init__(self, record_id, terms, rhs, source=None, kind="equation"):
        if not isinstance(record_id, str) or not record_id:
            raise CardError("record_id 必须是非空字符串")
        if not terms:
            raise CardError(f"记录 {record_id} 至少需要一个项")
        norm = []
        for var, coef in terms:
            if not isinstance(var, str) or not var:
                raise CardError(f"记录 {record_id} 的变量名非法")
            norm.append((var, parse_rational(coef, field=f"{record_id}.coef")))
        self.record_id = record_id
        self.terms = norm
        self.rhs = parse_rational(rhs, field=f"{record_id}.rhs")
        # source 是“这条记录引用了哪些原始卡/行/签认”，用于矛盾定位
        self.source = list(source) if source else [record_id]
        self.kind = kind

    def to_json(self):
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "terms": [[v, qstr(c)] for v, c in self.terms],
            "rhs": qstr(self.rhs),
            "source": list(self.source),
        }


# --------------------------------------------------------------------- 求解结果
class CardSolution:
    """一次快照求解的分类结果。

    ``status`` 取值：

    * ``"determined"``：没有自由变量，唯一有理数解；再叠加整数盒检查后即整桌补卡。
    * ``"parametric"``：有自由度，保留参数表达式与可行整桌联合补卡集合。
    * ``"inconsistent"``：记录互相矛盾，附 λ 证书与冲突记录来源。

    ``integer_cards`` 只在一致且能枚举时给出；每个都是一份“整桌联合补卡”
    （所有变量都取整数值，可直接代回全部原记录）。
    """

    def __init__(self, status, var_order, expressions, free_vars, cards,
                 contradictions, integer_ok, integer_reason):
        self.status = status
        self.var_order = var_order
        self.expressions = expressions  # var -> {"constant": Fraction, "free": {var: Fraction}}
        self.free_vars = free_vars
        self.integer_cards = cards  # list[dict[str,int]]
        self.contradictions = contradictions
        self.integer_ok = integer_ok
        self.integer_reason = integer_reason

    def cell_values(self, name):
        """单查某格：返回该格在全部可行整桌补卡上出现过的整数值（排序去重）。"""
        return sorted({card[name] for card in self.integer_cards})

    def to_json(self):
        return {
            "status": self.status,
            "free_vars": list(self.free_vars),
            "expressions": {
                v: {
                    "constant": qstr(e["constant"]),
                    "free_terms": [[k, qstr(c)] for k, c in sorted(e["free"].items())],
                }
                for v, e in sorted(self.expressions.items())
            },
            "integer_cards": self.integer_cards,
            "integer_feasible": self.integer_ok,
            "integer_reason": self.integer_reason,
            "contradictions": self.contradictions,
        }


class CardSystem:
    def __init__(self):
        self.vars = {}
        self.records = []
        self._record_ids = set()

    # -------------------------------------------------------------- 构建
    def add_var(self, name, lo, hi):
        if name in self.vars:
            raise CardError(f"变量 {name} 重复定义（永久格号必须唯一）")
        self.vars[name] = VarSpec(name, lo, hi)
        return self

    def add_record(self, record_id, terms, rhs, source=None, kind="equation"):
        if record_id in self._record_ids:
            raise CardError(f"记录 {record_id} 重复")
        rec = Record(record_id, terms, rhs, source=source, kind=kind)
        for var, _ in rec.terms:
            if var not in self.vars:
                raise CardError(f"记录 {record_id} 引用了未定义的格 {var}")
        self._record_ids.add(record_id)
        self.records.append(rec)
        return self

    # ---------------------------------------------- 读卡岗常见记录类型（便捷构造）
    def add_row_sum(self, record_id, cell_coefs, total, source=None):
        """行合计：一组格的加权和等于给定合计。``cell_coefs``: [(var, coef), ...]."""
        return self.add_record(record_id, cell_coefs, total, source=source,
                               kind="row_sum")

    def add_column_sum(self, record_id, cell_coefs, total, source=None):
        """列合计：另一方向的合计（同一线性形状，单独打标签便于审计）。"""
        return self.add_record(record_id, cell_coefs, total, source=source,
                               kind="column_sum")

    def add_seen_cell(self, record_id, var, value, source=None):
        """已看清的单格底分。"""
        return self.add_record(record_id, [(var, 1)], value, source=source,
                               kind="seen_cell")

    def add_cross_check(self, record_id, var_a, var_b, offset=0, source=None):
        """换桌互证：两格之差等于给定 offset（``var_a - var_b = offset``）。"""
        return self.add_record(record_id, [(var_a, 1), (var_b, -1)], offset,
                               source=source, kind="cross_check")

    def records_by_kind(self, kind):
        return [rec for rec in self.records if rec.kind == kind]

    # -------------------------------------------------------------- 求解
    def _assemble(self, extra_records=()):
        var_order = sorted(self.vars)
        idx = {v: i for i, v in enumerate(var_order)}
        n = len(var_order)
        rows, rhs, origins, order = [], [], [], []
        for rec in list(self.records) + list(extra_records):
            row = [Fraction(0)] * n
            for var, coef in rec.terms:
                row[idx[var]] += coef
            rows.append(row)
            rhs.append(rec.rhs)
            origins.append({rec.record_id: Fraction(1)})
            order.append(rec)
        return var_order, idx, n, rows, rhs, origins, order

    def solve(self, max_cards=10000):
        var_order, idx, n, rows, rhs, origins, order = self._assemble()
        rr, rrhs, _pmat, rorig, pivot_cols, bad = rref_augmented(
            rows, rhs, [[] for _ in rows], origins
        )
        if bad:
            contradictions = []
            for r in bad:
                combo = [
                    {"record_id": rid, "lambda": qstr(lam)}
                    for rid, lam in sorted(rorig[r].items()) if lam != 0
                ]
                offending = [rid for rid, lam in sorted(rorig[r].items()) if lam != 0]
                sources = sorted({
                    s
                    for rid in offending
                    for s in self.record_map()[rid].source
                })
                contradictions.append({
                    "residual_lhs": "0",
                    "residual_rhs": qstr(rrhs[r]),
                    "record_combination": combo,
                    "conflicting_records": sorted(offending),
                    "conflicting_sources": sources,
                })
            return CardSolution("inconsistent", var_order, {}, [], [], contradictions,
                                False, "记录互相矛盾，无任何整桌解")
        pivot_set = set(pivot_cols)
        free_cols = [c for c in range(n) if c not in pivot_set]
        pivot_row_of = {col: r for r, col in enumerate(pivot_cols)}
        expressions = {}
        free_vars = [var_order[c] for c in free_cols]
        for col, name in enumerate(var_order):
            if col in free_cols:
                expressions[name] = {"constant": Fraction(0), "free": {name: Fraction(1)}}
                continue
            pr = pivot_row_of[col]
            free_part = {}
            for fc in free_cols:
                coef = -rr[pr][fc]
                if coef != 0:
                    free_part[var_order[fc]] = coef
            expressions[name] = {"constant": rrhs[pr], "free": free_part}
        status = "determined" if not free_cols else "parametric"
        cards, ok, reason = self._enumerate_integers(expressions, free_vars, max_cards)
        return CardSolution(status, var_order, expressions, free_vars, cards,
                            [], ok, reason)

    def _enumerate_integers(self, expressions, free_vars, max_cards):
        if not free_vars:
            card = {}
            for name in self.vars:
                val = expressions[name]["constant"]
                if val.denominator != 1:
                    return [], False, f"格 {name} 唯一解 {qstr(val)} 不是整数"
                if not self.vars[name].contains(int(val)):
                    return [], False, (
                        f"格 {name} 唯一解 {int(val)} 超出整数盒 "
                        f"[{self.vars[name].lo},{self.vars[name].hi}]"
                    )
                card[name] = int(val)
            return [card], True, "唯一整桌补卡"

        domains = []
        for fv in free_vars:
            spec = self.vars[fv]
            domains.append(list(range(spec.lo, spec.hi + 1)))
        total = 1
        for d in domains:
            total *= len(d)
        if total > max_cards:
            return [], False, f"自由整数组合 {total} 超过上限 {max_cards}，无法枚举整桌补卡"
        cards = []
        for combo in product(*domains):
            assign = dict(zip(free_vars, combo))
            ok = True
            for name in self.vars:
                expr = expressions[name]
                val = expr["constant"] + sum(
                    c * Fraction(assign[fv]) for fv, c in expr["free"].items()
                )
                if val.denominator != 1 or not self.vars[name].contains(int(val)):
                    ok = False
                    break
                assign[name] = int(val)
            if ok:
                cards.append({k: assign[k] for k in self.vars})
        if not cards:
            return [], False, "整数盒内不存在满足全部记录的整桌补卡"
        return cards, True, f"共 {len(cards)} 份可行整桌联合补卡"

    # -------------------------------------------------------------- 查询
    def record_map(self):
        return {rec.record_id: rec for rec in self.records}

    def query_cell(self, name):
        """单查某格分：唯一值就报值；有自由度就报全部可行整数取值。"""
        if name not in self.vars:
            raise CardError(f"未知格 {name}")
        sol = self.solve()
        if sol.status == "inconsistent":
            return {"cell": name, "status": "inconsistent",
                    "contradictions": sol.contradictions}
        values = sol.cell_values(name)
        if not sol.integer_ok:
            return {"cell": name, "status": "no_integer_card", "reason": sol.integer_reason}
        if len(values) == 1:
            return {"cell": name, "status": "determined", "value": values[0]}
        return {"cell": name, "status": "free", "possible_values": values}

    def same_table(self, pin_a, pin_b, max_cards=10000):
        """两份补卡能否出自同一桌：合并钉住的格，再看是否存在一致整桌解。

        返回 ``(boolean, reason, witness_or_None)``。两份补卡若在同一格上取值不同，
        或合并后与快照原记录/整数盒冲突，则不能同出一桌。
        """
        merged = dict(pin_a)
        for var, val in pin_b.items():
            if var in merged and merged[var] != val:
                return False, f"格 {var} 在两份补卡中分别取 {merged[var]} 与 {val}", None
            merged[var] = val
        for var, val in merged.items():
            if var not in self.vars:
                return False, f"补卡引用未知格 {var}", None
            if not isinstance(val, int) or isinstance(val, bool):
                return False, f"格 {var} 的补卡值 {val!r} 不是整数", None
            if not self.vars[var].contains(val):
                return False, f"格 {var}={val} 超出整数盒", None
        ok, witness, reason = self._complete_integer(merged, max_cards)
        return ok, reason, witness

    def _complete_integer(self, fixed, max_cards):
        extra = [
            Record(f"__pin__{var}", [(var, 1)], str(val), source=[f"pin:{var}"])
            for var, val in sorted(fixed.items())
        ]
        saved = self.records
        try:
            self.records = list(saved) + extra
            sol = self.solve(max_cards=max_cards)
        finally:
            self.records = saved
        if sol.status == "inconsistent":
            return False, None, "合并钉住后与快照原记录矛盾"
        if not sol.integer_ok:
            return False, None, sol.integer_reason
        # 取一份与 fixed 一致（构造上必然一致）的整桌补卡作为见证
        return True, sol.integer_cards[0], "存在一致整桌补卡"

    def build_table(self, pinned=None, max_cards=10000):
        """构造一份**可代回的整桌联合补卡**，绝不容许把各格上下界拼成一张。

        * ``pinned`` 指定某些格的取值；其余格由联立记录 + 整数盒共同确定。
        * 若记录把某格只约束到区间（自由变量），本方法会在可行整桌补卡集合里取
          一致的一份，而**不会**独立地为每格挑一个边界值拼起来。
        * 若不存在任何一致整桌补卡，抛 :class:`CardError` 并附原因——这正是
          “不能把每人分数的上下界随意拼成一张”的强制点。
        """
        pinned = dict(pinned or {})
        sol = self.solve(max_cards=max_cards)
        if sol.status == "inconsistent":
            raise CardError(
                "记录矛盾，无法构造整桌补卡："
                + "; ".join(c["residual_rhs"] for c in sol.contradictions)
            )
        if not sol.integer_ok:
            raise CardError(f"无法构造整桌补卡：{sol.integer_reason}")
        for card in sol.integer_cards:
            if all(card.get(var) == val for var, val in pinned.items()):
                return card
        raise CardError("没有与指定钉住值一致的整桌补卡（不能拼凑边界值）")

    def bounds_only(self):
        """仅按各格整数盒给出每格的独立上下界——**不是**一张可代回的补卡。

        返回 ``{var: (lo, hi)}``；仅供审计/展示，明确标为不可当整桌补卡使用。
        """
        return {name: (spec.lo, spec.hi) for name, spec in self.vars.items()}
