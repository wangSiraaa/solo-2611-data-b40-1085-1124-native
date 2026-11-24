"""Lacquer stacking-box domain model.

A rim is divided into ``divisions`` equal angular cells (cells are numbered
clockwise ``0 .. divisions-1``).  A cell may carry at most one feature:

* a **lug** ("ear", 卡耳) -- a protruding tab;
* a **notch** ("mouth", 缺口) -- a recess that a lug can drop into;
* nothing at all.

Two rims mate when every lug of the lower rim sits on a notch of the upper
rim.  The upper rim may have additional notches that no lug uses; a lug may
never meet a cell that is not a notch.

A vessel (an intermediate box) has two rims -- the bottom rim
(``bottom``, notches facing downwards) and the top rim (``top``, lugs facing
upwards) -- that share one physical rotation.  Bases expose lugs, lids expose
notches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

LUG = "lug"
NOTCH = "notch"
BLANK = None

# Stable machine-readable vocabulary used in JSON materials and reports.
KIND_LUG = "lug"
KIND_NOTCH = "notch"


@dataclass(frozen=True)
class Ring:
    """One physical rim: the feature at each cell (``None`` when empty)."""

    cells: tuple[str | None, ...]
    name: str = "ring"

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError(f"{self.name}: a rim needs at least one cell")
        for cell in self.cells:
            if cell not in (LUG, NOTCH, BLANK):
                raise ValueError(f"{self.name}: unknown feature {cell!r}")

    @property
    def divisions(self) -> int:
        return len(self.cells)

    def feature(self, cell: int) -> str | None:
        return self.cells[cell % self.divisions]

    def positions(self, kind: str) -> tuple[int, ...]:
        return tuple(i for i, f in enumerate(self.cells) if f == kind)

    @property
    def lugs(self) -> tuple[int, ...]:
        return self.positions(LUG)

    @property
    def notches(self) -> tuple[int, ...]:
        return self.positions(NOTCH)

    def rotated(self, delta: int, *, name: str | None = None) -> "Ring":
        """Return the rim rotated clockwise by ``delta`` whole cells.

        A feature originally at cell ``i`` is read at cell ``(i + delta) % n``
        after the physical piece has been turned clockwise by ``delta``.
        """

        n = self.divisions
        delta %= n
        if delta == 0:
            return self if name is None else Ring(self.cells, name=name)
        turned = tuple(self.cells[(i - delta) % n] for i in range(n))
        return Ring(turned, name=name or self.name)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "divisions": self.divisions,
            "lugs": list(self.lugs),
            "notches": list(self.notches),
        }


def lower_lugs_fit_upper(lower: Ring, upper: Ring) -> bool:
    """Every lug of ``lower`` must land on a notch of ``upper``.

    The two rims are already expressed in the same angular frame (the common
    vertical line of the joint), so this is a pure feature test with no extra
    rotation applied here.
    """

    if lower.divisions != upper.divisions:
        return False
    return all(upper.feature(cell) == NOTCH for cell in lower.lugs)


@dataclass(frozen=True)
class Base:
    """The fixed seating base; it only exposes lugs upwards."""

    item_id: str
    ring: Ring

    @property
    def divisions(self) -> int:
        return self.ring.divisions


@dataclass(frozen=True)
class Lid:
    """A lid; it only exposes notches downwards."""

    item_id: str
    ring: Ring
    # If the workshop label only allows one seating direction it is pinned;
    # otherwise every whole-cell direction is an admissible seat.
    pinned_rotation: int | None = None

    @property
    def divisions(self) -> int:
        return self.ring.divisions


@dataclass(frozen=True)
class Vessel:
    """One intermediate box with a bottom rim and a top rim."""

    item_id: str
    bottom: Ring
    top: Ring
    # True when the vessel must be seated upright (no inversion); the two rims
    # still share one rotation either way.
    upright: bool = True

    def __post_init__(self) -> None:
        if self.bottom.divisions != self.top.divisions:
            raise ValueError(
                f"vessel {self.item_id}: bottom/top divisions disagree "
                f"({self.bottom.divisions} vs {self.top.divisions})"
            )

    @property
    def divisions(self) -> int:
        return self.bottom.divisions

    def rotated(self, delta: int) -> "Vessel":
        """Turn the whole box: both rims move together by the same delta."""

        return Vessel(
            self.item_id,
            self.bottom.rotated(delta, name=f"{self.item_id}.bottom"),
            self.top.rotated(delta, name=f"{self.item_id}.top"),
            upright=self.upright,
        )


@dataclass(frozen=True)
class VesselConfiguration:
    """One still-possible physical reading of a vessel.

    ``origin`` is the worn-base starting point (offset) that produced it;
    ``based_on`` names the notebook revision actually sampled.
    """

    item_id: str
    bottom: Ring
    top: Ring
    origin: int
    based_on: str
    witness_ok: bool = True

    @property
    def divisions(self) -> int:
        return self.bottom.divisions


def ring_from_features(
    divisions: int,
    *,
    lugs: Iterable[int] = (),
    notches: Iterable[int] = (),
    name: str = "ring",
) -> Ring:
    cells: list[str | None] = [BLANK] * divisions
    for cell in lugs:
        _claim(cells, cell, LUG, name)
    for cell in notches:
        _claim(cells, cell, NOTCH, name)
    return Ring(tuple(cells), name=name)


def _claim(cells: list[str | None], cell: int, kind: str, name: str) -> None:
    n = len(cells)
    if not isinstance(cell, int) or not 0 <= cell < n:
        raise ValueError(f"{name}: cell {cell!r} out of range 0..{n - 1}")
    if cells[cell] is not None:
        raise ValueError(f"{name}: cell {cell} cannot hold two features")
    cells[cell] = kind
