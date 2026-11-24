"""复议：两种组织的基准受理、联合听证的期限压缩复核、孤本不得转用。"""

from __future__ import annotations

from fractions import Fraction

import pytest

from pigeon import harness, wire
from pigeon.certificate import FlightCard, PigeonCard, Registration
from pigeon.review import (
    BookContext,
    DistributedSession,
    Film,
    JointHearing,
    ReviewError,
    ReviewItem,
    verify_ruling,
)
from telber.repository import Repository

JIA = "甲".encode("utf-8")
YI = "乙".encode("utf-8")
BING = "丙".encode("utf-8")
DING = "丁".encode("utf-8")
THRESHOLD = Fraction(3, 2)


@pytest.fixture
def repo():
    with Repository(":memory:") as repo:
        yield repo


@pytest.fixture
def product(repo):
    return harness.publish(repo)


def pending_stream(loft: int) -> bytes:
    """两次归巢（时刻 4、6）均缺读环的航次。"""
    return wire.stream(
        [
            wire.entry_close(loft=loft, time=3),
            wire.exit_close(loft=loft, time=4),
            wire.entry_close(loft=loft, time=5),
            wire.exit_close(loft=loft, time=6),
        ]
    )


@pytest.fixture
def two_items(repo, product):
    """两件复议：各有一张可辨自己压线到棚鸽的底片，期限两拍。"""
    book1, _, _ = harness.ingest_book(repo, product, pending_stream(1), loft=1)
    ctx1 = BookContext(
        book1,
        FlightCard(1, 7, (PigeonCard(JIA, 0, 6), PigeonCard(YI, 0, 9))),
    )
    reg1 = Registration("甲队", "团体达速", (JIA, YI), THRESHOLD)
    book2, _, _ = harness.ingest_book(repo, product, pending_stream(2), loft=2)
    ctx2 = BookContext(
        book2,
        FlightCard(2, 7, (PigeonCard(BING, 0, 6), PigeonCard(DING, 0, 9))),
    )
    reg2 = Registration("乙队", "团体达速", (BING, DING), THRESHOLD)
    contexts = {ctx1.key: ctx1, ctx2.key: ctx2}
    items = [
        ReviewItem("复议一", ctx1, reg1, deadline=2),
        ReviewItem("复议二", ctx2, reg2, deadline=2),
    ]
    films = [
        Film("底片一", ctx1.key, token_index=0, crossing_ordinal=1),
        Film("底片二", ctx2.key, token_index=0, crossing_ordinal=1),
    ]
    return contexts, items, films


# ---------------------------------------------------------------------------
# 基准：两种组织都能受理

def test_joint_hearing_baseline_accepts(two_items):
    contexts, items, films = two_items
    hearing = JointHearing(contexts, items, films, beats=2)
    guaranteed, first_move = hearing.plan()
    assert guaranteed == 2  # 两片两拍，两件都能期限内判定
    assert first_move in {"底片一", "底片二"}
    # 开片前不能预知显影值：遍历全部相容显影结果，保证件数不变
    for v1 in (JIA, YI):
        for v2 in (BING, DING):
            log = hearing.run({"底片一": v1, "底片二": v2})
            assert log.adjudicated_count == 2
            assert all(a.beat <= 2 for a in log.adjudications)
            assert log.expired == []
            for beat in (1, 2):
                assert len(log.openings_at(beat)) <= 1  # 每拍只接一张片
            assert len({o.film_id for o in log.openings}) == len(log.openings)  # 一片只开一次


def test_distributed_baseline_accepts(two_items):
    contexts, items, films = two_items
    session = DistributedSession(
        contexts, items, films, seats={"复议一": "甲队", "复议二": "乙队"}
    )
    session.issue_right("底片一", "甲队", "复议一")
    session.issue_right("底片二", "乙队", "复议二")
    log = session.run({"底片一": JIA, "底片二": BING}, beats=2)
    assert sorted(i for i, _, _ in log.adjudications) == ["复议一", "复议二"]
    assert all(beat == 1 for _, beat, _ in log.adjudications)  # 各席并行，一拍即判
    assert log.expired == []


