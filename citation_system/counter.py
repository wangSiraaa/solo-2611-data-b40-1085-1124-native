"""Counter workflow for allocating whole tickets to debts and releasing pledges.

The implemented solution is a deadline-based reservation.  A guarantee note is
not offered: requests that require one are rejected, so customers are never
made to accept an undisclosed quarantine or partial-delivery risk.

Tickets and physical objects are owned by concrete cabinets.  Once a real
ticket is accepted it cannot be returned by software, and a signed-out object
cannot be recovered by rolling a database back.  The state machine below makes
those transitions explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .pawn import (
    PawnError,
    PawnLedger,
    TERM_FULL,
    TERM_PRINCIPAL_CAP,
)

INSPECT_BEFORE_TICKETS = "before_tickets"
INSPECT_AFTER_FIRST_TICKET = "after_first_ticket"

ALL_OR_NONE = "all_or_none"
SINGLE_ITEMS_ALLOWED = "single_items_allowed"


class CounterError(PawnError):
    """A request cannot be accepted or executed under the agreed conditions."""


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    owner: str
    denomination: int
    cabinet_id: str
    authorized_debt_ids: tuple[str, ...] = ()
    max_payment: int | None = None

    def payment_cap(self) -> int:
        if self.max_payment is None:
            return self.denomination
        return min(self.denomination, self.max_payment)


@dataclass
class CashDrawer:
    cabinet_id: str
    change_coins: int = 0
    accepted_ticket_value: int = 0

    def give_change(self, amount: int) -> None:
        if amount < 0 or amount > self.change_coins:
            raise CounterError(
                "insufficient_change",
                f"cabinet {self.cabinet_id} cannot make {amount} change",
                {"cabinet_id": self.cabinet_id, "needed": amount,
                 "available": self.change_coins},
            )
        self.change_coins -= amount


@dataclass(frozen=True)
class PhysicalItem:
    item_id: str
    cabinet_id: str
    quarantine: bool


@dataclass(frozen=True)
class Payment:
    ticket_id: str
    debt_id: str
    tendered: int
    applied: int
    fee_applied: int
    principal_applied: int
    change: int


@dataclass
class ReleasePlan:
    item_ids: tuple[str, ...]
    cabinet_ids: tuple[str, ...]
    payments: tuple[Payment, ...]
    constraints: tuple[dict[str, Any], ...]
    partial_delivery_allowed: bool
    inspection_policy: str
    worst_case: dict[str, Any]
    reservations: dict[str, int] = field(default_factory=dict)
    accepted_ticket_ids: set[str] = field(default_factory=set)
    released_item_ids: set[str] = field(default_factory=set)
    closed: bool = False

    @property
    def all_items_released(self) -> bool:
        return set(self.item_ids) <= self.released_item_ids


class CounterOffice:
    """Stateful counter with physically separate cabinets."""

    def __init__(
        self,
        ledger: PawnLedger,
        tickets: Iterable[Ticket],
        items: Iterable[PhysicalItem],
        drawers: Iterable[CashDrawer],
    ):
        self.ledger = ledger
        ticket_list = list(tickets)
        self.tickets = {t.ticket_id: t for t in ticket_list}
        if len(self.tickets) != len(ticket_list):
            raise CounterError("duplicate_ticket", "ticket ids must be unique")
        item_list = list(items)
        self.physical_items = {i.item_id: i for i in item_list}
        if len(self.physical_items) != len(item_list):
            raise CounterError("duplicate_item", "physical item ids must be unique")
        drawer_list = list(drawers)
        self.drawers = {
            d.cabinet_id: CashDrawer(
                cabinet_id=d.cabinet_id,
                change_coins=d.change_coins,
                accepted_ticket_value=d.accepted_ticket_value,
            )
            for d in drawer_list
        }
        if len(self.drawers) != len(drawer_list):
            raise CounterError("duplicate_cash_drawer", "cash drawer cabinet ids must be unique")
        self.reserved_ticket_ids: set[str] = set()
        self.reserved_item_ids: set[str] = set()
        self.reservations: dict[str, ReleasePlan] = {}
        self._revealed: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------
    def plan_redemption(
        self,
        item_ids: Iterable[str],
        *,
        owner: str,
        delivery_policy: str = ALL_OR_NONE,
        inspection_policy: str = INSPECT_BEFORE_TICKETS,
        deadline: int | None = None,
        guarantee_note: bool = False,
        require_full_delivery_guarantee: bool = False,
    ) -> ReleasePlan:
        item_ids = tuple(dict.fromkeys(item_ids))
        if not item_ids:
            raise CounterError("empty_request", "at least one item is required")
        if delivery_policy not in (ALL_OR_NONE, SINGLE_ITEMS_ALLOWED):
            raise CounterError("invalid_delivery_policy", "unknown delivery policy")
        if inspection_policy not in (
            INSPECT_BEFORE_TICKETS,
            INSPECT_AFTER_FIRST_TICKET,
        ):
            raise CounterError("invalid_inspection_policy", "unknown inspection policy")
        if (
            require_full_delivery_guarantee
            and inspection_policy == INSPECT_AFTER_FIRST_TICKET
            and delivery_policy == ALL_OR_NONE
        ):
            # The second cabinet cannot be inspected before the first ticket is
            # non-refundable, and this counter has no partial/compensation note.
            raise CounterError(
                "full_delivery_guarantee_refused",
                "hidden later quarantine makes an all-or-none guarantee impossible",
            )
        if guarantee_note:
            # The selected implementation is reservation-only.  Do not infer a
            # guarantee that this office is not prepared to honor.
            raise CounterError(
                "guarantee_note_not_offered",
                "this office only supports deadline reservations; guarantee notes are not offered",
            )
        for item_id in item_ids:
            if item_id not in self.ledger.items:
                raise CounterError("item_not_pledged", f"item {item_id} is not pledged",
                                   {"item_id": item_id})
            if self.ledger.items[item_id].released:
                raise CounterError("item_already_signed_out", f"item {item_id} cannot be recovered",
                                   {"item_id": item_id})
            if item_id in self.reserved_item_ids:
                raise CounterError("item_already_reserved", f"item {item_id} has a reservation")

        cabinet_ids = self._required_cabinets(item_ids)
        constraints = self._build_constraints(item_ids)
        available = [
            ticket for ticket in self.tickets.values()
            if ticket.ticket_id not in self.reserved_ticket_ids
            and ticket.owner == owner
            and ticket.cabinet_id in cabinet_ids
            and any(
                self.ledger.debts[debt_id].authorizes(owner)
                for debt_id in (ticket.authorized_debt_ids or self.ledger.debts)
                if debt_id in self.ledger.debts
            )
        ]
        payment_search = _PaymentSearch(self.ledger, self.tickets, self.drawers, available)
        payments = payment_search.solve(constraints)
        if payments is None:
            raise CounterError(
                "no_feasible_payment",
                "no allocation of whole tickets satisfies every effective release term",
                {"constraints": constraints},
            )
        plan = ReleasePlan(
            item_ids=item_ids,
            cabinet_ids=tuple(sorted(cabinet_ids)),
            payments=tuple(payments),
            constraints=tuple(constraints),
            partial_delivery_allowed=delivery_policy == SINGLE_ITEMS_ALLOWED,
            inspection_policy=inspection_policy,
            worst_case=self._worst_case(inspection_policy, delivery_policy),
        )
        if deadline is not None:
            self.reserve(plan, deadline)
        return plan

    def reserve(self, plan: ReleasePlan, deadline: int) -> str:
        if not isinstance(deadline, int) or deadline < 0:
            raise CounterError("invalid_deadline", "deadline must be a non-negative integer")
        if plan.closed:
            raise CounterError("plan_closed", "cannot reserve a closed plan")
        reservation_id = f"R{len(self.reservations) + 1}"
        plan.reservations[reservation_id] = deadline
        for payment in plan.payments:
            if payment.ticket_id in self.reserved_ticket_ids:
                raise CounterError("ticket_already_reserved", "ticket is already reserved")
            self.reserved_ticket_ids.add(payment.ticket_id)
        for item_id in plan.item_ids:
            if item_id in self.reserved_item_ids:
                raise CounterError("item_already_reserved", "item is already reserved")
            self.reserved_item_ids.add(item_id)
        self.reservations[reservation_id] = plan
        return reservation_id

    def release_reservation(self, reservation_id: str, *, now: int) -> None:
        plan = self.reservations.get(reservation_id)
        if plan is None:
            raise CounterError("reservation_not_found", "unknown reservation")
        deadline = plan.reservations[reservation_id]
        if now > deadline:
            raise CounterError("reservation_expired", "reservation deadline has passed")
        if plan.closed:
            raise CounterError("plan_closed", "reservation is already closed")
        self._execute(plan)

    def cancel_reservation(self, reservation_id: str) -> None:
        """Cancel before any ticket is accepted."""

        plan = self.reservations.get(reservation_id)
        if plan is None:
            raise CounterError("reservation_not_found", "unknown reservation")
        if plan.accepted_ticket_ids:
            raise CounterError(
                "ticket_accepted_irreversible",
                "a real ticket has already entered the cash drawer",
            )
        for payment in plan.payments:
            self.reserved_ticket_ids.discard(payment.ticket_id)
        for item_id in plan.item_ids:
            self.reserved_item_ids.discard(item_id)
        plan.closed = True
        self.reservations.pop(reservation_id)

    # ------------------------------------------------------------------
    # physical execution
    # ------------------------------------------------------------------
    def _execute(self, plan: ReleasePlan) -> None:
        if plan.inspection_policy == INSPECT_BEFORE_TICKETS:
            self._inspect_all(plan)
            self._accept_all_tickets(plan)
            self._sign_out_all(plan)
            return

        # After-first-ticket policy: the second cabinet is intentionally still
        # hidden when the first non-refundable ticket is accepted.
        ordered_cabinets = list(plan.cabinet_ids)
        first = ordered_cabinets[0]
        first_payments = [p for p in plan.payments
                          if self.tickets[p.ticket_id].cabinet_id == first]
        if not first_payments:
            raise CounterError("invalid_plan", "first cabinet has no payment")
        self._accept_tickets(plan, first_payments)
        self._inspect_all(plan)
        self._sign_out_all(plan)

    def _inspect_all(self, plan: ReleasePlan) -> None:
        for item_id in plan.item_ids:
            physical = self.physical_items.get(item_id)
            if physical is None:
                raise CounterError("physical_item_missing", f"item {item_id} is not in storage")
            self._revealed[item_id] = physical.quarantine
            if physical.quarantine:
                raise CounterError(
                    "item_quarantined",
                    f"item {item_id} is quarantined and cannot leave the cabinet",
                    {"item_id": item_id, "cabinet_id": physical.cabinet_id},
                )

    def _accept_all_tickets(self, plan: ReleasePlan) -> None:
        self._accept_tickets(plan, list(plan.payments))

    def _accept_tickets(self, plan: ReleasePlan, payments: list[Payment]) -> None:
        by_cabinet: dict[str, list[Payment]] = {}
        for payment in payments:
            ticket = self.tickets.get(payment.ticket_id)
            if ticket is None or ticket.ticket_id in plan.accepted_ticket_ids:
                raise CounterError("invalid_ticket", "ticket is unknown or already used")
            by_cabinet.setdefault(ticket.cabinet_id, []).append(payment)
        for cabinet_id, group in by_cabinet.items():
            drawer = self.drawers.get(cabinet_id)
            if drawer is None:
                raise CounterError("drawer_not_found", "cabinet has no cash drawer")
            for payment in group:
                ticket = self.tickets[payment.ticket_id]
                if payment.tendered != ticket.denomination:
                    raise CounterError(
                        "invalid_ticket",
                        "ticket denomination does not match plan",
                    )
                drawer.accepted_ticket_value += payment.tendered
                if payment.change:
                    drawer.give_change(payment.change)
                plan.accepted_ticket_ids.add(payment.ticket_id)

    def _sign_out_all(self, plan: ReleasePlan) -> None:
        for item_id in plan.item_ids:
            if item_id in plan.released_item_ids:
                raise CounterError(
                    "duplicate_item_signature",
                    f"item {item_id} was already signed out",
                )
            if not self._ledger_term_satisfied(item_id):
                raise CounterError(
                    "release_term_not_satisfied",
                    f"item {item_id} cannot be released",
                )
            plan.released_item_ids.add(item_id)
            self.ledger.items[item_id].released = True
        if plan.all_items_released:
            plan.closed = True
            for item_id in plan.item_ids:
                self.reserved_item_ids.discard(item_id)
            for payment in plan.payments:
                self.reserved_ticket_ids.discard(payment.ticket_id)

    def customer_changes_mind(
        self, plan: ReleasePlan, wanted_item_ids: Iterable[str]
    ) -> tuple[str, ...]:
        """Handle a change of mind after some objects have left the cabinet."""

        wanted = tuple(dict.fromkeys(wanted_item_ids))
        if not plan.partial_delivery_allowed:
            raise CounterError(
                "single_item_delivery_not_agreed",
                "the contract is all-or-none; partial completion was not agreed",
            )
        already = set(plan.released_item_ids)
        if not already or not set(wanted).issubset(plan.item_ids):
            raise CounterError(
                "invalid_partial_request",
                "the changed request is outside this plan",
            )
        if any(item_id not in wanted for item_id in already):
            raise CounterError(
                "cannot_recover_signed_out_item",
                "signed-out items cannot be recovered",
            )
        for item_id in plan.item_ids:
            if item_id not in wanted:
                plan.released_item_ids.discard(item_id)
                self.reserved_item_ids.discard(item_id)
        if plan.all_items_released:
            plan.closed = True
        return wanted

    # ------------------------------------------------------------------
    # constraints and risk
    # ------------------------------------------------------------------
    def _build_constraints(self, item_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        constraints: list[dict[str, Any]] = []
        for item_id in item_ids:
            item = self.ledger.items[item_id]
            if not item.terms:
                raise CounterError(
                    "no_effective_release_term",
                    f"item {item_id} has no release term",
                )
            for term in item.terms:
                constraints.append({
                    "item_id": item_id,
                    "term_id": term.term_id,
                    "kind": term.kind,
                    "debt_ids": list(term.debt_ids),
                    "threshold": term.threshold,
                })
        return constraints

    def _required_cabinets(self, item_ids: tuple[str, ...]) -> set[str]:
        cabinet_ids: set[str] = set()
        for item_id in item_ids:
            physical = self.physical_items.get(item_id)
            if physical is None:
                raise CounterError("physical_item_missing", f"item {item_id} is not in storage")
            cabinet_ids.add(physical.cabinet_id)
        return cabinet_ids

    def _ledger_term_satisfied(self, item_id: str) -> bool:
        return self.ledger.can_release(item_id)

    @staticmethod
    def _worst_case(inspection_policy: str, delivery_policy: str) -> dict[str, Any]:
        if inspection_policy == INSPECT_BEFORE_TICKETS:
            return {
                "shop_liability": "reject_before_ticket",
                "description": (
                    "quarantine is discovered before payment; the whole order is "
                    "rejected and no ticket becomes non-refundable"
                ),
                "customer_risk_accepted": False,
            }
        return {
            "shop_liability": "first_ticket_non_refundable_hidden_second_cabinet",
            "description": (
                "the first ticket may be kept while the later cabinet reveals "
                "quarantine; without a partial/guarantee agreement no full "
                "delivery can be promised"
            ),
            "customer_risk_accepted": delivery_policy == SINGLE_ITEMS_ALLOWED,
        }


class _PaymentSearch:
    def __init__(
        self,
        ledger: PawnLedger,
        all_tickets: dict[str, Ticket],
        drawers: dict[str, CashDrawer],
        available: list[Ticket],
    ):
        self.ledger = ledger
        self.all_tickets = all_tickets
        self.available = available
        self.drawers = drawers
        self.needed_debt_ids = sorted({
            debt_id
            for ticket in available
            for debt_id in (ticket.authorized_debt_ids or ledger.debts.keys())
            if debt_id in ledger.debts
        })
        # Include debts that have no eligible ticket only when constraints need
        # them; the search will then correctly fail rather than invent money.
        self.debt_ids = self.needed_debt_ids

    def solve(self, constraints: list[dict[str, Any]]) -> list[Payment] | None:
        required = sorted({d for c in constraints for d in c["debt_ids"]})
        self.debt_ids = required
        debts = {d: self.ledger.debts[d] for d in required}
        available = [
            ticket for ticket in self.available
            if any(
                (not ticket.authorized_debt_ids or d in ticket.authorized_debt_ids)
                and (not debts[d].cabinet_id or ticket.cabinet_id == debts[d].cabinet_id)
                for d in required
            )
        ]
        self.available = available
        fee_needed = {d: debts[d].fee for d in required}
        principal_pay = self._principal_requirements(required, constraints, debts)
        minimum = {d: fee_needed[d] + principal_pay[d] for d in required}
        # Payment above an outstanding balance is allowed only as whole-ticket
        # change/refund handling; the candidate builder still caps applied money
        # at the balance and keeps the excess as change to the named payer.
        balances = {d: debts[d].fee + debts[d].principal for d in required}
        upper = {
            d: max(
                balances[d],
                max((t.denomination for t in self.available), default=0),
            )
            for d in required
        }
        if any(minimum[d] > upper[d] for d in required):
            return None
        self._assert_change_feasible(required, minimum, upper)

        # Prefer smaller applications; overpayment is only useful to consume a
        # fixed denomination and produce permitted change.
        results: dict[str, list[Payment]] = {}
        used: set[str] = set()
        change_used = {cid: 0 for cid in self.drawers}
        if not self._search(0, minimum, upper, used, change_used, results):
            return None
        payments = [payment for group in results.values() for payment in group]
        self._apply_payments(payments)
        return payments

    def _principal_requirements(
        self,
        debt_ids: list[str],
        constraints: list[dict[str, Any]],
        debts: dict[str, Any],
    ) -> dict[str, int]:
        # Full-debt terms require complete principal payment.  A cap only
        # requires reducing the listed aggregate to its threshold.  When several
        # terms overlap, every effective threshold must still be satisfiable.
        must_clear: set[str] = set()
        cap_groups: list[tuple[frozenset[str], int]] = []
        for constraint in constraints:
            ids = frozenset(constraint["debt_ids"])
            if constraint["kind"] == TERM_FULL:
                must_clear.update(ids)
            elif constraint["kind"] == TERM_PRINCIPAL_CAP:
                cap_groups.append((ids, int(constraint["threshold"])))
        principal = {d: (debts[d].principal if d in must_clear else 0) for d in debt_ids}
        for ids, threshold in cap_groups:
            current_total = sum(debts[d].principal for d in ids)
            required_total_reduction = max(0, current_total - threshold)
            if required_total_reduction == 0:
                continue
            # Any distribution that reduces the aggregate by this amount works;
            # use deterministic debt order for planning.  Full-clear requirements
            # already reduce the group and are credited here.
            remaining = required_total_reduction
            for debt_id in sorted(ids):
                if debt_id in must_clear:
                    remaining -= principal[debt_id]
                    continue
                room = debts[debt_id].principal - principal[debt_id]
                add = min(room, max(0, remaining))
                principal[debt_id] += add
                remaining -= add
            if remaining > 0:
                # A cap cannot be met because full-clear constraints or debt
                # balances leave insufficient payable principal.
                return {d: debts[d].principal + 1 for d in debt_ids}
        return principal

    def _assert_change_feasible(
        self, debt_ids: list[str], minimum: dict[str, int], upper: dict[str, int]
    ) -> None:
        for debt_id in debt_ids:
            debt = self.ledger.debts[debt_id]
            balance = debt.fee + debt.principal
            for ticket in self.available:
                if ticket.authorized_debt_ids and debt_id not in ticket.authorized_debt_ids:
                    continue
                if debt.cabinet_id and ticket.cabinet_id != debt.cabinet_id:
                    continue
                value = min(ticket.payment_cap(), balance)
                if value < minimum[debt_id]:
                    continue
                change = ticket.denomination - value
                drawer = self.drawers.get(ticket.cabinet_id)
                if drawer is not None and change <= drawer.change_coins:
                    return
        raise CounterError(
            "insufficient_change",
            "no authorized whole ticket can be tendered with available change",
            {"debt_ids": debt_ids},
        )

    def _search(
        self,
        index: int,
        minimum: dict[str, int],
        upper: dict[str, int],
        used: set[str],
        change_used: dict[str, int],
        results: dict[str, list[Payment]],
    ) -> bool:
        if index == len(self.debt_ids):
            return True
        debt_id = self.debt_ids[index]
        debt = self.ledger.debts[debt_id]
        candidates = self._candidate_payments(
            debt_id, minimum[debt_id], upper[debt_id], used, change_used
        )
        for payments in sorted(
            candidates,
            key=lambda group: (len(group), sum(p.applied for p in group)),
        ):
            group_change = {cid: 0 for cid in change_used}
            ok = True
            for payment in payments:
                ticket = self.all_tickets[payment.ticket_id]
                group_change[ticket.cabinet_id] += payment.change
                available_change = (
                    change_used[ticket.cabinet_id]
                    + group_change[ticket.cabinet_id]
                )
                if available_change > self.drawers[ticket.cabinet_id].change_coins:
                    ok = False
                    break
            if not ok:
                continue
            for payment in payments:
                used.add(payment.ticket_id)
                ticket = self.all_tickets[payment.ticket_id]
                change_used[ticket.cabinet_id] += payment.change
            results[debt_id] = payments
            if self._search(index + 1, minimum, upper, used, change_used, results):
                return True
            for payment in payments:
                used.remove(payment.ticket_id)
                ticket = self.all_tickets[payment.ticket_id]
                change_used[ticket.cabinet_id] -= payment.change
            results.pop(debt_id, None)
        return False

    def _candidate_payments(
        self,
        debt_id: str,
        minimum: int,
        upper: int,
        used: set[str],
        change_used: dict[str, int],
    ) -> list[list[Payment]]:
        debt = self.ledger.debts[debt_id]
        balance = debt.fee + debt.principal
        eligible = [
            t for t in self.available
            if t.ticket_id not in used
            and (not t.authorized_debt_ids or debt_id in t.authorized_debt_ids)
            and (not debt.cabinet_id or t.cabinet_id == debt.cabinet_id)
        ]
        target_values = {minimum, upper}
        for ticket in eligible:
            target_values.add(min(ticket.payment_cap(), upper))
            target_values.add(min(ticket.denomination, upper))
        choices = [
            v for v in target_values
            if minimum <= v <= upper and (v <= balance or v == upper)
        ]
        candidates: list[list[Payment]] = []
        seen: set[tuple[str, ...]] = set()

        def visit(
            remaining: int,
            tickets: list[Ticket],
            payments: list[Payment],
            ticket_key: tuple[str, ...],
        ) -> None:
            if remaining <= 0:
                key = tuple(sorted(ticket_key))
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(list(payments))
                return
            for pos, ticket in enumerate(tickets):
                if ticket.ticket_id in ticket_key:
                    continue
                cap = min(ticket.payment_cap(), balance)
                values = {
                    min(cap, remaining),
                    min(ticket.denomination, remaining),
                    balance,
                    upper,
                }
                for value in sorted(v for v in values if 0 <= v <= upper):
                    applied = min(value, balance)
                    # A ticket always tenders its whole face value; value above
                    # the balance is refunded as change to the named payer.
                    tendered = ticket.denomination
                    change = tendered - applied
                    if change < 0:
                        continue
                    drawer = self.drawers.get(ticket.cabinet_id)
                    available_change = (
                        change_used[ticket.cabinet_id] + change
                    )
                    if drawer is None or available_change > drawer.change_coins:
                        continue
                    fee_applied = min(applied, debt.fee)
                    principal_applied = applied - fee_applied
                    payments.append(Payment(
                        ticket_id=ticket.ticket_id,
                        debt_id=debt_id,
                        tendered=tendered,
                        applied=applied,
                        fee_applied=fee_applied,
                        principal_applied=principal_applied,
                        change=change,
                    ))
                    visit(remaining - applied, tickets[pos + 1:], payments,
                          ticket_key + (ticket.ticket_id,))
                    payments.pop()

        for value in sorted(choices):
            visit(value, eligible, [], ())
        return candidates

    def _apply_payments(self, payments: list[Payment]) -> None:
        # Apply in the same fee-first order the ledger records use.  The search
        # has already ensured all effective terms can become satisfied.
        for payment in sorted(payments, key=lambda p: (p.debt_id, p.ticket_id)):
            debt = self.ledger.debts[payment.debt_id]
            value = min(payment.applied, debt.fee + debt.principal)
            fee_used = min(value, debt.fee)
            principal_used = value - fee_used
            debt.fee -= fee_used
            debt.principal -= principal_used
            debt.paid_fee += fee_used
            debt.paid_principal += principal_used
            if debt.fee == 0 and debt.principal == 0:
                debt.cleared = True
