"""Two-bench workshop: prep orders, cut orders, routing, tickets and contention.

Material model
--------------
Every axis carries exactly one real yarn.  A yarn is measured in unit inches;
each inch has a *physical object number* ``InchId(yarn, index)``.  Residuals
come from an adopted :class:`~obsdelivery.twist.TwistLedger`, so the readings
(cumulative boundary values, backoff, clamp direction) stay authoritative.

Two bench commitments (pick one, we commit to TICKETS as the external promise)
------------------------------------------------------------------------------
The workshop can move material two ways:

*Transport* — bind two uncut yarns into a *named prep pair* and route both axes
to a bench.  Each handoff costs one shift.  While an axis is in transit no
bench may draw from it; changing the binding requires recalling the axis (a
further shift).

*Tickets* — keep the axes in place and let each steward issue a shift-limited
**exclusive** ticket for one concrete interval, redeemable by exactly one cut
order.  Both physical tickets must reach the on-shift bench before it may obey
the order.  A ticketed uncut interval cannot be re-issued until released.

Our externally promised method is **two matching tickets at the on-shift
bench**.  Transport is supported as an alternative when it can meet the shift,
but the ticket path is what we guarantee.  The result is that no two orders
ever cut the same inch (tickets are exclusive and physical consumption is
permanent) and every shift has a decidable basis.

Cutting
-------
A cut order is signed only if, inch by inch, ``left + right`` lies in the
closed interval for that inch, both spans are different axes, and none of the
inches is already consumed.  Cutting consumes those inches *forever*.
Cancelling a prep order never restores a consumed inch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .canonical import frac_str
from .errors import ValidationError
from .ropes import HEAD, TAIL, Interval
from .twist import PointReading, TwistLedger, build_ledger

CUT_NOW = "cut_now"
WAIT = "wait"
BLOCKED = "blocked"

PENDING = "pending"
CUT = "cut"
CANCELED = "canceled"


# ---------------------------------------------------------------------------
# physical object numbers
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class InchId:
    """The named physical object for one inch of one yarn."""

    yarn: str
    index: int

    def canonical(self) -> dict[str, Any]:
        return {"yarn": self.yarn, "inch": self.index}


@dataclass(frozen=True)
class SpanRef:
    """A contiguous run of inches on a yarn, with an attachment orientation."""

    yarn: str
    start: int
    length: int
    orientation: str = HEAD

    def __post_init__(self) -> None:
        if self.start < 0 or self.length <= 0:
            raise ValidationError("span needs start >= 0 and length > 0")
        if self.orientation not in (HEAD, TAIL):
            raise ValidationError(f"bad orientation {self.orientation!r}")

    def inches(self) -> list[InchId]:
        return [InchId(self.yarn, i) for i in range(self.start, self.start + self.length)]

    def indices(self) -> range:
        return range(self.start, self.start + self.length)

    def canonical(self) -> dict[str, Any]:
        return {
            "yarn": self.yarn,
            "start": self.start,
            "length": self.length,
            "orientation": self.orientation,
        }


# ---------------------------------------------------------------------------
# yarns and adopted ledgers
# ---------------------------------------------------------------------------

@dataclass
class Yarn:
    yarn_id: str
    readings: list[PointReading]
    adopted: TwistLedger
    consumed: set[int] = field(default_factory=set)

    def ledger(self) -> TwistLedger:
        """Re-derive from the latest readings (used after clamp supplements)."""
        return build_ledger(self.readings, version=self.yarn_id)

    def residuals(self) -> list[Fraction | None]:
        return self.ledger().signable_residuals()

    def length(self) -> int:
        return self.ledger().length

    def available(self, span: SpanRef) -> tuple[bool, str | None]:
        if span.start + span.length > self.length():
            return False, f"span exceeds yarn {self.yarn_id} length {self.length()}"
        consumed = sorted(set(span.indices()) & self.consumed)
        if consumed:
            return False, f"inches {consumed} of {self.yarn_id} already cut (forever invalid)"
        return True, None

    def span_values(self, span: SpanRef) -> tuple[Fraction | None, ...]:
        res = self.ledger().signable_residuals()
        run = res[span.start:span.start + span.length]
        ordered = list(reversed(run)) if span.orientation == TAIL else list(run)
        return tuple(ordered)

    def canonical(self) -> dict[str, Any]:
        return {
            "yarn_id": self.yarn_id,
            "length": self.length(),
            "residuals": [None if v is None else frac_str(v) for v in self.residuals()],
            "consumed": sorted(self.consumed),
        }


# ---------------------------------------------------------------------------
# orders and tickets
# ---------------------------------------------------------------------------

@dataclass
class PrepOrder:
    prep_id: str
    left: SpanRef
    right: SpanRef
    intervals: tuple[Interval, ...]
    created_shift: int
    status: str = "active"
    bound_pair: str | None = None

    def spans(self) -> tuple[SpanRef, SpanRef]:
        return self.left, self.right


@dataclass
class Ticket:
    ticket_id: str
    yarn: str
    span: SpanRef
    cut_id: str
    valid_shift: int
    status: str = "valid"  # valid | void | released | expired
    at_bench: str | None = None

    def covers(self, span: SpanRef) -> bool:
        return (
            self.yarn == span.yarn
            and self.span.start <= span.start
            and span.start + span.length <= self.span.start + self.span.length
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "yarn": self.yarn,
            "span": self.span.canonical(),
            "cut_id": self.cut_id,
            "valid_shift": self.valid_shift,
            "status": self.status,
            "at_bench": self.at_bench,
        }


@dataclass
class CutOrder:
    cut_id: str
    left: SpanRef
    right: SpanRef
    intervals: tuple[Interval, ...]
    claim_shift: int
    arrival_seq: int
    bench: str
    prep_id: str | None = None
    status: str = PENDING
    # set when actually cut
    delivered: tuple[InchId, ...] = ()

    def spans(self) -> tuple[SpanRef, SpanRef]:
        return self.left, self.right

    def priority(self) -> tuple[int, int, str]:
        return (self.claim_shift, self.arrival_seq, self.cut_id)

    def inch_set(self) -> set[InchId]:
        return set(self.left.inches()) | set(self.right.inches())


# ---------------------------------------------------------------------------
# the shop
# ---------------------------------------------------------------------------

BENCH_A = "bench-1"
BENCH_B = "bench-2"


class Workshop:
    """A deterministic two-bench workshop.

    The committed cutting basis is the **ticket** method: an order cuts at a
    shift only when both axis tickets are physically at that bench, still valid
    for that shift, tied to the same order, and the pair passes the per-inch
    check on uncut material.  Transport is available but only counts as
    authority once both axes have actually arrived.
    """

    def __init__(self) -> None:
        self.yarns: dict[str, Yarn] = {}
        self.prep: dict[str, PrepOrder] = {}
        self.cuts: dict[str, CutOrder] = {}
        self.tickets: dict[str, Ticket] = {}
        # axis/yarn physical location and transit
        self.location: dict[str, str] = {}
        self.transit: dict[str, int] = {}  # yarn -> arrival shift
        # binding of a routed prep pair: pair name -> (yarn_left, yarn_right, bench)
        self.pairs: dict[str, tuple[str, str, str]] = {}
        # a yarn bound into a prep pair is reserved to that bench until recalled;
        # no other bench may draw from a to-be-handed-over axis before handover.
        self.reserved: dict[str, str] = {}
        # each bench may hand over to a supplier at most once before its first
        # cut; after the first cut only named physical pieces may move.
        self.handovers: dict[str, int] = {}
        self.bench_has_cut: dict[str, bool] = {}
        self.log: list[dict[str, Any]] = []

    # -- setup ---------------------------------------------------------------
    def add_yarn(
        self,
        yarn_id: str,
        readings: Sequence[Mapping[str, Any] | PointReading],
        *,
        bench: str = BENCH_A,
    ) -> Yarn:
        if yarn_id in self.yarns:
            raise ValidationError(f"yarn {yarn_id} already exists")
        prs = [
            r if isinstance(r, PointReading) else _coerce_point(r) for r in readings
        ]
        adopted = build_ledger(prs, version=yarn_id)
        yarn = Yarn(yarn_id, prs, adopted)
        self.yarns[yarn_id] = yarn
        self.location[yarn_id] = bench
        return yarn

    def add_prep(
        self,
        prep_id: str,
        left: SpanRef,
        right: SpanRef,
        intervals: Sequence[Interval],
        shift: int,
    ) -> PrepOrder:
        _require_diff_axes(left, right)
        prep = PrepOrder(prep_id, left, right, tuple(intervals), shift)
        self.prep[prep_id] = prep
        self._record("prep_created", prep_id=prep_id, shift=shift,
                     left=left.canonical(), right=right.canonical())
        return prep

    def add_cut(self, order: CutOrder) -> CutOrder:
        if order.cut_id in self.cuts:
            raise ValidationError(f"cut order {order.cut_id} already exists")
        _require_diff_axes(order.left, order.right)
        if len(order.intervals) != order.left.length or order.left.length != order.right.length:
            raise ValidationError("cut order spans and intervals must share one length")
        self.cuts[order.cut_id] = order
        return order

    # -- prep mutations ------------------------------------------------------
    def cancel_prep(self, prep_id: str) -> PrepOrder:
        prep = self._prep(prep_id)
        prep.status = CANCELED
        self._record("prep_canceled", prep_id=prep_id)
        return prep

    def swap_axis(self, prep_id: str, side: str, new_span: SpanRef) -> PrepOrder:
        """Switch one side of a prep order to another axis' span."""
        prep = self._prep(prep_id)
        if side == "left":
            _require_diff_axes(new_span, prep.right)
            prep.left = new_span
        elif side == "right":
            _require_diff_axes(prep.left, new_span)
            prep.right = new_span
        else:
            raise ValidationError("side must be 'left' or 'right'")
        self._record("prep_swap_axis", prep_id=prep_id, side=side,
                     new_span=new_span.canonical())
        return prep

    def supplement_clamp_direction(self, yarn_id: str, *, flip: bool = True) -> Yarn:
        """Supplementary-record the clamp direction of a whole yarn.

        The fixture card says the yarn was clamped in the opposite direction;
        every point is re-marked, which negates every inch residual exactly.
        """
        yarn = self._yarn(yarn_id)
        yarn.readings = [
            PointReading(r.bits, r.backoff, (r.flip != flip)) for r in yarn.readings
        ]
        yarn.adopted = yarn.ledger()
        self._record("supplement_clamp_direction", yarn=yarn_id, flip=flip,
                     residuals=[None if v is None else frac_str(v)
                                for v in yarn.residuals()])
        return yarn

    # -- transport -----------------------------------------------------------
    def route(self, yarn_id: str, to_bench: str, shift: int) -> int:
        """Hand one axis to a bench; costs one shift.  Returns arrival shift."""
        self._yarn(yarn_id)
        if yarn_id in self.transit:
            raise ValidationError(f"yarn {yarn_id} is already in transit")
        arrive = shift + 1
        self.transit[yarn_id] = arrive
        self.location[yarn_id] = to_bench
        self._record("route", yarn=yarn_id, to=to_bench, dispatch_shift=shift,
                     arrive_shift=arrive)
        return arrive

    def handover(self, bench: str, shift: int) -> dict[str, Any]:
        """Hand material to a supplier: at most once per bench before its first cut.

        Before the first cut a bench may meet the requisitioner exactly once.
        After the first cut only named physical half-segments exist and may be
        reallocated; a bulk handover is refused.
        """
        if self.bench_has_cut.get(bench, False):
            raise ValidationError(
                f"{bench} has already cut; only named physical pieces may move"
            )
        if self.handovers.get(bench, 0) >= 1:
            raise ValidationError(
                f"{bench} already used its one pre-cut handover"
            )
        self.handovers[bench] = self.handovers.get(bench, 0) + 1
        self._record("handover", bench=bench, shift=shift)
        return {"bench": bench, "shift": shift, "handovers_used": self.handovers[bench]}

    def bind_pair(self, pair_id: str, left: str, right: str, bench: str) -> None:
        if pair_id in self.pairs:
            raise ValidationError(f"prep pair {pair_id} already bound")
        for yarn in (left, right):
            other = self.reserved.get(yarn)
            if other is not None and other != bench:
                raise ValidationError(
                    f"yarn {yarn} is reserved for {other}; cannot also bind for {bench}"
                )
        self.pairs[pair_id] = (left, right, bench)
        for yarn in (left, right):
            self.reserved[yarn] = bench
            prep = self._find_bound_prep(yarn)
            if prep is not None:
                prep.bound_pair = pair_id
        self._record("bind_pair", pair_id=pair_id, left=left, right=right, bench=bench)

    def recall_for_rebind(self, yarn_id: str, shift: int) -> int:
        """Changing a binding requires retrieving the real axis (one shift)."""
        self.reserved.pop(yarn_id, None)
        return self.route(yarn_id, self.location[yarn_id], shift)

    def _axis_ready_at(self, yarn_id: str, bench: str, now: int) -> bool:
        if yarn_id in self.transit and now < self.transit[yarn_id]:
            return False  # in transit: no bench may draw from it
        reserved = self.reserved.get(yarn_id)
        if reserved is not None and reserved != bench:
            # bound for another bench and not yet recalled
            return False
        return self.location.get(yarn_id) == bench

    def _settle_transit(self, now: int) -> None:
        for yarn_id, arrive in list(self.transit.items()):
            if now >= arrive:
                del self.transit[yarn_id]

    # -- tickets -------------------------------------------------------------
    def issue_ticket(
        self,
        ticket_id: str,
        yarn: str,
        span: SpanRef,
        cut_id: str,
        valid_shift: int,
    ) -> Ticket:
        if ticket_id in self.tickets:
            raise ValidationError(f"ticket {ticket_id} already exists")
        yarn_state = self._yarn(yarn)
        ok, reason = yarn_state.available(span)
        if not ok:
            raise ValidationError(f"cannot ticket {ticket_id}: {reason}")
        # exclusivity: an untcut interval under a live ticket cannot be re-issued
        for other in self.tickets.values():
            if (
                other.status == "valid"
                and other.yarn == yarn
                and _overlap(other.span, span)
            ):
                raise ValidationError(
                    f"cannot ticket {ticket_id}: inches overlap live ticket "
                    f"{other.ticket_id} (release it first)"
                )
        ticket = Ticket(ticket_id, yarn, span, cut_id, valid_shift)
        self.tickets[ticket_id] = ticket
        self._record("ticket_issued", **ticket.canonical())
        return ticket

    def send_ticket(self, ticket_id: str, bench: str) -> Ticket:
        ticket = self._ticket(ticket_id)
        ticket.at_bench = bench
        self._record("ticket_at_bench", ticket_id=ticket_id, bench=bench)
        return ticket

    def void_ticket(self, ticket_id: str) -> Ticket:
        ticket = self._ticket(ticket_id)
        ticket.status = "void"
        self._record("ticket_voided", ticket_id=ticket_id)
        return ticket

    def release_ticket(self, ticket_id: str) -> Ticket:
        ticket = self._ticket(ticket_id)
        ticket.status = "released"
        self._record("ticket_released", ticket_id=ticket_id)
        return ticket

    def _ticket_authority(self, order: CutOrder, now: int) -> tuple[bool, str | None]:
        """Both axis tickets must be at the bench, valid now, same order."""
        need = [order.left.yarn, order.right.yarn]
        found: dict[str, Ticket] = {}
        for ticket in self.tickets.values():
            if ticket.cut_id != order.cut_id:
                continue
            if ticket.yarn not in need:
                continue
            if ticket.at_bench != order.bench:
                continue
            if ticket.status != "valid":
                continue
            if ticket.valid_shift != now:
                continue
            span = order.left if ticket.yarn == order.left.yarn else order.right
            if not ticket.covers(span):
                continue
            found[ticket.yarn] = ticket
        if len(found) == len(set(need)):
            return True, None
        # diagnose
        stale = [t.ticket_id for t in self.tickets.values()
                 if t.cut_id == order.cut_id and t.at_bench == order.bench
                 and t.status != "valid"]
        expired = [t.ticket_id for t in self.tickets.values()
                   if t.cut_id == order.cut_id and t.status == "valid"
                   and t.valid_shift != now]
        if stale:
            return False, f"ticket authority voided: {stale}"
        if expired:
            return False, (
                f"ticket authority expired for shift {now}: {expired} "
                "(seemingly same order but stale tickets cannot cut)"
            )
        missing = [y for y in set(need) if y not in found]
        return False, f"missing on-shift ticket(s) for {missing} at {order.bench}"

    def _transport_authority(self, order: CutOrder, now: int) -> tuple[bool, str | None]:
        left_ok = self._axis_ready_at(order.left.yarn, order.bench, now)
        right_ok = self._axis_ready_at(order.right.yarn, order.bench, now)
        if left_ok and right_ok:
            return True, None
        return False, (
            f"axes not at {order.bench}: "
            f"{order.left.yarn}={left_ok}, {order.right.yarn}={right_ok}"
        )

    # -- validity ------------------------------------------------------------
    def evaluate(self, cut_id: str) -> dict[str, Any]:
        """Exact per-inch check of a cut order against current material."""
        order = self._cut(cut_id)
        left_yarn = self._yarn(order.left.yarn)
        right_yarn = self._yarn(order.right.yarn)
        left_avail, left_reason = left_yarn.available(order.left)
        right_avail, right_reason = right_yarn.available(order.right)
        rows: list[dict[str, Any]] = []
        ok = True
        reasons: list[str] = []
        if not left_avail:
            ok = False
            reasons.append(left_reason or "left unavailable")
        if not right_avail:
            ok = False
            reasons.append(right_reason or "right unavailable")
        if left_avail and right_avail:
            if order.left.length != order.right.length:
                ok = False
                reasons.append("span lengths differ")
            else:
                lvals = left_yarn.span_values(order.left)
                rvals = right_yarn.span_values(order.right)
                for i, (lv, rv, interval) in enumerate(
                    zip(lvals, rvals, order.intervals), start=1
                ):
                    if lv is None or rv is None:
                        ok = False
                        rows.append({
                            "inch": i,
                            "left": None if lv is None else frac_str(lv),
                            "right": None if rv is None else frac_str(rv),
                            "total": None,
                            "interval": interval.canonical(),
                            "ok": False,
                            "reason": "un-signable inch (missing/non-finite point)",
                        })
                        continue
                    total = lv + rv
                    good = interval.contains(total)
                    ok = ok and good
                    rows.append({
                        "inch": i,
                        "left": frac_str(lv),
                        "right": frac_str(rv),
                        "total": frac_str(total),
                        "interval": interval.canonical(),
                        "ok": good,
                        "reason": None if good else "outside closed interval",
                    })
                bad = [r["inch"] for r in rows if not r["ok"]]
                if bad:
                    reasons.append(f"inch(es) {bad} outside closed interval / un-signable")
        return {
            "cut_id": cut_id,
            "ok": ok,
            "reasons": reasons,
            "rows": rows,
            "delivered_inches": [i.canonical() for i in order.inch_set()]
            if ok else [],
        }

    def authority(self, cut_id: str, now: int) -> tuple[bool, str | None, str]:
        """Which authority path (tickets first) allows the cut right now."""
        order = self._cut(cut_id)
        self._settle_transit(now)
        t_ok, t_reason = self._ticket_authority(order, now)
        if t_ok:
            return True, None, "tickets"
        tr_ok, tr_reason = self._transport_authority(order, now)
        if tr_ok:
            return True, None, "transport"
        return False, f"tickets: {t_reason}; transport: {tr_reason}", "none"

    # -- contention resolution ----------------------------------------------
    def plan_shift(self, now: int) -> dict[str, dict[str, Any]]:
        """Decide, for every pending order, cut_now / wait / blocked at ``now``.

        * ``cut_now`` — materially valid, authorized by a ticket pair or by
          arrived axes, and the highest-priority order in its contention
          component.  An uncontended, valid, authorized order always cuts now.
        * ``wait`` — can still become cuttable (material is there and valid)
          but must wait this shift: it lost an overlapping-inch contest to a
          ``cut_now`` order, or it lacks on-shift authority.
        * ``blocked`` — materially impossible now: per-inch check fails, an
          inch is already cut, or the two spans share one axis.

        Two orders contend when their physical inch sets intersect.  Within
        each contention component only the smallest-priority executable order
        cuts; the others must wait.
        """
        pending = [c for c in self.cuts.values() if c.status == PENDING]
        info: dict[str, dict[str, Any]] = {}
        executable: list[str] = []
        for order in pending:
            validity = self.evaluate(order.cut_id)
            a_ok, a_reason, path = self.authority(order.cut_id, now)
            ok = validity["ok"] and a_ok
            info[order.cut_id] = {
                "executable": ok,
                "material_ok": validity["ok"],
                "authorized": a_ok,
                "reasons": validity["reasons"] + ([a_reason] if a_reason else []),
                "path": path,
                "inches": [i.canonical() for i in order.inch_set()],
            }
            if ok:
                executable.append(order.cut_id)

        # union-find over inch overlap among executable orders
        parent = {cid: cid for cid in executable}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i, a in enumerate(executable):
            for b in executable[i + 1:]:
                if self.cuts[a].inch_set() & self.cuts[b].inch_set():
                    union(a, b)

        components: dict[str, list[str]] = {}
        for cid in executable:
            components.setdefault(find(cid), []).append(cid)

        winners: list[str] = []
        for members in components.values():
            members.sort(key=lambda cid: self.cuts[cid].priority())
            winner = members[0]
            winners.append(winner)
            info[winner]["decision"] = CUT_NOW
            info[winner]["note"] = (
                "no contention" if len(members) == 1
                else f"wins contention among {members}"
            )
            for loser in members[1:]:
                info[loser]["decision"] = WAIT
                info[loser]["note"] = (
                    f"contends with {winner} for overlapping inch(es); must wait"
                )

        winner_inches: set[InchId] = set()
        for winner in winners:
            winner_inches |= self.cuts[winner].inch_set()

        for cid, entry in info.items():
            if "decision" in entry:
                continue
            order = self.cuts[cid]
            if not entry["material_ok"]:
                entry["decision"] = BLOCKED
                entry["note"] = "; ".join(entry["reasons"]) or "material invalid"
            elif order.inch_set() & winner_inches:
                entry["decision"] = WAIT
                entry["note"] = (
                    "overlaps an inch a higher-priority order cuts now; must wait"
                )
            else:
                # materially valid but not authorized this shift
                entry["decision"] = WAIT
                entry["note"] = "; ".join(entry["reasons"]) or "no authority yet"
        return info

    def _overlap_already_cut(self, order: CutOrder) -> list[str]:
        clashes: list[str] = []
        for other in self.cuts.values():
            if other.status != CUT or other.cut_id == order.cut_id:
                continue
            shared = set(order.inch_set()) & set(other.delivered)
            if shared:
                clashes.append(other.cut_id)
        return clashes

    def cut(self, cut_id: str, now: int) -> dict[str, Any]:
        """Execute one order atomically; consume its inches forever.

        Only the order the contention plan designates ``CUT_NOW`` at ``now``
        may proceed.  This is what prevents a waiting order from grabbing an
        inch the winner was about to cut.
        """
        order = self._cut(cut_id)
        validity = self.evaluate(cut_id)
        if not validity["ok"]:
            raise ValidationError(f"cut {cut_id} refused: {validity['reasons']}")
        plan = self.plan_shift(now)
        decision = plan.get(cut_id, {}).get("decision")
        if decision != CUT_NOW:
            note = plan.get(cut_id, {}).get("note", "not planned to cut now")
            raise ValidationError(
                f"cut {cut_id} refused: decision at shift {now} is {decision!r} ({note})"
            )
        a_ok, a_reason, path = self.authority(cut_id, now)
        if not a_ok:
            raise ValidationError(f"cut {cut_id} refused: {a_reason}")
        clashes = self._overlap_already_cut(order)
        if clashes:
            raise ValidationError(
                f"cut {cut_id} refused: inches already cut by {clashes}"
            )
        delivered = tuple(sorted(order.inch_set()))
        # commit
        for span in order.spans():
            self.yarns[span.yarn].consumed.update(span.indices())
        order.status = CUT
        order.delivered = delivered
        self.bench_has_cut[order.bench] = True
        # any live ticket for these inches is now spent
        for ticket in self.tickets.values():
            if ticket.status == "valid" and any(
                i.yarn == ticket.yarn and i.index in ticket.span.indices()
                for i in delivered
            ):
                ticket.status = "released"
        result = {
            "cut_id": cut_id,
            "bench": order.bench,
            "shift": now,
            "path": path,
            "delivered": [i.canonical() for i in delivered],
            "physical_numbers": [f"{i.yarn}#{i.index}" for i in delivered],
        }
        self._record("cut_executed", **result)
        return result

    # -- returned half-segments ---------------------------------------------
    def remaining_pieces(self, yarn_id: str) -> list[dict[str, Any]]:
        """Named physical pieces left after cuts, by actual remaining length."""
        yarn = self._yarn(yarn_id)
        pieces: list[dict[str, Any]] = []
        start = None
        for idx in range(yarn.length()):
            if idx in yarn.consumed:
                if start is not None:
                    pieces.append(self._piece(yarn_id, start, idx))
                    start = None
            else:
                if start is None:
                    start = idx
        if start is not None:
            pieces.append(self._piece(yarn_id, start, yarn.length()))
        return pieces

    def _piece(self, yarn_id: str, start: int, end: int) -> dict[str, Any]:
        return {
            "piece_id": f"{yarn_id}[{start}:{end}]",
            "yarn": yarn_id,
            "start": start,
            "length": end - start,
            "inches": [InchId(yarn_id, i).canonical() for i in range(start, end)],
        }

    def reallocate_returned(self, piece_id: str, span: SpanRef) -> tuple[bool, str | None]:
        """A returned half-segment can only be reallocated by actual remaining length."""
        for yarn_id in self.yarns:
            for piece in self.remaining_pieces(yarn_id):
                if piece["piece_id"] == piece_id:
                    if span.yarn != yarn_id:
                        return False, "span is on a different yarn"
                    if span.start < piece["start"] or span.start + span.length > piece["start"] + piece["length"]:
                        return False, (
                            f"requested {span.length} inch(es) at {span.start} exceed "
                            f"remaining piece {piece_id} (actual length {piece['length']})"
                        )
                    return True, None
        return False, f"no such returned piece {piece_id}"

    # -- auditing ------------------------------------------------------------
    def bench_cut_rights(self) -> dict[str, list[str]]:
        """Physical object numbers each bench actually cut."""
        out: dict[str, list[str]] = {}
        for order in self.cuts.values():
            if order.status != CUT:
                continue
            out.setdefault(order.bench, [])
            out[order.bench].extend(f"{i.yarn}#{i.index}" for i in order.delivered)
        for bench in out:
            out[bench].sort()
        return out

    def audit_physical_exclusivity(self) -> dict[str, Any]:
        """Re-check from physical numbers that no inch was given to two yarns."""
        seen: dict[str, list[str]] = {}
        for order in self.cuts.values():
            if order.status != CUT:
                continue
            for inch in order.delivered:
                key = f"{inch.yarn}#{inch.index}"
                seen.setdefault(key, []).append(order.cut_id)
        collisions = {k: v for k, v in seen.items() if len(v) > 1}
        return {
            "delivered_physical_numbers": sorted(seen),
            "collisions": collisions,
            "exclusive": not collisions,
        }

    # -- helpers -------------------------------------------------------------
    def _yarn(self, yarn_id: str) -> Yarn:
        if yarn_id not in self.yarns:
            raise ValidationError(f"unknown yarn {yarn_id!r}")
        return self.yarns[yarn_id]

    def _prep(self, prep_id: str) -> PrepOrder:
        if prep_id not in self.prep:
            raise ValidationError(f"unknown prep order {prep_id!r}")
        return self.prep[prep_id]

    def _cut(self, cut_id: str) -> CutOrder:
        if cut_id not in self.cuts:
            raise ValidationError(f"unknown cut order {cut_id!r}")
        return self.cuts[cut_id]

    def _ticket(self, ticket_id: str) -> Ticket:
        if ticket_id not in self.tickets:
            raise ValidationError(f"unknown ticket {ticket_id!r}")
        return self.tickets[ticket_id]

    def _find_bound_prep(self, yarn_id: str) -> PrepOrder | None:
        for prep in self.prep.values():
            if yarn_id in (prep.left.yarn, prep.right.yarn):
                return prep
        return None

    def _record(self, event: str, **fields: Any) -> None:
        self.log.append({"event": event, **fields})

    # -- snapshot ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "yarns": {k: v.canonical() for k, v in self.yarns.items()},
            "prep": {
                k: {
                    "prep_id": v.prep_id,
                    "left": v.left.canonical(),
                    "right": v.right.canonical(),
                    "status": v.status,
                    "created_shift": v.created_shift,
                    "bound_pair": v.bound_pair,
                }
                for k, v in self.prep.items()
            },
            "cuts": {
                k: {
                    "cut_id": v.cut_id,
                    "bench": v.bench,
                    "claim_shift": v.claim_shift,
                    "status": v.status,
                    "delivered": [f"{i.yarn}#{i.index}" for i in v.delivered],
                }
                for k, v in self.cuts.items()
            },
            "tickets": {k: v.canonical() for k, v in self.tickets.items()},
            "pairs": {k: {"left": v[0], "right": v[1], "bench": v[2]}
                      for k, v in self.pairs.items()},
            "location": dict(self.location),
            "transit": dict(self.transit),
            "bench_cut_rights": self.bench_cut_rights(),
        }


