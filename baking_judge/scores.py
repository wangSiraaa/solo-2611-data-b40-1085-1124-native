"""轮次记分：整席回避 → 每家分别去一高一低 → 恰两位取均值 → 一位以下重尝。

规则来源（题面）：

* 每轮**先整席删除**有利益冲突的评委对**所有**供应社的分（不是只删某一家）；
* 再按每家**分别**去掉一个最高、一个最低，取其余平均；
* 恰好两位合格时直接取均值（不降为“去一高一低”后只有 0 位）；
* 一位以下该轮**必须重尝**，本轮该家无有效均值；
* 各轮按指定权重相加。

所有均值用 :class:`fractions.Fraction` 精确计算。
"""

from fractions import Fraction

from budget_solver.rationals import parse_rational, qstr

MAX_SEATS = 4
MAX_SUPPLIERS = 3


def _rational(value, field="value"):
    """统一接受 int / 分数字符串 / Fraction，返回 Fraction（不引入浮点）。"""
    if isinstance(value, Fraction):
        return value
    return parse_rational(value, field=field)


def trimmed_mean(scores):
    """按题面规则对单个供应社、单轮的合格分求均值。

    * ``len>=3``：排序后去掉一个最高、一个最低，其余平均；
    * ``len==2``：直接取均值；
    * ``len<=1``：返回 ``None``，表示该轮必须重尝。
    """
    values = [_rational(s, field="score") for s in scores]
    n = len(values)
    if n <= 1:
        return None
    if n == 2:
        return (values[0] + values[1]) / 2
    values.sort()
    kept = values[1:-1]
    return sum(kept, Fraction(0)) / len(kept)


def _recusal_key(judge, supplier=None):
    if isinstance(judge, dict):
        return (judge["judge"], judge.get("supplier"))
    return (judge, supplier)


def round_supplier_means(judges, scores, suppliers, recusals=()):
    """计算一轮中每家供应社的均值。

    参数
    ----
    judges : list[str]
        本轮到场的席位/评委（整席）。
    scores : dict[(judge, supplier) -> rational]
        原始打分格。
    suppliers : list[str]
    recusals : iterable
        回避项。元素可以是 ``("J1", None)``（J1 回避**所有**供应社，整席删除）、
        ``"J1"``（同上），或 ``("J1", "甲")``（J1 仅对甲回避；题面要求整席删除时
        应使用整席形式，此处保留细分以便审计）。

    返回 ``{supplier: {"mean": Fraction|None, "qualified_scores": [...],
    "needs_retaste": bool, "dropped": (low, high)|None}}``。
    只要该家 needs_retaste，该轮就不是完整有效轮。
    """
    recused_all = set()
    recused_pair = set()
    for item in recusals:
        judge, sup = _recusal_key(item) if not isinstance(item, str) else (item, None)
        if sup is None:
            recused_all.add(judge)
        else:
            recused_pair.add((judge, sup))

    result = {}
    for sup in suppliers:
        qualified = []
        for judge in judges:
            if judge in recused_all or (judge, sup) in recused_pair:
                continue
            key = (judge, sup)
            if key not in scores:
                continue
            qualified.append(_rational(scores[key], field=f"{judge}/{sup}"))
        mean = trimmed_mean(qualified) if qualified else None
        dropped = None
        if len(qualified) >= 3:
            ordered = sorted(qualified)
            dropped = (ordered[0], ordered[-1])
        result[sup] = {
            "mean": mean,
            "qualified_scores": qualified,
            "needs_retaste": mean is None,
            "dropped": dropped,
        }
    return result


def weighted_totals(round_means, weights):
    """把多轮的各家均值按权重相加。

    ``round_means``：list，每个元素是 :func:`round_supplier_means` 的返回。
    ``weights``：与轮次等长的权重（精确有理数）。

    若某家在任意一轮 ``needs_retaste``，则该家总分为 ``None`` 且整轮权重提示
    该轮必须补做有效复尝；不会用残值凑一个总分。
    """
    if len(round_means) != len(weights):
        raise ValueError("轮次数与权重数不一致")
    suppliers = sorted(round_means[0]) if round_means else []
    weights = [_rational(w, field="weight") for w in weights]
    totals, retaste_rounds = {}, {}
    for sup in suppliers:
        total = Fraction(0)
        bad = []
        for index, (means, weight) in enumerate(zip(round_means, weights)):
            entry = means.get(sup)
            if entry is None or entry["needs_retaste"]:
                bad.append(index)
                continue
            total += weight * entry["mean"]
        if bad:
            totals[sup] = None
            retaste_rounds[sup] = bad
        else:
            totals[sup] = total
    return {"totals": totals, "retaste_rounds": retaste_rounds,
            "weights": [str(w) for w in weights]}


def validate_session(judges, suppliers):
    if len(judges) > MAX_SEATS:
        raise ValueError(f"每场至多 {MAX_SEATS} 席，实际 {len(judges)}")
    if len(set(judges)) != len(judges):
        raise ValueError("同一席位在一场中出现两次")
    if len(suppliers) > MAX_SUPPLIERS:
        raise ValueError(f"每场至多 {MAX_SUPPLIERS} 家供应社，实际 {len(suppliers)}")
    if len(set(suppliers)) != len(suppliers):
        raise ValueError("供应社重复")
    return True
