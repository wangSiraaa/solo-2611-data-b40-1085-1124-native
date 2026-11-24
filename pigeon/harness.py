"""演示与测试共用的导入便捷封装（裁决路径不经过这里）。"""

from __future__ import annotations

from pathlib import Path

from telber.errors import TelberError
from telber.repository import Repository

from .book import ClockBook, load_book

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "pigeon_clock_v1.json"


def publish(repo: Repository):
    return repo.publish_product(SCHEMA_PATH.read_text(encoding="utf-8"))


def ingest_book(
    repo: Repository,
    product,
    data: bytes,
    *,
    loft: int = 1,
    gate_roles=None,
    source_name: str = "clock.bin",
    block_size: int | None = None,
) -> tuple[ClockBook, int, object]:
    """导入字节流并载入整棚钟簿；截断等部分导入也照常载入（sealed 为假）。"""
    import_id = repo.create_import(product.product_id, source_name=source_name)
    try:
        info = repo.ingest_bytes(import_id, data, block_size=block_size)
    except TelberError as exc:
        info = getattr(exc, "info", repo.import_info(import_id))
    book = load_book(repo, import_id, loft=loft, gate_roles=gate_roles)
    return book, import_id, info
