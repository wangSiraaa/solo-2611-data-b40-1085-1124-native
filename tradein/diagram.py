"""实机分解图：格（cell）/件（piece）/字篮（basket）/复装（reassembly）。

一台旧机被画成一张分解图。图上的**件**沿 *依序* 的阶段排列：
    0 螺帽（nut） → 1 压条（retainer / 固定杆） → 2 字杆（bar）。
只有当所有更低阶段的件都拆掉后，才能拆下一阶段的件；同阶段内顺序自由
（正是甲机“先取Y、后取X”能成立的空间）。

拆下一件时，会按 `Piece.damage` 损掉**登记在案的相邻字杆**——这就是
“拆下一格会损掉登记的相邻字杆 / 取 X 会损 Y”。若目标杆已经在更早一步
被取走，损伤落在空处，不影响已到手的杆。

字篮（basket）把若干字杆整体拆下：能保住篮内的杆，但它们从此只作为
“一整副字篮”交付，**不得再当散杆交**。

部分操作标为可复装（`Piece.reversible`），复装占用 `reassemble_cells` 格班时；
不可复装的操作一旦发生，整机就再也不能当作“整机”追认。

所有时间都以“格班时”计：每拆一件占 `Piece.cells` 格，另占一个工位（由
调用方的工位容量约束，见 workshop/convert）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

PHASE_NUT = 0
PHASE_RETAINER = 1
PHASE_BAR = 2

_KIND_PHASE = {"nut": PHASE_NUT, "retainer": PHASE_RETAINER, "bar": PHASE_BAR}


class CellLockError(Exception):
    """阶段顺序、字篮或重复拆取被违反。"""


@dataclass(frozen=True)
class Piece:
    """分解图上的一件。name 唯一标识一个格位。"""

    name: str
    kind: str  # "nut" | "retainer" | "bar"
    key: Optional[str] = None  # 字杆的键位（具名订单按此匹配）
    cells: int = 1  # 拆下该件占用的格班时（“若干整格”）
    damage: tuple[str, ...] = ()  # 拆下本件时损掉的相邻字杆（件名）
    reversible: bool = False  # 是否可复装
    reassemble_cells: int = 0  # 复装占用的格班时

    def __post_init__(self):
        if self.kind not in _KIND_PHASE:
            raise ValueError(f"未知件类型 {self.kind!r}")
        if self.kind == "bar" and not self.key:
            raise ValueError(f"字杆 {self.name} 必须有键位 key")
        if self.cells < 0:
            raise ValueError("cells 不能为负")
        if self.reversible and self.reassemble_cells < 0:
            raise ValueError("reassemble_cells 不能为负")

    @property
    def phase(self) -> int:
        return _KIND_PHASE[self.kind]


@dataclass(frozen=True)
class Basket:
    """一整副字篮：整体拆走，保住篮内杆但不可再作散杆交。"""

    name: str
    members: tuple[str, ...]  # 成员件的 name（必须都是 bar）
    cells: int = 2  # 拆整副字篮占用的格班时


@dataclass
class Diagram:
    """一台旧机的实机分解图。"""

    model: str
    pieces: "dict[str, Piece]"
    basket: Optional[Basket] = None
    whole_cells: int = 0  # 整机交走占用的格班时（通常为 0：直接抬走）
    label: str = ""

    def __post_init__(self):
        if not self.model:
            raise ValueError("model 必须非空")
        for name, piece in self.pieces.items():
            if name != piece.name:
                raise ValueError(f"pieces 键 {name} 与 Piece.name {piece.name} 不一致")
            for victim in piece.damage:
                if victim not in self.pieces:
                    raise ValueError(f"{piece.name} 登记的损伤目标 {victim} 不存在")
        if self.basket is not None:
            for member in self.basket.members:
                piece = self.pieces.get(member)
                if piece is None or piece.kind != "bar":
                    raise ValueError(f"字篮成员 {member} 必须是已登记的字杆")

    # ----------------------------------------------------------------- helpers
    def phase(self, piece: Piece) -> int:
        return piece.phase

    def by_phase(self, phase: int) -> list[Piece]:
        return [p for p in self.pieces.values() if p.phase == phase]

    def bar_pieces(self) -> list[Piece]:
        return self.by_phase(PHASE_BAR)

    def basket_members(self) -> frozenset[str]:
        return frozenset(self.basket.members) if self.basket else frozenset()

    # ------------------------------------------------------------ state machine
    def initial_state(self) -> "WorkState":
        return WorkState()

    def apply_remove(self, state: "WorkState", piece_name: str) -> "WorkState":
        """拆下某件，返回新状态（不修改原状态）。"""
        self._require_removable(state, piece_name)
        piece = self.pieces[piece_name]
        new = state.copy()
        new.spent += piece.cells
        new.removed.add(piece_name)
        # 损伤落在仍“在场”的相邻字杆上；已拆走/已进篮的则落空
        for victim in piece.damage:
            v = self.pieces[victim]
            if v.kind != "bar":
                continue
            if victim in state.removed:
                continue
            if state.basket_taken and victim in self.basket_members():
                continue
            new.damaged.add(victim)
        if piece.kind == "bar" and piece_name not in state.damaged:
            # 完好到手；进篮的杆不算散杆
            if not (state.basket_taken and piece_name in self.basket_members()):
                new.produced.append(Item(self.model, "bar", piece.key, piece_name))
        return new

    def apply_take_basket(self, state: "WorkState") -> "WorkState":
        self._require_basket(state)
        assert self.basket is not None
        new = state.copy()
        new.spent += self.basket.cells
        new.basket_taken = True
        for member in self.basket.members:
            new.removed.add(member)
        new.produced.append(Item(self.model, "basket", None, self.basket.name))
        return new

    def apply_deliver_whole(self, state: "WorkState") -> "WorkState":
        self._require_whole(state)
        new = state.copy()
        new.spent += self.whole_cells
        # 已拆的可复装件需复装回位（花掉的班时照计，复装另计）
        new.spent += sum(
            self.pieces[name].reassemble_cells for name in state.removed
        )
        new.whole_delivered = True
        new.produced.append(Item(self.model, "whole_machine", None, "whole"))
        return new

    # ------------------------------------------------------------- legality
    def _phase_ready(self, state: "WorkState", phase: int) -> bool:
        for other in self.pieces.values():
            if other.phase < phase and other.name not in state.removed:
                return False
        return True

    def _require_removable(self, state: "WorkState", piece_name: str) -> None:
        piece = self.pieces.get(piece_name)
        if piece is None:
            raise CellLockError(f"未知件 {piece_name!r}")
        if state.whole_delivered:
            raise CellLockError("整机已交走，不能再拆")
        if piece_name in state.removed:
            raise CellLockError(f"件 {piece_name} 已拆过")
        if state.basket_taken and piece_name in self.basket_members():
            raise CellLockError(f"字篮已整体拆走，{piece_name} 不能再当散杆取")
        if not self._phase_ready(state, piece.phase):
            raise CellLockError(
                f"阶段顺序：拆 {piece_name}(阶段{piece.phase}) 前须先拆完更低阶段"
            )

    def _require_basket(self, state: "WorkState") -> None:
        if self.basket is None:
            raise CellLockError(f"{self.model} 没有登记字篮")
        if state.whole_delivered:
            raise CellLockError("整机已交走，不能再拆字篮")
        if state.basket_taken:
            raise CellLockError("字篮已拆过")
        if not self._phase_ready(state, PHASE_BAR):
            raise CellLockError("拆字篮前须先拆完螺帽与压条")
        for member in self.basket.members:
            if member in state.removed:
                raise CellLockError(f"字篮成员 {member} 已被拆走，不能整体拆篮")
            if member in state.damaged:
                raise CellLockError(f"字篮成员 {member} 已损坏，不能整体拆篮")

    def _require_whole(self, state: "WorkState") -> None:
        if state.whole_delivered:
            raise CellLockError("整机已交走")
        if state.basket_taken:
            raise CellLockError("已拆字篮，不能再追认整机")
        for name in state.removed:
            piece = self.pieces[name]
            if not piece.reversible:
                raise CellLockError(f"件 {name} 不可复装，整机不能再追认")
        # produced 里若已有散杆，也不能再追认整机
        if any(item.kind in ("bar", "basket") for item in state.produced):
            raise CellLockError("已有散杆/字篮交出，不能再追认整机")

    # ----------------------------------------------------- irreversibility
    def first_irreversible(self, state: "WorkState") -> Optional[str]:
        """返回已发生的首个人为不可复装件名（按拆除集合判断，None 表示尚无）。"""
        for name in state.removed:
            if not self.pieces[name].reversible:
                return name
        return None

    def reassembly_cost(self, state: "WorkState") -> int:
        return sum(self.pieces[name].reassemble_cells for name in state.removed)


@dataclass(frozen=True)
class Item:
    """一件可交付物：一根散杆、一整副字篮，或整机。"""

    model: str
    kind: str  # "bar" | "basket" | "whole_machine"
    key: Optional[str] = None
    source: str = ""  # 件名 / 字篮名 / "whole"，用于追溯

    def matches(self, model: str, kind: str, key: Optional[str]) -> bool:
        if self.model != model or self.kind != kind:
            return False
        if kind == "bar":
            return self.key == key
        return True

    def as_json(self) -> dict:
        return {"model": self.model, "kind": self.kind, "key": self.key, "source": self.source}


@dataclass
class WorkState:
    """一台旧机的拆取进度（可变，用 copy() 分叉）。"""

    removed: "set[str]" = field(default_factory=set)
    damaged: "set[str]" = field(default_factory=set)
    basket_taken: bool = False
    whole_delivered: bool = False
    spent: int = 0
    produced: list[Item] = field(default_factory=list)

    def copy(self) -> "WorkState":
        return WorkState(
            removed=set(self.removed),
            damaged=set(self.damaged),
            basket_taken=self.basket_taken,
            whole_delivered=self.whole_delivered,
            spent=self.spent,
            produced=list(self.produced),
        )

    def fingerprint(self) -> tuple:
        return (
            frozenset(self.removed),
            frozenset(self.damaged),
            self.basket_taken,
            self.whole_delivered,
            tuple(sorted((i.kind, i.model, i.key or "", i.source) for i in self.produced)),
        )
