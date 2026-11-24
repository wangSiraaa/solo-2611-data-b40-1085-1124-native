"""全棚一致性引擎：把进棚通道的先进先出队列与梁/天线约束归集成整棚履历。

模型
----

* 每条"合"记次在入门梁压入一个通行单元（token），在出门梁弹出队首单元并
  记一次正式到棚；"开"记次不发生通过。出门梁合上时队列为空，则报文自相
  矛盾（:class:`QueueInconsistent`）。
* 读环约束：具名 ⇒ 该单元即此环（须在册）；显式空值 ⇒ 不参赛的空环模型；
  缺读环 ⇒ 某羽在册参赛鸽（身份未读）。同一单元的入门、出门两条记次的
  读环必须相容（如入门具名甲、出门具名乙即矛盾）。
* 每羽参赛鸽同一航次只可到棚一次，且一羽在通道中至多占一个单元：
  所有单元的参赛身份互不相同（空环模型不限数）。
* 某一根梁局部可解释，不能代替全棚的排队顺序：是否相容以整条 FIFO
  队列归集出的整棚履历为准，而不是逐梁各自解释。

规模上限：限六羽、十二次通过（见 :mod:`pigeon.book`），枚举量有界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .book import READ_EMPTY, READ_NAMED, ClockBook, Event


class EmptyRing:
    """不参赛的空环模型（单例）。"""

    _instance: "EmptyRing | None" = None

    def __new__(cls) -> "EmptyRing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "空环"

    def __reduce__(self):
        return (EmptyRing, ())


EMPTY = EmptyRing()

Identity = bytes | EmptyRing


def identity_name(identity: Identity) -> str:
    if identity is EMPTY:
        return "空环"
    try:
        return identity.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover - defensive
        return "0x" + identity.hex()


def _sort_key(identity: Identity) -> tuple:
    return (identity is EMPTY, identity if isinstance(identity, bytes) else b"")


class QueueInconsistent(Exception):
    """队列层面自相矛盾（如出门梁合上时通道为空）。"""


@dataclass
class Token:
    """通道中的一个通行单元：由入门记次创建，由出门记次弹出。"""

    index: int
    entry: Event
    exit: Event | None = None


def build_tokens(book: ClockBook) -> list[Token]:
    """按记次顺序重演 FIFO 队列，返回全部通行单元。"""
    tokens: list[Token] = []
    queue: list[Token] = []
    for ev in book.events:
        if not ev.is_crossing:
            continue
        if ev.role == "entry":
            token = Token(index=len(tokens), entry=ev)
            tokens.append(token)
            queue.append(token)
        else:
            if not queue:
                raise QueueInconsistent(
                    f"出门梁合上（记次 {ev.ordinal}，时刻 {ev.time}）时进棚通道为空"
                )
            queue[0].exit = ev
            queue.pop(0)
    return tokens


def _read_domain(read) -> set[Identity] | None:
    """单条记次读环给出的身份集合；``None`` 表示该记次不带读环约束。"""
    if read is None:
        return None
    if read.kind == READ_NAMED:
        return {read.ring}
    if read.kind == READ_EMPTY:
        return {EMPTY}
    return None  # 缺读环：只知道有鸽，由调用方给在册全集


def token_domain(token: Token, universe: Iterable[bytes]) -> set[Identity]:
    """一个通行单元的候选身份：入/出门两条记次读环的交集，限在册或空环。"""
    racing = set(universe)
    domain: set[Identity] | None = None
    for ev in (token.entry, token.exit):
        if ev is None:
            continue
        read_domain = _read_domain(ev.read)
        if read_domain is None:
            read_domain = set(racing)  # 缺读环：某羽参赛鸽
        domain = read_domain if domain is None else (domain & read_domain)
    if domain is None:  # pragma: no cover - 入门记次总是存在
        domain = set(racing)
    return domain & (racing | {EMPTY})


def _normalize_constraints(
    constraints: Mapping[int, Identity | Iterable[Identity]] | None,
) -> dict[int, set[Identity]]:
    out: dict[int, set[Identity]] = {}
    for token_index, value in (constraints or {}).items():
        if isinstance(value, (bytes, EmptyRing)):
            out[token_index] = {value}
        else:
            out[token_index] = set(value)
    return out


@dataclass
class History:
    """一份相容的整棚归巢履历：每个通行单元的身份指派。"""

    assignment: dict[int, Identity]
    tokens: list[Token]

    def identity_of(self, token_index: int) -> Identity:
        return self.assignment[self.tokens[token_index].index]

    def arrivals(self) -> dict[bytes, int]:
        """参赛鸽的正式到棚时刻（空环模型不计）。"""
        out: dict[bytes, int] = {}
        for token in self.tokens:
            if token.exit is None:
                continue
            identity = self.assignment[token.index]
            if identity is EMPTY:
                continue
            out[identity] = token.exit.time
        return out

    def arrival_events(self) -> dict[bytes, Event]:
        """参赛鸽正式到棚对应的出门梁记次。"""
        out: dict[bytes, Event] = {}
        for token in self.tokens:
            if token.exit is None:
                continue
            identity = self.assignment[token.index]
            if identity is EMPTY:
                continue
            out[identity] = token.exit
        return out

    def as_rows(self) -> list[dict]:
        rows = []
        for token in self.tokens:
            rows.append(
                {
                    "token": token.index,
                    "identity": identity_name(self.assignment[token.index]),
                    "entry_ordinal": token.entry.ordinal,
                    "entry_time": token.entry.time,
                    "entry_range": list(token.entry.record_range),
                    "exit_ordinal": None if token.exit is None else token.exit.ordinal,
                    "exit_time": None if token.exit is None else token.exit.time,
                    "exit_range": None if token.exit is None else list(token.exit.record_range),
                }
            )
        return rows


def enumerate_histories(
    book: ClockBook,
    universe: Iterable[bytes],
    constraints: Mapping[int, Identity | Iterable[Identity]] | None = None,
) -> list[History]:
    """枚举全部相容整棚履历。

    ``constraints`` 把指定通行单元限制到某个（或某组）身份，用于复议时
    注入底片显影结果。队列结构矛盾抛 :class:`QueueInconsistent`；
    读环/约束矛盾则返回空表（无任何相容履历）。
    """
    tokens = build_tokens(book)
    racing = list(universe)
    fixed = _normalize_constraints(constraints)

    domains: list[list[Identity]] = []
    for token in tokens:
        domain = token_domain(token, racing)
        if token.index in fixed:
            domain &= fixed[token.index]
        if not domain:
            return []
        domains.append(sorted(domain, key=_sort_key))

    histories: list[History] = []
    assignment: dict[int, Identity] = {}
    used: set[bytes] = set()

    def backtrack(pos: int) -> None:
        if pos == len(tokens):
            histories.append(History(dict(assignment), list(tokens)))
            return
        for identity in domains[pos]:
            if identity is not EMPTY and identity in used:
                continue  # 每羽参赛鸽同一航次只可到棚一次
            assignment[pos] = identity
            if identity is not EMPTY:
                used.add(identity)
            backtrack(pos + 1)
            if identity is not EMPTY:
                used.discard(identity)
            del assignment[pos]

    backtrack(0)
    return histories


def compatible_values(
    book: ClockBook,
    universe: Iterable[bytes],
    token_index: int,
    constraints: Mapping[int, Identity | Iterable[Identity]] | None = None,
) -> list[Identity]:
    """某通行单元在全部相容履历中可取的身份（底片的相容显影值集合）。"""
    histories = enumerate_histories(book, universe, constraints)
    values = {h.assignment[token_index] for h in histories}
    return sorted(values, key=_sort_key)
