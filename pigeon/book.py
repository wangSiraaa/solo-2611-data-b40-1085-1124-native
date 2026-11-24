"""整棚钟簿：把鸽钟报文经 telber 事实仓库导入后，按棚整理成带来源的记次序列。

记录类型见 ``schemas/pigeon_clock_v1.json``：棚号 loft、门 gate、时刻 time、
开闭 action、读环 ring（可缺省）、空环 empty_ring（可缺省，显式空值）。

* 入门梁、出门梁一次只过一羽；只有"合"（``action=1``）记一次通过，"开"不记。
* 出门梁合上一次才记一次正式到棚；进棚通道的先进先出队列由
  :mod:`pigeon.engine` 归集。
* 读环三态：具名（天线在该截面扫到脚环）、缺省（缺读环字段，只知道有鸽，
  即某羽参赛鸽而身份未读）、显式空值（不参赛的空环模型）。
  ``ring`` 与 ``empty_ring`` 同现属于报文矛盾。
* 截断尾部不得作为已封棚：只有完整导入（import status ``complete``）的钟簿
  才算封棚；部分导入的已完成记次仍可载入，但 ``sealed`` 为假。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from telber.repository import Repository

ACTION_OPEN = 0
ACTION_CLOSE = 1
ACTION_NAMES = {ACTION_OPEN: "开", ACTION_CLOSE: "合"}

ROLE_ENTRY = "entry"
ROLE_EXIT = "exit"
ROLE_NAMES = {ROLE_ENTRY: "入门梁", ROLE_EXIT: "出门梁"}

DEFAULT_GATE_ROLES = {0: ROLE_ENTRY, 1: ROLE_EXIT}

MAX_PIGEONS = 6  # 限六羽
MAX_CROSSINGS = 12  # 限十二次通过

READ_NAMED = "named"
READ_MISSING = "missing"
READ_EMPTY = "empty"

READ_NAMES = {READ_NAMED: "具名", READ_MISSING: "缺读环", READ_EMPTY: "空环"}


class BookError(Exception):
    """钟簿报文与鸽钟记录类型相悖（带记次序号与报文范围）。"""

    def __init__(
        self,
        message: str,
        *,
        ordinal: int | None = None,
        record_range: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.ordinal = ordinal
        self.record_range = record_range

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        where = ""
        if self.ordinal is not None:
            where += f" (记次 {self.ordinal}"
            if self.record_range is not None:
                where += f", 报文 [{self.record_range[0]},{self.record_range[1]})"
            where += ")"
        return f"{self.message}{where}"


@dataclass(frozen=True)
class RingRead:
    """一次过梁的读环结果及其字段报文范围。"""

    kind: str  # READ_NAMED / READ_MISSING / READ_EMPTY
    ring: bytes | None = None
    source_ranges: tuple[tuple[str, int, int], ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ring": None if self.ring is None else self.ring.hex(),
            "source_ranges": [list(r) for r in self.source_ranges],
        }


@dataclass(frozen=True)
class Event:
    """一条鸽钟记次：序号即公开过梁号，报文范围指向原始字节流。"""

    ordinal: int
    loft: int
    gate: int
    role: str
    time: int
    action: int
    record_range: tuple[int, int]
    read: RingRead | None  # None 表示"开"，不发生通过

    @property
    def is_crossing(self) -> bool:
        return self.action == ACTION_CLOSE

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "loft": self.loft,
            "gate": self.gate,
            "role": self.role,
            "time": self.time,
            "action": self.action,
            "crossing": self.is_crossing,
            "record_range": list(self.record_range),
            "read": None if self.read is None else self.read.as_dict(),
        }


@dataclass
class ClockBook:
    """一棚一航次的整棚钟簿。"""

    loft: int
    import_id: int
    events: list[Event]
    sealed: bool
    import_status: str
    import_error: dict | None = None
    skipped_lofts: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"import-{self.import_id}/loft-{self.loft}"

    @property
    def crossings(self) -> list[Event]:
        return [e for e in self.events if e.is_crossing]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "loft": self.loft,
            "import_id": self.import_id,
            "sealed": self.sealed,
            "import_status": self.import_status,
            "import_error": self.import_error,
            "skipped_lofts": list(self.skipped_lofts),
            "events": [e.as_dict() for e in self.events],
        }


def load_book(
    repo: Repository,
    import_id: int,
    *,
    loft: int,
    gate_roles: Mapping[int, str] | None = None,
    max_crossings: int = MAX_CROSSINGS,
) -> ClockBook:
    """从事实仓库载入一棚的记次，保留各记次实际来源的报文范围。

    ``gate_roles`` 把门号映射为 ``entry``/``exit``（入门梁/出门梁）。
    其他棚的记次被滤出并记入 ``skipped_lofts``，不混入本棚队列。
    """
    roles = dict(DEFAULT_GATE_ROLES if gate_roles is None else gate_roles)
    info = repo.import_info(import_id)
    records = repo.records(import_id)
    relation = repo.relation_rows(import_id)
    ranges = repo.source_ranges(import_id)

    fields_by_record: dict[int, dict[str, dict]] = {}
    for row in relation:
        fields_by_record.setdefault(row["record_ordinal"], {})[row["path"]] = row
    ranges_by_record: dict[int, dict[str, list[tuple[str, int, int]]]] = {}
    for row in ranges:
        ranges_by_record.setdefault(row["record_ordinal"], {}).setdefault(
            row["path"], []
        ).append((row["role"], row["start_offset"], row["end_offset"]))

    def required_int(ordinal: int, path: str, rec_range: tuple[int, int]) -> int:
        row = fields_by_record.get(ordinal, {}).get(path)
        if row is None or not row["present"] or row["int_value"] is None:
            raise BookError(f"记次缺少必备字段 {path}", ordinal=ordinal, record_range=rec_range)
        return int(row["int_value"])

    events: list[Event] = []
    skipped: set[int] = set()
    prev_time: int | None = None
    crossing_count = 0
    for rec in records:
        ordinal = rec["ordinal"]
        rec_range = (rec["start_offset"], rec["end_offset"])
        r_loft = required_int(ordinal, "loft", rec_range)
        if r_loft != loft:
            skipped.add(r_loft)
            continue
        gate = required_int(ordinal, "gate", rec_range)
        time = required_int(ordinal, "time", rec_range)
        action = required_int(ordinal, "action", rec_range)
        if action not in (ACTION_OPEN, ACTION_CLOSE):
            raise BookError(
                f"开闭取值只能是 0(开)/1(合)，实为 {action}",
                ordinal=ordinal,
                record_range=rec_range,
            )
        if gate not in roles:
            raise BookError(
                f"门 {gate} 不在钟房的门角色配置中",
                ordinal=ordinal,
                record_range=rec_range,
            )
        if prev_time is not None and time < prev_time:
            raise BookError(
                f"时刻回退：{prev_time} 之后出现 {time}",
                ordinal=ordinal,
                record_range=rec_range,
            )
        prev_time = time

        rows = fields_by_record.get(ordinal, {})
        ring_row = rows.get("ring")
        empty_row = rows.get("empty_ring")
        ring_present = bool(ring_row and ring_row["present"])
        empty_present = bool(empty_row and empty_row["present"])
        if ring_present and empty_present:
            raise BookError(
                "读环与空环不得同现：一次通过只能具名、缺读环或显式空值其一",
                ordinal=ordinal,
                record_range=rec_range,
            )

        read: RingRead | None = None
        if action == ACTION_CLOSE:
            crossing_count += 1
            if crossing_count > max_crossings:
                raise BookError(
                    f"通过次数超过上限 {max_crossings}（限十二次通过）",
                    ordinal=ordinal,
                    record_range=rec_range,
                )
            if ring_present:
                read = RingRead(
                    READ_NAMED,
                    bytes(ring_row["bytes_value"]),
                    tuple(ranges_by_record.get(ordinal, {}).get("ring", [])),
                )
            elif empty_present:
                read = RingRead(
                    READ_EMPTY,
                    None,
                    tuple(ranges_by_record.get(ordinal, {}).get("empty_ring", [])),
                )
            else:
                read = RingRead(READ_MISSING)
        events.append(
            Event(
                ordinal=ordinal,
                loft=loft,
                gate=gate,
                role=roles[gate],
                time=time,
                action=action,
                record_range=rec_range,
                read=read,
            )
        )

    sealed = info.status == "complete"
    import_error = None
    if not sealed:
        import_error = {
            "status": info.status,
            "error_code": info.error_code,
            "error_offset": info.error_offset,
            "error_record_start": info.error_record_start,
        }
    return ClockBook(
        loft=loft,
        import_id=import_id,
        events=events,
        sealed=sealed,
        import_status=info.status,
        import_error=import_error,
        skipped_lofts=tuple(sorted(skipped)),
    )
