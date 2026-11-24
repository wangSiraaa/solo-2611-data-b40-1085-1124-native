"""Administrator queries over the reconstructed event trail.

Three capabilities the request calls out explicitly:

* list the (guest, coat) pairings that are still possible (合牌相容);
* identify the *real* INVENTORY (结场清点) event that makes it impossible for
  two coats to belong to the same person at once;
* at the closing beat, list which hooks each person's coat may still be on.

Plus the standing rule that a coat whose hook is temporarily unknown is
recovered from neighbouring hooks' MOVE (换钩登记) registrations, and that an
owner is never concluded from a coincident steel number alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .model import Coat, Guest, compatible_pairs

LEFT = "left"
RIGHT = "right"


# ---------------------------------------------------------------------------
# Still-possible pairings
# ---------------------------------------------------------------------------


def possible_pairings(coats: Iterable[Coat], guests: Iterable[Guest]) -> list[dict]:
    """Every (guest, coat) pairing whose halves still combine, live coats only."""
    live = [c for c in coats if not c.delivered]
    out = []
    for gid, cid in compatible_pairs(live, guests):
        out.append({"guest": gid, "coat": cid})
    return out


def steel_only_owners(coats: Iterable[Coat], guests: Iterable[Guest]) -> list[dict]:
    """What a naive same-steel-number match would claim — shown to be wrong.

    Returned pairs are those sharing only the steel number; the caller compares
    them against :func:`possible_pairings` to see where the shortcut differs.
    """
    out = []
    for g in guests:
        for c in coats:
            if c.delivered:
                continue
            if c.left.steel == g.right.steel:
                out.append(
                    {
                        "guest": g.gid,
                        "coat": c.cid,
                        "half_tags_combine": c.left.compatible(g.right),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Hook recovery from MOVE registrations
# ---------------------------------------------------------------------------


@dataclass
class MoveRecord:
    coat_key: int          # wire coat id
    from_ring: str
    from_slot: int
    to_ring: str
    to_slot: int


def recover_hook(
    hang_hook: Mapping[int, tuple[str, int]],
    moves: Iterable[MoveRecord],
    coat_key: int,
) -> set[tuple[str, int]]:
    """Follow a coat's HANG + MOVE chain to its current hook position(s).

    A coat whose current hook is not directly known is recovered from the MOVE
    registrations of the hooks around it.  If a MOVE leaves the hook blank
    (``to_slot`` of 0 = "not recorded"), the candidates become the recorded
    destinations **on the same ring, from slots adjacent to the coat's last
    known slot** (its neighbours); the result is a set, never a single guess.
    Crucially this uses the physical move log only — it never consults the steel
    number, and a coincident number can never appoint an owner.
    """
    pos = hang_hook.get(coat_key)
    if pos is None:
        return set()
    ring, slot = pos
    candidates: set[tuple[str, int]] = {(ring, slot)}
    ordered = [m for m in moves if m.coat_key == coat_key]
    for m in ordered:
        if (m.from_ring, m.from_slot) not in candidates:
            continue
        candidates.discard((m.from_ring, m.from_slot))
        if m.to_slot == 0:
            # Unknown destination: use the neighbouring hooks' own registrations
            # on the same ring.  "Adjacent" is +-1 around the last known slot.
            neighbours = {slot - 1, slot + 1}
            adjacent = {
                (mm.to_ring, mm.to_slot)
                for mm in moves
                if mm.coat_key != coat_key
                and mm.to_ring == m.from_ring
                and mm.to_slot != 0
                and mm.from_slot in neighbours
            }
            candidates |= adjacent or {(m.to_ring, 0)}
        else:
            candidates.add((m.to_ring, m.to_slot))
        # Track the last known slot so a later unknown move uses the right
        # neighbours.
        if m.to_slot:
            slot = m.to_slot
    return candidates


def identify_owner(coat: Coat, guests: Iterable[Guest]) -> list[str]:
    """Owner candidates from *half-tags only*; never from a coincident number."""
    return [g.gid for g in guests if coat.left.compatible(g.right)]


# ---------------------------------------------------------------------------
# INVENTORY witness
# ---------------------------------------------------------------------------


@dataclass
class Inventory:
    ring: str
    counted: int
    beat: int
    event_key: int  # ordinal/wire id of the real clearing event


def inventory_witness_for_pair(
    guest: Guest,
    coat_a: Coat,
    coat_b: Coat,
    inventories: Iterable[Inventory],
) -> dict | None:
    """Find a real INVENTORY event that stops ``coat_a`` and ``coat_b`` from both
    being ``guest``'s at once.

    Both coats are candidate claims of ``guest`` (each half-tag combines).  A
    session-end count on a ring that reports fewer than the number of live
    candidate coats on that ring proves at least one of them is not present /
    not that guest's — so they cannot both belong to ``guest`` simultaneously.
    Returns the witnessing event, or ``None`` if no count excludes the pair.
    """
    pair = [coat_a, coat_b]
    for inv in inventories:
        on_ring = [
            c for c in pair
            if not c.delivered
            and c.ring == inv.ring
            and c.left.compatible(guest.right)
        ]
        if len(on_ring) >= 2 and inv.counted < 2:
            return {
                "guest": guest.gid,
                "coats": [c.cid for c in on_ring],
                "witness_event_key": inv.event_key,
                "ring": inv.ring,
                "counted": inv.counted,
                "beat": inv.beat,
                "conclusion": (
                    "the real count saw fewer than two candidate coats on this "
                    "ring, so they cannot both be this guest's at once"
                ),
            }
    return None


# ---------------------------------------------------------------------------
# Closing-time possible hooks per guest
# ---------------------------------------------------------------------------


def possible_hooks_at_close(
    guests: Iterable[Guest],
    coats: Iterable[Coat],
    coat_candidates: Mapping[str, set[tuple[str, int]]] | None = None,
) -> dict[str, list[list]]:
    """At the closing beat, the hooks each guest's coat may still be on.

    For every guest we union the candidate hooks of each live coat whose halves
    combine.  ``coat_candidates`` lets callers pass recovery sets (possibly more
    than one hook); absent that, the coat's own ``(ring, slot)`` is used.
    """
    cand = dict(coat_candidates or {})
    live = [c for c in coats if not c.delivered]
    out: dict[str, list[list]] = {}
    for g in guests:
        hooks: set[tuple[str, int]] = set()
        for c in live:
            if not c.left.compatible(g.right):
                continue
            hooks |= cand.get(c.cid, {(c.ring, c.slot)})
        out[g.gid] = [list(h) for h in sorted(hooks)]
    return out
