from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from citation_system.catalog import Catalog
from citation_system.counter import (
    ALL_OR_NONE,
    SINGLE_ITEMS_ALLOWED,
    CashDrawer,
    CounterError,
    CounterOffice,
    INSPECT_AFTER_FIRST_TICKET,
    INSPECT_BEFORE_TICKETS,
    PhysicalItem,
    Ticket,
)
from citation_system.pawn import (
    PAWN_FEE,
    PAWN_PAYMENT,
    PAWN_PLEDGE,
    PAWN_DEBT,
    PAWN_TERM,
    PawnLedger,
    PawnValidationError,
    TERM_FULL,
    TERM_PRINCIPAL_CAP,
    load_export_evidence,
)
from citation_system.repository import CitationRepository
from citation_system import export


def page(page_id: str, *, active: bool = True):
    return {
        "page_id": page_id,
        "section_id": "ledger",
        "label": page_id,
        "active": active,
    }


def pinned(repo, citation_id, page_ids):
    repo.create_pinned(citation_id, citation_id, "V", page_ids)


class PawnExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog.create(str(self.tmp / "pawn.db"))
        self.cat.load_pages_document({
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [{"volume_id": "V", "pages": [
                page("debt"), page("pledge"), page("cap"), page("full"),
                page("renewal"),
            ]}],
        })
        self.repo = CitationRepository(self.cat)
        pinned(self.repo, "S_DEBT", ["debt"])
        pinned(self.repo, "S_PLEDGE", ["pledge"])
        pinned(self.repo, "S_CAP", ["cap"])
        self.repo.create_range("R_CAP", "cap term", "V", "cap", "cap")
        pinned(self.repo, "S_FULL", ["full"])
        self.repo.create_range("R_FULL", "full term", "V", "full", "full")
        pinned(self.repo, "S_RENEWAL", ["renewal"])
        self.entries = [
            {
                "operation_id": "OP1", "volume_id": "V", "page_id": "debt",
                "kind": PAWN_DEBT, "debt_id": "D1", "amount": 5,
                "owner": "owner", "signer": "owner",
                "signature_citation_id": "S_DEBT",
            },
            {
                "operation_id": "OP2", "volume_id": "V", "page_id": "pledge",
                "kind": PAWN_PLEDGE, "debt_id": "D1",
                "item_ids": ["watch", "seal"], "signer": "owner",
                "signature_citation_id": "S_PLEDGE",
                "responsible_person": "keeper",
            },
            {
                "operation_id": "OP3", "volume_id": "V", "page_id": "cap",
                "kind": PAWN_TERM, "debt_id": "D1", "term_id": "T_WATCH",
                "item_id": "watch", "term_kind": TERM_PRINCIPAL_CAP,
                "threshold": 2, "signer": "owner",
                "signature_citation_id": "S_CAP",
                "term_citation_id": "R_CAP",
                "responsible_person": "watch-keeper",
            },
            {
                "operation_id": "OP4", "volume_id": "V", "page_id": "full",
                "kind": PAWN_TERM, "debt_id": "D1", "term_id": "T_SEAL",
                "item_id": "seal", "term_kind": TERM_FULL,
                "signer": "owner", "signature_citation_id": "S_FULL",
                "term_citation_id": "R_FULL",
                "responsible_person": "seal-keeper",
            },
        ]

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def ledger(self, entries=None):
        return PawnLedger.replay(self.cat, entries or self.entries)

    def test_watch_can_leave_when_its_own_principal_cap_is_satisfied(self):
        self.entries.append({
            "operation_id": "OP5", "volume_id": "V", "page_id": "renewal",
            "kind": PAWN_FEE, "debt_id": "D1", "amount": 2,
            "signer": "owner", "signature_citation_id": "S_RENEWAL",
        })
        ledger = self.ledger()
        self.assertEqual(ledger.debt_summary("D1")["fee"], 2)
        self.assertFalse(ledger.can_release("watch"))

        # A later payment is replayed after the fee and pays fees first.
        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "at_end": True,
            "page": page("pay2"),
        })
        pinned(self.repo, "S_PAY2", ["pay2"])
        self.entries.append({
            "operation_id": "OP6", "volume_id": "V", "page_id": "pay2",
            "kind": PAWN_PAYMENT, "debt_id": "D1", "amount": 2,
            "payer": "owner", "signer": "owner",
            "signature_citation_id": "S_PAY2",
        })
        ledger = self.ledger()
        summary = ledger.debt_summary("D1")
        self.assertEqual(summary["fee"], 0)
        self.assertEqual(summary["principal"], 5)
        self.assertFalse(ledger.can_release("seal"))

        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "at_end": True,
            "page": page("pay3"),
        })
        pinned(self.repo, "S_PAY3", ["pay3"])
        self.entries.append({
            "operation_id": "OP7", "volume_id": "V", "page_id": "pay3",
            "kind": PAWN_PAYMENT, "debt_id": "D1", "amount": 2,
            "payer": "owner", "signer": "owner",
            "signature_citation_id": "S_PAY3",
        })
        ledger = self.ledger()
        self.assertEqual(ledger.debt_summary("D1")["principal"], 3)
        self.assertFalse(ledger.can_release("watch"))

        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "at_end": True,
            "page": page("pay4"),
        })
        pinned(self.repo, "S_PAY4", ["pay4"])
        self.entries.append({
            "operation_id": "OP8", "volume_id": "V", "page_id": "pay4",
            "kind": PAWN_PAYMENT, "debt_id": "D1", "amount": 1,
            "payer": "owner", "signer": "owner",
            "signature_citation_id": "S_PAY4",
        })
        ledger = self.ledger()
        self.assertEqual(ledger.debt_summary("D1")["principal"], 2)
        self.assertTrue(ledger.can_release("watch"))
        self.assertFalse(ledger.can_release("seal"))
        watch = ledger.item_status("watch")
        self.assertEqual(watch["effective_terms"][0]["responsible_person"], "watch-keeper")

    def test_fee_inserted_before_renewal_changes_replay_order_and_blocks_watch(self):
        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "before_page_id": "renewal",
            "page": page("late-fee"),
        })
        pinned(self.repo, "S_LATE_FEE", ["late-fee"])
        entries = self.entries + [
            {
                "operation_id": "OPF", "volume_id": "V", "page_id": "late-fee",
                "kind": PAWN_FEE, "debt_id": "D1", "amount": 2,
                "signer": "owner", "signature_citation_id": "S_LATE_FEE",
            },
            {
                "operation_id": "OP5", "volume_id": "V", "page_id": "renewal",
                "kind": PAWN_PAYMENT, "debt_id": "D1", "amount": 3,
                "payer": "owner", "signer": "owner",
                "signature_citation_id": "S_RENEWAL",
            },
            {
                "operation_id": "OP6", "volume_id": "V", "page_id": "full",
                "kind": PAWN_PAYMENT, "debt_id": "D1", "amount": 2,
                "payer": "owner", "signer": "owner",
                "signature_citation_id": "S_FULL",
            },
        ]
        ledger = PawnLedger.replay(self.cat, entries)
        invalid = {e.operation_id: e.reason for e in ledger.invalid_entries}
        self.assertEqual(invalid["OP6"], "duplicate_signature_citation")
        summary = ledger.debt_summary("D1")
        self.assertEqual(summary["fee"], 0)
        self.assertEqual(summary["principal"], 4)
        self.assertFalse(ledger.can_release("watch"))

    def test_unique_operation_and_integer_amount_are_input_contracts(self):
        with self.assertRaises(PawnValidationError) as ctx:
            PawnLedger.replay(self.cat, [
                dict(self.entries[0], operation_id="DUP"),
                dict(self.entries[0], operation_id="DUP"),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_operation_id")
        entries = self.entries + [dict(
            self.entries[0],
            operation_id="OP_FRACTION",
            page_id="renewal",
            kind=PAWN_FEE,
            amount=1.5,
            signature_citation_id="S_RENEWAL",
        )]
        ledger = PawnLedger.replay(self.cat, entries)
        self.assertEqual(ledger.invalid_entries[0].reason, "amount_must_be_integer")

    def test_later_page_cannot_repair_unauthorized_or_cleared_debt_operation(self):
        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "at_end": True,
            "page": page("bad"),
        })
        pinned(self.repo, "S_BAD", ["bad"])
        entries = self.entries + [{
            "operation_id": "OP_BAD", "volume_id": "V", "page_id": "bad",
            "kind": PAWN_FEE, "debt_id": "D1", "amount": 1,
            "signer": "intruder", "signature_citation_id": "S_BAD",
        }]
        ledger = PawnLedger.replay(self.cat, entries)
        self.assertEqual(ledger.invalid_entries[0].reason, "unauthorized_signer")
        self.assertEqual(ledger.debt_summary("D1")["fee"], 0)

    def test_term_cannot_use_uncreated_or_cleared_debt(self):
        self.cat.apply_batch({
            "operation": "insert", "volume_id": "V", "at_end": True,
            "page": page("future-term"),
        })
        pinned(self.repo, "S_FUTURE", ["future-term"])
        self.repo.create_range("R_FUTURE", "r", "V", "future-term", "future-term")
        entries = self.entries + [{
            "operation_id": "OPT", "volume_id": "V", "page_id": "future-term",
            "kind": PAWN_TERM, "debt_id": "NOPE", "term_id": "X",
            "item_id": "watch", "term_kind": TERM_FULL, "signer": "owner",
            "signature_citation_id": "S_FUTURE", "term_citation_id": "R_FUTURE",
        }]
        ledger = PawnLedger.replay(self.cat, entries)
        self.assertEqual(ledger.invalid_entries[0].reason, "debt_not_created")

    def test_real_export_detects_range_endpoint_failure_and_duplicate_release_signature(self):
        usable = self.tmp / "usable.jsonl"
        attention = self.tmp / "attention.jsonl"
        export.write_products(self.cat.conn, usable, attention)
        evidence = load_export_evidence(usable, attention)
        self.assertTrue(evidence["R_CAP"]["status"] == "usable")

        ledger = PawnLedger.replay(self.cat, evidence=evidence, raw_entries=self.entries)
        self.assertEqual(ledger.invalid_entries, [])

        self.cat.apply_batch({
            "operation": "deactivate", "volume_id": "V", "page_id": "cap",
        })
        export.write_products(self.cat.conn, usable, attention)
        evidence = load_export_evidence(usable, attention)
        ledger = PawnLedger.replay(self.cat, evidence=evidence, raw_entries=self.entries)
        reasons = {e.operation_id: e.reason for e in ledger.invalid_entries}
        self.assertEqual(reasons["OP3"], "term_citation_unusable")
        self.assertFalse(ledger.item_status("watch")["effective_terms"])

        # Re-activation makes the original fixed page identity usable again.
        self.cat.apply_batch({
            "operation": "activate", "volume_id": "V", "page_id": "cap",
        })
        ledger = PawnLedger.replay(self.cat, self.entries)
        self.assertEqual(len(ledger.item_status("watch")["effective_terms"]), 1)
        self.assertFalse(ledger.can_release("watch"))


