#!/usr/bin/env python3
"""赛鸽协会成绩复核演示：真实字节流导入、达速证裁决、互斥对质与复议听证。

运行（仓库根目录）::

    python3 -m pigeon.demo

场景（对应题目各问）：

1. 原案：甲乙同时从 0 出发，航距 6、9，截止 7 前两次归巢（时刻 4、6），
   关门 4 的过梁读到甲 ⇒ 两羽均达 3/2，盖章。
2. 仅把该条读环纠正为缺失 ⇒ 交换身份亦成立，待核，交相反两份整棚见证。
3. 恢复实名、仅把乙航距更正为 6 ⇒ 全部履历不达，撤证。
4. 同一羽出现在两个互斥评比的回执中 ⇒ 当场指出各自见证是否同真。
5. 截断尾部 ⇒ 不得作为已封棚，不出裁。
6. 复议：本场采用联合听证；先验两件各一片、期限两拍时两种组织都能受理，
   再复核联合听证在期限压到同一末拍时如实判定（不虚报已开两片）；
   队席分权下同一孤本的显影权不得被另一件取走。总共最多三张片、三个窗口拍。
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pigeon import harness  # noqa: E402
from pigeon.book import load_book  # noqa: E402
from pigeon.certificate import (  # noqa: E402
    FlightCard,
    PigeonCard,
    Registration,
    cross_check,
    decide,
    team_receipt,
)
from pigeon.engine import identity_name  # noqa: E402
from pigeon.review import (  # noqa: E402
    BookContext,
    DistributedSession,
    Film,
    JointHearing,
    ReviewError,
    ReviewItem,
)
from pigeon import wire  # noqa: E402
from telber.repository import Repository  # noqa: E402

JIA = "甲".encode("utf-8")
YI = "乙".encode("utf-8")
BING = "丙".encode("utf-8")
DING = "丁".encode("utf-8")

THRESHOLD = Fraction(3, 2)


def ring(ring_bytes: bytes) -> str:
    return identity_name(ring_bytes)


# ---------------------------------------------------------------------------
# 各场景字节流（独立拼装）

def stream_main(*, named_exit_read: bool) -> bytes:
    """甲乙航次：入门梁两次过梁、出门梁两次合上；关门 4 读环可具名甲或缺失。"""
    records = [
        wire.entry_close(loft=1, time=3),  # 入门梁过一羽，缺读环
        wire.exit_close(loft=1, time=4, ring=JIA if named_exit_read else None),
        wire.gate_open(loft=1, time=5, gate=1),  # 出门梁"开"不记通过
        wire.entry_close(loft=1, time=5),
        wire.exit_close(loft=1, time=6),
    ]
    return wire.stream(records)


def stream_exclusive() -> bytes:
    """甲乙丙航次：关门 4 读到甲，关门 6、8 缺读环。"""
    records = [
        wire.entry_close(loft=1, time=3),
        wire.exit_close(loft=1, time=4, ring=JIA),
        wire.entry_close(loft=1, time=5),
        wire.exit_close(loft=1, time=6),
        wire.entry_close(loft=1, time=7),
        wire.exit_close(loft=1, time=8),
    ]
    return wire.stream(records)


def stream_mirror(loft: int) -> bytes:
    """另一棚的同构航次（丙丁），全部缺读环。"""
    records = [
        wire.entry_close(loft=loft, time=3),
        wire.exit_close(loft=loft, time=4),
        wire.entry_close(loft=loft, time=5),
        wire.exit_close(loft=loft, time=6),
    ]
    return wire.stream(records)


def stream_shared(loft: int) -> bytes:
    """三羽航次（同一羽被两件复议依赖）。"""
    records = [
        wire.entry_close(loft=loft, time=3),
        wire.exit_close(loft=loft, time=4),
        wire.entry_close(loft=loft, time=5),
        wire.exit_close(loft=loft, time=6),
        wire.entry_close(loft=loft, time=7),
        wire.exit_close(loft=loft, time=8),
    ]
    return wire.stream(records)


# ---------------------------------------------------------------------------

def main_card(dist_yi: int = 9) -> FlightCard:
    return FlightCard(
        loft=1,
        close_time=7,
        pigeons=(
            PigeonCard(JIA, release=0, distance=6),
            PigeonCard(YI, release=0, distance=dist_yi),
        ),
    )


def main_registration(**kw) -> Registration:
    return Registration(
        team="甲队",
        evaluation="团体达速",
        pair=(JIA, YI),
        threshold=THRESHOLD,
        **kw,
    )


def run() -> dict:
    report: dict = {}
    repo = Repository(":memory:")
    product = harness.publish(repo)

    # -- 1. 原案：两羽均达 3/2 ----------------------------------------
    data1 = stream_main(named_exit_read=True)
    book1, import1, _ = harness.ingest_book(repo, product, data1, loft=1)
    decision1 = decide(book1, main_card(), main_registration())
    report["原案"] = {
        "裁决": decision1.outcome,
        "理由": decision1.reason,
        "相容履历数": decision1.history_count,
        "回执": team_receipt(decision1),
        "过梁报文范围": [e.as_dict() for e in book1.crossings],
    }

    # -- 2. 读环纠正为缺失：交换身份亦成立 ------------------------------
    data2 = stream_main(named_exit_read=False)
    book2, import2, _ = harness.ingest_book(repo, product, data2, loft=1)
    decision2 = decide(book2, main_card(), main_registration())
    report["读环缺失"] = {
        "裁决": decision2.outcome,
        "理由": decision2.reason,
        "相反两份见证": [w.as_dict() for w in decision2.witnesses],
    }

    # -- 3. 恢复实名、乙航距更正为 6 ------------------------------------
    book3 = book1
    decision3 = decide(book3, main_card(dist_yi=6), main_registration())
    report["乙航距更正"] = {"裁决": decision3.outcome, "理由": decision3.reason}

    # -- 4. 互斥评比：同一羽出现在两份回执 ------------------------------
    data4 = stream_exclusive()
    book4, import4, _ = harness.ingest_book(repo, product, data4, loft=1)
    card4 = FlightCard(
        loft=1,
        close_time=9,
        pigeons=(
            PigeonCard(JIA, 0, 6),
            PigeonCard(YI, 0, 9),
            PigeonCard(BING, 0, 9),
        ),
    )
    reg_a = Registration("甲队", "评比一", (JIA, YI), THRESHOLD, exclusive_group="互斥A")
    reg_b = Registration("乙队", "评比二", (JIA, BING), THRESHOLD, exclusive_group="互斥A")
    decision_a = decide(book4, card4, reg_a)
    decision_b = decide(book4, card4, reg_b)
    check = cross_check(decision_a, decision_b, book4, card4.universe())
    report["互斥评比"] = {
        "评比一": {"裁决": decision_a.outcome, "回执": team_receipt(decision_a)},
        "评比二": {"裁决": decision_b.outcome, "回执": team_receipt(decision_b)},
        "当场对质": check.as_dict(),
    }

    # -- 5. 截断尾部不得作为已封棚 --------------------------------------
    truncated_tail = wire.exit_close(loft=1, time=8)[:4]  # 一条残缺的记次
    data5 = data1 + truncated_tail
    book5, import5, info5 = harness.ingest_book(repo, product, data5, loft=1)
    decision5 = decide(book5, main_card(), main_registration())
    report["截断尾部"] = {
        "导入状态": book5.import_status,
        "封棚": book5.sealed,
        "裁决": decision5.outcome,
        "理由": decision5.reason,
    }

    # -- 6. 复议 ---------------------------------------------------------
    report["复议"] = review_section(repo, product, book2)

    repo.close()
    return report


def review_section(repo, product, book2) -> dict:
    """复议：联合听证（本场采用）与队席分权对照；最多三张片、三个窗口拍。"""
    out: dict = {}

    # 两件申请各有一张可辨自己压线到棚鸽的底片，期限两拍。
    ctx1 = BookContext(book2, main_card())
    reg1 = main_registration()
    item1 = ReviewItem("复议一", ctx1, reg1, deadline=2)
    film1 = Film("底片一", ctx1.key, token_index=0, crossing_ordinal=1)

    book_r2, _, _ = harness.ingest_book(repo, product, stream_mirror(2), loft=2)
    card2 = FlightCard(
        loft=2,
        close_time=7,
        pigeons=(PigeonCard(BING, 0, 6), PigeonCard(DING, 0, 9)),
    )
    ctx2 = BookContext(book_r2, card2)
    reg2 = Registration("乙队", "团体达速", (BING, DING), THRESHOLD)
    item2 = ReviewItem("复议二", ctx2, reg2, deadline=2)
    film2 = Film("底片二", ctx2.key, token_index=0, crossing_ordinal=1)

    contexts = {ctx1.key: ctx1, ctx2.key: ctx2}
    items = [item1, item2]
    films = [film1, film2]

    # 基准：两种组织都能受理（期限两拍）。
    hearing = JointHearing(contexts, items, films, beats=2)
    guaranteed, first_move = hearing.plan()
    log = hearing.run({film1.film_id: JIA, film2.film_id: BING})
    out["联合听证·基准"] = {
        "保证期限内判定件数": guaranteed,
        "首拍应开": first_move,
        "开庭记录": [
            {"拍": o.beat, "片": o.film_id, "显影": identity_name(o.value)}
            for o in log.openings
        ],
        "判定": [(a.item_id, a.beat, a.outcome) for a in log.adjudications],
        "逾期未判": log.expired,
    }

    session = DistributedSession(
        contexts, items, films, seats={"复议一": "甲队", "复议二": "乙队"}
    )
    session.issue_right(film1.film_id, "甲队", "复议一")
    session.issue_right(film2.film_id, "乙队", "复议二")
    slog = session.run({film1.film_id: JIA, film2.film_id: BING}, beats=2)
    out["队席分权·基准"] = {
        "开庭记录": [
            {"拍": o.beat, "席": o.seat, "片": o.film_id, "所见": o.view}
            for o in slog.openings
        ],
        "判定": slog.adjudications,
        "逾期未判": slog.expired,
    }

    # 复核联合听证：两件各看一片的期限压到同一末拍（第 1 拍），窗口三拍。
    tight1 = ReviewItem("复议一", ctx1, reg1, deadline=1)
    tight2 = ReviewItem("复议二", ctx2, reg2, deadline=1)
    hearing_tight = JointHearing(contexts, [tight1, tight2], films, beats=3)
    guaranteed_tight, first_tight = hearing_tight.plan()
    log_tight = hearing_tight.run({film1.film_id: JIA, film2.film_id: BING})
    out["联合听证·期限压到同一末拍"] = {
        "保证期限内判定件数": guaranteed_tight,
        "首拍应开": first_tight,
        "开庭记录": [
            {"拍": o.beat, "片": o.film_id, "显影": identity_name(o.value)}
            for o in log_tight.openings
        ],
        "判定": [(a.item_id, a.beat, a.outcome) for a in log_tight.adjudications],
        "逾期未判": log_tight.expired,
        "纪律": "每拍只接一张片；一张只能开一次；不虚报已开两片",
    }

    # 一片的发现可用在多份申请（联合听证）。
    reg_shared = Registration("甲队", "达标复核", (JIA, YI), Fraction(5, 4))
    item_shared = ReviewItem("复议三", ctx1, reg_shared, deadline=2)
    hearing_share = JointHearing(
        contexts, [item1, item_shared, item2], films, beats=2
    )
    guaranteed_share, _ = hearing_share.plan()
    log_share = hearing_share.run({film1.film_id: JIA, film2.film_id: BING})
    out["联合听证·一片多用"] = {
        "保证期限内判定件数": guaranteed_share,
        "判定": [(a.item_id, a.beat, a.outcome) for a in log_share.adjudications],
    }

    # 队席分权：两件都须凭同一张孤本补认同一羽，另一件不能取走显影权。
    book_r3, _, _ = harness.ingest_book(repo, product, stream_shared(3), loft=3)
    card3 = FlightCard(
        loft=3,
        close_time=9,
        pigeons=(
            PigeonCard(JIA, 0, 6),
            PigeonCard(YI, 0, 12),
            PigeonCard(BING, 0, 12),
        ),
    )
    ctx3 = BookContext(book_r3, card3)
    reg_x = Registration("甲队", "评比一", (JIA, YI), THRESHOLD)
    reg_y = Registration("乙队", "评比二", (JIA, BING), THRESHOLD)
    item_x = ReviewItem("复议甲", ctx3, reg_x, deadline=2)
    item_y = ReviewItem("复议乙", ctx3, reg_y, deadline=2)
    film_shared = Film("孤本片", ctx3.key, token_index=0, crossing_ordinal=1)
    session2 = DistributedSession(
        {ctx3.key: ctx3},
        [item_x, item_y],
        [film_shared],
        seats={"复议甲": "甲队", "复议乙": "乙队"},
    )
    session2.issue_right(film_shared.film_id, "甲队", "复议甲")
    try:
        session2.issue_right(film_shared.film_id, "乙队", "复议乙")
        reissue = "未拦截（错误）"
    except ReviewError as exc:
        reissue = f"已拦截：{exc}"
    slog2 = session2.run({film_shared.film_id: JIA}, beats=3)
    out["队席分权·孤本不得转用"] = {
        "重复发权": reissue,
        "开庭记录": [
            {"拍": o.beat, "席": o.seat, "片": o.film_id, "所见": o.view}
            for o in slog2.openings
        ],
        "判定": slog2.adjudications,
        "逾期未判": slog2.expired,
        "说明": "显影甲对双方都有利，但复议乙取不走该孤本的显影权",
    }
    return out


def main() -> int:
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
