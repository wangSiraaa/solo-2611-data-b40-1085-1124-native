"""Fixed-reference witnesses: two marks known to share one vertical line.

A witness is preserved evidence that, when two pieces are stacked in the
recorded seating, a particular cell of one rim and a particular cell of the
other rim lay on the same vertical line.  Witnesses never rotate pieces by
themselves; they only rule out seating solutions that cannot reproduce the
recorded alignment.

A witness constrains the two seating rotations:

    lower_cell + lower_rotation == upper_cell + upper_rotation (mod n)

The packing solver evaluates every candidate seating against this equation,
so a worn-base configuration is only kept when *some* legal seating of the
whole stack reproduces every witness.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Ring, VesselConfiguration


@dataclass(frozen=True)
class Witness:
    """Cell ``lower_cell`` below and ``upper_cell`` above share a vertical."""

    lower_item: str
    lower_rim: str  # "top" for a vessel, "ring" for a base
    lower_cell: int
    upper_item: str
    upper_rim: str  # "bottom" for a vessel, "ring" for a lid
    upper_cell: int

    def as_dict(self) -> dict[str, object]:
        return {
            "lower_item": self.lower_item,
            "lower_rim": self.lower_rim,
            "lower_cell": self.lower_cell,
            "upper_item": self.upper_item,
            "upper_rim": self.upper_rim,
            "upper_cell": self.upper_cell,
        }


def vessel_ring(configuration: VesselConfiguration, rim: str) -> Ring:
    if rim == "bottom":
        return configuration.bottom
    if rim == "top":
        return configuration.top
    raise ValueError(f"unknown vessel rim {rim!r}")


def witness_holds(
    witness: Witness,
    *,
    rings_by_item: dict[str, Ring],
    rotation_by_item: dict[str, int],
    divisions: int,
) -> bool:
    """Evaluate one witness under a concrete seating of every piece."""

    lower_ring = rings_by_item[witness.lower_item]
    upper_ring = rings_by_item[witness.upper_item]
    if (
        lower_ring.feature(witness.lower_cell) is None
        or upper_ring.feature(witness.upper_cell) is None
    ):
        return False
    lower_angle = (witness.lower_cell + rotation_by_item[witness.lower_item]) % divisions
    upper_angle = (witness.upper_cell + rotation_by_item[witness.upper_item]) % divisions
    return lower_angle == upper_angle
