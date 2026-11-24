"""Integer-token pawn ledger cleanup and release-threshold evaluation.

The ledger is replayed from physical pages in the existing catalog.  Every
valid ledger page is one uniquely numbered operation.  Fixed signatures use
PINNED citations and therefore remain bound to the original evidence pages;
RANGE citations may supply a release term only while their current exported
interval still contains the term page.

Invalid pages are retained as audit findings.  They are never silently
discarded, and a later valid page cannot make an earlier invalid operation
legal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Any, Iterable

from .catalog import Catalog
from . import export

PAWN_DEBT = "debt"
PAWN_FEE = "fee"
PAWN_PAYMENT = "payment"
PAWN_PLEDGE = "pledge"
PAWN_TERM = "release_term"

TERM_FULL = "full_debt"
TERM_PRINCIPAL_CAP = "principal_cap"

ENTRY_KINDS = frozenset(
    (PAWN_DEBT, PAWN_FEE, PAWN_PAYMENT, PAWN_PLEDGE, PAWN_TERM)
)


class PawnError(Exception):
    """Base class for expected pawn-ledger failures."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PawnValidationError(PawnError):
    """The input document itself is malformed rather than merely invalid."""


@dataclass(frozen=True)
class PawnEntry:
    operation_id: str
    volume_id: str
    page_id: str
    seq: int
    kind: str
    valid: bool
    reason: str | None = None
    debt_id: str | None = None
    amount: int = 0
    refund: int = 0
    payer: str | None = None
    signer: str | None = None
    signature_citation_id: str | None = None
    term_id: str | None = None
    term_kind: str | None = None
    threshold: int | None = None
    debt_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    responsible_person: str | None = None
    cabinet_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DebtState:
    debt_id: str
    owner: str
    cosigners: tuple[str, ...]
    volume_id: str
    created_seq: int
    cabinet_id: str | None = None
    principal: int = 0
    fee: int = 0
    initial_principal: int = 0
    total_fees: int = 0
    paid_principal: int = 0
    paid_fee: int = 0
    cleared: bool = False
    cleared_seq: int | None = None

    def authorizes(self, person: str) -> bool:
        return person == self.owner or person in self.cosigners


@dataclass(frozen=True)
class Pledge:
    item_id: str
    debt_id: str
    volume_id: str
    page_id: str
    seq: int
    responsible_person: str
    cabinet_id: str | None


@dataclass(frozen=True)
class ReleaseTerm:
    term_id: str
    item_id: str
    debt_id: str
    debt_ids: tuple[str, ...]
    kind: str
    threshold: int | None
    responsible_person: str
    volume_id: str
    page_id: str
    seq: int
    citation_id: str


@dataclass
class ItemState:
    item_id: str
    pledges: list[Pledge] = field(default_factory=list)
    terms: list[ReleaseTerm] = field(default_factory=list)
    released: bool = False
    release_signature_id: str | None = None


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_export_evidence(
    usable_path: str | Path, attention_path: str | Path
) -> dict[str, dict[str, Any]]:
    """Load a real citation-product/1 export without opening SQLite."""

    evidence: dict[str, dict[str, Any]] = {}
    for path, status in ((usable_path, "usable"), (attention_path, "needs_attention")):
        for row in _read_jsonl(path):
            if row.get("format") != export.FORMAT:
                raise PawnValidationError(
                    "invalid_export_format",
                    f"{path} is not {export.FORMAT}",
                    {"path": str(path), "format": row.get("format")},
                )
            row = dict(row)
            row["product_status"] = status
            cid = row.get("citation_id")
            if not isinstance(cid, str) or not cid:
                raise PawnValidationError("invalid_export", "citation_id is required")
            if cid in evidence:
                raise PawnValidationError(
                    "duplicate_export_citation",
                    f"citation {cid} appears in more than one product row",
                    {"citation_id": cid},
                )
            evidence[cid] = row
    return evidence


def build_export_evidence(catalog: Catalog) -> dict[str, dict[str, Any]]:
    """Take evidence from one consistent catalog export snapshot."""

    _, usable, attention = export.collect_products(catalog.conn)
    evidence: dict[str, dict[str, Any]] = {}
    for row in usable:
        evidence[row["citation_id"]] = {**row, "product_status": "usable"}
    for row in attention:
        evidence[row["citation_id"]] = {**row, "product_status": "needs_attention"}
    return evidence


