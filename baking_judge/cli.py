"""盲评/颁章/复尝系统的 JSON CLI（脚本化复跑用）。

子命令：

* ``score``        读轮次 JSON，输出各家均值与加权总分；
* ``card-solve``   读卡快照 JSON，输出解/矛盾/整桌补卡/单格查询；
* ``award``        读补卡+均值 JSON，输出颁章判定；
* ``plan``         读需求+机位+留样 JSON，输出最少复尝位排位；
* ``appeal``       读申诉场景 JSON，输出重判/作废/新复尝去向。

所有数值仍是精确分数；JSON 里的分数写成 ``"3/2"`` 字符串。错误以非零退出码 +
结构化 JSON 写到 stderr。
"""

import argparse
import json
import sys
from fractions import Fraction

from .award import decide_award, supporting_cards
from .cards import CardError, CardSystem
from .scores import round_supplier_means, weighted_totals
from .scheduling import Sample, Slot, plan_retaste


def _read(args):
    if args.payload == "-":
        return json.load(sys.stdin)
    with open(args.payload, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _frac_json(value):
    return None if value is None else str(value)


def _cmd_score(payload):
    rounds = []
    for rnd in payload["rounds"]:
        scores = {tuple(k.split("|", 1)): v for k, v in rnd["scores"].items()}
        rounds.append(round_supplier_means(
            rnd["judges"], scores, payload["suppliers"],
            recusals=rnd.get("recusals", [])))
    agg = weighted_totals(rounds, payload["weights"])
    per_round = [
        {sup: {"mean": _frac_json(e["mean"]),
               "needs_retaste": e["needs_retaste"]}
         for sup, e in r.items()}
        for r in rounds
    ]
    return {
        "rounds": per_round,
        "totals": {s: _frac_json(v) for s, v in agg["totals"].items()},
        "retaste_rounds": agg["retaste_rounds"],
    }


def _cmd_card_solve(payload):
    sysm = CardSystem()
    for var in payload["vars"]:
        sysm.add_var(var["name"], int(var["lo"]), int(var["hi"]))
    for rec in payload["records"]:
        sysm.add_record(rec["record_id"], [(v, c) for v, c in rec["terms"]],
                        rec["rhs"], source=rec.get("source"),
                        kind=rec.get("kind", "equation"))
    sol = sysm.solve()
    out = {
        "status": sol.status,
        "integer_feasible": sol.integer_ok,
        "integer_reason": sol.integer_reason,
        "integer_cards": sol.integer_cards,
        "contradictions": sol.contradictions,
        "free_vars": sol.free_vars,
    }
    if payload.get("query_cell"):
        out["cell_query"] = sysm.query_cell(payload["query_cell"])
    return out


def _cmd_award(payload):
    cards = []
    for item in payload["cards"]:
        cards.append({
            "card": {k: int(v) for k, v in item["card"].items()},
            "means": {s: (None if v is None else Fraction(v))
                      for s, v in item["means"].items()},
        })
    decision = decide_award(cards, payload.get("required_suppliers"))
    decision["supporting_cards"] = supporting_cards(decision, cards)
    # Fraction 不可 JSON 序列化：把可能残留的 Fraction 转字符串
    decision = json.loads(json.dumps(decision, default=str))
    return decision


def _cmd_plan(payload):
    slots = [Slot(s["slot_id"], s["session"], s["time"], s["capacity"], s["label"])
             for s in payload["slots"]]
    samples = [Sample(s["sample_id"], s["supplier"], s["label"], tuple(s["window"]),
                      s["last_session"], s.get("count", 1))
               for s in payload["samples"]]
    return plan_retaste(payload["required_suppliers"], slots, samples)


def _cmd_appeal(payload):
    from .appeal import Booking, Session, resolve_delayed_recusal

    scores = {tuple(k.split("|", 1)): v for k, v in payload["scores"].items()}
    session = Session(payload["session_id"], payload["judges"],
                      payload["suppliers"], scores, weight=payload.get("weight", 1))
    bookings = [
        Booking(b["supplier"], b["sample_id"], b["state"], b["slot_session"],
                b["slot_time"], b["slot_label"], b.get("with_recused_judge", False),
                b.get("count", 1))
        for b in payload.get("bookings", [])
    ]
    slots = [Slot(s["slot_id"], s["session"], s["time"], s["capacity"], s["label"])
             for s in payload.get("slots", [])]
    samples_by_supplier = {}
    for s in payload.get("samples", []):
        samples_by_supplier.setdefault(s["supplier"], []).append(
            Sample(s["sample_id"], s["supplier"], s["label"], tuple(s["window"]),
                   s["last_session"], s.get("count", 1)))
    res = resolve_delayed_recusal(
        session, payload["recused_judge"], existing_bookings=bookings,
        available_slots=slots, samples_by_supplier=samples_by_supplier)
    return json.loads(json.dumps(res, default=str))


def build_parser():
    p = argparse.ArgumentParser(prog="baking-judge")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("score", "card-solve", "award", "plan", "appeal"):
        sp = sub.add_parser(name)
        sp.add_argument("payload", help="JSON 文件，- 表示 stdin")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {
        "score": _cmd_score,
        "card-solve": _cmd_card_solve,
        "award": _cmd_award,
        "plan": _cmd_plan,
        "appeal": _cmd_appeal,
    }
    try:
        _emit(handlers[args.command](_read(args)))
    except (CardError, ValueError, KeyError) as exc:
        json.dump({"error": type(exc).__name__, "message": str(exc)},
                  sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
