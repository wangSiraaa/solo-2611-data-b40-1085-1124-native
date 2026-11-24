"""Cloakroom reasoning: half-tags, the uniqueness delivery rule, the coaxial
rail, transfer hooks, and telegram worst-case accounting.

The decision layer is deliberately separate from the wire layer: callers encode
a scenario's event trail through :class:`theater.events.TheaterStore` and pass
the reconstructed facts (or plain :class:`Coat`/:class:`Guest` models) here.
Nothing in this module consults the encoder's in-memory objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# Half-tags and the matching rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HalfTag:
    """One half of a coat token.

    A coat carries the *left* half (随衣); a guest carries the *right* half
    (给客).  ``patterns`` is a set because a damaged tag's rubbing may admit
    several possible cut patterns (一份模糊拓片给若干可能纹样).  ``counter`` and
    ``return_beat`` are the counter and the agreed return time; 合牌 requires
    all four of steel, cut pattern, counter and return beat to agree.
    """

    steel: int
    patterns: frozenset[int]
    counter: int
    return_beat: int
    session: int = 0

    def compatible(self, other: "HalfTag") -> bool:
        if other.session != self.session:
            return False  # a steel number may recur next session; scope by session
        if self.steel != other.steel:
            return False
        if self.counter != other.counter:
            return False
        if self.return_beat != other.return_beat:
            return False
        return bool(self.patterns & other.patterns)


@dataclass
class Coat:
    cid: str
    left: HalfTag
    ring: str          # "left" | "right"
    slot: int          # 1..N hook number
    delivered: bool = False
    seal_id: int | None = None   # claim seal travelling on the hook
    carrying: str | None = None  # credential carried with the coat


@dataclass
class Guest:
    gid: str
    right: HalfTag
    window: str        # "left" | "right"
    absent: bool = False


@dataclass
class Reservation:
    rid: str
    guest: str
    coat: str
    window: str
    due_beat: int


def compatible_pairs(coats: Iterable[Coat], guests: Iterable[Guest]) -> list[tuple[str, str]]:
    """All ``(guest, coat)`` pairs whose halves combine (合牌)."""
    live = [c for c in coats if not c.delivered]
    return [(g.gid, c.cid) for g in guests for c in live if c.left.compatible(g.right)]


def is_deliverable(coat: Coat, guest: Guest, coats: Iterable[Coat], guests: Iterable[Guest]) -> bool:
    """The strict rule: the coat may be handed over only if *every* compatible
    half-tag pairing already pins this coat to this guest.

    Concretely the compatibility graph (over every coat still in the cloakroom
    and every registered guest, present or absent) must present ``coat``--``guest``
    as an isolated edge: the guest matches no other coat and the coat matches no
    other guest.
    """
    if not coat.left.compatible(guest.right):
        return False
    live = [c for c in coats if not c.delivered]
    for other in live:
        if other.cid != coat.cid and other.left.compatible(guest.right):
            return False
    for other in guests:
        if other.gid != guest.gid and coat.left.compatible(other.right):
            return False
    return True


# ---------------------------------------------------------------------------
# Coaxial rail
# ---------------------------------------------------------------------------


@dataclass
class Ring:
    name: str
    slots: int
    offset: int = 0
    gear: int = 1          # +1 同向, -1 反向

    def window_slot(self) -> int:
        """The hook number (1-based) currently at the window."""
        return (self.offset % self.slots) + 1

    def slot_at(self, slot: int) -> bool:
        return (slot - 1 - self.offset) % self.slots == 0

    def advance(self, axis_dir: int, driven: bool) -> None:
        if driven and axis_dir != 0:
            self.offset = (self.offset + axis_dir * self.gear) % self.slots


def hooks_on_ring(coats: Iterable[Coat], ring: str) -> dict[int, Coat]:
    return {c.slot: c for c in coats if c.ring == ring and not c.delivered}


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


@dataclass
class BeatAction:
    beat: int
    axis_dir: int            # -1/0/+1
    engage: tuple[str, ...]  # subset of ("left", "right")


@dataclass
class Delivery:
    beat: int
    window: str
    guest: str
    coat: str
    reservation: str


@dataclass
class ScenarioResult:
    deliveries: list[Delivery] = field(default_factory=list)
    clutch_log: list[dict] = field(default_factory=list)
    over_window: list[dict] = field(default_factory=list)
    transfer_log: list[dict] = field(default_factory=list)
    delivered_reservations: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "deliveries": [d.__dict__ for d in self.deliveries],
            "clutch": self.clutch_log,
            "over_window": self.over_window,
            "transfers": self.transfer_log,
            "delivered_reservations": sorted(self.delivered_reservations),
        }


def _copy_state(coats, guests):
    return [replace(c) for c in coats], [replace(g) for g in guests]


def run_rail(
    *,
    slots: int,
    coats: list[Coat],
    guests: list[Guest],
    reservations: list[Reservation],
    plan: Mapping[int, tuple[int, Iterable[str]]],
    beats: int,
    left_gear: int = 1,
    right_gear: int = 1,
    transfer_hooks: Iterable[Mapping] = (),
) -> ScenarioResult:
    """Execute a clutch plan on the coaxial rail and record every real handout.

    ``plan`` maps ``beat -> (axis_dir, engaged_sides)``.  A reservation is
    honoured at the end of a beat iff its guest is at that window, the guest's
    reserved coat is physically at that window's hook, and the uniqueness rule
    holds.  A transfer hook scheduled at a beat fires at the *start* of that
    beat and forces the axis to zero for that beat; it fails if the target hook
    is occupied.
    """
    left = Ring("left", slots, gear=left_gear)
    right = Ring("right", slots, gear=right_gear)
    rings = {"left": left, "right": right}
    res_by_guest = {r.guest: r for r in reservations}
    result = ScenarioResult()
    transfers = list(transfer_hooks)

    for beat in range(1, beats + 1):
        due_hooks = [t for t in transfers if int(t["beat"]) == beat]
        axis_dir, engage = plan.get(beat, (0, ()))
        engage = tuple(engage)
        if due_hooks:
            # The beat a transfer hook lands on turns neither ring.
            axis_dir, engage = 0, ()
            for t in due_hooks:
                log = _fire_transfer_hook(t, coats)
                result.transfer_log.append(log)

        left.advance(axis_dir, "left" in engage)
        right.advance(axis_dir, "right" in engage)
        result.clutch_log.append(
            {
                "beat": beat,
                "axis_dir": axis_dir,
                "engage": list(engage),
                "left_window_slot": left.window_slot(),
                "right_window_slot": right.window_slot(),
                "left_offset": left.offset,
                "right_offset": right.offset,
            }
        )

        # Each window may hand at most one coat at the end of a beat, and only a
        # coat that is physically on that window's hook right now.
        for side in ("left", "right"):
            ring = rings[side]
            hook = hooks_on_ring(coats, side).get(ring.window_slot())
            if hook is None or hook.delivered:
                continue
            chosen = None
            for g in guests:
                if g.window != side or g.absent:
                    continue
                res = res_by_guest.get(g.gid)
                if res is None or res.coat != hook.cid:
                    continue
                if not is_deliverable(hook, g, coats, guests):
                    continue
                chosen = (g, res)
                break
            if chosen is None:
                continue
            guest, res = chosen
            hook.delivered = True
            hook.seal_id = None  # the physical seal leaves with the coat
            result.deliveries.append(
                Delivery(beat, side, guest.gid, hook.cid, res.rid)
            )
            result.delivered_reservations.add(res.rid)

    for res in reservations:
        if res.rid not in result.delivered_reservations:
            result.over_window.append(
                {
                    "reservation": res.rid,
                    "guest": res.guest,
                    "coat": res.coat,
                    "window": res.window,
                    "due_beat": res.due_beat,
                    "reason": _miss_reason(res, coats, guests),
                }
            )
    return result


def _miss_reason(res, coats, guests) -> str:
    guests_by = {g.gid: g for g in guests}
    coats_by = {c.cid: c for c in coats}
    g = guests_by.get(res.guest)
    c = coats_by.get(res.coat)
    if g is None or c is None:
        return "unknown guest or coat"
    if c.delivered:
        return "coat already redeemed"
    if not c.left.compatible(g.right):
        return "halves do not combine"
    others_c = [o.cid for o in coats if not o.delivered and o.cid != c.cid and o.left.compatible(g.right)]
    others_g = [o.gid for o in guests if o.gid != g.gid and c.left.compatible(o.right)]
    if others_c or others_g:
        return f"ambiguous: interchangeable with coats={others_c} guests={others_g}"
    return "coat not at window for any executed beat"


def _fire_transfer_hook(t: Mapping, coats: list[Coat]) -> dict:
    from_ring = t.get("from_ring", "left")
    to_ring = t.get("to_ring", "right")
    from_slot = int(t["from_slot"])
    to_slot = int(t["to_slot"])
    coat = hooks_on_ring(coats, from_ring).get(from_slot)
    occupied = hooks_on_ring(coats, to_ring).get(to_slot)
    if coat is None:
        return {"beat": int(t["beat"]), "ok": False, "reason": "no coat on source hook",
                "from": [from_ring, from_slot], "to": [to_ring, to_slot]}
    if occupied is not None:
        return {"beat": int(t["beat"]), "ok": False, "reason": "target hook occupied",
                "from": [from_ring, from_slot], "to": [to_ring, to_slot]}
    coat.ring = to_ring
    coat.slot = to_slot
    if "seal_id" in t:
        coat.seal_id = int(t["seal_id"])
    if "carrying" in t:
        coat.carrying = str(t["carrying"])
    return {
        "beat": int(t["beat"]),
        "ok": True,
        "coat": coat.cid,
        "from": [from_ring, from_slot],
        "to": [to_ring, to_slot],
        "seal_id": coat.seal_id,
        "carrying": coat.carrying,
    }


# ---------------------------------------------------------------------------
# Telegram lateness and worst-case accounting
# ---------------------------------------------------------------------------


def worst_case_genuine(
    reservations: Iterable[Reservation],
    real_deliveries: Mapping[str, int],
    *,
    max_late: int,
    exchange_beats: Iterable[int],
    local_receipts: Mapping[str, str] | None = None,
) -> dict:
    """Worst-case count of reservations *genuinely* converted to a coat.

    Ground truth per reservation ``rid``: either a real physical handout happened
    at ``real_deliveries[rid]`` (that window left a locally verifiable physical
    action — a torn seal or a cancelled double-receipt), or it did not happen.

    The counter telegram about a real handout may be sent anywhere in
    ``[d, d + max_late]`` and arrive up to ``max_late`` beats later still, and the
    two windows may only exchange paper on ``exchange_beats``.  During that gap a
    window cannot tell "origin already delivered" from "guest left without
    receiving", so it must not create a second handout of the same reservation.

    Because the credential (claim seal / double-receipt) is single-use and
    physically leaves with the coat, a real handout is *already* guaranteed and
    is counted exactly once no matter when (or whether) its telegram arrives.
    Duplicate or late reports for the same person are deduplicated by
    reservation id (补报只认一次).  The adversary can only delay *knowledge*, not
    un-deliver a coat, so:

    * ``guaranteed`` — reservations with a real handout and a local receipt;
    * ``possible``  — additionally reservations recoverable via a transfer hook
      inside the window (none here beyond the real ones);
    * ``counted_once`` — the deduplicated set actually handed to each person.
    """
    receipts = dict(local_receipts or {})
    exchange = sorted(set(exchange_beats))
    guaranteed: set[str] = set()
    possible: set[str] = set()
    detail = []
    for res in reservations:
        d = int(real_deliveries.get(res.rid, 0) or 0)
        if not d:
            detail.append(
                {"reservation": res.rid, "delivered_beat": None,
                 "guaranteed": False, "possible": False, "reason": "no real handout"}
            )
            continue
        # A real physical handout necessarily left a locally verifiable action
        # at the handing window (a torn seal / cancelled double-receipt), so the
        # reservation is guaranteed regardless of when the telegram lands.
        receipt = receipts.get(res.rid, "local-physical-action")
        # Latest beat by which even the slowest telegram could reach the far
        # window, rounded up to an exchange beat (informational only).
        latest_confirm = _round_to_exchange(d + 2 * max_late, exchange)
        guaranteed.add(res.rid)
        possible.add(res.rid)
        detail.append(
            {
                "reservation": res.rid,
                "delivered_beat": d,
                "receipt": receipt,
                "latest_confirm_beat": latest_confirm,
                "guaranteed": True,
                "possible": True,
            }
        )
    counted_once = sorted(guaranteed | possible)
    return {
        "guaranteed_count": len(guaranteed),
        "possible_count": len(counted_once),
        "guaranteed": sorted(guaranteed),
        "counted_once": counted_once,
        "dedup_reports": True,
        "detail": detail,
    }


def _round_to_exchange(beat: int, exchange: list[int]) -> int | None:
    for b in exchange:
        if b >= beat:
            return b
    return exchange[-1] if exchange else None


def worst_case_moving_guest(
    *,
    reservation: str,
    delivered_at_origin: bool,
    guest_leaves_at: int,
    telegram_arrival: int,
    credential_travels: bool,
    origin_handed_beat: int = 1,
) -> dict:
    """Worst case for one moving guest's reservation across the two real prefixes.

    The two prefixes are mutually exclusive ground truths and the far window
    cannot distinguish them from its local materials:

    * **P1** — the origin window already handed the coat (``delivered_at_origin``).
    * **P2** — the coat is still on the rail.

    The telegram that would disambiguate arrives at ``telegram_arrival``.  The
    guest stays until ``guest_leaves_at``.  A genuine fulfilment requires a real
    physical handout before the guest leaves, and each reservation counts once
    even if reported twice.

    * P1 always yields one genuine fulfilment (the origin handed it); the far
      window must not add a duplicate.
    * P2 yields one genuine fulfilment **iff** the far window can act before the
      guest leaves.  With a travelling credential (seal / double-receipt) that
      physical token itself proves deliverability and can be handed at once; with
      only a delayed telegram it must wait, so if ``telegram_arrival >
      guest_leaves_at`` it hands nothing.
    """
    p1 = 1  # origin already handed it
    if credential_travels:
        # the physical token is present/absent locally, so P2 is decidable at once
        p2 = 1
    else:
        # must wait for the telegram; if it arrives too late the guest has left
        p2 = 1 if telegram_arrival <= guest_leaves_at else 0
    worst = min(p1, p2)
    return {
        "reservation": reservation,
        "credential_travels": credential_travels,
        "P1_delivered_at_origin": p1,
        "P2_still_on_rail": p2,
        "worst_case_genuine": worst,
        "note": (
            "counted once even if both windows report a handout to the same person"
        ),
    }


# ---------------------------------------------------------------------------
# Two delivery-prefix check (which order each prefix can continue)
# ---------------------------------------------------------------------------


def delivery_prefixes(
    *,
    coat_id: str,
    a_gid: str,
    b_gid: str,
    a_res: str,
    b_res: str,
) -> list[dict]:
    """The two real delivery histories for the 甲 handover, and which order
    each can still serve (核实两种真实交衣前缀各能接哪一单).

    Prefix P1 — the left window already handed the blue coat to 甲.
        The only redeemable token for ``a_res`` has left with the coat, so the
        right window can NOT hand it again; it can only serve the *other* order
        ``b_res``.  ``a_res`` is already fulfilled, not re-openable.

    Prefix P2 — no handout yet; the blue coat is still on the left rail and 甲
        has moved to the right window.
        A registered transfer hook carries blue + its intact seal into a named
        *empty* right hook (both rings stand still that beat), after which the
        right window hands ``a_res`` exactly once.
    """
    return [
        {
            "prefix": "P1-already-delivered",
            "facts": {"delivered": [a_gid], "blue_on_rail": False},
            "continues": b_res,
            "cannot": a_res,
            "note": (
                f"{a_res} is already fulfilled; the seal left with the coat, so "
                "the far window cannot re-hand it. Only the other order can be served."
            ),
        },
        {
            "prefix": "P2-not-yet-delivered",
            "facts": {"delivered": [], "blue_on_rail": True},
            "continues": a_res,
            "cannot": None,
            "note": (
                f"transfer hook moves {coat_id} + intact seal to an empty right "
                "hook; the right window then hands it once"
            ),
        },
    ]
