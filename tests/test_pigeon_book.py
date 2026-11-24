"""整棚钟簿：真实字节流导入、报文来源范围、域校验与封棚判定。"""

from __future__ import annotations

import pytest

from pigeon import harness, wire
from pigeon.book import (
    ACTION_CLOSE,
    READ_EMPTY,
    READ_MISSING,
    READ_NAMED,
    BookError,
    load_book,
)
from telber import build as B
from telber.repository import Repository

JIA = "甲".encode("utf-8")
YI = "乙".encode("utf-8")


@pytest.fixture
def repo():
    with Repository(":memory:") as repo:
        yield repo


@pytest.fixture
def product(repo):
    return harness.publish(repo)


def main_stream() -> bytes:
    return wire.stream(
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4, ring=JIA),
            wire.gate_open(loft=1, time=5, gate=1),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
        ]
    )


def test_import_real_byte_stream_with_provenance(repo, product):
    data = main_stream()
    book, import_id, info = harness.ingest_book(repo, product, data, loft=1)
    assert info.status == "complete"
    assert book.sealed is True
    # 五条记次：入门合、出门合（读甲）、出门开、入门合、出门合
    assert [e.action for e in book.events] == [1, 1, 0, 1, 1]
    assert [e.is_crossing for e in book.events] == [True, True, False, True, True]
    assert [e.time for e in book.events] == [3, 4, 5, 5, 6]
    assert [e.role for e in book.events] == ["entry", "exit", "exit", "entry", "exit"]
    # 各记次报文范围指向原始字节流，切片即该条完整编码
    for event in book.events:
        start, end = event.record_range
        assert data[start:end]  # 非空
    assert book.events[0].record_range == (0, 14)
    first_crossing = wire.entry_close(loft=1, time=3)
    assert data[0 : len(first_crossing)] == first_crossing


def test_ring_read_source_ranges(repo, product):
    data = main_stream()
    book, _, _ = harness.ingest_book(repo, product, data, loft=1)
    named = book.events[1].read
    assert named.kind == READ_NAMED and named.ring == JIA
    roles = [role for role, _, _ in named.source_ranges]
    assert roles == ["wrapper", "inner"]
    # wrapper 切片正是 context [4] 显式包裹的独立编码
    expect = B.explicit(B.tag("context", True, 4), B.octets(JIA))
    ws, we = named.source_ranges[0][1], named.source_ranges[0][2]
    assert data[ws:we] == expect
    assert book.events[0].read.kind == READ_MISSING
    assert book.events[0].read.source_ranges == ()


def test_explicit_empty_ring(repo, product):
    data = wire.stream(
        [
            wire.entry_close(loft=1, time=3, empty=True),
            wire.exit_close(loft=1, time=4, empty=True),
        ]
    )
    book, _, _ = harness.ingest_book(repo, product, data, loft=1)
    assert all(e.read.kind == READ_EMPTY for e in book.events)


def test_ring_and_empty_together_is_rejected(repo, product):
    record = wire.clock_record(loft=1, gate=0, time=3, action=1, ring=JIA, empty=True)
    with pytest.raises(BookError, match="不得同现"):
        harness.ingest_book(repo, product, wire.stream([record]), loft=1)


def test_bad_action_is_rejected(repo, product):
    record = wire.clock_record(loft=1, gate=0, time=3, action=2)
    with pytest.raises(BookError, match="开闭取值"):
        harness.ingest_book(repo, product, wire.stream([record]), loft=1)


def test_unknown_gate_is_rejected(repo, product):
    record = wire.clock_record(loft=1, gate=9, time=3, action=1)
    with pytest.raises(BookError, match="门角色"):
        harness.ingest_book(repo, product, wire.stream([record]), loft=1)


def test_time_regression_is_rejected(repo, product):
    data = wire.stream(
        [wire.entry_close(loft=1, time=5), wire.entry_close(loft=1, time=3)]
    )
    with pytest.raises(BookError, match="时刻回退"):
        harness.ingest_book(repo, product, data, loft=1)


def test_crossing_limit(repo, product):
    records = []
    for i in range(7):  # 7 次入门 + 7 次出门 = 14 > 十二次通过
        records.append(wire.entry_close(loft=1, time=2 * i))
        records.append(wire.exit_close(loft=1, time=2 * i + 1))
    with pytest.raises(BookError, match="十二次通过"):
        harness.ingest_book(repo, product, wire.stream(records), loft=1)


def test_truncated_tail_is_not_sealed(repo, product):
    data = main_stream() + wire.exit_close(loft=1, time=8)[:4]
    book, import_id, info = harness.ingest_book(repo, product, data, loft=1)
    assert book.import_status == "partial"
    assert book.sealed is False  # 截断尾部不得作为已封棚
    assert book.import_error["error_code"] == "truncated"
    # 已完成的前缀记次仍然载入
    assert len(book.events) == 5


def test_mixed_lofts_are_filtered(repo, product):
    data = wire.stream(
        [
            wire.entry_close(loft=1, time=3),
            wire.entry_close(loft=2, time=3),
            wire.exit_close(loft=1, time=4, ring=JIA),
        ]
    )
    book, _, _ = harness.ingest_book(repo, product, data, loft=1)
    assert len(book.events) == 2
    assert book.skipped_lofts == (2,)
