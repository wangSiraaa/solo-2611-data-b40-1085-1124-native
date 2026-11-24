"""系统门面：持有一条 SQLite 连接，组合目录与台账两个独立服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db
from .catalog import CatalogService
from .ledger import LedgerService
from .storybox import StoryBoxDesk


class LibrarySystem:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self.catalog = CatalogService(self.conn)
        self.ledger = LedgerService(self.conn, self.catalog)
        # 故事盒带读层复用同一连接；盒子资料/签约单表由该服务自行幂等建立。
        self.storybox = StoryBoxDesk(self.conn)
        # 常见语义别名，方便不同调用习惯。
        self.story_box = self.storybox
        self.scent_desk = self.storybox
        self.guide_desk = self.storybox

    @classmethod
    def reopen(cls, db_path: str) -> "LibrarySystem":
        """重开持久化库：schema 用 IF NOT EXISTS 幂等建立，历史数据原样可见。"""
        if db_path == ":memory:":
            raise ValueError("cannot reopen an in-memory database")
        if not Path(db_path).exists():
            raise FileNotFoundError(db_path)
        return cls(db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LibrarySystem":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