# ---------------------------------------------------------------------------
# coercion helpers
# ---------------------------------------------------------------------------

def _coerce_point(raw: Mapping[str, Any] | PointReading) -> PointReading:
    if isinstance(raw, PointReading):
        return raw
    if not isinstance(raw, Mapping):
        raise ValidationError(f"point reading must be a mapping, got {raw!r}")
    from .canonical import parse_fraction
    bits = raw.get("bits")
    backoff = parse_fraction(raw.get("backoff", 0), field="backoff")
    flip = raw.get("flip", False)
    if not isinstance(flip, bool):
        raise ValidationError("flip must be a bool")
    return PointReading(bits, backoff, flip)


def _require_diff_axes(left: SpanRef, right: SpanRef) -> None:
    if left.yarn == right.yarn:
        raise ValidationError(
            f"the same inch cannot serve two yarns; spans share yarn {left.yarn}"
        )


def _overlap(a: SpanRef, b: SpanRef) -> bool:
    return a.yarn == b.yarn and max(a.start, b.start) < min(
        a.start + a.length, b.start + b.length
    )


__all__ = [
    "CUT_NOW",
    "WAIT",
    "BLOCKED",
    "PENDING",
    "CUT",
    "CANCELED",
    "InchId",
    "SpanRef",
    "Yarn",
    "PrepOrder",
    "Ticket",
    "CutOrder",
    "Workshop",
    "BENCH_A",
    "BENCH_B",
]
