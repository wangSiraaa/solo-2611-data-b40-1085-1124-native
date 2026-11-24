"""连印带推断（制带室）。

玩具社从拆包回条中抽样还原贴纸厂的连印带。规则（题目固定，照此实现）：

* 一圈固定的具名格位绕回起点；每条候选带型列出每一格的图案与亮/普面。
* 分装机按指定包序连续切下若干格，一班只有一个**未知起格**和一个
  **前进方向**（正向按格位号递增绕回，反向按格位号递减绕回）。
* 回条来自确实入样的开包人，注明包序、包内第几张，以及“看到的图”或
  “亮/普面”至少其一。
* 制带室要核出哪些（带型、起格、方向）能**共同**解释全部回条——同一条
  带、同一个起格、同一个方向必须同时满足每一张回条，不能不同包各挑一个
  有利的起格。
* 还可查同一个未开包在不同解释下有哪些整组内容；以及拿到新观察后
  （开包、回条撤回）信念如何更新。

本模块是纯函数/纯数据结构，无 I/O，只依赖标准库；与换卡柜
(:mod:`wrsource.counter`) 共用同一套“候选带 + 回条 -> 可行解释 -> 信念”
词汇，柜台直接消费 :func:`TapeRoom.build_belief` 的输出。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

FORWARD = 1
REVERSE = -1
DIRECTIONS = (FORWARD, REVERSE)
DIRECTION_NAME = {FORWARD: "forward", REVERSE: "reverse"}
NAME_DIRECTION = {v: k for k, v in DIRECTION_NAME.items()}


class TapeError(ValueError):
    """带型/回条等输入不合法。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Card:
    """一张卡的完整具名图案：图案 + 面（True=亮面, False=普通面）。"""

    design: str
    foil: bool

    def as_dict(self) -> dict:
        return {"design": self.design, "foil": self.foil}

    @staticmethod
    def from_dict(obj: Mapping) -> "Card":
        return Card(str(obj["design"]), bool(obj["foil"]))

    def label(self) -> str:
        return f"{'亮' if self.foil else '普'}{self.design}"


