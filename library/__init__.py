"""合并书目预约台账与故事盒带读求解层。

模块划分：
- catalog ：带修订的书目别名目录（不可变修订、单向别名边、多跳规范书目）
- ledger  ：独立的实物图书预约/取件台账（等待票据、保留、取件、回执）
- storybox：故事盒香片/遮蔽、两座排程、自由页保证与交班续办
- db      ：SQLite 连接、schema 与幂等基础设施
"""

from .errors import IdempotencyConflict, LibraryError, Rejection
from .storybox import (
    Card,
    MaskRule,
    Page,
    Reader,
    ReadingSession,
    Reveal,
    ScentBox,
    ScentCard,
    StoryBoxDesk,
    StoryBoxError,
    active_residues,
    analyze_turn,
    booking_demo_guide,
    canonical_mode,
    compatibility_conflicts,
    guaranteed_seats,
    jsonable,
    maximum_guaranteed_seats,
    plan_guaranteed,
    plan_session,
    replay_record,
    sign_booking,
    sign_sheet,
    songli_box,
    transfer_demo_guide,
    verify_run,
)
from .system import LibrarySystem

__all__ = [
    "LibrarySystem",
    "LibraryError",
    "Rejection",
    "IdempotencyConflict",
    "StoryBoxError",
    "StoryBoxDesk",
    "ScentBox",
    "ScentCard",
    "Card",
    "MaskRule",
    "Reveal",
    "Page",
    "Reader",
    "ReadingSession",
    "analyze_turn",
    "replay_record",
    "active_residues",
    "compatibility_conflicts",
    "plan_guaranteed",
    "plan_session",
    "maximum_guaranteed_seats",
    "guaranteed_seats",
    "sign_booking",
    "sign_sheet",
    "verify_run",
    "canonical_mode",
    "songli_box",
    "transfer_demo_guide",
    "booking_demo_guide",
    "jsonable",
]
