"""Tasting-day cabinet: stocking plans, two opening modes and robust fulfilment.

The cabinet receives, per buyer:

* a visible shelf list (what they are allowed to see),
* an arrival order,
* an acceptable gift-variety list,

and a set of real sealed boxes whose per-variety quantities and prices are
fixed.  Before opening, whole boxes are chosen for the shelf; after opening,
buyers arrive one by one and take one pack of the most preferred variety
among the visible, acceptable, currently available ones.  Public stock may
only change through a real handover (or an arranged restock arrival): a later
reservation can never make the shop hide stock that was already on the shelf.

Two selectable opening modes share one budget, one acceptable-variety list and
one visibility record:

* ``reserve``: before opening, named guarantee packs may be split out of whole
  boxes.  They leave the public shelf and can only be handed to that buyer on
  time; everyone else picks from the public remainder.  Each split costs a
  per-pack labour fee.
* ``restock``: the shelf stays public and the shop may hold named-variety
  restock rights ordered next door.  Each right can be called at most once;
  the pack arrives only *after the next buyer has picked*, and a handed-out
  pack can never be undone.  Buying a right costs an order fee, calling it
  costs an execution fee.

A stocking plan is accepted only if *every* preference order compatible with
the sampled receipts can be fully honoured.  The cheapest accepted plan and
its minimum guaranteed total cost are reported; for every affordable plan
that cannot guarantee fulfilment, a concrete failure story (the buyer who
misses out first and every handover leading up to it) is produced instead.

Only the Python standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

MODE_PLAIN = "plain"
MODE_RESERVE = "reserve"
MODE_RESTOCK = "restock"
MODES = (MODE_PLAIN, MODE_RESERVE, MODE_RESTOCK)


class CabinetError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Static configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    """A real sealed box: fixed quantities and one fixed price."""

    case_id: str
    contents: Mapping[str, int]
    price: int

    def __post_init__(self):
        if not self.case_id:
            raise CabinetError("case_id must be non-empty")
        if self.price < 0:
            raise CabinetError("box price must be >= 0")
        contents = dict(self.contents)
        for v, q in contents.items():
            if not isinstance(v, str) or not v:
                raise CabinetError("variety names must be non-empty strings")
            if not isinstance(q, int) or q <= 0:
                raise CabinetError(
                    f"box {self.case_id}: quantity of {v!r} must be positive")
        object.__setattr__(self, "contents", contents)

    @property
    def total_packs(self) -> int:
        return sum(self.contents.values())


@dataclass(frozen=True)
class Buyer:
    buyer: str
    order: int                       # arrival order, 0-based
    acceptable: Tuple[str, ...]
    visible: Tuple[str, ...]

    def __post_init__(self):
        if not self.buyer:
            raise CabinetError("buyer id must be non-empty")
        if not self.acceptable:
            raise CabinetError(f"buyer {self.buyer}: acceptable list empty")
        if not self.visible:
            raise CabinetError(f"buyer {self.buyer}: visible list empty")
        if len(set(self.acceptable)) != len(self.acceptable):
            raise CabinetError(f"buyer {self.buyer}: duplicate acceptable")
        if len(set(self.visible)) != len(self.visible):
            raise CabinetError(f"buyer {self.buyer}: duplicate visible")
        object.__setattr__(self, "acceptable", tuple(self.acceptable))
        object.__setattr__(self, "visible", tuple(self.visible))


@dataclass(frozen=True)
class Reservation:
    """A named guarantee pack split out of a whole box before opening."""

    buyer: str
    variety: str
    fee: int                           # per-pack splitting labour fee

    def __post_init__(self):
        if self.fee < 0:
            raise CabinetError("reservation fee must be >= 0")


@dataclass(frozen=True)
class RestockRight:
    """A named-variety restock right that can be called at most once."""

    right_id: str
    variety: str
    order_fee: int                    # paid when the right is bought
    execute_fee: int                  # paid only if/when called

    def __post_init__(self):
        if not self.right_id:
            raise CabinetError("right_id must be non-empty")
        if self.order_fee < 0 or self.execute_fee < 0:
            raise CabinetError("restock fees must be >= 0")


@dataclass(frozen=True)
class CabinetConfig:
    varieties: Tuple[str, ...]
    boxes: Tuple[Box, ...]
    buyers: Tuple[Buyer, ...]
    budget: int
    mode: str = MODE_PLAIN
    reservations: Tuple[Reservation, ...] = ()
    restock_rights: Tuple[RestockRight, ...] = ()
    # A right called at step k becomes available at step k + arrival_delay.
    restock_arrival_delay: int = 2

    def __post_init__(self):
        if self.mode not in MODES:
            raise CabinetError(f"unknown mode {self.mode!r}")
        if not self.varieties:
            raise CabinetError("at least one variety is required")
        if len(set(self.varieties)) != len(self.varieties):
            raise CabinetError("duplicate varieties")
        if self.budget < 0:
            raise CabinetError("budget must be >= 0")
        ids = [b.case_id for b in self.boxes]
        if len(ids) != len(set(ids)):
            raise CabinetError("duplicate box case_id")
        orders = [b.order for b in self.buyers]
        if sorted(orders) != list(range(len(self.buyers))):
            raise CabinetError("buyer orders must be a permutation of 0..n-1")
        seen_buyers = set()
        for b in self.buyers:
            if b.buyer in seen_buyers:
                raise CabinetError(f"duplicate buyer {b.buyer!r}")
            seen_buyers.add(b.buyer)
            for v in (*b.acceptable, *b.visible):
                if v not in self.varieties:
                    raise CabinetError(f"unknown variety {v!r}")
        if self.mode != MODE_RESERVE and self.reservations:
            raise CabinetError("reservations are only valid in reserve mode")
        if self.mode != MODE_RESTOCK and self.restock_rights:
            raise CabinetError("restock rights are only valid in restock mode")
        res_buyers = set()
        for r in self.reservations:
            if r.buyer not in seen_buyers:
                raise CabinetError(f"reservation for unknown buyer {r.buyer!r}")
            if r.variety not in self.varieties:
                raise CabinetError(f"reservation of unknown variety {r.variety!r}")
            if r.buyer in res_buyers:
                raise CabinetError(f"buyer {r.buyer!r} has two reservations")
            buyer = next(x for x in self.buyers if x.buyer == r.buyer)
            if r.variety not in buyer.acceptable:
                raise CabinetError(
                    f"reservation {r.variety!r} not acceptable to {r.buyer!r}")
            res_buyers.add(r.buyer)
        right_ids = [r.right_id for r in self.restock_rights]
        if len(right_ids) != len(set(right_ids)):
            raise CabinetError("duplicate restock right_id")
        for r in self.restock_rights:
            if r.variety not in self.varieties:
                raise CabinetError(f"restock of unknown variety {r.variety!r}")
        if self.restock_arrival_delay < 2:
            # "arrives only after the next buyer has picked" => at least 2.
            raise CabinetError("restock_arrival_delay must be >= 2")
        object.__setattr__(self, "varieties", tuple(self.varieties))
        object.__setattr__(self, "boxes", tuple(self.boxes))
        object.__setattr__(self, "buyers",
                           tuple(sorted(self.buyers, key=lambda x: x.order)))
        object.__setattr__(self, "reservations", tuple(self.reservations))
        object.__setattr__(self, "restock_rights", tuple(self.restock_rights))

    def buyer_by_id(self, buyer: str) -> Buyer:
        for b in self.buyers:
            if b.buyer == buyer:
                return b
        raise CabinetError(f"unknown buyer {buyer!r}")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class TraceEvent:
    """One real step of an execution (a replayable failure story or handover)."""

    step: int
    buyer: str
    shelf: List[str]
    visible: List[str]
    options: List[str]
    picked: Optional[str]
    source: str                       # public | reserved | restock | none
    pack_id: Optional[str] = None
    called_right: Optional[str] = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "step": self.step, "buyer": self.buyer,
            "shelf": list(self.shelf), "visible": list(self.visible),
            "options": list(self.options), "picked": self.picked,
            "source": self.source, "pack_id": self.pack_id,
            "called_right": self.called_right, "note": self.note,
        }


@dataclass
class Handover:
    step: int
    buyer: str
    variety: str
    source: str
    pack_id: str

    def as_dict(self) -> dict:
        return {"step": self.step, "buyer": self.buyer,
                "variety": self.variety, "source": self.source,
                "pack_id": self.pack_id}


@dataclass
class PlanResult:
    mode: str
    boxes: Tuple[str, ...]
    box_cost: int
    reservation_fee: int
    restock_order_fee: int
    restock_execute_fee: int          # worst-case guaranteed execution cost
    total_cost: int
    within_budget: bool
    feasible: bool
    first_failed_buyer: Optional[str] = None
    witness: List[TraceEvent] = field(default_factory=list)
    reservations: List[dict] = field(default_factory=list)
    restock_rights: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "boxes": list(self.boxes),
            "box_cost": self.box_cost,
            "reservation_fee": self.reservation_fee,
            "restock_order_fee": self.restock_order_fee,
            "restock_execute_fee": self.restock_execute_fee,
            "total_cost": self.total_cost,
            "within_budget": self.within_budget,
            "feasible": self.feasible,
            "first_failed_buyer": self.first_failed_buyer,
            "witness": [e.as_dict() for e in self.witness],
            "reservations": list(self.reservations),
            "restock_rights": list(self.restock_rights),
        }


@dataclass
class CabinetReport:
    mode: str
    budget: int
    feasible: List[PlanResult]
    infeasible: List[PlanResult]

    @property
    def cheapest(self) -> Optional[PlanResult]:
        return self.feasible[0] if self.feasible else None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "budget": self.budget,
            "feasible": [p.as_dict() for p in self.feasible],
            "infeasible": [p.as_dict() for p in self.infeasible],
            "cheapest": self.cheapest.as_dict() if self.cheapest else None,
        }


# ---------------------------------------------------------------------------
# Plan materialisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pack:
    pack_id: str
    case_id: str
    seq: int
    variety: str


def _materialise_boxes(boxes: Sequence[Box]) -> List[Pack]:
    packs: List[Pack] = []
    for box in boxes:
        seq = 0
        for variety in sorted(box.contents):
            for _ in range(box.contents[variety]):
                packs.append(Pack(f"{box.case_id}#p{seq:02d}", box.case_id,
                                  seq, variety))
                seq += 1
    return packs


@dataclass
class Plan:
    config: CabinetConfig
    boxes: Tuple[Box, ...]
    public_packs: List[Pack]
    reserved_packs: Dict[str, Pack]
    rights: Tuple[RestockRight, ...]

    def public_counts(self) -> Dict[str, int]:
        out = {v: 0 for v in self.config.varieties}
        for p in self.public_packs:
            out[p.variety] += 1
        return out


def build_plan(config: CabinetConfig, chosen: Sequence[Box],
               rights: Sequence[RestockRight] = ()) -> Plan:
    """Split reserved packs out of the chosen boxes before opening."""
    packs = _materialise_boxes(chosen)
    by_variety: Dict[str, List[Pack]] = {}
    for p in packs:
        by_variety.setdefault(p.variety, []).append(p)
    reserved: Dict[str, Pack] = {}
    for r in config.reservations:
        pool = by_variety.get(r.variety)
        if not pool:
            raise CabinetError(
                f"cannot reserve {r.variety!r} for {r.buyer!r}: not in boxes")
        pack = pool.pop(0)
        reserved[r.buyer] = pack
    public = [p for pool in by_variety.values() for p in pool]
    return Plan(config, tuple(chosen), public, reserved, tuple(rights))


PossiblePicksFn = Callable[[str, Sequence[str]], List[str]]


# ---------------------------------------------------------------------------
# Adversarial DFS, shared by planning and concrete execution
# ---------------------------------------------------------------------------

class CabinetEngine:
    """Evaluate stocking plans against all compatible preference orders."""

    def __init__(self, config: CabinetConfig,
                 possible_picks: Optional[PossiblePicksFn] = None):
        self.config = config
        # Default: structurally every available, acceptable variety may be
        # picked.  The preference desk narrows this to compatible choices.
        self.possible_picks = possible_picks or (lambda _b, opts: list(opts))
        self.buyers = sorted(config.buyers, key=lambda b: b.order)
        self._n = len(self.buyers)
        self._delay = config.restock_arrival_delay
        self._future_acceptable = [
            {v for b in self.buyers[i + 1:] for v in b.acceptable}
            for i in range(self._n)]

    # -- enumeration -----------------------------------------------------
    def all_box_subsets(self) -> List[Tuple[Box, ...]]:
        out: List[Tuple[Box, ...]] = []
        boxes = self.config.boxes
        for size in range(len(boxes) + 1):
            for combo in combinations(range(len(boxes)), size):
                out.append(tuple(boxes[i] for i in combo))
        out.sort(key=lambda c: (sum(b.price for b in c),
                                tuple(b.case_id for b in c)))
        return out

    def evaluate(self) -> CabinetReport:
        cfg = self.config
        feasible: List[PlanResult] = []
        infeasible: List[PlanResult] = []
        for chosen in self.all_box_subsets():
            box_cost = sum(b.price for b in chosen)
            reservation_fee = sum(r.fee for r in cfg.reservations)
            order_fee = sum(r.order_fee for r in cfg.restock_rights)
            fixed_cost = box_cost + reservation_fee + order_fee
            if fixed_cost > cfg.budget:
                # Even without any execution fee the plan is unaffordable.
                continue
            try:
                plan = build_plan(cfg, chosen, cfg.restock_rights)
            except CabinetError:
                # The chosen boxes cannot supply the required reservations.
                continue
            ok, witness, exec_fee = self.robust(plan)
            total = fixed_cost + exec_fee
            if total > cfg.budget:
                # The guaranteed execution cost breaks the budget.
                ok = False
            result = PlanResult(
                mode=cfg.mode,
                boxes=tuple(b.case_id for b in chosen),
                box_cost=box_cost,
                reservation_fee=reservation_fee,
                restock_order_fee=order_fee,
                restock_execute_fee=exec_fee,
                total_cost=total,
                within_budget=total <= cfg.budget,
                feasible=ok,
                first_failed_buyer=(None if ok else
                                     witness[-1].buyer if witness else None),
                witness=[] if ok else witness,
                reservations=[{"buyer": r.buyer, "variety": r.variety,
                               "fee": r.fee,
                               "pack_id": plan.reserved_packs[r.buyer].pack_id}
                              for r in cfg.reservations],
                restock_rights=[{"right_id": r.right_id, "variety": r.variety,
                                 "order_fee": r.order_fee,
                                 "execute_fee": r.execute_fee}
                                for r in cfg.restock_rights],
            )
            (feasible if ok else infeasible).append(result)
        feasible.sort(key=lambda p: p.total_cost)
        infeasible.sort(key=lambda p: (p.total_cost, p.boxes))
        return CabinetReport(cfg.mode, cfg.budget, feasible, infeasible)

    # -- helpers ---------------------------------------------------------
    def _options(self, buyer: Buyer, public: Mapping[str, int]) -> List[str]:
        on_shelf = sorted(v for v, q in public.items() if q > 0
                          and v in buyer.visible and v in buyer.acceptable)
        possible = self.possible_picks(buyer.buyer, on_shelf)
        return [v for v in on_shelf if v in possible]

    @staticmethod
    def _take(public: Dict[str, int], variety: str) -> None:
        if public.get(variety, 0) <= 0:
            raise AssertionError("shelf may only shrink via a real handover")
        public[variety] -= 1

    def _box_pack_ids(self, plan: Plan, variety: str) -> List[str]:
        return [p.pack_id for p in sorted(plan.public_packs,
                                          key=lambda x: (x.case_id, x.seq))
                if p.variety == variety]

    def _pick_pack_id(self, plan: Plan, variety: str,
                      used: frozenset, external: Mapping[str, List[str]]
                      ) -> Tuple[str, str]:
        """Choose a real pack id; box packs first, restock arrivals second."""
        for pid in self._box_pack_ids(plan, variety):
            if pid not in used:
                return pid, "public"
        for pid in external.get(variety, []):
            if pid not in used:
                return pid, "restock"
        raise CabinetError(f"no pack of {variety!r} left on the shelf")

    @staticmethod
    def _state_key(step, public, arrivals, rights, used, external):
        return (step,
                tuple(sorted(public.items())),
                tuple(sorted((k, tuple(vs)) for k, vs in arrivals.items())),
                tuple(sorted(r.right_id for r in rights)),
                used,
                tuple(sorted((v, tuple(ids))
                              for v, ids in external.items())))

    # -- adversarial search ---------------------------------------------
    def robust(self, plan: Plan,
               start_step: int = 0,
               initial_public: Optional[Dict[str, int]] = None,
               initial_arrivals: Optional[Dict[int, List[Tuple[str, str]]]] = None,
               initial_rights: Optional[List[RestockRight]] = None,
               initial_used: Optional[frozenset] = None,
               initial_external: Optional[Dict[str, List[str]]] = None
               ) -> Tuple[bool, List[TraceEvent], int]:
        """Guarantee fulfilment from a state over every compatible pick.

        Returns ``(ok, suffix_events, guaranteed_execution_fee)``: on failure
        ``suffix_events`` is a concrete story beginning at ``start_step``; on
        success the event list is empty and the fee is the minimum execution
        cost the shop must be ready to pay in the worst compatible branch.
        Memo entries store suffixes only, so they are path independent.
        """
        memo: Dict[tuple, Tuple[bool, List[TraceEvent], int]] = {}
        plan_public = plan.public_counts()
        public0 = (plan_public if initial_public is None
                   else dict(initial_public))
        arrivals0: Dict[int, List[Tuple[str, str]]] = (
            {} if initial_arrivals is None
            else {k: list(vs) for k, vs in initial_arrivals.items()})
        rights0: List[RestockRight] = (
            list(plan.rights) if initial_rights is None
            else list(initial_rights))
        used0: frozenset = (
            frozenset() if initial_used is None else frozenset(initial_used))
        external0: Dict[str, List[str]] = (
            {} if initial_external is None
            else {v: list(ids) for v, ids in initial_external.items()})

        def dfs(step: int, public: Dict[str, int],
                arrivals: Dict[int, List[Tuple[str, str]]],
                rights: List[RestockRight],
                used: frozenset,
                external: Dict[str, List[str]]
                ) -> Tuple[bool, List[TraceEvent], int]:
            if step == self._n:
                return True, [], 0
            mk = self._state_key(step, public, arrivals, rights, used,
                                 external)
            if mk in memo:
                return memo[mk]

            buyer = self.buyers[step]
            # Restock packs due at this step join the public shelf first.
            for right_id, v in arrivals.pop(step, []):
                public[v] = public.get(v, 0) + 1
                external.setdefault(v, []).append(f"restock:{right_id}")
            shelf = sorted(v for v, q in public.items() if q > 0)

            # A named reservation is a deterministic handover that never
            # touches the public shelf.  Reservations of buyers already
            # served before a re-entry are simply absent from this step.
            if buyer.buyer in plan.reserved_packs:
                pack = plan.reserved_packs[buyer.buyer]
                event = TraceEvent(
                    step, buyer.buyer, shelf, list(buyer.visible),
                    [pack.variety], pack.variety, "reserved", pack.pack_id)
                ok, suffix, fee = dfs(step + 1, public, arrivals, rights,
                                      used, external)
                memo[mk] = ok, ([event] + suffix if not ok else []), fee
                return memo[mk]

            options = self._options(buyer, public)
            if not options:
                event = TraceEvent(step, buyer.buyer, shelf,
                                   list(buyer.visible), [], None, "none",
                                   note="no visible acceptable pack available")
                memo[mk] = False, [event], 0
                return memo[mk]

            if self.config.mode == MODE_RESTOCK:
                # The current buyer picks first.  Only *after* the pick may
                # the shop call at most one held right, and the called pack
                # lands after the *next* buyer has picked (step + delay), so
                # a call can never rescue the current step or the next one.
                # A robust plan must survive every compatible pick; for each
                # pick the shop picks the cheapest post-pick action whose
                # adversarial suffix is still robust.
                worst_fee = 0
                for pick in options:
                    n_public = dict(public)
                    self._take(n_public, pick)
                    pack_id, source = self._pick_pack_id(
                        plan, pick, used, external)
                    actions: List[Optional[RestockRight]] = [None]
                    for r in rights:
                        lands = step + self._delay
                        if lands < self._n and any(
                                r.variety in b.acceptable
                                for b in self.buyers[lands:]):
                            actions.append(r)
                    best_fee: Optional[int] = None
                    best_witness: List[TraceEvent] = []
                    for action in actions:
                        n_arrivals = {k: list(vs)
                                      for k, vs in arrivals.items()}
                        n_rights = list(rights)
                        called = None
                        action_fee = 0
                        if action is not None:
                            n_rights.remove(action)
                            n_arrivals.setdefault(
                                step + self._delay, []).append(
                                    (action.right_id, action.variety))
                            called = action.right_id
                            action_fee = action.execute_fee
                        event = TraceEvent(
                            step, buyer.buyer, shelf, list(buyer.visible),
                            options, pick, source, pack_id,
                            called_right=called,
                            note=("called restock right after the pick; it "
                                  "arrives after the next buyer picks")
                            if called else "")
                        ok, suffix, fee = dfs(
                            step + 1, n_public, n_arrivals, n_rights,
                            used | {pack_id},
                            {v: list(ids) for v, ids in external.items()})
                        if not ok:
                            if best_fee is None:
                                best_witness = [event] + suffix
                            continue
                        total_fee = action_fee + fee
                        if best_fee is None or total_fee < best_fee:
                            best_fee = total_fee
                    if best_fee is None:
                        memo[mk] = False, best_witness, 0
                        return memo[mk]
                    worst_fee = max(worst_fee, best_fee)
                memo[mk] = True, [], worst_fee
                return memo[mk]

            # Plain / reserve public picking: every compatible pick must work.
            worst_fee = 0
            for pick in options:
                n_public = dict(public)
                self._take(n_public, pick)
                pack_id, source = self._pick_pack_id(
                    plan, pick, used, external)
                event = TraceEvent(
                    step, buyer.buyer, shelf, list(buyer.visible),
                    options, pick, source, pack_id)
                ok, suffix, fee = dfs(
                    step + 1, n_public,
                    {k: list(vs) for k, vs in arrivals.items()},
                    list(rights), used | {pack_id},
                    {v: list(ids) for v, ids in external.items()})
                if not ok:
                    memo[mk] = False, [event] + suffix, 0
                    return memo[mk]
                worst_fee = max(worst_fee, fee)
            memo[mk] = True, [], worst_fee
            return memo[mk]

        return dfs(start_step, public0, arrivals0, rights0, used0, external0)

    # -- concrete execution ---------------------------------------------
    def execute(self, plan: Plan, orders: Mapping[str, Sequence[str]],
                restock_policy: str = "robust"
                ) -> List[Handover]:
        """Run one real day with fixed per-buyer strict orders.

        ``orders[buyer]`` is a strict total order, favourite first.  In
        restock mode ``restock_policy`` selects the call rule:

        * ``robust`` (default): re-run the adversarial suffix for every
          legal action and take the cheapest one that guarantees the rest
          of the day -- identical decisions to the planner;
        * ``none``: never call a right (useful for demonstrating that a
          late restock cannot undo an already handed-out pack).

        Handovers are the only thing that removes public stock.
        """
        if restock_policy not in ("robust", "none"):
            raise CabinetError(f"unknown restock_policy {restock_policy!r}")
        for b in self.buyers:
            if tuple(sorted(orders[b.buyer])) != tuple(sorted(
                    self.config.varieties)):
                raise CabinetError(
                    f"order for {b.buyer!r} must cover every variety once")
        public = plan.public_counts()
        arrivals: Dict[int, List[Tuple[str, str]]] = {}
        rights = list(plan.rights)
        external: Dict[str, List[str]] = {}
        used: set = set()
        handovers: List[Handover] = []

        for step, buyer in enumerate(self.buyers):
            for right_id, v in arrivals.pop(step, []):
                public[v] = public.get(v, 0) + 1
                pid = f"restock:{right_id}"
                external.setdefault(v, []).append(pid)
            if buyer.buyer in plan.reserved_packs:
                pack = plan.reserved_packs[buyer.buyer]
                handovers.append(Handover(step, buyer.buyer, pack.variety,
                                          "reserved", pack.pack_id))
                continue
            options = self._options(buyer, public)
            if not options:
                raise CabinetFailure(buyer.buyer, step, handovers)
            rank = {v: i for i, v in enumerate(orders[buyer.buyer])}
            pick = min(options, key=lambda v: rank[v])
            self._take(public, pick)
            pack_id, source = self._pick_pack_id(plan, pick,
                                                 frozenset(used), external)
            used.add(pack_id)
            handovers.append(Handover(step, buyer.buyer, pick, source,
                                      pack_id))
            # The call happens only after the handover; the new pack cannot
            # arrive before the next buyer has finished picking.
            if self.config.mode == MODE_RESTOCK and restock_policy == "robust":
                action = self._choose_call(plan, step, public, arrivals,
                                           rights, frozenset(used), external)
                if action is not None:
                    rights.remove(action)
                    arrivals.setdefault(step + self._delay, []).append(
                        (action.right_id, action.variety))
        return handovers

    def _choose_call(self, plan: Plan, step: int,
                     public: Mapping[str, int],
                     arrivals: Mapping[int, List[Tuple[str, str]]],
                     rights: Sequence[RestockRight],
                     used: frozenset,
                     external: Mapping[str, List[str]]
                     ) -> Optional[RestockRight]:
        """Cheapest post-pick call whose adversarial suffix stays robust."""
        lands = step + self._delay
        candidates: List[Optional[RestockRight]] = [None]
        if lands < self._n:
            for r in rights:
                if any(r.variety in b.acceptable
                       for b in self.buyers[lands:]):
                    candidates.append(r)
        best: Optional[Tuple[int, Optional[RestockRight]]] = None
        for action in candidates:
            n_arrivals = {k: list(vs) for k, vs in arrivals.items()}
            n_rights = list(rights)
            fee = 0
            if action is not None:
                n_rights.remove(action)
                n_arrivals.setdefault(lands, []).append(
                    (action.right_id, action.variety))
                fee = action.execute_fee
            ok, _suf, suffix_fee = self.robust(
                plan, start_step=step + 1,
                initial_public=dict(public),
                initial_arrivals=n_arrivals,
                initial_rights=n_rights,
                initial_used=used,
                initial_external={v: list(ids)
                                   for v, ids in external.items()})
            if ok and (best is None or fee + suffix_fee < best[0]):
                best = (fee + suffix_fee, action)
        return best[1] if best else None


class CabinetFailure(Exception):
    """A concrete execution broke its promise."""

    def __init__(self, buyer: str, step: int,
                 handovers: Sequence[Handover]):
        self.buyer = buyer
        self.step = step
        self.handovers = list(handovers)
        super().__init__(f"buyer {buyer!r} cannot be served at step {step}")
