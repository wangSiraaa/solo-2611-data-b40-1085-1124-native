"""pigeon — 赛鸽协会成绩复核：鸽钟记录类型、整棚履历、达速证与复议。

构建在 telber 的 BER 子集编解码与事实仓库之上：

``book``        整棚钟簿：棚号/门/时刻/开闭/读环记次的导入与报文来源
``engine``      FIFO 队列 + 梁/天线约束 → 全部相容整棚履历
``certificate`` 团体达速证裁决（盖章/撤证/待核）、见证与匿名回执、互斥对质
``review``      复议：孤本底片、联合听证（本场采用）与队席分权内核
``wire``        独立字节拼装（仅测试与演示）
"""

from .book import (
    ACTION_CLOSE,
    ACTION_OPEN,
    DEFAULT_GATE_ROLES,
    MAX_CROSSINGS,
    MAX_PIGEONS,
    ROLE_ENTRY,
    ROLE_EXIT,
    BookError,
    ClockBook,
    Event,
    RingRead,
    load_book,
)
from .certificate import (
    OUTCOME_INCONSISTENT,
    OUTCOME_PENDING,
    OUTCOME_REVOKE,
    OUTCOME_STAMP,
    OUTCOME_UNSEALED,
    CrossCheck,
    Decision,
    FlightCard,
    PigeonCard,
    Registration,
    Witness,
    cross_check,
    decide,
    team_receipt,
)
from .engine import EMPTY, EmptyRing, History, QueueInconsistent, enumerate_histories
from .review import (
    DistributedSession,
    Film,
    JointHearing,
    ReviewError,
    ReviewItem,
    BookContext,
)

__all__ = [
    "ACTION_CLOSE",
    "ACTION_OPEN",
    "DEFAULT_GATE_ROLES",
    "MAX_CROSSINGS",
    "MAX_PIGEONS",
    "ROLE_ENTRY",
    "ROLE_EXIT",
    "BookError",
    "ClockBook",
    "Event",
    "RingRead",
    "load_book",
    "EMPTY",
    "EmptyRing",
    "History",
    "QueueInconsistent",
    "enumerate_histories",
    "FlightCard",
    "PigeonCard",
    "Registration",
    "Decision",
    "Witness",
    "CrossCheck",
    "decide",
    "team_receipt",
    "cross_check",
    "OUTCOME_STAMP",
    "OUTCOME_REVOKE",
    "OUTCOME_PENDING",
    "OUTCOME_UNSEALED",
    "OUTCOME_INCONSISTENT",
    "Film",
    "ReviewItem",
    "BookContext",
    "JointHearing",
    "DistributedSession",
    "ReviewError",
]
