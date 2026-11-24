"""独立字节拼装：仅测试与演示使用，裁决路径不经过这里。

与 :mod:`telber.build` 的关系同 ``telber/demo.py``：手工拼 TLV，不与
编解码器共享实现，以此独立地验证导入链路。
"""

from __future__ import annotations

from telber import build as B

from .book import ACTION_CLOSE, ACTION_OPEN


def clock_record(
    *,
    loft: int,
    gate: int,
    time: int,
    action: int,
    ring: bytes | None = None,
    empty: bool = False,
    indefinite: bool = False,
) -> bytes:
    """按 pigeon.clock v1 手工拼一条记次。"""
    body = B.integer(loft, cls="context", number=0)
    body += B.integer(gate, cls="context", number=1)
    body += B.integer(time, cls="context", number=2)
    body += B.integer(action, cls="context", number=3)
    if ring is not None:
        body += B.explicit(B.tag("context", True, 4), B.octets(ring))
    if empty:
        body += B.explicit(B.tag("context", True, 5), B.null())
    return B.tlv(B.tag("universal", True, 16), body, indefinite=indefinite)


def stream(records: list[bytes]) -> bytes:
    return b"".join(records)


def entry_close(*, loft: int, time: int, gate: int = 0, **kw) -> bytes:
    return clock_record(loft=loft, gate=gate, time=time, action=ACTION_CLOSE, **kw)


def exit_close(*, loft: int, time: int, gate: int = 1, **kw) -> bytes:
    return clock_record(loft=loft, gate=gate, time=time, action=ACTION_CLOSE, **kw)


def gate_open(*, loft: int, time: int, gate: int) -> bytes:
    return clock_record(loft=loft, gate=gate, time=time, action=ACTION_OPEN)
