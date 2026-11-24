"""KnitRoom 门面：在 library 的同一条 SQLite 连接上组合版师与缝台。

版师构造时拿到 ledger 句柄但只做只读查询；缝台只依赖版师。
重开复用 LibrarySystem 的持久化文件，针织域 schema 幂等建立，历史记录原样可见。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from library import db
from library.system import LibrarySystem

from . import schema
from .pattern import PatternService
from .sewing import SewingService


class KnitRoom:
    def __init__(self, library: LibrarySystem) -> None:
        self.library = library
        self.conn = library.conn
        schema.ensure_schema(self.conn)
        self.pattern = PatternService(self.conn, library.ledger)
        self.sewing = SewingService(self.conn, self.pattern)

    @classmethod
    def open(cls, db_path: str = ":memory:") -> "KnitRoom":
        """新建或打开一套“图书馆 + 针织班”库。"""
        return cls(LibrarySystem(db_path))

    @classmethod
    def reopen(cls, db_path: str) -> "KnitRoom":
        if db_path == ":memory:":
            raise ValueError("cannot reopen an in-memory database")
        if not Path(db_path).exists():
            raise FileNotFoundError(db_path)
        return cls(LibrarySystem.reopen(db_path))

    def close(self) -> None:
        self.library.close()

    def __enter__(self) -> "KnitRoom":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
