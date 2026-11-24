"""申诉：迟来回避先重判原始有效票，再为仍有资格者排有效复尝。

时序语义（题面）：

* 申诉可在某场**已经开样之后**才确认一席应回避；
* **先重判原始有效票**：整席删除该回避席对**所有**供应社的分，重算该轮均值；
* 再只为**仍有资格者**（重判后 haven't lost / still needs retaste）安排有效的复尝；
* 与该回避席**同场开出**的复尝结果**作废**（采样时该席仍被当作合格而出现在场，
  其在场污染了该复尝）——无论是否来得及重判，只要那次复尝是在该回避席同桌时
  开出的，结果不可信；
* **已经吃掉的留样无法收回**（份数已耗），未开的订位可以改。

关键区分：一份留样可能只是**订了座**（booking，未开封）或**已经开样**（opened，
份数已真正耗掉）。同样的旧名次与预约数，只因这两个时刻不同，实际复尝去向会不同。

申诉未决时的耗样政策（本系统自定，可运行）
==========================================
``PENDING_POLICY``：

* 申诉**未决**时，**只有保管台（custodian）能真正耗掉一份留样**并开封——任何
  评委、供应社或场务的临时请求都只能得到“订座（booking）”，不得开封；
* 若某家**唯一留样已在申诉未决期间被开封耗掉**（例如与回避席同场的那次复尝），
  事后确认回避也只能把该次结果作废，**无法补尝**；
* 缺样时的可履行性判定见 :func:`viability`：区分“有样可排（viable）”“无样（no
  sample）”“有样但全被污染/超保鲜期（unusable）”，并据此决定还能否履行。
"""

from fractions import Fraction

from .scores import round_supplier_means

# 申诉未决期唯一能真正耗掉留样的角色
PENDING_POLICY = {
    "may_consume_while_pending": {"custodian"},
    "allowed_states_for_others": {"booked"},
    "note": "申诉未决时非保管台只能订座，不能开封耗样；开封须由保管台执行并留痕。",
}


def who_may_consume(pending=True, actor="custodian"):
    """申诉未决时，谁能把一份留样真正耗掉。返回 ``(allowed, reason)``。"""
    if not pending:
        return True, "申诉已决，按常规开样"
    if actor in PENDING_POLICY["may_consume_while_pending"]:
        return True, "保管台可在未决期开封耗样（留痕）"
    return False, f"申诉未决：{actor} 只能订座，不能开封耗样"


def viability(supplier, samples, slots=None, burned_ids=()):
    """缺样/污染情形下，判断某家还能否履行有效复尝。

    返回 dict：``status`` ∈ {``viable``, ``no_sample``, ``unusable``}，
    ``detail`` 说明原因。``burned_ids`` 是已作废/已耗且结果无效的留样 id。
    """
    own = [s for s in samples if getattr(s, "supplier", None) == supplier]
    if not own:
        return {"status": "no_sample", "detail": "该家没有任何留样"}
    alive = [s for s in own
             if getattr(s, "sample_id", None) not in burned_ids
             and getattr(s, "count", 0) > 0]
    if not alive:
        return {"status": "no_sample",
                "detail": "全部留样已开封耗掉且结果作废，无法收回"}
    if slots is not None:
        placeable = [s for s in alive
                     if any(s.available_at(slot) for slot in slots)]
        if not placeable:
            return {"status": "unusable",
                    "detail": "留样均在全部机位下超保鲜期/标签不符/超最后班次"}
    return {"status": "viable", "detail": "仍有可用的留样与机位"}


class Booking:
    """一份留样的订位/开样状态。

    ``state``：``"booked"``（只订座、未开封，可改）或 ``"opened"``（已开封，份数已耗）。
    ``slot_session``/``slot_label``：所在复尝位；``with_recused_judge``：该位是否与
    现已确认的回避席同桌（同场开出）。
    """

    __slots__ = ("supplier", "sample_id", "state", "slot_session", "slot_time",
                 "slot_label", "with_recused_judge", "count")

    def __init__(self, supplier, sample_id, state, slot_session, slot_time,
                 slot_label, with_recused_judge=False, count=1):
        if state not in ("booked", "opened"):
            raise ValueError("state 只能是 booked 或 opened")
        self.supplier = supplier
        self.sample_id = sample_id
        self.state = state
        self.slot_session = slot_session
        self.slot_time = slot_time
        self.slot_label = slot_label
        self.with_recused_judge = with_recused_judge
        self.count = count

    @property
    def consumed(self):
        """已吃掉的留样无法收回。"""
        return self.state == "opened"

    @property
    def void(self):
        """与该回避席同场开出的复尝结果作废。"""
        return self.state == "opened" and self.with_recused_judge

    def to_json(self):
        return {
            "supplier": self.supplier,
            "sample_id": self.sample_id,
            "state": self.state,
            "slot": {"session": self.slot_session, "time": self.slot_time,
                     "label": self.slot_label},
            "with_recused_judge": self.with_recused_judge,
            "consumed": self.consumed,
            "count": self.count,
        }