# ---------------------------------------------------------------------------
# 复核联合听证：期限压到同一末拍

def test_book_decisive_item_settles_without_opening(repo, product, two_items):
    """钟簿本身已可判的申请在第 0 拍即判，不消耗底片。"""
    contexts, items, films = two_items
    data = wire.stream(
        [
            wire.entry_close(loft=9, time=3),
            wire.exit_close(loft=9, time=4, ring=JIA),
            wire.entry_close(loft=9, time=5),
            wire.exit_close(loft=9, time=6),
        ]
    )
    book, _, _ = harness.ingest_book(repo, product, data, loft=9)
    ctx = BookContext(
        book, FlightCard(9, 7, (PigeonCard(JIA, 0, 6), PigeonCard(YI, 0, 9)))
    )
    decisive = ReviewItem(
        "复议零", ctx, Registration("甲队", "团体达速", (JIA, YI), THRESHOLD), deadline=2
    )
    hearing = JointHearing(
        {**contexts, ctx.key: ctx}, items + [decisive], films, beats=2
    )
    log = hearing.run({"底片一": JIA, "底片二": BING})
    zero = next(a for a in log.adjudications if a.item_id == "复议零")
    assert zero.beat == 0 and zero.outcome == "盖章"


def test_joint_hearing_same_final_beat_is_honest(two_items):
    contexts, _, films = two_items
    reg1 = Registration("甲队", "团体达速", (JIA, YI), THRESHOLD)
    reg2 = Registration("乙队", "团体达速", (BING, DING), THRESHOLD)
    ctx1 = contexts[next(k for k in contexts if "loft-1" in k)]
    ctx2 = contexts[next(k for k in contexts if "loft-2" in k)]
    items = [
        ReviewItem("复议一", ctx1, reg1, deadline=1),
        ReviewItem("复议二", ctx2, reg2, deadline=1),
    ]
    hearing = JointHearing(contexts, items, films, beats=3)
    guaranteed, first_move = hearing.plan()
    assert guaranteed == 1  # 每拍只接一张片：同一末拍前只能判一件
    log = hearing.run({"底片一": JIA, "底片二": BING})
    # 不虚报已开两片：第 1 拍至多一条开片记录
    assert len(log.openings_at(1)) == 1
    assert len(log.openings) == 1
    assert log.adjudicated_count == 1
    assert log.adjudications[0].beat == 1
    assert log.expired == ["复议二"]
    # 另一件的底片未被偷开
    assert log.openings[0].film_id == first_move


def test_joint_hearing_one_film_serves_many(two_items):
    """一片的发现可用在多份申请：两张片判三件。"""
    contexts, items, films = two_items
    ctx1 = contexts[next(k for k in contexts if "loft-1" in k)]
    reg_shared = Registration("甲队", "达标复核", (JIA, YI), Fraction(5, 4))
    items3 = items + [ReviewItem("复议三", ctx1, reg_shared, deadline=2)]
    hearing = JointHearing(contexts, items3, films, beats=2)
    guaranteed, _ = hearing.plan()
    assert guaranteed == 3
    log = hearing.run({"底片一": JIA, "底片二": BING})
    assert log.adjudicated_count == 3
    assert len(log.openings) == 2  # 总共最多三张片、三个窗口拍


