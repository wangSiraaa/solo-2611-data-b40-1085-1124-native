"""一致性引擎：FIFO 队列耦合、读环三态、每羽每航次至多一次到棚。"""

from __future__ import annotations

import pytest

from pigeon import harness, wire
from pigeon.engine import (
    EMPTY,
    QueueInconsistent,
    compatible_values,
    enumerate_histories,
)
from telber.repository import Repository

JIA = "甲".encode("utf-8")
YI = "乙".encode("utf-8")
BING = "丙".encode("utf-8")


@pytest.fixture
def repo():
    with Repository(":memory:") as repo:
        yield repo


@pytest.fixture
def product(repo):
    return harness.publish(repo)


def make_book(repo, product, records, loft=1):
    data = wire.stream(records)
    book, _, _ = harness.ingest_book(repo, product, data, loft=loft)
    return book


def test_exit_without_entry_is_queue_inconsistent(repo, product):
    book = make_book(repo, product, [wire.exit_close(loft=1, time=4)])
    with pytest.raises(QueueInconsistent, match="通道为空"):
        enumerate_histories(book, [JIA, YI])


def test_local_beam_ok_but_queue_order_contradicts(repo, product):
    """入门梁读乙、出门梁读乙，各自局部可解释；全棚排队顺序下乙要到棚两次。"""
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3, ring=YI),
            wire.exit_close(loft=1, time=4),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6, ring=YI),
        ],
    )
    assert enumerate_histories(book, [JIA, YI]) == []


def test_missing_read_means_some_competing_pigeon(repo, product):
    book = make_book(
        repo,
        product,
        [wire.entry_close(loft=1, time=3), wire.exit_close(loft=1, time=4)],
    )
    histories = enumerate_histories(book, [JIA, YI])
    assert {h.assignment[0] for h in histories} == {JIA, YI}
    # 缺读环不是空环：空环只能由显式空值给出
    assert all(h.assignment[0] is not EMPTY for h in histories)


def test_explicit_empty_ring_model(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3, empty=True),
            wire.exit_close(loft=1, time=4, empty=True),
        ],
    )
    histories = enumerate_histories(book, [JIA, YI])
    assert len(histories) == 1
    assert histories[0].assignment[0] is EMPTY
    assert histories[0].arrivals() == {}  # 空环模型不是参赛到棚


def test_missing_read_contradicts_explicit_empty_on_same_token(repo, product):
    # 入门显式空环、出门缺读环（缺读环=某羽参赛鸽）⇒ 同一单元读环矛盾
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3, empty=True),
            wire.exit_close(loft=1, time=4),
        ],
    )
    assert enumerate_histories(book, [JIA, YI]) == []


def test_each_pigeon_arrives_at_most_once(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3, ring=JIA),
            wire.exit_close(loft=1, time=4),
            wire.entry_close(loft=1, time=5, ring=JIA),
            wire.exit_close(loft=1, time=6),
        ],
    )
    assert enumerate_histories(book, [JIA, YI]) == []


def test_foreign_named_ring_is_inconsistent(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4, ring=BING),
        ],
    )
    assert enumerate_histories(book, [JIA, YI]) == []


def test_fifo_forces_second_arrival_identity(repo, product):
    """关门 4 读到甲后，另一羽只能为乙（在册仅甲乙且甲已到棚一次）。"""
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4, ring=JIA),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
        ],
    )
    histories = enumerate_histories(book, [JIA, YI])
    assert len(histories) == 1
    assert histories[0].arrivals() == {JIA: 4, YI: 6}


def test_constraints_inject_film_values(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
        ],
    )
    assert len(enumerate_histories(book, [JIA, YI])) == 2
    assert len(enumerate_histories(book, [JIA, YI], {0: JIA})) == 1
    assert len(enumerate_histories(book, [JIA, YI], {0: YI})) == 1
    assert enumerate_histories(book, [JIA, YI], {0: BING}) == []
    # 集合约束（队席分权下"外队"= 在册全集减去本队）
    assert len(enumerate_histories(book, [JIA, YI], {0: {JIA}})) == 1


def test_compatible_values(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
        ],
    )
    assert set(compatible_values(book, [JIA, YI], 0)) == {JIA, YI}
    assert set(compatible_values(book, [JIA, YI], 1)) == {JIA, YI}
    assert compatible_values(book, [JIA, YI], 1, {0: JIA}) == [YI]


def test_open_marks_are_not_crossings(repo, product):
    book = make_book(
        repo,
        product,
        [
            wire.gate_open(loft=1, time=3, gate=0),
            wire.gate_open(loft=1, time=4, gate=1),
        ],
    )
    histories = enumerate_histories(book, [JIA, YI])
    assert len(histories) == 1 and histories[0].arrivals() == {}