class CounterWorkflowTests(unittest.TestCase):
    def _two_cabinet_ledger(self):
        tmp = Path(tempfile.mkdtemp())
        cat = Catalog.create(str(tmp / "counter.db"))
        cat.load_pages_document({
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [{"volume_id": "V", "pages": [
                page("d1"), page("d2"), page("pledge1"), page("pledge2"),
                page("t1"), page("t2"),
            ]}],
        })
        repo = CitationRepository(cat)
        pinned(repo, "S_D1", ["d1"])
        pinned(repo, "S_D2", ["d2"])
        pinned(repo, "S_P1", ["pledge1"])
        pinned(repo, "S_P2", ["pledge2"])
        pinned(repo, "S_T1", ["t1"])
        pinned(repo, "S_T2", ["t2"])
        repo.create_range("R1", "t1", "V", "t1", "t1")
        repo.create_range("R2", "t2", "V", "t2", "t2")
        entries = [
            {"operation_id": "D1", "volume_id": "V", "page_id": "d1",
             "kind": PAWN_DEBT, "debt_id": "D1", "amount": 2, "owner": "owner",
             "signer": "owner", "signature_citation_id": "S_D1", "cabinet_id": "C1"},
            {"operation_id": "D2", "volume_id": "V", "page_id": "d2",
             "kind": PAWN_DEBT, "debt_id": "D2", "amount": 2, "owner": "owner",
             "cosigners": ["ally"], "signer": "owner",
             "signature_citation_id": "S_D2", "cabinet_id": "C2"},
            {"operation_id": "P1", "volume_id": "V", "page_id": "pledge1",
             "kind": PAWN_PLEDGE, "debt_id": "D1", "item_ids": ["A"],
             "signer": "owner", "signature_citation_id": "S_P1", "cabinet_id": "C1"},
            {"operation_id": "P2", "volume_id": "V", "page_id": "pledge2",
             "kind": PAWN_PLEDGE, "debt_id": "D2", "item_ids": ["B"],
             "signer": "owner", "signature_citation_id": "S_P2", "cabinet_id": "C2"},
            {"operation_id": "T1", "volume_id": "V", "page_id": "t1",
             "kind": PAWN_TERM, "debt_id": "D1", "term_id": "TA", "item_id": "A",
             "term_kind": TERM_FULL, "signer": "owner",
             "signature_citation_id": "S_T1", "term_citation_id": "R1"},
            {"operation_id": "T2", "volume_id": "V", "page_id": "t2",
             "kind": PAWN_TERM, "debt_id": "D2", "term_id": "TB", "item_id": "B",
             "term_kind": TERM_FULL, "signer": "owner",
             "signature_citation_id": "S_T2", "term_citation_id": "R2"},
        ]
        ledger = PawnLedger.replay(cat, entries)
        return tmp, cat, ledger

    def test_shift_a_can_inspect_before_tickets_and_reject_quarantine_without_taking_money(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            tickets = [
                Ticket("K1", "owner", 2, "C1", ("D1",)),
                Ticket("K2", "owner", 2, "C2", ("D2",)),
            ]
            office = CounterOffice(
                ledger, tickets,
                [PhysicalItem("A", "C1", False), PhysicalItem("B", "C2", True)],
                [CashDrawer("C1"), CashDrawer("C2")],
            )
            plan = office.plan_redemption(
                ["A", "B"], owner="owner", delivery_policy=ALL_OR_NONE,
                inspection_policy=INSPECT_BEFORE_TICKETS, deadline=10,
            )
            reservation = next(iter(office.reservations))
            with self.assertRaises(CounterError) as ctx:
                office.release_reservation(reservation, now=1)
            self.assertEqual(ctx.exception.code, "item_quarantined")
            self.assertEqual(office.drawers["C1"].accepted_ticket_value, 0)
            self.assertEqual(office.drawers["C2"].accepted_ticket_value, 0)
            self.assertEqual(plan.accepted_ticket_ids, set())
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_shift_b_must_refuse_guarantee_when_second_cabinet_is_hidden(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K1", "owner", 2, "C1", ("D1",)),
                 Ticket("K2", "owner", 2, "C2", ("D2",))],
                [PhysicalItem("A", "C1", False), PhysicalItem("B", "C2", True)],
                [CashDrawer("C1"), CashDrawer("C2")],
            )
            with self.assertRaises(CounterError) as ctx:
                office.plan_redemption(
                    ["A", "B"], owner="owner", delivery_policy=ALL_OR_NONE,
                    inspection_policy=INSPECT_AFTER_FIRST_TICKET,
                    require_full_delivery_guarantee=True,
                )
            self.assertEqual(ctx.exception.code, "full_delivery_guarantee_refused")
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_ticket_cannot_be_reused_across_cabinets_or_rolled_back_after_signout(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K1", "owner", 2, "C1", ("D1",)),
                 Ticket("K2", "owner", 2, "C2", ("D2",))],
                [PhysicalItem("A", "C1", False), PhysicalItem("B", "C2", False)],
                [CashDrawer("C1"), CashDrawer("C2")],
            )
            plan = office.plan_redemption(
                ["A", "B"], owner="owner",
                inspection_policy=INSPECT_BEFORE_TICKETS, deadline=5,
            )
            reservation = next(iter(office.reservations))
            office.release_reservation(reservation, now=1)
            self.assertTrue(plan.all_items_released)
            self.assertEqual(office.drawers["C1"].accepted_ticket_value, 2)
            self.assertEqual(office.drawers["C2"].accepted_ticket_value, 2)
            with self.assertRaises(CounterError) as ctx:
                office.plan_redemption(
                    ["A"], owner="owner", deadline=5,
                )
            self.assertEqual(ctx.exception.code, "item_already_signed_out")
            self.assertEqual(sorted(office.reserved_ticket_ids), [])
            self.assertEqual(sorted(office.reserved_item_ids), [])
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_two_items_on_one_debt_clear_with_one_payment_not_two(self):
        tmp = Path(tempfile.mkdtemp())
        cat = Catalog.create(str(tmp / "shared.db"))
        cat.load_pages_document({
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [{"volume_id": "V", "pages": [
                page("d"), page("p"), page("t"),
            ]}],
        })
        repo = CitationRepository(cat)
        pinned(repo, "S_D", ["d"])
        pinned(repo, "S_P", ["p"])
        pinned(repo, "S_T", ["t"])
        repo.create_range("R_T", "t", "V", "t", "t")
        ledger = PawnLedger.replay(cat, [
            {"operation_id": "D", "volume_id": "V", "page_id": "d",
             "kind": PAWN_DEBT, "debt_id": "D", "amount": 2, "owner": "owner",
             "signer": "owner", "signature_citation_id": "S_D", "cabinet_id": "C"},
            {"operation_id": "P", "volume_id": "V", "page_id": "p",
             "kind": PAWN_PLEDGE, "debt_id": "D", "item_ids": ["A", "B"],
             "signer": "owner", "signature_citation_id": "S_P", "cabinet_id": "C"},
            {"operation_id": "T", "volume_id": "V", "page_id": "t",
             "kind": PAWN_TERM, "debt_id": "D", "term_id": "T", "item_id": "A",
             "term_kind": TERM_FULL, "signer": "owner",
             "signature_citation_id": "S_T", "term_citation_id": "R_T"},
        ])
        ledger.items["B"].terms.append(ledger.items["A"].terms[0])
        office = CounterOffice(
            ledger,
            [Ticket("K", "owner", 2, "C", ("D",))],
            [PhysicalItem("A", "C", False), PhysicalItem("B", "C", False)],
            [CashDrawer("C")],
        )
        try:
            plan = office.plan_redemption(["A", "B"], owner="owner", deadline=2)
            self.assertEqual(len(plan.payments), 1)
            self.assertEqual(plan.payments[0].debt_id, "D")
            reservation = next(iter(office.reservations))
            office.release_reservation(reservation, now=1)
            self.assertTrue(plan.all_items_released)
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_cosigner_authorization_and_change_are_enforced(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K3", "ally", 3, "C2", ("D2",), max_payment=2)],
                [PhysicalItem("A", "C1", False), PhysicalItem("B", "C2", False)],
                [CashDrawer("C1"), CashDrawer("C2", change_coins=0)],
            )
            with self.assertRaises(CounterError) as ctx:
                office.plan_redemption(["B"], owner="ally", deadline=3)
            self.assertEqual(ctx.exception.code, "insufficient_change")
            office.drawers["C2"].change_coins = 1
            plan = office.plan_redemption(["B"], owner="ally", deadline=3)
            reservation = next(iter(office.reservations))
            office.release_reservation(reservation, now=1)
            self.assertEqual(office.drawers["C2"].change_coins, 0)
            self.assertEqual(office.drawers["C2"].accepted_ticket_value, 3)
            self.assertTrue(plan.all_items_released)
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_whole_ticket_authorization_cap_and_denomination_are_both_enforced(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K3", "owner", 3, "C1", ("D1",), max_payment=2)],
                [PhysicalItem("A", "C1", False)],
                [CashDrawer("C1", change_coins=0)],
            )
            with self.assertRaises(CounterError) as ctx:
                office.plan_redemption(["A"], owner="owner", deadline=3)
            self.assertEqual(ctx.exception.code, "insufficient_change")
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_overpayment_is_refunded_from_change_and_does_not_create_negative_debt(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K3", "owner", 3, "C1", ("D1",))],
                [PhysicalItem("A", "C1", False)],
                [CashDrawer("C1", change_coins=1)],
            )
            plan = office.plan_redemption(["A"], owner="owner", deadline=3)
            self.assertEqual(plan.payments[0].applied, 2)
            self.assertEqual(plan.payments[0].change, 1)
            reservation = next(iter(office.reservations))
            office.release_reservation(reservation, now=1)
            self.assertEqual(ledger.debt_summary("D1")["principal"], 0)
            self.assertEqual(office.drawers["C1"].change_coins, 0)
        finally:
            cat.close()
            shutil.rmtree(tmp)

    def test_change_of_mind_respects_agreed_single_item_delivery(self):
        tmp, cat, ledger = self._two_cabinet_ledger()
        try:
            office = CounterOffice(
                ledger,
                [Ticket("K1", "owner", 2, "C1", ("D1",)),
                 Ticket("K2", "owner", 2, "C2", ("D2",))],
                [PhysicalItem("A", "C1", False), PhysicalItem("B", "C2", False)],
                [CashDrawer("C1"), CashDrawer("C2")],
            )
            plan = office.plan_redemption(
                ["A", "B"], owner="owner",
                delivery_policy=SINGLE_ITEMS_ALLOWED,
                inspection_policy=INSPECT_BEFORE_TICKETS, deadline=4,
            )
            reservation = next(iter(office.reservations))
            office.release_reservation(reservation, now=1)
            self.assertTrue(plan.all_items_released)
            with self.assertRaises(CounterError) as ctx:
                office.customer_changes_mind(plan, ["A"])
            self.assertEqual(ctx.exception.code, "cannot_recover_signed_out_item")
        finally:
            cat.close()
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