class Session:
    """一场（一轮）：在场席位、各供应社原始分、回避名单、权重。"""

    def __init__(self, session_id, judges, suppliers, scores, recusals=(),
                 weight=1):
        self.session_id = session_id
        self.judges = list(judges)
        self.suppliers = list(suppliers)
        self.scores = dict(scores)  # (judge, supplier) -> rational
        self.recusals = list(recusals)
        self.weight = weight

    def round_means(self, extra_recusals=()):
        all_recusals = list(self.recusals) + list(extra_recusals)
        return round_supplier_means(
            self.judges, self.scores, self.suppliers, recusals=all_recusals
        )


def _rank(means):
    """对 {sup: mean|None} 排名；None 视为无有效均值（不可颁章）。"""
    valid = {s: m for s, m in means.items() if m is not None}
    if not valid:
        return []
    ordered = sorted(valid.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered


def resolve_delayed_recusal(session, recused_judge,
                            existing_bookings=(),
                            available_slots=(),
                            samples_by_supplier=None,
                            other_round_means=None,
                            weights=None,
                            planner=None):
    """申诉在某场已开样后确认 ``recused_judge`` 应回避。

    步骤：

    1. **先重判原始有效票**：整席删除该席对全部供应社的分，重算本轮均值；
    2. **作废同场复尝**：``with_recused_judge`` 且已开封的 booking → void（结果弃用，
       留样已耗无法收回）；只订座未开的同场位可改；
    3. **为仍有资格者安排有效复尝**：对重判后 ``needs_retaste`` 的家，用剩余可用
       机位/份数重新排位；若某家已把唯一留样吃掉且结果作废 → 标明**无法补尝**；
    4. 结合 ``other_round_means``/``weights`` 给最终去向。

    返回结构化报告，含 ``rejudged_means``、``void_retastes``、``new_retaste_plan``、
    ``uncompensable``、``standings``、``award`` 等。
    """
    # --- 1. 重判原始有效票
    rejudged = session.round_means(extra_recusals=[recused_judge])
    means = {sup: entry["mean"] for sup, entry in rejudged.items()}
    needs = sorted(s for s, e in rejudged.items() if e["needs_retaste"])

    # --- 2. 现有 booking 分类
    consumed = []
    void_retastes = []
    recoverable_booked = []
    for b in existing_bookings:
        if b.void:
            void_retastes.append(b)
            # 留样已吃，无法收回
            consumed.append(b)
        elif b.consumed and not b.with_recused_judge:
            # 已开但未污染：结果有效
            continue
        elif b.state == "booked":
            recoverable_booked.append(b)

    # --- 3. 为仍 needs_retaste 的家安排有效复尝
    samples = []
    if samples_by_supplier:
        for sup, lst in samples_by_supplier.items():
            samples.extend(lst)
    # 已作废/被污染且已开封的留样不再可用：从候选中剔除
    burned_sample_ids = {b.sample_id for b in consumed}
    usable_samples = [
        s for s in samples
        if getattr(s, "sample_id", None) not in burned_sample_ids
    ]

    new_plan = None
    uncompensable = {}
    viability_report = {}
    if needs:
        if planner is None:
            from .scheduling import plan_retaste
            planner = plan_retaste
        new_plan = planner(needs, list(available_slots), usable_samples)
        if not new_plan.get("ok"):
            for sup in new_plan.get("missing", []):
                v = viability(sup, samples, list(available_slots), burned_sample_ids)
                viability_report[sup] = v
                if v["status"] == "no_sample":
                    uncompensable[sup] = v["detail"]
                elif v["status"] == "unusable":
                    uncompensable[sup] = v["detail"]
                else:
                    # 有样可排但整组容量凑不齐
                    uncompensable[sup] = new_plan.get("missing_detail", {}).get(
                        sup, "无有效机位可排"
                    )
        else:
            for sup in needs:
                viability_report[sup] = viability(
                    sup, samples, list(available_slots), burned_sample_ids
                )

    # --- 4. 合并其他轮，给最终去向
    standings = None
    award = None
    if other_round_means is not None and weights is not None:
        rounds = list(other_round_means) + [rejudged]
        w = list(weights)
        if len(w) != len(rounds):
            raise ValueError("权重数与轮次数不一致")
        from .scores import weighted_totals
        agg = weighted_totals(rounds, w)
        totals = agg["totals"]
        # 重判后仍无有效均值的家，总分不可定
        for sup in needs:
            totals[sup] = None
        standings = {
            "totals": {s: (None if v is None else Fraction(v)) for s, v in totals.items()},
            "retaste_rounds": agg["retaste_rounds"],
        }
        vals = {s: v for s, v in totals.items() if v is not None}
        if len(vals) == len(totals) and vals:
            top = max(vals.values())
            top_sups = sorted(s for s, v in vals.items() if v == top)
            award = {"can_award": len(top_sups) == 1, "top": top_sups,
                     "values": {s: str(vals[s]) for s in sorted(vals)}}
        else:
            award = {"can_award": False, "top": [],
                     "reason": "存在重判后仍无有效均值的家，需有效复尝"}

    return {
        "session_id": session.session_id,
        "recused_judge": recused_judge,
        "rejudged_means": {s: (None if m is None else m) for s, m in means.items()},
        "needs_retaste": needs,
        "void_retastes": [b.to_json() for b in void_retastes],
        "consumed_samples": [b.to_json() for b in consumed],
        "recoverable_bookings": [b.to_json() for b in recoverable_booked],
        "new_retaste_plan": new_plan,
        "uncompensable": uncompensable,
        "viability": viability_report,
        "standings": standings,
        "award": award,
    }
