"""达速证裁决：盖章/撤证/待核、相反两份见证、匿名回执与互斥对质。"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from pigeon import harness, wire
from pigeon.certificate import (
    OUTCOME_INCONSISTENT,
    OUTCOME_PENDING,
    OUTCOME_REVOKE,
    OUTCOME_STAMP,
    OUTCOME_UNSEALED,
    FlightCard,
    PigeonCard,
    Registration,
    cross_check,
    decide,
    team_receipt,
)
from telber.repository import Repository

JIA = "甲".encode("utf-8")
YI = "乙".encode("utf-8")
BING = "丙".encode("utf-8")
THRESHOLD = Fraction(3, 2)


@pytest.fixture
def repo():
    with Repository(":memory:") as repo:
        yield repo


@pytest.fixture
def product(repo):
    return harness.publish(repo)


def main_stream(named: bool) -> bytes:
    return wire.stream(
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4, ring=JIA if named else None),
            wire.gate_open(loft=1, time=5, gate=1),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
        ]
    )


def main_card(dist_yi: int = 9) -> FlightCard:
    return FlightCard(
        loft=1,
        close_time=7,
        pigeons=(PigeonCard(JIA, 0, 6), PigeonCard(YI, 0, dist_yi)),
    )


def main_reg() -> Registration:
    return Registration("甲队", "团体达速", (JIA, YI), THRESHOLD)


def test_stamp_when_all_histories_meet_threshold(repo, product):
    """原案：甲乙从 0 出发，航距 6、9，截止 7 前时刻 4、6 到棚，关门 4 读甲。"""
    book, _, _ = harness.ingest_book(repo, product, main_stream(True), loft=1)
    decision = decide(book, main_card(), main_reg())
    assert decision.outcome == OUTCOME_STAMP
    assert decision.history_count == 1
    receipt = team_receipt(decision)
    pair = receipt["basis"][0]["pair"]
    assert pair[0]["ring"] == "甲" and pair[0]["speed"] == "3/2" and pair[0]["ok"]
    assert pair[1]["ring"] == "乙" and pair[1]["speed"] == "3/2" and pair[1]["ok"]
    # 覆盖裁决的公开过梁号：关门 4 与关门 6 两条记次
    assert [c["ordinal"] for c in receipt["public_crossings"]] == [1, 4]


def test_pending_with_two_opposing_witnesses(repo, product):
    """仅把关门 4 的读环纠正为缺失：交换身份亦成立，待核。"""
    book, _, _ = harness.ingest_book(repo, product, main_stream(False), loft=1)
    decision = decide(book, main_card(), main_reg())
    assert decision.outcome == OUTCOME_PENDING
    assert decision.history_count == 2 and decision.satisfy_count == 1
    sat, unsat = decision.witnesses
    assert sat.satisfies and not unsat.satisfies
    assert sat.history.arrivals() == {JIA: 4, YI: 6}
    assert unsat.history.arrivals() == {YI: 4, JIA: 6}  # 甲 6/6=1 < 3/2
    # 见证保留各记次实际来源的报文范围
    rows = sat.history.as_rows()
    assert rows[0]["exit_range"] == [14, 28]
    assert rows[1]["entry_range"] == [42, 56]


def test_revoke_when_distance_corrected(repo, product):
    """恢复实名、仅把乙航距更正为 6：乙 6/6=1 < 3/2，全部履历不达，撤证。"""
    book, _, _ = harness.ingest_book(repo, product, main_stream(True), loft=1)
    decision = decide(book, main_card(dist_yi=6), main_reg())
    assert decision.outcome == OUTCOME_REVOKE
    assert decision.satisfy_count == 0


def test_unsealed_book_is_not_decided(repo, product):
    data = main_stream(True) + wire.exit_close(loft=1, time=8)[:4]
    book, _, _ = harness.ingest_book(repo, product, data, loft=1)
    decision = decide(book, main_card(), main_reg())
    assert decision.outcome == OUTCOME_UNSEALED


def test_inconsistent_records(repo, product):
    book, _, _ = harness.ingest_book(
        repo, product, wire.stream([wire.exit_close(loft=1, time=4)]), loft=1
    )
    decision = decide(book, main_card(), main_reg())
    assert decision.outcome == OUTCOME_INCONSISTENT


def test_pigeon_limit(repo, product):
    book, _, _ = harness.ingest_book(repo, product, main_stream(True), loft=1)
    card = FlightCard(
        loft=1,
        close_time=7,
        pigeons=tuple(PigeonCard(bytes([i + 1]), 0, 6) for i in range(7)),
    )
    with pytest.raises(ValueError, match="限六羽"):
        decide(book, card, main_reg())


# ---------------------------------------------------------------------------
# 互斥评比

def exclusive_setup(repo, product):
    data = wire.stream(
        [
            wire.entry_close(loft=1, time=3),
            wire.exit_close(loft=1, time=4, ring=JIA),
            wire.entry_close(loft=1, time=5),
            wire.exit_close(loft=1, time=6),
            wire.entry_close(loft=1, time=7),
            wire.exit_close(loft=1, time=8),
        ]
    )
    book, _, _ = harness.ingest_book(repo, product, data, loft=1)
    card = FlightCard(
        loft=1,
        close_time=9,
        pigeons=(PigeonCard(JIA, 0, 6), PigeonCard(YI, 0, 9), PigeonCard(BING, 0, 9)),
    )
    reg_a = Registration("甲队", "评比一", (JIA, YI), THRESHOLD, exclusive_group="互斥A")
    reg_b = Registration("乙队", "评比二", (JIA, BING), THRESHOLD, exclusive_group="互斥A")
    return book, card, reg_a, reg_b


def test_exclusive_evaluations_cross_check(repo, product):
    """同一羽（甲）出现在两个互斥评比的回执中，当场指出各自见证是否同真。"""
    book, card, reg_a, reg_b = exclusive_setup(repo, product)
    decision_a = decide(book, card, reg_a)
    decision_b = decide(book, card, reg_b)
    assert decision_a.outcome == OUTCOME_PENDING
    assert decision_b.outcome == OUTCOME_PENDING
    check = cross_check(decision_a, decision_b, book, card.universe())
    assert check.declared_exclusive is True
    assert check.jointly_satisfiable is False  # 互斥成立：无一份履历同时满足两项
    # 两队各自据以申领的见证（满足方）不同真：冲突在通行单元 1、2
    assert check.favorable_verdict == "不同真"
    fav = next(
        p for p in check.pairs if (p["first"], p["second"]) == check.favorable_pair
    )
    assert not fav["compatible"]
    assert {c["token"] for c in fav["conflicts"]} == {1, 2}
    conflict = fav["conflicts"][0]
    assert conflict["first"] == "乙" and conflict["second"] == "丙"
    assert conflict["exit_range"] == [49, 63]  # 冲突记次的报文范围
    # 也存在同真的对（一方的满足见证与另一方的不满足见证是同一履历）
    assert any(p["compatible"] for p in check.pairs)


def test_receipt_hides_opponent_rings(repo, product):
    book, card, reg_a, reg_b = exclusive_setup(repo, product)
    receipt_a = team_receipt(decide(book, card, reg_a))
    text = json.dumps(receipt_a, ensure_ascii=False)
    assert "甲" in text and "乙" in text  # 本队具名
    assert "丙" not in text  # 不列对手脚环
    assert "外队位" in text  # 他队以匿名位置区分
    # 内部具名见证则保留全名，供有权的钟房内部复核
    internal = json.dumps(
        [w.as_dict() for w in decide(book, card, reg_a).witnesses], ensure_ascii=False
    )
    assert "丙" in internal