@dataclass(frozen=True)
class TapePattern:
    """一条候选带型：固定格位号 0..n-1，每格一张完整卡面。"""

    name: str
    cells: Tuple[Card, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise TapeError("tape pattern name must not be empty")
        if not self.cells:
            raise TapeError(f"tape {self.name!r} has no cells")

    @property
    def length(self) -> int:
        return len(self.cells)

    def as_dict(self) -> dict:
        return {"name": self.name,
                "cells": [c.as_dict() for c in self.cells]}

    @staticmethod
    def from_dict(obj: Mapping) -> "TapePattern":
        return TapePattern(str(obj["name"]),
                           tuple(Card.from_dict(c) for c in obj["cells"]))


@dataclass(frozen=True)
class Receipt:
    """一张入样回条。

    ``design`` / ``foil`` 至少给一个；只给面时表示该回条只观察了亮/普面。
    ``pack_index`` 是分装机的包序（0 起），``slot_in_pack`` 是包内第几张
    （0 起；每包一张的场景恒为 0）。
    """

    receipt_id: str
    pack_index: int
    slot_in_pack: int
    design: Optional[str] = None
    foil: Optional[bool] = None

    def __post_init__(self) -> None:
        if not self.receipt_id:
            raise TapeError("receipt_id must not be empty")
        if self.pack_index < 0:
            raise TapeError("pack_index must be >= 0")
        if self.slot_in_pack < 0:
            raise TapeError("slot_in_pack must be >= 0")
        if self.design is None and self.foil is None:
            raise TapeError(
                f"receipt {self.receipt_id!r} must observe design or foil")

    def matches(self, card: Card) -> bool:
        if self.design is not None and card.design != self.design:
            return False
        if self.foil is not None and card.foil != self.foil:
            return False
        return True

    def as_dict(self) -> dict:
        return {"receipt_id": self.receipt_id,
                "pack_index": self.pack_index,
                "slot_in_pack": self.slot_in_pack,
                "design": self.design, "foil": self.foil}

    @staticmethod
    def from_dict(obj: Mapping) -> "Receipt":
        return Receipt(str(obj["receipt_id"]), int(obj["pack_index"]),
                       int(obj["slot_in_pack"]),
                       obj.get("design"),
                       None if obj.get("foil") is None else bool(obj["foil"]))


@dataclass(frozen=True)
class Hypothesis:
    """一个可行的整班解释：某条带型 + 唯一未知起格 + 唯一方向。"""

    pattern: TapePattern
    start_cell: int
    direction: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_cell < self.pattern.length:
            raise TapeError("start_cell out of range")
        if self.direction not in DIRECTIONS:
            raise TapeError("direction must be FORWARD/REVERSE")

    @property
    def key(self) -> Tuple[str, int, int]:
        return (self.pattern.name, self.start_cell, self.direction)

    def cell_at(self, pack_index: int, slot_in_pack: int,
                pack_size: int = 1) -> Card:
        """该解释下，指定包序、包内格位切到的完整卡面。"""
        n = self.pattern.length
        if pack_size <= 0:
            raise TapeError("pack_size must be positive")
        steps = pack_index * pack_size + slot_in_pack
        idx = (self.start_cell + self.direction * steps) % n
        return self.pattern.cells[idx]

    def pack_content(self, pack_index: int, pack_size: int) -> Tuple[Card, ...]:
        return tuple(self.cell_at(pack_index, s, pack_size)
                     for s in range(pack_size))

    def as_dict(self) -> dict:
        return {"pattern": self.pattern.name,
                "start_cell": self.start_cell,
                "direction": DIRECTION_NAME[self.direction]}


@dataclass(frozen=True)
class Belief:
    """一组共同解释全部回条的可行解释（同一未开包的全部可能包况）。"""

    hypotheses: Tuple[Hypothesis, ...]
    pack_size: int = 1
    receipts: Tuple[Receipt, ...] = ()

    def __post_init__(self) -> None:
        if self.pack_size <= 0:
            raise TapeError("pack_size must be positive")

    @property
    def possible(self) -> bool:
        return bool(self.hypotheses)

    def pack_outcomes(self, pack_index: int
                      ) -> Tuple[Tuple[Card, ...], ...]:
        """同一未开包在不同解释下的整组内容（去重，保持稳定顺序）。"""
        seen: Dict[Tuple[Card, ...], None] = {}
        for h in self.hypotheses:
            content = h.pack_content(pack_index, self.pack_size)
            seen.setdefault(content, None)
        return tuple(seen)

    def observed_pack_indices(self) -> Tuple[int, ...]:
        return tuple(sorted({r.pack_index for r in self.receipts}))

    def as_dict(self) -> dict:
        return {
            "pack_size": self.pack_size,
            "receipts": [r.as_dict() for r in self.receipts],
            "hypotheses": [h.as_dict() for h in self.hypotheses],
            "possible": self.possible,
        }


# ---------------------------------------------------------------------------
# 制带室
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TapeRoom:
    """候选带型 + 入样回条 -> 可行解释与未开包包况。

    柜台按这版整包候选接收新的册号、缺卡清单及可拆包号，因此本类是
    不可变值对象：换一版候选就新建一个 ``TapeRoom``。
    """

    patterns: Tuple[TapePattern, ...]
    receipts: Tuple[Receipt, ...] = ()
    pack_size: int = 1
    sampled_pack_indices: Optional[frozenset] = None

    def __post_init__(self) -> None:
        if not self.patterns:
            raise TapeError("at least one candidate tape pattern is required")
        names = [p.name for p in self.patterns]
        if len(set(names)) != len(names):
            raise TapeError("duplicate tape pattern names")
        if self.pack_size <= 0:
            raise TapeError("pack_size must be positive")
        rids = [r.receipt_id for r in self.receipts]
        if len(set(rids)) != len(rids):
            raise TapeError("duplicate receipt ids")
        n = self.patterns[0].length
        for p in self.patterns[1:]:
            if p.length != n:
                raise TapeError("all candidate tapes must share the fixed "
                                "ring length")
        for r in self.receipts:
            if r.slot_in_pack >= self.pack_size:
                raise TapeError(
                    f"receipt {r.receipt_id!r} slot exceeds pack size")
        if self.sampled_pack_indices is not None:
            for r in self.receipts:
                if r.pack_index not in self.sampled_pack_indices:
                    raise TapeError(
                        f"receipt {r.receipt_id!r} comes from a pack that is "
                        "not in the sampled directory")

    # -- 构造 ------------------------------------------------------------
    @staticmethod
    def from_dict(obj: Mapping) -> "TapeRoom":
        patterns = tuple(TapePattern.from_dict(p) for p in obj["patterns"])
        receipts = tuple(Receipt.from_dict(r) for r in obj.get("receipts", ()))
        sampled = obj.get("sampled_pack_indices")
        return TapeRoom(
            patterns, receipts,
            pack_size=int(obj.get("pack_size", 1)),
            sampled_pack_indices=None if sampled is None
            else frozenset(int(x) for x in sampled))

    def with_receipts(self, receipts: Sequence[Receipt]) -> "TapeRoom":
        """换一组回条（撤回/新增回条都走这里，候选带型不变）。"""
        return replace(self, receipts=tuple(receipts))

    def add_receipt(self, receipt: Receipt) -> "TapeRoom":
        return self.with_receipts((*self.receipts, receipt))

    def retract_receipt(self, receipt_id: str) -> "TapeRoom":
        kept = tuple(r for r in self.receipts
                     if r.receipt_id != receipt_id)
        if len(kept) == len(self.receipts):
            raise TapeError(f"receipt not found: {receipt_id!r}")
        return self.with_receipts(kept)

    # -- 推断 ------------------------------------------------------------
    def _explains(self, h: Hypothesis) -> bool:
        # 同一个（带型、起格、方向）必须共同解释全部回条。
        for r in self.receipts:
            if not r.matches(h.cell_at(r.pack_index, r.slot_in_pack,
                                       self.pack_size)):
                return False
        return True

    def feasible_hypotheses(self,
                            patterns: Optional[Iterable[TapePattern]] = None,
                            start_cells: Optional[Iterable[int]] = None,
                            directions: Iterable[int] = DIRECTIONS
                            ) -> Tuple[Hypothesis, ...]:
        """枚举并核出能共同解释全部回条的解释，按稳定序排列。"""
        pats = tuple(patterns) if patterns is not None else self.patterns
        out: List[Hypothesis] = []
        for p in pats:
            cells = (range(p.length) if start_cells is None
                     else tuple(start_cells))
            for s in cells:
                if not 0 <= s < p.length:
                    raise TapeError("start_cell out of range")
                for d in directions:
                    if d not in DIRECTIONS:
                        raise TapeError("bad direction")
                    h = Hypothesis(p, s, d)
                    if self._explains(h):
                        out.append(h)
        out.sort(key=lambda h: h.key)
        return tuple(out)

    def build_belief(self) -> Belief:
        return Belief(self.feasible_hypotheses(), self.pack_size,
                      self.receipts)

    def pack_outcomes(self, pack_index: int) -> Tuple[Tuple[Card, ...], ...]:
        return self.build_belief().pack_outcomes(pack_index)

    # -- 信念更新 --------------------------------------------------------
    def observe(self, pack_index: int, slot_in_pack: int,
                design: Optional[str] = None,
                foil: Optional[bool] = None,
                receipt_id: Optional[str] = None) -> "TapeRoom":
        """拿到一次新的开包观察，等同于追加一张回条后重建制带室。"""
        rid = receipt_id or f"obs@{pack_index}.{slot_in_pack}"
        return self.add_receipt(Receipt(rid, pack_index, slot_in_pack,
                                        design, foil))

    def filter_belief(self, belief: Belief, pack_index: int,
                      observed: Sequence[Card]) -> Belief:
        """用某包开包实卡，把已有信念过滤到与观察相容的解释。"""
        observed = tuple(observed)
        if len(observed) != self.pack_size:
            raise TapeError("observed content must be one full pack")
        kept = tuple(
            h for h in belief.hypotheses
            if h.pack_content(pack_index, self.pack_size) == observed)
        return Belief(kept, self.pack_size, belief.receipts)
