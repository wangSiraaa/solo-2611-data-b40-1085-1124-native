"""成绩席：报名表、放飞时刻与题定航距下的团体达速证裁决。

裁决规则（对一队指定两羽、队定门槛、关棚时刻）：

* 全部相容整棚履历中两羽均在关棚前到棚且 航距/实际耗时 均达门槛 ⇒ 盖章；
* 全部相容履历均不满足 ⇒ 撤证；
* 其余 ⇒ 待核，并交相反的两份整棚见证（一份满足、一份不满足）。
* 钟簿未封棚（如截断尾部）不出裁；报文自相矛盾（无任何相容履历）单列。

盖章/撤证同样保留两份具名整棚见证，供有权的钟房内部复核；发给队伍的
回执只透露该队裁决、覆盖它的公开过梁号及裁决依据，对手脚环一律匿名。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from fractions import Fraction
from typing import Iterable

from .book import MAX_PIGEONS, ClockBook
from .engine import EMPTY, History, QueueInconsistent, enumerate_histories, identity_name

OUTCOME_STAMP = "盖章"
OUTCOME_REVOKE = "撤证"
OUTCOME_PENDING = "待核"
OUTCOME_UNSEALED = "未封棚"
OUTCOME_INCONSISTENT = "记录矛盾"


@dataclass(frozen=True)
class PigeonCard:
    """报名表中一羽：脚环、放飞时刻、题定航距。"""

    ring: bytes
    release: int
    distance: int


@dataclass(frozen=True)
class FlightCard:
    """一棚一航次的题定数据：关棚时刻与各鸽放飞时刻、航距。"""

    loft: int
    close_time: int
    pigeons: tuple[PigeonCard, ...]

    def universe(self) -> list[bytes]:
        return [p.ring for p in self.pigeons]

    def card_of(self, ring: bytes) -> PigeonCard:
        for p in self.pigeons:
            if p.ring == ring:
                return p
        raise KeyError(f"脚环 {identity_name(ring)} 不在本航次报名表中")


@dataclass(frozen=True)
class Registration:
    """一队为某评比报的名：指定两羽与队定门槛；互斥评比填同一组名。"""

    team: str
    evaluation: str
    pair: tuple[bytes, bytes]
    threshold: Fraction
    exclusive_group: str | None = None


def criterion(history: History, card: FlightCard, reg: Registration) -> bool:
    """一份履历下该队是否达成：两羽均在关棚前到棚且均达队定门槛。"""
    arrivals = history.arrivals()
    for ring in reg.pair:
        if ring not in arrivals:
            return False
        arrived = arrivals[ring]
        if arrived > card.close_time:
            return False
        pigeon = card.card_of(ring)
        used = arrived - pigeon.release
        if used <= 0:
            return False
        if Fraction(pigeon.distance, used) < reg.threshold:
            return False
    return True


@dataclass
class Witness:
    """一份具名整棚见证（内部复核用）。"""

    satisfies: bool
    history: History

    def as_dict(self) -> dict:
        return {
            "satisfies": self.satisfies,
            "rows": self.history.as_rows(),
            "arrivals": {
                identity_name(r): t for r, t in self.history.arrivals().items()
            },
        }


@dataclass
class Decision:
    outcome: str
    registration: Registration
    card: FlightCard
    book_key: str
    history_count: int = 0
    satisfy_count: int = 0
    witnesses: list[Witness] = dc_field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "team": self.registration.team,
            "evaluation": self.registration.evaluation,
            "book": self.book_key,
            "history_count": self.history_count,
            "satisfy_count": self.satisfy_count,
            "reason": self.reason,
            "witnesses": [w.as_dict() for w in self.witnesses],
        }


def evaluate(
    histories: list[History], card: FlightCard, reg: Registration
) -> tuple[list[History], list[History]]:
    sat = [h for h in histories if criterion(h, card, reg)]
    return sat, [h for h in histories if not criterion(h, card, reg)]


def decide(
    book: ClockBook,
    card: FlightCard,
    reg: Registration,
    *,
    constraints=None,
    max_pigeons: int = MAX_PIGEONS,
) -> Decision:
    """对一项报名作出达速证裁决。"""
    if len(card.pigeons) > max_pigeons:
        raise ValueError(f"在册 {len(card.pigeons)} 羽超过上限 {max_pigeons}（限六羽）")
    base = Decision(outcome="", registration=reg, card=card, book_key=book.key)
    if not book.sealed:
        base.outcome = OUTCOME_UNSEALED
        base.reason = "钟簿未封棚（截断尾部不得作为已封棚），不出裁"
        return base
    try:
        histories = enumerate_histories(book, card.universe(), constraints)
    except QueueInconsistent as exc:
        base.outcome = OUTCOME_INCONSISTENT
        base.reason = str(exc)
        return base
    if not histories:
        base.outcome = OUTCOME_INCONSISTENT
        base.reason = "梁与天线记次无任何相容整棚履历"
        return base
    sat, unsat = evaluate(histories, card, reg)
    base.history_count = len(histories)
    base.satisfy_count = len(sat)
    if not unsat:
        base.outcome = OUTCOME_STAMP
        base.reason = "全部相容整棚履历均达队定门槛"
        base.witnesses = [Witness(True, h) for h in sat[:2]]
    elif not sat:
        base.outcome = OUTCOME_REVOKE
        base.reason = "全部相容整棚履历均不达队定门槛"
        base.witnesses = [Witness(False, h) for h in unsat[:2]]
    else:
        base.outcome = OUTCOME_PENDING
        base.reason = "相容履历中满足与不满足并存，交相反的两份整棚见证"
        base.witnesses = [Witness(True, sat[0]), Witness(False, unsat[0])]
    return base


# ---------------------------------------------------------------------------
# 队伍回执（匿名化）

def anonymized_rows(
    witness: Witness, own_rings: set[bytes], anon: dict[bytes, str]
) -> list[dict]:
    """整棚见证的队伍视图：本队具名，他队脚环以匿名位置区分。"""
    rows = []
    for token in witness.history.tokens:
        identity = witness.history.assignment[token.index]
        if identity is EMPTY:
            label = "空环"
        elif identity in own_rings:
            label = identity_name(identity)
        else:
            label = anon.setdefault(identity, f"外队位{len(anon) + 1}")
        rows.append(
            {
                "token": token.index,
                "identity": label,
                "entry_ordinal": token.entry.ordinal,
                "exit_ordinal": None if token.exit is None else token.exit.ordinal,
                "exit_time": None if token.exit is None else token.exit.time,
            }
        )
    return rows


def make_receipt(
    *,
    registration: Registration,
    card: FlightCard,
    outcome: str,
    witnesses: list[Witness],
    public_crossings: list[dict],
) -> dict:
    """发给队伍的回执：只含该队裁决、覆盖它的公开过梁号及裁决依据。"""
    own = set(registration.pair)
    anon: dict[bytes, str] = {}
    basis = []
    for i, witness in enumerate(witnesses):
        arrivals = witness.history.arrivals()
        pair_rows = []
        for ring in registration.pair:
            pigeon = card.card_of(ring)
            arrived = arrivals.get(ring)
            speed = None
            ok = False
            if arrived is not None and arrived - pigeon.release > 0:
                speed = Fraction(pigeon.distance, arrived - pigeon.release)
                ok = arrived <= card.close_time and speed >= registration.threshold
            pair_rows.append(
                {
                    "ring": identity_name(ring),
                    "arrival": arrived,
                    "used": None if arrived is None else arrived - pigeon.release,
                    "distance": pigeon.distance,
                    "speed": None if speed is None else f"{speed.numerator}/{speed.denominator}",
                    "threshold": f"{registration.threshold.numerator}/{registration.threshold.denominator}",
                    "ok": ok,
                }
            )
        basis.append(
            {
                "witness": i,
                "satisfies": witness.satisfies,
                "pair": pair_rows,
                "tokens": anonymized_rows(witness, own, anon),
            }
        )
    return {
        "team": registration.team,
        "evaluation": registration.evaluation,
        "outcome": outcome,
        "basis": basis,
        "public_crossings": public_crossings,
    }


def public_crossings_of(witnesses: list[Witness], pair: tuple[bytes, bytes]) -> list[dict]:
    """覆盖裁决的公开过梁号：见证中本队两羽正式到棚的出门梁记次。"""
    seen: dict[int, tuple[int, int]] = {}
    for witness in witnesses:
        events = witness.history.arrival_events()
        for ring in pair:
            ev = events.get(ring)
            if ev is not None:
                seen[ev.ordinal] = ev.record_range
    return [
        {"ordinal": ordinal, "record_range": list(seen[ordinal])}
        for ordinal in sorted(seen)
    ]


def team_receipt(decision: Decision) -> dict:
    return make_receipt(
        registration=decision.registration,
        card=decision.card,
        outcome=decision.outcome,
        witnesses=decision.witnesses,
        public_crossings=public_crossings_of(
            decision.witnesses, decision.registration.pair
        ),
    )


# ---------------------------------------------------------------------------
# 互斥评比的当场对质

@dataclass
class WitnessConflict:
    token: int
    first: str
    second: str
    entry_ordinal: int
    entry_range: tuple[int, int]
    exit_ordinal: int | None
    exit_range: tuple[int, int] | None

    def as_dict(self) -> dict:
        return {
            "token": self.token,
            "first": self.first,
            "second": self.second,
            "entry_ordinal": self.entry_ordinal,
            "entry_range": list(self.entry_range),
            "exit_ordinal": self.exit_ordinal,
            "exit_range": None if self.exit_range is None else list(self.exit_range),
        }


@dataclass
class CrossCheck:
    """两份回执的当场对质结果。"""

    declared_exclusive: bool
    jointly_satisfiable: bool
    pairs: list[dict]  # 每对见证是否同真及冲突点
    favorable_pair: tuple[int, int]
    favorable_verdict: str  # 同真 / 不同真

    def as_dict(self) -> dict:
        return {
            "declared_exclusive": self.declared_exclusive,
            "jointly_satisfiable": self.jointly_satisfiable,
            "pairs": self.pairs,
            "favorable_pair": list(self.favorable_pair),
            "favorable_verdict": self.favorable_verdict,
        }


def _witness_conflicts(w1: Witness, w2: Witness) -> list[WitnessConflict]:
    conflicts = []
    for token in w1.history.tokens:
        a = w1.history.assignment[token.index]
        b = w2.history.assignment[token.index]
        if a != b:
            conflicts.append(
                WitnessConflict(
                    token=token.index,
                    first=identity_name(a),
                    second=identity_name(b),
                    entry_ordinal=token.entry.ordinal,
                    entry_range=token.entry.record_range,
                    exit_ordinal=None if token.exit is None else token.exit.ordinal,
                    exit_range=None if token.exit is None else token.exit.record_range,
                )
            )
    return conflicts


def _favorable_index(decision: Decision) -> int:
    for i, witness in enumerate(decision.witnesses):
        if witness.satisfies:
            return i
    return 0


def cross_check(
    first: Decision,
    second: Decision,
    book: ClockBook,
    universe: Iterable[bytes],
) -> CrossCheck:
    """同一羽出现在两个互斥评比的回执中时，当场指出各自见证是否同真。

    * ``jointly_satisfiable``：是否存在一份整棚履历同时满足两项报名
      （不存在则互斥成立）；
    * ``pairs``：双方保留的整棚见证逐对比较，给出是否同真及最先冲突的
      通行单元与报文范围；
    * ``favorable_verdict``：两队各自据以申领的那份（满足方）见证是否同真。
    """
    declared = (
        first.registration.exclusive_group is not None
        and first.registration.exclusive_group == second.registration.exclusive_group
    )
    histories = enumerate_histories(book, list(universe))
    joint = any(
        criterion(h, first.card, first.registration)
        and criterion(h, second.card, second.registration)
        for h in histories
    )
    pairs = []
    for i, w1 in enumerate(first.witnesses):
        for j, w2 in enumerate(second.witnesses):
            conflicts = _witness_conflicts(w1, w2)
            pairs.append(
                {
                    "first": i,
                    "second": j,
                    "compatible": not conflicts,
                    "conflicts": [c.as_dict() for c in conflicts],
                }
            )
    fav = (_favorable_index(first), _favorable_index(second))
    fav_compatible = any(
        p["first"] == fav[0] and p["second"] == fav[1] and p["compatible"]
        for p in pairs
    )
    return CrossCheck(
        declared_exclusive=declared,
        jointly_satisfiable=joint,
        pairs=pairs,
        favorable_pair=fav,
        favorable_verdict="同真" if fav_compatible else "不同真",
    )
