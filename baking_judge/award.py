"""颁章判定：只有每份合法补卡都选出**同一个严格最高者**才直接颁章。

输入是一组“合法补卡”以及每份补卡下各供应社的（可能为空的）总分。合法补卡来自
:class:`baking_judge.cards.CardSolution.integer_cards`；每份补卡都代回全部原记录。
某家在某份补卡里无有效均值（该轮需重尝）时，这份补卡**不能**直接决出该家，
因此也不能直接把章颁给它。

结果分三种：

* ``award``：每份合法补卡都选出同一严格最高者 → 直接颁章；
* ``no_award``：不能直接颁章。给出可能并列/夺章者、**真正支持**它们的补卡，
  并订最少复尝位覆盖这些供应社；
* ``undecidable_empty``：没有任何合法补卡（矛盾/无整数解）。
"""

from fractions import Fraction

from budget_solver.rationals import qstr


def _winner_within(means):
    """单一补卡下：返回 (winner_or_None, top_tied_suppliers, valid)。

    若任一家 ``None``（需重尝），该补卡不能决出最高者，``valid=False``。
    否则若最高者唯一 → winner；若并列最高 → winner=None 并给出并列者。
    """
    valid = True
    best = None
    tied = []
    for sup, val in means.items():
        if val is None:
            valid = False
            continue
        if best is None or val > best:
            best = val
            tied = [sup]
        elif val == best:
            tied.append(sup)
    if not valid:
        return None, [], False
    if len(tied) == 1:
        return tied[0], tied, True
    return None, tied, True


def decide_award(cards_with_means, required_suppliers=None):
    """判定能否直接颁章。

    ``cards_with_means``：list of dict，每项形如
    ``{"card": {var: int}, "means": {supplier: Fraction|None}}``。

    返回 dict（见模块 docstring）。``contesting`` 是需要复尝才能分出高下的供应社
    集合：所有“在任一合法补卡里可能夺冠或并列夺冠”的供应社；若存在需重尝的补卡，
    则所有在该补卡中无均值、但其上界可能超过当前最高者的供应商也应纳入——本系统
    保守地把**出现过的全部候选**都纳入 ``contesting``。
    """
    n = len(cards_with_means)
    if n == 0:
        return {
            "decision": "undecidable_empty",
            "reason": "没有任何合法整桌补卡（记录矛盾或无整数解），无法颁章",
            "winner": None,
            "contesting": sorted(required_suppliers or []),
            "support": {},
            "award_support": [],
            "retaste_required": bool(required_suppliers),
        }

    if required_suppliers is None:
        suppliers = set()
        for c in cards_with_means:
            suppliers.update(c["means"])
        required_suppliers = sorted(suppliers)
    suppliers = list(required_suppliers)

    winners = []
    tie_sets = []      # list of (card_index, [tied suppliers])
    invalid_cards = []
    support = {sup: [] for sup in suppliers}
    # “真正支持”= 在该补卡里该家是**严格**最高者；并列/缺均值不算严格支持
    strict_support = {sup: [] for sup in suppliers}
    award_support = []
    for index, entry in enumerate(cards_with_means):
        means = entry["means"]
        winner, tied, valid = _winner_within(means)
        if not valid:
            invalid_cards.append(index)
            continue
        if winner is not None:
            winners.append(winner)
            strict_support[winner].append(index)
            award_support.append({"card_index": index, "winner": winner,
                                  "card": entry["card"]})
        else:
            tie_sets.append((index, tied))

    # contesting：任何在合法补卡里“可能单独夺冠（严格）”的家，或在并列/重尝补卡
    # 中仍处于最高一档/可能反超的家。这里取“严格支持过或在并列里出现过”的并集。
    ever_tied = {s for _, ts in tie_sets for s in ts}
    contesting = sorted({s for s in suppliers
                         if strict_support[s] or s in ever_tied})
    # 有补卡需重尝时，其余正常候选也应保留在 contesting 里（它们仍可能夺冠）
    if invalid_cards:
        for sup in suppliers:
            if any(c["means"].get(sup) is not None for c in cards_with_means):
                if sup not in contesting:
                    contesting.append(sup)
        contesting = sorted(contesting)

    support = {s: strict_support[s] for s in contesting}
    # 有并列或重尝补卡时永远不能直接颁章
    strictly_consistent = (
        not invalid_cards
        and not tie_sets
        and len(set(winners)) == 1
        and len(winners) == n
    )

    if strictly_consistent:
        only = winners[0]
        return {
            "decision": "award",
            "reason": f"每份合法补卡都严格选出同一最高者 {only}",
            "winner": only,
            "contesting": [only],
            "winner_value": qstr(cards_with_means[0]["means"][only]),
            "support": support,
            "award_support": award_support,
            "retaste_required": False,
        }

    # 归纳可能并列 / 夺章者与原因
    reasons = []
    if tie_sets:
        reasons.append({"kind": "tie", "cards": [
            {"card_index": ci, "tied": ts} for ci, ts in tie_sets
        ]})
    if len(set(winners)) > 1:
        reasons.append({"kind": "different_winners",
                        "winners": sorted(set(winners))})
    if invalid_cards:
        reasons.append({"kind": "needs_retaste",
                        "cards": invalid_cards})
    return {
        "decision": "no_award",
        "reason": "并非每份合法补卡都严格选出同一最高者，不能直接颁章",
        "winner": None,
        "contesting": contesting,
        "support": {s: support[s] for s in contesting},
        "award_support": award_support,
        "retaste_required": True,
        "reasons": reasons,
    }


def supporting_cards(decision, cards_with_means):
    """把“真正支持”各候选的补卡索引换成可读的补卡内容。"""
    out = {}
    for sup, indices in decision.get("support", {}).items():
        out[sup] = [cards_with_means[i]["card"] for i in indices]
    return out