def test_adjudication_verifiable_from_evidence(two_items):
    """每件判定都能凭该席当时实际拥有的证据与整棚钟簿复核。"""
    contexts, items, films = two_items
    hearing = JointHearing(contexts, items, films, beats=2)
    log = hearing.run({"底片一": YI, "底片二": DING})
    by_item = {item.item_id: item for item in items}
    for adjudication in log.adjudications:
        item = by_item[adjudication.item_id]
        constraints = {}
        for film_id, value in adjudication.evidence.items():
            film = next(f for f in films if f.film_id == film_id)
            if film.book_key == item.ctx.key:
                constraints[film.token_index] = value
        assert verify_ruling(
            item.ctx, item.registration, constraints, adjudication.outcome
        )
    # 显影乙 ⇒ 复议一撤证（甲 6/6=1 < 3/2）
    first = next(a for a in log.adjudications if a.item_id == "复议一")
    assert first.outcome == "撤证"


def test_team_receipt_does_not_leak_opponent_rings(two_items):
    contexts, items, films = two_items
    hearing = JointHearing(contexts, items, films, beats=2)
    log = hearing.run({"底片一": JIA, "底片二": BING})
    first = next(a for a in log.adjudications if a.item_id == "复议一")
    import json

    text = json.dumps(first.receipt, ensure_ascii=False)
    assert "甲" in text and "乙" in text
    assert "丙" not in text and "丁" not in text  # 不列对手脚环
    assert first.receipt["outcome"] == "盖章"


# ---------------------------------------------------------------------------
# 队席分权：孤本显影权不得转用

@pytest.fixture
def shared_book(repo, product):
    data = wire.stream(
        [
            wire.entry_close(loft=3, time=3),
            wire.exit_close(loft=3, time=4),
            wire.entry_close(loft=3, time=5),
            wire.exit_close(loft=3, time=6),
            wire.entry_close(loft=3, time=7),
            wire.exit_close(loft=3, time=8),
        ]
    )
    book, _, _ = harness.ingest_book(repo, product, data, loft=3)
    ctx = BookContext(
        book,
        FlightCard(
            3, 9, (PigeonCard(JIA, 0, 6), PigeonCard(YI, 0, 12), PigeonCard(BING, 0, 12))
        ),
    )
    reg_x = Registration("甲队", "评比一", (JIA, YI), THRESHOLD)
    reg_y = Registration("乙队", "评比二", (JIA, BING), THRESHOLD)
    items = [
        ReviewItem("复议甲", ctx, reg_x, deadline=2),
        ReviewItem("复议乙", ctx, reg_y, deadline=2),
    ]
    film = Film("孤本片", ctx.key, token_index=0, crossing_ordinal=1)
    return ctx, items, film


def test_distributed_same_film_cannot_be_taken_by_the_other(shared_book):
    """两件都须凭同一张孤本补认同一羽；即使结果有利于双方，另一件也取不走。"""
    ctx, items, film = shared_book
    session = DistributedSession(
        {ctx.key: ctx},
        items,
        [film],
        seats={"复议甲": "甲队", "复议乙": "乙队"},
    )
    session.issue_right(film.film_id, "甲队", "复议甲")
    with pytest.raises(ReviewError, match="孤本"):
        session.issue_right(film.film_id, "乙队", "复议乙")
    log = session.run({film.film_id: JIA}, beats=3)
    # 显影甲对双方都有利，但只有持权的一件能判
    assert log.adjudications == [("复议甲", 1, "盖章")]
    assert log.expired == ["复议乙"]
    assert len(log.openings) == 1  # 一张只能开一次


def test_distributed_foreign_ring_view(shared_book):
    """读到非本队环仅见"外队"，且仍能凭此判定。"""
    ctx, items, film = shared_book
    session = DistributedSession(
        {ctx.key: ctx},
        items,
        [film],
        seats={"复议甲": "甲队", "复议乙": "乙队"},
    )
    session.issue_right(film.film_id, "甲队", "复议甲")
    log = session.run({film.film_id: BING}, beats=3)
    assert log.openings[0].view == "外队"
    # 通行单元 0 是外队丙 ⇒ 甲只能 6 或 8 到棚，6/6=1 < 3/2 ⇒ 撤证
    assert log.adjudications == [("复议甲", 1, "撤证")]