class PawnLedger:
    """Replayed, immutable-in-spirit ledger state for one cleanup run."""

    def __init__(self, catalog: Catalog, evidence: dict[str, dict[str, Any]]):
        self.catalog = catalog
        self.evidence = evidence
        self.entries: list[PawnEntry] = []
        self.invalid_entries: list[PawnEntry] = []
        self.debts: dict[str, DebtState] = {}
        self.items: dict[str, ItemState] = {}
        self._used_signature_citations: set[str] = set()

    # ------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------
    @classmethod
    def replay(
        cls,
        catalog: Catalog,
        raw_entries: Iterable[dict[str, Any]],
        *,
        evidence: dict[str, dict[str, Any]] | None = None,
    ) -> "PawnLedger":
        ledger = cls(catalog, evidence or build_export_evidence(catalog))
        ledger._replay(raw_entries)
        return ledger

    def _replay(self, raw_entries: Iterable[dict[str, Any]]) -> None:
        ordered: list[tuple[str, int, int, dict[str, Any]]] = []
        seen_operation_ids: set[str] = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise PawnValidationError("invalid_entry", "each ledger entry must be an object")
            oid = raw.get("operation_id")
            if not isinstance(oid, str) or not oid:
                raise PawnValidationError(
                    "invalid_entry",
                    "operation_id must be a non-empty string",
                )
            if oid in seen_operation_ids:
                raise PawnValidationError(
                    "duplicate_operation_id",
                    f"operation {oid} is not uniquely numbered",
                    {"operation_id": oid},
                )
            seen_operation_ids.add(oid)
            volume_id = raw.get("volume_id")
            page_id = raw.get("page_id")
            if not isinstance(volume_id, str) or not isinstance(page_id, str):
                raise PawnValidationError(
                    "invalid_entry",
                    "volume_id and page_id are required strings",
                )
            page = self.catalog.get_page(volume_id, page_id, missing_ok=True)
            if page is None:
                raise PawnValidationError(
                    "page_not_found",
                    f"page {volume_id}/{page_id} does not exist",
                    {"volume_id": volume_id, "page_id": page_id},
                )
            ordered.append((volume_id, page.seq, len(ordered), raw))

        for volume_id, _seq, _tie, raw in sorted(ordered, key=lambda x: (x[0], x[1], x[2])):
            entry = self._process_page(raw)
            self.entries.append(entry)
            if not entry.valid:
                self.invalid_entries.append(entry)

    def _process_page(self, raw: dict[str, Any]) -> PawnEntry:
        oid = raw["operation_id"]
        volume_id = raw["volume_id"]
        page_id = raw["page_id"]
        page = self.catalog.get_page(volume_id, page_id)
        kind = raw.get("kind") or raw.get("operation")
        common = {
            "operation_id": oid,
            "volume_id": volume_id,
            "page_id": page_id,
            "seq": page.seq,
            "kind": kind,
            "raw": dict(raw),
        }
        if kind not in ENTRY_KINDS:
            return self._invalid(common, "unsupported_entry_kind")
        if not page.active and kind != PAWN_TERM:
            return self._invalid(common, "inactive_page")

        amount = self._integer(raw.get("amount", 0), common)
        if amount is None:
            return common["entry"]
        debt_id = raw.get("debt_id")
        if not isinstance(debt_id, str) or not debt_id:
            return self._invalid(common, "debt_id_required")
        signer = raw.get("signer")
        citation_id = raw.get("signature_citation_id")
        if not isinstance(signer, str) or not signer:
            return self._invalid(common, "signer_required")
        if not isinstance(citation_id, str) or not citation_id:
            return self._invalid(common, "signature_citation_required")

        signature_reason = None
        if page.active or kind != PAWN_TERM:
            signature_reason = self._check_fixed_signature(
                citation_id, volume_id, page_id, signer, debt_id, raw
            )
        if signature_reason:
            return self._invalid(common, signature_reason)
        if citation_id in self._used_signature_citations:
            return self._invalid(common, "duplicate_signature_citation")
        if kind == PAWN_DEBT:
            entry = self._process_debt(common, raw, amount, debt_id, signer, citation_id)
        elif kind == PAWN_FEE:
            entry = self._process_fee(common, raw, amount, debt_id, signer, citation_id)
        elif kind == PAWN_PAYMENT:
            entry = self._process_payment(common, raw, amount, debt_id, signer, citation_id)
        elif kind == PAWN_PLEDGE:
            entry = self._process_pledge(common, raw, debt_id, signer, citation_id)
        else:
            entry = self._process_term(common, raw, debt_id, signer, citation_id)

        if entry.valid:
            self._used_signature_citations.add(citation_id)
        return entry

    def _process_debt(
        self, common: dict[str, Any], raw: dict[str, Any], amount: int,
        debt_id: str, signer: str, citation_id: str,
    ) -> PawnEntry:
        if amount <= 0:
            return self._invalid(common, "amount_must_be_positive")
        if debt_id in self.debts:
            return self._invalid(common, "duplicate_debt")
        owner = raw.get("owner", signer)
        if owner != signer:
            return self._invalid(common, "unauthorized_signer")
        cosigners = self._string_tuple(raw.get("cosigners", ()))
        if cosigners is None:
            return self._invalid(common, "invalid_cosigners")
        cabinet_id = raw.get("cabinet_id")
        if cabinet_id is not None and not isinstance(cabinet_id, str):
            return self._invalid(common, "invalid_cabinet_id")
        debt = DebtState(
            debt_id=debt_id,
            owner=owner,
            cosigners=tuple(c for c in cosigners if c != owner),
            volume_id=common["volume_id"],
            created_seq=common["seq"],
            cabinet_id=cabinet_id,
            principal=amount,
            initial_principal=amount,
        )
        self.debts[debt_id] = debt
        return self._valid({**common, "debt_id": debt_id, "amount": amount,
                            "payer": None, "signer": signer,
                            "signature_citation_id": citation_id,
                            "cabinet_id": cabinet_id})

    def _process_fee(
        self, common: dict[str, Any], raw: dict[str, Any], amount: int,
        debt_id: str, signer: str, citation_id: str,
    ) -> PawnEntry:
        debt = self.debts.get(debt_id)
        reason = self._require_live_debt(debt, common, signer)
        if reason:
            return self._invalid(common, reason)
        assert debt is not None
        if amount <= 0:
            return self._invalid(common, "amount_must_be_positive")
        debt.fee += amount
        debt.total_fees += amount
        return self._valid({**common, "debt_id": debt_id, "amount": amount,
                            "signer": signer, "signature_citation_id": citation_id})

    def _process_payment(
        self, common: dict[str, Any], raw: dict[str, Any], amount: int,
        debt_id: str, signer: str, citation_id: str,
    ) -> PawnEntry:
        debt = self.debts.get(debt_id)
        reason = self._require_live_debt(debt, common, signer)
        if reason:
            return self._invalid(common, reason)
        assert debt is not None
        if amount <= 0:
            return self._invalid(common, "amount_must_be_positive")
        payer = raw.get("payer", signer)
        if payer != signer:
            return self._invalid(common, "payment_signer_must_be_named_payer")
        fee_used = min(amount, debt.fee)
        principal_used = min(amount - fee_used, debt.principal)
        refund = amount - fee_used - principal_used
        debt.fee -= fee_used
        debt.principal -= principal_used
        debt.paid_fee += fee_used
        debt.paid_principal += principal_used
        if debt.principal == 0 and debt.fee == 0:
            debt.cleared = True
            debt.cleared_seq = common["seq"]
        return self._valid({**common, "debt_id": debt_id, "amount": amount,
                            "refund": refund, "payer": payer, "signer": signer,
                            "signature_citation_id": citation_id})

    def _process_pledge(
        self, common: dict[str, Any], raw: dict[str, Any],
        debt_id: str, signer: str, citation_id: str,
    ) -> PawnEntry:
        debt = self.debts.get(debt_id)
        reason = self._require_live_debt(debt, common, signer)
        if reason:
            return self._invalid(common, reason)
        assert debt is not None
        item_ids = self._string_tuple(raw.get("item_ids", ()))
        if not item_ids:
            return self._invalid(common, "pledge_requires_items")
        cabinet_id = raw.get("cabinet_id", debt.cabinet_id)
        if cabinet_id is not None and not isinstance(cabinet_id, str):
            return self._invalid(common, "invalid_cabinet_id")
        responsible = raw.get("responsible_person", debt.owner)
        if not isinstance(responsible, str) or not responsible:
            return self._invalid(common, "invalid_responsible_person")
        for item_id in item_ids:
            pledge = Pledge(
                item_id=item_id,
                debt_id=debt_id,
                volume_id=common["volume_id"],
                page_id=common["page_id"],
                seq=common["seq"],
                responsible_person=responsible,
                cabinet_id=cabinet_id,
            )
            self.items.setdefault(item_id, ItemState(item_id)).pledges.append(pledge)
        return self._valid({**common, "debt_id": debt_id, "item_ids": tuple(item_ids),
                            "signer": signer, "signature_citation_id": citation_id,
                            "responsible_person": responsible, "cabinet_id": cabinet_id})

    def _process_term(
        self, common: dict[str, Any], raw: dict[str, Any],
        debt_id: str, signer: str, citation_id: str,
    ) -> PawnEntry:
        debt = self.debts.get(debt_id)
        reason = self._require_live_debt(debt, common, signer)
        if reason:
            return self._invalid(common, reason)
        assert debt is not None
        term_id = raw.get("term_id")
        item_id = raw.get("item_id")
        term_kind = raw.get("term_kind", TERM_FULL)
        if not isinstance(term_id, str) or not term_id:
            return self._invalid(common, "term_id_required")
        if not isinstance(item_id, str) or not item_id:
            return self._invalid(common, "item_id_required")
        if term_kind not in (TERM_FULL, TERM_PRINCIPAL_CAP):
            return self._invalid(common, "unsupported_term_kind")
        debt_ids = self._string_tuple(raw.get("debt_ids", (debt_id,)))
        if not debt_ids or debt_id not in debt_ids:
            return self._invalid(common, "term_must_include_owning_debt")
        if len(set(debt_ids)) != len(debt_ids):
            return self._invalid(common, "duplicate_term_debt")
        threshold = None
        if term_kind == TERM_PRINCIPAL_CAP:
            threshold = self._integer(raw.get("threshold"), common)
            if threshold is None or threshold < 0:
                return self._invalid(common, "threshold_must_be_non_negative_integer")
        term_citation_id = raw.get("term_citation_id", citation_id)
        if not isinstance(term_citation_id, str) or not term_citation_id:
            return self._invalid(common, "term_citation_required")
        citation_reason = self._check_term_citation(
            term_citation_id, common["volume_id"], common["page_id"], debt_ids
        )
        if citation_reason:
            return self._invalid(common, citation_reason)
        for related_id in debt_ids:
            related = self.debts.get(related_id)
            if related is None:
                return self._invalid(common, "term_references_uncreated_debt")
            if related.cleared:
                return self._invalid(common, "term_references_cleared_debt")
            if related.volume_id != common["volume_id"]:
                return self._invalid(common, "cross_volume_term")
            if not related.authorizes(signer):
                return self._invalid(common, "unauthorized_signer")
        item = self.items.setdefault(item_id, ItemState(item_id))
        pledged_debts = {p.debt_id for p in item.pledges}
        if not set(debt_ids).issubset(pledged_debts):
            return self._invalid(common, "term_debt_not_pledged_by_item")
        if any(t.term_id == term_id for t in item.terms):
            return self._invalid(common, "duplicate_term_id")
        responsible = raw.get("responsible_person", debt.owner)
        if not isinstance(responsible, str) or not responsible:
            return self._invalid(common, "invalid_responsible_person")
        term = ReleaseTerm(
            term_id=term_id,
            item_id=item_id,
            debt_id=debt_id,
            debt_ids=tuple(debt_ids),
            kind=term_kind,
            threshold=threshold,
            responsible_person=responsible,
            volume_id=common["volume_id"],
            page_id=common["page_id"],
            seq=common["seq"],
            citation_id=term_citation_id,
        )
        item.terms.append(term)
        return self._valid({**common, "debt_id": debt_id, "term_id": term_id,
                            "term_kind": term_kind, "threshold": threshold,
                            "debt_ids": tuple(debt_ids), "item_ids": (item_id,),
                            "signer": signer,
                            "signature_citation_id": citation_id,
                            "responsible_person": responsible})

    # ------------------------------------------------------------------
    # validation helpers
    # ------------------------------------------------------------------
    def _check_fixed_signature(
        self, citation_id: str, volume_id: str, page_id: str,
        signer: str, debt_id: str, raw: dict[str, Any],
    ) -> str | None:
        debt = self.debts.get(debt_id)
        if debt is not None and not debt.authorizes(signer):
            return "unauthorized_signer"
        product = self.evidence.get(citation_id)
        if product is None:
            return "signature_citation_not_exported"
        if product.get("kind") != "PINNED":
            return "signature_must_be_pinned"
        if product.get("product_status") != "usable" or product.get("reason") is not None:
            return "signature_evidence_inactive"
        key = (volume_id, page_id)
        members = {(p.get("volume_id"), p.get("page_id")) for p in product.get("pages", [])}
        if key not in members:
            return "signature_not_on_original_page"
        return None

    def _check_term_citation(
        self, citation_id: str, volume_id: str, page_id: str, debt_ids: tuple[str, ...]
    ) -> str | None:
        product = self.evidence.get(citation_id)
        if product is None:
            return "term_citation_not_exported"
        if product.get("product_status") != "usable" or product.get("reason") is not None:
            return "term_citation_unusable"
        if product.get("kind") == "PINNED":
            members = {(p.get("volume_id"), p.get("page_id")) for p in product.get("pages", [])}
            if (volume_id, page_id) not in members:
                return "term_signature_not_on_original_page"
            return None
        if product.get("kind") != "RANGE":
            return "unsupported_term_citation"
        pages = {(p.get("volume_id"), p.get("page_id")) for p in product.get("pages", [])}
        if (volume_id, page_id) not in pages:
            return "term_page_outside_current_range"
        # RANGE is current-order evidence, but every debt named by the term must
        # remain in the same physical volume as the term page.
        for debt_id in debt_ids:
            debt = self.debts.get(debt_id)
            if debt is not None and debt.volume_id != volume_id:
                return "cross_volume_term"
        return None

    def _require_live_debt(
        self, debt: DebtState | None, common: dict[str, Any], signer: str
    ) -> str | None:
        if debt is None:
            return "debt_not_created"
        if debt.volume_id != common["volume_id"]:
            return "cross_volume_debt_operation"
        if debt.cleared:
            return "debt_already_cleared"
        if not debt.authorizes(signer):
            return "unauthorized_signer"
        return None

    @staticmethod
    def _integer(value: Any, common: dict[str, Any]) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            PawnLedger._invalid(common, "amount_must_be_integer")
            return None
        if value < 0:
            PawnLedger._invalid(common, "amount_must_be_non_negative")
            return None
        return value

    @staticmethod
    def _string_tuple(value: Any) -> tuple[str, ...] | None:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            return None
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                return None
            out.append(item)
        return tuple(out)

    @staticmethod
    def _invalid(common: dict[str, Any], reason: str) -> PawnEntry:
        entry = PawnEntry(
            operation_id=common["operation_id"],
            volume_id=common["volume_id"],
            page_id=common["page_id"],
            seq=common["seq"],
            kind=common["kind"],
            valid=False,
            reason=reason,
            debt_id=common.get("raw", {}).get("debt_id"),
            raw=common.get("raw", {}),
        )
        common["entry"] = entry
        return entry

    @staticmethod
    def _valid(values: dict[str, Any]) -> PawnEntry:
        values = dict(values)
        values.pop("entry", None)
        return PawnEntry(valid=True, reason=None, **values)

    # ------------------------------------------------------------------
    # current state and release evaluation
    # ------------------------------------------------------------------
    def debt_summary(self, debt_id: str) -> dict[str, Any]:
        debt = self.debts[debt_id]
        return {
            "debt_id": debt.debt_id,
            "owner": debt.owner,
            "cosigners": list(debt.cosigners),
            "principal": debt.principal,
            "fee": debt.fee,
            "initial_principal": debt.initial_principal,
            "paid_principal": debt.paid_principal,
            "paid_fee": debt.paid_fee,
            "cleared": debt.cleared,
            "cabinet_id": debt.cabinet_id,
        }

    def term_status(self, term: ReleaseTerm) -> dict[str, Any]:
        debts = [self.debts[d] for d in term.debt_ids]
        total_fee = sum(d.fee for d in debts)
        total_principal = sum(d.principal for d in debts)
        if term.kind == TERM_FULL:
            satisfied = total_fee == 0 and total_principal == 0
        else:
            satisfied = total_fee == 0 and total_principal <= int(term.threshold)
        return {
            "term_id": term.term_id,
            "kind": term.kind,
            "debt_ids": list(term.debt_ids),
            "threshold": term.threshold,
            "responsible_person": term.responsible_person,
            "source": {"volume_id": term.volume_id, "page_id": term.page_id},
            "citation_id": term.citation_id,
            "satisfied": satisfied,
            "total_fee": total_fee,
            "total_principal": total_principal,
        }

    def item_status(self, item_id: str) -> dict[str, Any]:
        item = self.items[item_id]
        return {
            "item_id": item_id,
            "released": item.released,
            "pledges": [
                {
                    "debt_id": p.debt_id,
                    "responsible_person": p.responsible_person,
                    "cabinet_id": p.cabinet_id,
                    "source": {"volume_id": p.volume_id, "page_id": p.page_id},
                }
                for p in item.pledges
            ],
            "effective_terms": [self.term_status(t) for t in item.terms],
        }

    def all_item_statuses(self) -> list[dict[str, Any]]:
        return [self.item_status(item_id) for item_id in sorted(self.items)]

    def can_release(self, item_id: str) -> bool:
        status = self.item_status(item_id)
        return any(t["satisfied"] for t in status["effective_terms"])
