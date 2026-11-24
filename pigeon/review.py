"""复议：孤本底片、听证组织与期限内判定的调度。

本场采用**联合听证**：钟房独占底片，每拍只接一张片；钟房认识全棚的环，
一片的发现可用在多份申请；发给队伍的证明只透露该队裁决、覆盖它的公开
过梁号及裁决依据，不列对手脚环。另实现队席分权内核（具名显影权开庭前
分给各队席、每席每拍并行开一张、权与开过的原片不得转用、非本队环仅见
"外队"），用于对照受理同一批申请。

底片：每张对应一条过梁（一个通行单元），孤本，一张只能开一次；开片前
不能预知显影值，但相容显影值集合可由整棚钟簿算出。调度目标：在所有
相容显影结果中，尽量提高期限内判定的件数（极小化极大）。每件判定都
保留两份具名整棚见证供有权的钟房内部复核，且能凭该席当时实际拥有的
证据与整棚钟簿复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .book import ClockBook
from .certificate import (
    OUTCOME_REVOKE,
    OUTCOME_STAMP,
    FlightCard,
    Registration,
    Witness,
    criterion,
    make_receipt,
    public_crossings_of,
)
from .engine import EMPTY, Identity, enumerate_histories, identity_name
from .engine import compatible_values as _engine_compatible_values

OUTCOME_CONFLICT = "记录矛盾"


class ReviewError(Exception):
    """复议纪律被违反（如孤本重复发权、显影权转用）。"""


@dataclass
class BookContext:
    """一件复议所依托的整棚钟簿与题定数据。"""

    book: ClockBook
    card: FlightCard

    @property
    def key(self) -> str:
        return self.book.key


@dataclass(frozen=True)
class Film:
    """一张孤本底片：对应一条过梁（一个通行单元）。"""

    film_id: str
    book_key: str
    token_index: int
    crossing_ordinal: int  # 公开过梁号


@dataclass(frozen=True)
class ReviewItem:
    """一件复议申请：对某项报名的达速证裁决请求复核。"""

    item_id: str
    ctx: BookContext
    registration: Registration
    deadline: int  # 领取裁决的最迟拍


@dataclass
class Ruling:
    outcome: str  # 盖章 / 撤证 / 记录矛盾
    witnesses: list[Witness]


def _constraints_for(
    ctx: BookContext, films: dict[str, Film], evidence: dict[str, Identity]
) -> dict[int, Identity]:
    out: dict[int, Identity] = {}
    for film_id, value in evidence.items():
        film = films[film_id]
        if film.book_key == ctx.key:
            out[film.token_index] = value
    return out


def adjudicate(
    ctx: BookContext,
    registration: Registration,
    constraints,
) -> Ruling | None:
    """凭现有证据与整棚钟簿复核一件申请；证据不足返回 ``None``。"""
    histories = enumerate_histories(ctx.book, ctx.card.universe(), constraints)
    if not histories:
        return Ruling(OUTCOME_CONFLICT, [])
    sat = [h for h in histories if criterion(h, ctx.card, registration)]
    if len(sat) == len(histories):
        return Ruling(OUTCOME_STAMP, [Witness(True, h) for h in sat[:2]])
    if not sat:
        return Ruling(OUTCOME_REVOKE, [Witness(False, h) for h in histories[:2]])
    return None


def compatible_values(
    ctx: BookContext, token_index: int, constraints
) -> list[Identity]:
    """一张底片在当前证据下的相容显影值集合（开片前不能预知具体值）。"""
    return _engine_compatible_values(
        ctx.book, ctx.card.universe(), token_index, constraints
    )


def verify_ruling(
    ctx: BookContext,
    registration: Registration,
    constraints,
    expected_outcome: str,
) -> bool:
    """复核一件判定：凭该席当时实际拥有的证据与整棚钟簿重算。"""
    ruling = adjudicate(ctx, registration, constraints)
    return ruling is not None and ruling.outcome == expected_outcome


# ---------------------------------------------------------------------------
# 联合听证（本场采用）


@dataclass
class Opening:
    beat: int
    film_id: str
    value: Identity


@dataclass
class Adjudication:
    item_id: str
    beat: int
    outcome: str
    witnesses: list[Witness]
    evidence: dict[str, Identity]  # 该席当时实际拥有的证据（已开底片及显影值）
    receipt: dict


@dataclass
class HearingLog:
    beats: int
    openings: list[Opening] = dc_field(default_factory=list)
    adjudications: list[Adjudication] = dc_field(default_factory=list)
    expired: list[str] = dc_field(default_factory=list)

    def openings_at(self, beat: int) -> list[Opening]:
        return [o for o in self.openings if o.beat == beat]

    @property
    def adjudicated_count(self) -> int:
        return len(self.adjudications)


class JointHearing:
    """联合听证：钟房独占底片，每拍只接一张片，发现可用于多份申请。"""

    def __init__(
        self,
        contexts: dict[str, BookContext],
        items: list[ReviewItem],
        films: list[Film],
        beats: int,
    ) -> None:
        self.contexts = contexts
        self.items = {item.item_id: item for item in items}
        self.films = {film.film_id: film for film in films}
        self.beats = beats
        for film in films:
            if film.book_key not in contexts:
                raise ReviewError(f"底片 {film.film_id} 指向未知钟簿 {film.book_key}")
        self._cache: dict = {}

    # -- 证据与判定 ------------------------------------------------------
    def _settle(
        self, evidence: dict[str, Identity], adjudicated: set[str], beat: int
    ) -> set[str]:
        """证据一旦足够即判；期限已过的申请不再判。"""
        out = set(adjudicated)
        changed = True
        while changed:
            changed = False
            for item_id, item in self.items.items():
                if item_id in out or item.deadline < beat:
                    continue
                constraints = _constraints_for(item.ctx, self.films, evidence)
                ruling = adjudicate(item.ctx, item.registration, constraints)
                if ruling is not None and ruling.outcome in (OUTCOME_STAMP, OUTCOME_REVOKE):
                    out.add(item_id)
                    changed = True
        return out

    def _resolved(self, adjudicated: set[str], beat: int) -> bool:
        return all(
            item_id in adjudicated or item.deadline < beat
            for item_id, item in self.items.items()
        )

    # -- 极小化极大调度 ----------------------------------------------------
    def _value(
        self, beat: int, evidence: dict[str, Identity], adjudicated: frozenset
    ) -> tuple[int, str | None]:
        """从该拍起的最大保证判定件数及本拍应开的底片。"""
        key = (beat, frozenset(evidence.items()), adjudicated)
        if key in self._cache:
            return self._cache[key]
        if beat > self.beats or self._resolved(set(adjudicated), beat):
            result = (len(adjudicated), None)
            self._cache[key] = result
            return result
        unopened = [f for f in self.films.values() if f.film_id not in evidence]
        if not unopened:
            result = (len(adjudicated), None)
            self._cache[key] = result
            return result
        best_value, best_film = -1, None
        for film in unopened:
            ctx = self.contexts[film.book_key]
            constraints = _constraints_for(ctx, self.films, evidence)
            values = compatible_values(ctx, film.token_index, constraints)
            if not values:
                continue  # 证据已矛盾的分支不会出现
            worst = None
            for value in values:
                evidence2 = dict(evidence, **{film.film_id: value})
                adj2 = frozenset(self._settle(evidence2, set(adjudicated), beat))
                got, _ = self._value(beat + 1, evidence2, adj2)
                worst = got if worst is None else min(worst, got)
            if worst is not None and worst > best_value:
                best_value, best_film = worst, film.film_id
        # 本拍也可以选择不开片
        got_pass, _ = self._value(beat + 1, evidence, adjudicated)
        if got_pass > best_value:
            best_value, best_film = got_pass, None
        result = (best_value, best_film)
        self._cache[key] = result
        return result

    def plan(self) -> tuple[int, str | None]:
        """开听证前的保证判定件数与首拍应开的底片。"""
        adjudicated = frozenset(self._settle({}, set(), 0))
        return self._value(1, {}, adjudicated)

    # -- 开庭 ------------------------------------------------------------
    def run(self, outcomes: dict[str, Identity]) -> HearingLog:
        """按调度开庭；``outcomes`` 给出各底片的实际显影值。"""
        log = HearingLog(beats=self.beats)
        evidence: dict[str, Identity] = {}
        adjudicated: set[str] = set()

        def settle_and_record(beat: int) -> None:
            nonlocal adjudicated
            newly = self._settle(evidence, adjudicated, beat) - adjudicated
            for item_id in sorted(newly):
                item = self.items[item_id]
                constraints = _constraints_for(item.ctx, self.films, evidence)
                ruling = adjudicate(item.ctx, item.registration, constraints)
                assert ruling is not None
                adjudicated.add(item_id)
                receipt = make_receipt(
                    registration=item.registration,
                    card=item.ctx.card,
                    outcome=ruling.outcome,
                    witnesses=ruling.witnesses,
                    public_crossings=public_crossings_of(
                        ruling.witnesses, item.registration.pair
                    )
                    + [
                        {"ordinal": self.films[f].crossing_ordinal, "film": f}
                        for f in sorted(evidence)
                        if self.films[f].book_key == item.ctx.key
                    ],
                )
                log.adjudications.append(
                    Adjudication(
                        item_id=item_id,
                        beat=beat,
                        outcome=ruling.outcome,
                        witnesses=ruling.witnesses,
                        evidence=dict(evidence),
                        receipt=receipt,
                    )
                )

        settle_and_record(0)
        for beat in range(1, self.beats + 1):
            if self._resolved(adjudicated, beat):
                break
            _, film_id = self._value(beat, evidence, frozenset(adjudicated))
            if film_id is None:
                continue
            if film_id not in outcomes:
                raise ReviewError(f"底片 {film_id} 缺少实际显影值")
            value = outcomes[film_id]
            film = self.films[film_id]
            ctx = self.contexts[film.book_key]
            constraints = _constraints_for(ctx, self.films, evidence)
            if value not in compatible_values(ctx, film.token_index, constraints):
                raise ReviewError(
                    f"底片 {film_id} 的显影值 {identity_name(value)} 与整棚钟簿不相容"
                )
            evidence[film_id] = value  # 一张只能开一次：已开的不再开
            log.openings.append(Opening(beat=beat, film_id=film_id, value=value))
            settle_and_record(beat)
        log.expired = sorted(set(self.items) - adjudicated)
        return log


# ---------------------------------------------------------------------------
# 队席分权内核（对照用）

VIEW_OWN = "own"
VIEW_FOREIGN = "外队"
VIEW_EMPTY = "空环"


@dataclass
class SeatOpening:
    beat: int
    seat: str
    film_id: str
    view: str  # 本队具名 / 外队 / 空环


@dataclass
class SessionLog:
    beats: int
    rights: dict[str, tuple[str, str]]  # film -> (seat, item)
    openings: list[SeatOpening] = dc_field(default_factory=list)
    adjudications: list[tuple[str, int, str]] = dc_field(default_factory=list)  # item, beat, outcome
    expired: list[str] = dc_field(default_factory=list)


class DistributedSession:
    """队席分权：具名显影权开庭前分给各队席，每席每拍并行开一张。

    受领的是哪一件复议就只能用于该件；读到非本队环仅见"外队"；
    权和开过的原片均不能转用。
    """

    def __init__(
        self,
        contexts: dict[str, BookContext],
        items: list[ReviewItem],
        films: list[Film],
        seats: dict[str, str],  # item_id -> 队席
    ) -> None:
        self.contexts = contexts
        self.items = {item.item_id: item for item in items}
        self.films = {film.film_id: film for film in films}
        self.seats = dict(seats)
        self.rights: dict[str, tuple[str, str]] = {}

    def issue_right(self, film_id: str, seat: str, item_id: str) -> None:
        if film_id in self.rights:
            raise ReviewError(f"底片 {film_id} 是孤本，显影权已发出，不能转用")
        if film_id not in self.films:
            raise ReviewError(f"未知底片 {film_id}")
        item = self.items[item_id]
        if self.seats.get(item_id) != seat:
            raise ReviewError(f"复议 {item_id} 不属于队席 {seat}")
        if self.films[film_id].book_key != item.ctx.key:
            raise ReviewError(f"底片 {film_id} 与复议 {item_id} 不在同一钟簿")
        self.rights[film_id] = (seat, item_id)

    def run(self, outcomes: dict[str, Identity], beats: int) -> SessionLog:
        log = SessionLog(beats=beats, rights=dict(self.rights))
        constraints_by_item: dict[str, dict[int, set]] = {
            item_id: {} for item_id in self.items
        }
        adjudicated: set[str] = set()
        opened: set[str] = set()

        def try_settle(item_id: str, beat: int) -> None:
            if item_id in adjudicated:
                return
            item = self.items[item_id]
            if item.deadline < beat:
                return
            ruling = adjudicate(
                item.ctx, item.registration, constraints_by_item[item_id]
            )
            if ruling is not None and ruling.outcome in (OUTCOME_STAMP, OUTCOME_REVOKE):
                adjudicated.add(item_id)
                log.adjudications.append((item_id, beat, ruling.outcome))

        for item_id in self.items:
            try_settle(item_id, 0)
        for beat in range(1, beats + 1):
            for seat in sorted(set(self.seats.values())):
                held = [
                    (self.items[iid].deadline, fid, iid)
                    for fid, (s, iid) in self.rights.items()
                    if s == seat and fid not in opened and iid not in adjudicated
                ]
                if not held:
                    continue
                _, film_id, item_id = min(held)
                if film_id not in outcomes:
                    raise ReviewError(f"底片 {film_id} 缺少实际显影值")
                value = outcomes[film_id]
                film = self.films[film_id]
                item = self.items[item_id]
                own = set(item.registration.pair)
                if value is EMPTY:
                    view = VIEW_EMPTY
                    constraint = {EMPTY}
                elif value in own:
                    view = identity_name(value)
                    constraint = {value}
                else:
                    view = VIEW_FOREIGN
                    constraint = set(item.ctx.card.universe()) - own
                opened.add(film_id)
                log.openings.append(
                    SeatOpening(beat=beat, seat=seat, film_id=film_id, view=view)
                )
                constraints_by_item[item_id][film.token_index] = constraint
                try_settle(item_id, beat)
        log.expired = sorted(set(self.items) - adjudicated)
        return log
