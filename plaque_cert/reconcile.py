"""销售核单与合格包册。

规则落点（对应题面）：
- 外部收银凭据须列：具名包号、货号、客单、成交行；
- 内部调货镜像（P->S 一箱交接）不得报零售，每包只能由收到该批的卖场
  凭真实对外成交计一次；
- 售后可整包退或只退指定盘，退量须符合该包原配；
- 旧底稿期末声明只记未对外售出数；补办退回实物另册验收；
- 逐批以“原售出数 - 实退数”核合格份数，对不上的包进异常报告；
- 合格包册逐码写明仍可为哪些唱片（发行名）作证，条目带哈希链，可复查。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

# 试办场次的硬性规模：限十二包五牌
MAX_PACKAGES = 12
MAX_PLAQUES = 5


@dataclass(frozen=True)
class Disc:
    """一张盘：归属某条录音的某个发行（再版是独立发行）。"""

    release_id: str
    recording_id: str
    release_name: str


@dataclass
class Package:
    """一个具名包（一包一认证码），skus 为原配货号（发行 id）有序列表。"""

    pkg_id: str
    batch_id: str
    store: str  # 收到该批、唯一可报零售的卖场
    skus: list[str]
    code: str


@dataclass
class ExternalReceipt:
    """外部收银凭据：具名包号、货号、客单、成交行齐备。"""

    receipt_id: str
    store: str
    pkg_id: str
    lines: list[str]  # 成交行（货号）
    customer_order: str


@dataclass(frozen=True)
class InternalTransfer:
    """内部调货镜像：凭证前缀成对，绝不构成对外零售。"""

    invoice_id: str
    seller_prefix: str
    buyer_prefix: str
    seller: str
    buyer: str
    batch_id: str
    pkg_ids: tuple[str, ...]


@dataclass(frozen=True)
class PeriodDraft:
    """旧底稿期末声明：只允许记未对外售出数。"""

    batch_id: str
    unsold_external: int


@dataclass(frozen=True)
class ReturnVoucher:
    """退货凭据；reprocessed=True 表示补办退回实物，进另册验收。"""

    voucher_id: str
    pkg_id: str
    returned_skus: tuple[str, ...]
    reprocessed: bool = False


@dataclass
class ReconcileIssue:
    pkg_id: Optional[str]
    code: Optional[str]
    reason: str


@dataclass
class BookletEntry:
    """合格包册的一条：一个认证码仍可给哪些唱片作证。"""

    seq: int
    code: str
    pkg_id: str
    batch_id: str
    store: str
    original_skus: list[str]
    returned_skus: list[str]
    retained_skus: list[str]
    sold_qty: int
    returned_qty: int
    eligible_releases: list[str]  # 仍可作证的发行 id
    status: str  # eligible / revoked
    note: str
    prev_hash: str = ""
    entry_hash: str = ""


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Reconciler:
    """汇总一个巡演场次的全部原始凭据，核出合格包册与异常清单。"""

    def __init__(self, tour_id: str):
        self.tour_id = tour_id
        self.packages: dict[str, Package] = {}
        self.receipts: dict[str, ExternalReceipt] = {}
        self.transfers: list[InternalTransfer] = []
        self.drafts: dict[str, PeriodDraft] = {}
        self.returns: list[ReturnVoucher] = []
        self.reprocessed_book: list[ReturnVoucher] = []  # 另册验收
        self.discs: dict[str, Disc] = {}
        self.issues: list[ReconcileIssue] = []

    # ---- 登记侧 -----------------------------------------------------------
    def register_disc(self, disc: Disc) -> None:
        self.discs[disc.release_id] = disc

    def register_package(self, pkg: Package) -> None:
        if len(self.packages) >= MAX_PACKAGES:
            raise ValueError(f"本场次限 {MAX_PACKAGES} 包，拒绝再登记 {pkg.pkg_id}")
        if pkg.pkg_id in self.packages:
            raise ValueError(f"包号重复：{pkg.pkg_id}")
        self.packages[pkg.pkg_id] = pkg

    def register_receipt(self, rc: ExternalReceipt) -> None:
        if rc.receipt_id in self.receipts:
            raise ValueError(f"收银凭据重复报账：{rc.receipt_id}")
        self.receipts[rc.receipt_id] = rc

    def register_transfer(self, tr: InternalTransfer) -> None:
        self.transfers.append(tr)

    def register_draft(self, draft: PeriodDraft) -> None:
        self.drafts[draft.batch_id] = draft

    def register_return(self, rv: ReturnVoucher) -> None:
        self.returns.append(rv)
        if rv.reprocessed:
            self.reprocessed_book.append(rv)  # 补办实物另册

    # ---- 核单侧 -----------------------------------------------------------
    def _internal_pkg_ids(self) -> set[str]:
        out: set[str] = set()
        for tr in self.transfers:
            out.update(tr.pkg_ids)
        return out

    def run(self) -> tuple[list[BookletEntry], list[ReconcileIssue]]:
        issues = self.issues
        internal = self._internal_pkg_ids()
        # 包号 -> 引用它的外部凭据（一包只可计一次）
        receipt_by_pkg: dict[str, ExternalReceipt] = {}
        for rc in self.receipts.values():
            if rc.pkg_id not in self.packages:
                issues.append(ReconcileIssue(rc.pkg_id, None, f"凭据 {rc.receipt_id} 引用未知包号"))
                continue
            if rc.pkg_id in receipt_by_pkg:
                issues.append(
                    ReconcileIssue(
                        rc.pkg_id,
                        self.packages[rc.pkg_id].code,
                        f"同一包被第二张凭据 {rc.receipt_id} 重复计零售",
                    )
                )
                continue
            receipt_by_pkg[rc.pkg_id] = rc

        # 逐包核退货
        returns_by_pkg: dict[str, list[ReturnVoucher]] = {}
        for rv in self.returns:
            returns_by_pkg.setdefault(rv.pkg_id, []).append(rv)

        entries: list[BookletEntry] = []
        # 逐批：原售出数（凡有真实对外成交，含其后整退者）- 实退数
        batch_sold: dict[str, int] = {}
        batch_returned: dict[str, int] = {}

        for pkg in self.packages.values():
            if pkg.pkg_id in receipt_by_pkg:
                batch_sold[pkg.batch_id] = batch_sold.get(pkg.batch_id, 0) + len(pkg.skus)

        for pkg in self.packages.values():
            original = list(pkg.skus)
            rv_list = returns_by_pkg.get(pkg.pkg_id, [])
            returned: list[str] = []
            note = ""
            for rv in rv_list:
                for sku in rv.returned_skus:
                    # 逐盘累计退量不得超过该包原配数量
                    if returned.count(sku) + 1 > original.count(sku):
                        issues.append(
                            ReconcileIssue(
                                pkg.pkg_id,
                                pkg.code,
                                f"退单 {rv.voucher_id} 超出原配数量：{sku}",
                            )
                        )
                        note = "退货超原配"
                    else:
                        returned.append(sku)

            rc = receipt_by_pkg.get(pkg.pkg_id)
            if rc is None:
                if pkg.pkg_id in internal:
                    reason = "内部调货镜像不得报零售：内售发票齐但实际外售缺席"
                else:
                    reason = "缺真实对外成交凭据，不能计零售"
                issues.append(ReconcileIssue(pkg.pkg_id, pkg.code, reason))
                status = "revoked"
                note = (note + "；" if note else "") + reason
            elif rc.store != pkg.store:
                reason = f"凭据卖场 {rc.store} 非收货卖场 {pkg.store}，无权报零售"
                issues.append(ReconcileIssue(pkg.pkg_id, pkg.code, reason))
                status = "revoked"
                note = (note + "；" if note else "") + reason
            else:
                # 成交行货号必须落在原配内
                bad = [s for s in rc.lines if s not in original]
                if bad:
                    issues.append(
                        ReconcileIssue(pkg.pkg_id, pkg.code, f"成交行含非原配货号：{bad}")
                    )
                    status = "revoked"
                    note = f"成交行货号不符 {bad}"
                else:
                    status = "eligible"

            retained = list(original)
            for sku in returned:
                retained.remove(sku)
            if not retained and status == "eligible":
                status = "revoked"
                note = (note + "；" if note else "") + "整包退尽，无盘可作证"

            eligible = sorted(set(retained))
            entries.append(
                BookletEntry(
                    seq=len(entries) + 1,
                    code=pkg.code,
                    pkg_id=pkg.pkg_id,
                    batch_id=pkg.batch_id,
                    store=pkg.store,
                    original_skus=original,
                    returned_skus=sorted(returned),
                    retained_skus=sorted(retained),
                    sold_qty=len(original),
                    returned_qty=len(returned),
                    eligible_releases=eligible,
                    status=status,
                    note=note,
                )
            )
            batch_returned[pkg.batch_id] = batch_returned.get(pkg.batch_id, 0) + len(returned)

        # 逐批：原售出数 - 实退数 = 合格份数；与旧底稿（只记未售数）对账
        for batch_id, sold in batch_sold.items():
            actual_returned = batch_returned.get(batch_id, 0)
            qualified = sold - actual_returned
            draft = self.drafts.get(batch_id)
            if draft is not None:
                # 该批总包内盘数
                batch_total = sum(
                    len(p.skus) for p in self.packages.values() if p.batch_id == batch_id
                )
                draft_implied_sold = batch_total - draft.unsold_external
                if draft_implied_sold != qualified:
                    issues.append(
                        ReconcileIssue(
                            None,
                            None,
                            f"批次 {batch_id} 对不上：底稿隐含售出 {draft_implied_sold}，"
                            f"核单合格份数 {qualified}（原售 {sold} - 实退 {actual_returned}）",
                        )
                    )

        # 未被任何包引用的退单
        known = {p.pkg_id for p in self.packages.values()}
        for rv in self.returns:
            if rv.pkg_id not in known:
                issues.append(ReconcileIssue(rv.pkg_id, None, f"退单 {rv.voucher_id} 引用未知包"))

        self._seal(entries)
        return entries, issues

    def _seal(self, entries: list[BookletEntry]) -> None:
        """给包册加逐条哈希链，任何事后改动都会断链。"""
        prev = ""
        for e in entries:
            e.prev_hash = prev
            payload = _canonical(
                {
                    k: v
                    for k, v in asdict(e).items()
                    if k not in ("prev_hash", "entry_hash")
                }
            )
            e.entry_hash = hashlib.sha256((prev + "|" + payload).encode()).hexdigest()
            prev = e.entry_hash

    @staticmethod
    def verify_chain(entries: list[BookletEntry]) -> bool:
        """独立复查：重算哈希链。"""
        prev = ""
        for e in entries:
            payload = _canonical(
                {
                    k: v
                    for k, v in asdict(e).items()
                    if k not in ("prev_hash", "entry_hash")
                }
            )
            expect = hashlib.sha256((prev + "|" + payload).encode()).hexdigest()
            if e.prev_hash != prev or e.entry_hash != expect:
                return False
            prev = e.entry_hash
        return True

    def reprocessed_summary(self) -> list[dict]:
        """补办退回实物的另册验收记录。"""
        return [asdict(rv) for rv in self.reprocessed_book]
