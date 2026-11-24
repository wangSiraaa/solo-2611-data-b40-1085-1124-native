"""换卡柜：开包前保证单、可适应拆包观察的最大可签册数规划与台账。

柜面规则（题目固定，照此实现）

* 订户相册的每个空位要求一张具名卡（指定图案、指定亮/普面）；标明的一换一
  清单允许把柜台持有的某张真牌换成另一张具名真牌，**每次对调占一个换权**。
* 柜台持有带包号的未拆包，只能拆约定次数；每开一包才看到整包实物，之后才
  决定下一包开哪号，或跟哪户换。不能预看没开包的印带——规划器只依据
  :class:`~wrsource.tape.Belief` 中与回条相容的解释，按“观察分支”推进。
* 保证单在拆包前开给具名册号；不论之后出现哪一种合法包况，同一组已签册都
  必须补齐。给出去的卡、用掉的换权不可收回；押换未成交时原卡与换权才释放。
* 两种柜制：
    * 即换：当场把真牌交给换卡户、换入牌马上能装册，占一个换权，户随即离柜。
    * 押换：户主离柜前把双方两张具名实卡封入唯一押柜格，占一个换权；约定包
      揭出指定图案才成交，落空则两张原卡退还、换权释放。封着的卡不能转押或
      提前贴册，全柜只有一格押柜。
* 换卡户会在约定包拆开前离柜；离柜后只能结算已入押的信封，不能新办交易。

规划器对“签哪几本 + 怎么办”做穷举 AND-OR 搜索：开包是机会结点，每个与回条
相容的观察都必须能继续赢；其余动作是确定性结点。返回一棵可直接照办的办理树
（:class:`PolicyNode`），并在 :class:`OrderLedger` 里逐笔记账，柜员可按单号
查出开包前承诺给谁、揭包后实卡和换权分别去了哪里。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from wrsource.tape import (Belief, Card, TapeRoom, NAME_DIRECTION)


class CounterError(ValueError):
    """柜面配置或动作不合法。"""


# ---------------------------------------------------------------------------
# 静态柜面配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    album_id: str
    index: int
    card: Card

    @staticmethod
    def from_dict(album_id: str, index: int, obj: Mapping) -> "Slot":
        return Slot(album_id, index, Card.from_dict(obj))


@dataclass(frozen=True)
class SwapRule:
    """一换一清单的一行：持 ``from`` 可对调成 ``to``，占一个换权。"""

    rule_id: str
    from_card: Card
    to_card: Card

    @staticmethod
    def from_dict(obj: Mapping) -> "SwapRule":
        return SwapRule(str(obj["rule_id"]), Card.from_dict(obj["from"]),
                        Card.from_dict(obj["to"]))


@dataclass(frozen=True)
class HeldCard:
    """一张具名实物真牌（卡号终身不变，换权/押柜都按卡号追踪）。"""

    card_id: str
    card: Card

    @staticmethod
    def from_dict(obj: Mapping) -> "HeldCard":
        return HeldCard(str(obj["card_id"]), Card.from_dict(obj["card"]))


@dataclass(frozen=True)
class Matcher:
    """图案/亮面的匹配条件，缺省项不约束。"""

    design: Optional[str] = None
    foil: Optional[bool] = None

    def matches(self, card: Card) -> bool:
        if self.design is not None and card.design != self.design:
            return False
        if self.foil is not None and card.foil != self.foil:
            return False
        return True

    def label(self) -> str:
        parts = []
        if self.foil is not None:
            parts.append("亮" if self.foil else "普")
        parts.append(self.design if self.design is not None else "任意图案")
        return "".join(parts)

    @staticmethod
    def from_dict(obj: Optional[Mapping]) -> "Matcher":
        if obj is None:
            return Matcher()
        return Matcher(obj.get("design"),
                       None if obj.get("foil") is None else bool(obj["foil"]))


@dataclass(frozen=True)
class Customer:
    """换卡户：带一张真牌、想换匹配 ``wants`` 的牌，约定包前离柜。"""

    customer_id: str
    card: HeldCard
    wants: Matcher
    departs_before_pack: Optional[int] = None
    available: bool = True

    @staticmethod
    def from_dict(obj: Mapping) -> "Customer":
        dep = obj.get("departs_before_pack")
        return Customer(str(obj["customer_id"]),
                        HeldCard.from_dict(obj["card"]),
                        Matcher.from_dict(obj.get("wants")),
                        None if dep is None else int(dep),
                        bool(obj.get("available", True)))


@dataclass(frozen=True)
class Escrow:
    """押柜格里唯一一封在押信封。"""

    escrow_id: str
    counter_card: HeldCard          # 柜台押出的具名卡
    customer: Customer              # 户的在押卡随客户身份走
    condition: Matcher              # 约定包揭出该图案/亮面才成交
    settle_pack: int                # 约定包号
    right_id: str                   # 本笔押换占用的换权


@dataclass(frozen=True)
class CounterSpec:
    albums: Tuple[Tuple[Slot, ...], ...]            # 按 album 顺序的空位
    album_ids: Tuple[str, ...]
    swap_rules: Tuple[SwapRule, ...]
    holdings: Tuple[HeldCard, ...]
    right_ids: Tuple[str, ...]
    customers: Tuple[Customer, ...]
    pack_ids: Tuple[int, ...]                       # 可拆包号（包序）
    max_opens: int
    escrow_allowed: bool = True
    escrow_blocked_cards: FrozenSet[str] = frozenset()
    pack_size: int = 1

    @staticmethod
    def from_dict(obj: Mapping) -> "CounterSpec":
        albums: List[Tuple[Slot, ...]] = []
        album_ids: List[str] = []
        for alb in obj["albums"]:
            aid = str(alb["album_id"])
            slots = tuple(Slot.from_dict(aid, i, s)
                          for i, s in enumerate(alb["slots"]))
            albums.append(slots)
            album_ids.append(aid)
        if len(set(album_ids)) != len(album_ids):
            raise CounterError("duplicate album ids")
        holdings = tuple(HeldCard.from_dict(h) for h in obj.get("holdings", ()))
        hids = [h.card_id for h in holdings]
        if len(set(hids)) != len(hids):
            raise CounterError("duplicate held card ids")
        rights = obj.get("rights", 0)
        if isinstance(rights, int):
            right_ids = tuple(f"R{i}" for i in range(rights))
        else:
            right_ids = tuple(str(x) for x in rights)
        customers = tuple(Customer.from_dict(c)
                          for c in obj.get("customers", ()))
        cids = [c.customer_id for c in customers]
        if len(set(cids)) != len(cids):
            raise CounterError("duplicate customer ids")
        blocked = frozenset(str(x)
                            for x in obj.get("escrow_blocked_cards", ()))
        packs = tuple(int(x) for x in obj["packs"])
        if len(set(packs)) != len(packs):
            raise CounterError("duplicate pack ids")
        max_opens = int(obj["max_opens"])
        if not 0 <= max_opens <= len(packs):
            raise CounterError("max_opens out of range")
        return CounterSpec(
            tuple(albums), tuple(album_ids),
            tuple(SwapRule.from_dict(r)
                  for r in obj.get("swap_rules", ())),
            holdings, right_ids, customers, packs, max_opens,
            bool(obj.get("escrow_allowed", True)), blocked,
            int(obj.get("pack_size", 1)))


# ---------------------------------------------------------------------------
# 动态规划状态（不可变；所有资源按身份卡号/换权号追踪）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class State:
    remaining: Tuple[Tuple[Card, ...], ...]     # 每册尚未填的空位要求
    signed: FrozenSet[str]
    holdings: Tuple[HeldCard, ...]
    rights: Tuple[str, ...]                     # 空闲换权号
    customers: Tuple[Customer, ...]
    escrow: Optional[Escrow]
    opened: FrozenSet[int]
    opens_used: int
    hypotheses: Tuple[str, ...]                 # 当前信念解释的 key 名
    escrow_seq: int = 0                         # 信封编号计数


# ---------------------------------------------------------------------------
# 办理树
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicyNode:
    kind: str                                   # action / chance / done
    detail: Dict[str, Any]
    next: Optional["PolicyNode"] = None
    branches: Optional[Tuple[Tuple[str, "PolicyNode"], ...]] = None

    def is_done(self) -> bool:
        return self.kind == "done"

    def to_dict(self) -> dict:
        out = {"kind": self.kind, "detail": self.detail}
        if self.next is not None:
            out["next"] = self.next.to_dict()
        if self.branches is not None:
            out["branches"] = [
                {"observation": k, "policy": v.to_dict()}
                for k, v in self.branches]
        return out

    def walk_lines(self, indent: int = 0) -> List[str]:
        pad = "  " * indent
        lines = [pad + self.detail.get("text", self.kind)]
        if self.next is not None:
            lines += self.next.walk_lines(indent)
        if self.branches is not None:
            for k, v in self.branches:
                lines.append(pad + f"├─ 若观察到【{k}】：")
                lines += v.walk_lines(indent + 2)
        return lines


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------
class CounterPlanner:
    def __init__(self, spec: CounterSpec, belief: Belief):
        self.spec = spec
        self.belief = belief
        if not belief.possible:
            raise CounterError("no tape hypothesis survives the receipts; "
                               "cannot guarantee any album")
        self._hyp = {h.key: h for h in belief.hypotheses}
        self._memo: Dict[State, Optional[PolicyNode]] = {}

    # -- 初始态 ----------------------------------------------------------
    def initial_state(self, signed: Sequence[str]) -> State:
        signed = frozenset(signed)
        unknown = signed - set(self.spec.album_ids)
        if unknown:
            raise CounterError(f"unsigned/unknown albums: {sorted(unknown)}")
        remaining = tuple(
            tuple(s.card for s in slots) if aid in signed else ()
            for aid, slots in zip(self.spec.album_ids, self.spec.albums))
        # 离柜拍点为 0 的户在签单时已经离柜，任何交易都办不了。
        customers = tuple(
            replace(c, available=False)
            if c.available and c.departs_before_pack == 0 else c
            for c in self.spec.customers)
        return State(
            remaining, signed, self.spec.holdings, self.spec.right_ids,
            customers, None, frozenset(), 0,
            tuple(sorted(self._hyp)), 0)

    # -- 目标与上界剪枝 --------------------------------------------------
    @staticmethod
    def _all_filled(st: State) -> bool:
        return all(not slots for slots in st.remaining)

    def _still_possible(self, st: State) -> bool:
        need = sum(len(slots) for slots in st.remaining)
        if need == 0:
            return True
        opens_left = self.spec.max_opens - st.opens_used
        future_cards = len(st.holdings) + opens_left * self.belief.pack_size
        if st.escrow is not None:
            future_cards += 1            # 押入的户卡成交后可装册
        if future_cards < need:
            return False
        # 已开包数不能超过约定次数。
        return st.opens_used <= self.spec.max_opens

    # -- 动作：贴册 ------------------------------------------------------
    def _moves_mount(self, st: State) -> List[Tuple[State, dict]]:
        out: List[Tuple[State, dict]] = []
        for h in st.holdings:
            for ai, slots in enumerate(st.remaining):
                if h.card not in slots:
                    continue
                new_rem = list(st.remaining)
                new_slots = list(slots)
                new_slots.remove(h.card)
                new_rem[ai] = tuple(new_slots)
                new_hold = tuple(x for x in st.holdings
                                 if x.card_id != h.card_id)
                ev = {"event": "mount", "card_id": h.card_id,
                      "card": h.card.as_dict(),
                      "album_id": self.spec.album_ids[ai]}
                nst = replace(st, remaining=tuple(new_rem),
                              holdings=new_hold)
                text = f"把 {h.card_id}（{h.card.label()}）贴入册 " \
                       f"{self.spec.album_ids[ai]}"
                out.append((nst, {"event": ev, "text": text}))
        return out

    # -- 动作：一换一清单对调 --------------------------------------------
    def _moves_listed_swap(self, st: State) -> List[Tuple[State, dict]]:
        if not st.rights:
            return []
        out: List[Tuple[State, dict]] = []
        for rule in self.spec.swap_rules:
            for h in st.holdings:
                if h.card != rule.from_card:
                    continue
                new_id = f"{h.card_id}>{rule.rule_id}"
                new_card = HeldCard(new_id, rule.to_card)
                new_hold = tuple(new_card if x.card_id == h.card_id else x
                                 for x in st.holdings)
                right = st.rights[0]
                nst = replace(st, holdings=new_hold,
                              rights=tuple(st.rights[1:]))
                ev = {"event": "listed_swap", "rule_id": rule.rule_id,
                      "from_card_id": h.card_id,
                      "from_card": h.card.as_dict(),
                      "to_card_id": new_id,
                      "to_card": rule.to_card.as_dict(),
                      "right_id": right}
                text = f"动用换权 {right}，按清单 {rule.rule_id} 把 " \
                       f"{h.card_id}（{h.card.label()}）对调成 " \
                       f"{new_id}（{rule.to_card.label()}）"
                out.append((nst, {"event": ev, "text": text}))
        return out

    # -- 动作：即换 ------------------------------------------------------
    def _moves_instant(self, st: State) -> List[Tuple[State, dict]]:
        if not st.rights or st.escrow is not None:
            return []
        out: List[Tuple[State, dict]] = []
        for cust in st.customers:
            if not cust.available:
                continue
            for h in st.holdings:
                if not cust.wants.matches(h.card):
                    continue
                right = st.rights[0]
                new_hold = tuple(x for x in st.holdings
                                 if x.card_id != h.card_id) + (cust.card,)
                new_cust = replace(cust, available=False)
                new_customers = tuple(new_cust if c.customer_id ==
                                      cust.customer_id else c
                                      for c in st.customers)
                nst = replace(st, holdings=new_hold,
                              rights=tuple(st.rights[1:]),
                              customers=new_customers)
                ev = {"event": "instant_swap",
                      "customer_id": cust.customer_id,
                      "gave_card_id": h.card_id,
                      "gave_card": h.card.as_dict(),
                      "received_card_id": cust.card.card_id,
                      "received_card": cust.card.card.as_dict(),
                      "right_id": right}
                text = f"与 {cust.customer_id} 即换：交出 {h.card_id}" \
                       f"（{h.card.label()}），换入 {cust.card.card_id}" \
                       f"（{cust.card.card.label()}），占换权 {right}，户离柜"
                out.append((nst, {"event": ev, "text": text}))
        return out

    # -- 动作：押换入柜 --------------------------------------------------
    def _moves_escrow(self, st: State) -> List[Tuple[State, dict]]:
        if not self.spec.escrow_allowed or not st.rights \
                or st.escrow is not None:
            return []
        out: List[Tuple[State, dict]] = []
        for cust in st.customers:
            if not cust.available:
                continue
            for h in st.holdings:
                if h.card_id in self.spec.escrow_blocked_cards:
                    continue
                if not cust.wants.matches(h.card):
                    continue
                for pack_id in self.spec.pack_ids:
                    if pack_id in st.opened:
                        continue
                    # 条件只能取自该约定包在当前信念下确实可能揭出的卡。
                    conds = _condition_matchers(
                        self._pack_outcomes(st, pack_id))
                    for cond in conds:
                        right = st.rights[0]
                        eid = f"E{st.escrow_seq}"
                        escrow = Escrow(eid, h, cust, cond, pack_id, right)
                        new_hold = tuple(x for x in st.holdings
                                         if x.card_id != h.card_id)
                        new_cust = replace(cust, available=False)
                        new_customers = tuple(
                            new_cust if c.customer_id == cust.customer_id
                            else c for c in st.customers)
                        nst = replace(st, holdings=new_hold,
                                      rights=tuple(st.rights[1:]),
                                      customers=new_customers, escrow=escrow,
                                      escrow_seq=st.escrow_seq + 1)
                        ev = {"event": "escrow_placed", "escrow_id": eid,
                              "counter_card_id": h.card_id,
                              "counter_card": h.card.as_dict(),
                              "customer_id": cust.customer_id,
                              "customer_card_id": cust.card.card_id,
                              "customer_card": cust.card.card.as_dict(),
                              "condition": _matcher_dict(cond),
                              "settle_pack": pack_id, "right_id": right}
                        text = f"押换入柜（{eid}）：{h.card_id} 与 " \
                               f"{cust.customer_id} 的 {cust.card.card_id} " \
                               f"封存，约定第 {pack_id} 包揭出【" \
                               f"{cond.label()}】才成交，占换权 {right}"
                        out.append((nst, {"event": ev, "text": text}))
        return out

    # -- 动作：开包（机会结点）------------------------------------------
    def _pack_outcomes(self, st: State, pack_id: int
                       ) -> Tuple[Tuple[Card, ...], ...]:
        seen: Dict[Tuple[Card, ...], None] = {}
        for key in st.hypotheses:
            content = self._hyp[key].pack_content(
                pack_id, self.belief.pack_size)
            seen.setdefault(content, None)
        return tuple(seen)

    def _open_branch(self, st: State, pack_id: int,
                     content: Tuple[Card, ...]) -> Tuple[State, List[dict]]:
        events: List[dict] = [
            {"event": "pack_opened", "pack_id": pack_id,
             "cards": [c.as_dict() for c in content]}]
        hypotheses = tuple(
            key for key in st.hypotheses
            if self._hyp[key].pack_content(pack_id,
                                           self.belief.pack_size) == content)
        # 本包拆开后：离柜包号 <= 本包号的户都已离柜（在押信封随物留存，
        # 仍按约定包结算；离柜后不能再新办即换/押换）。
        customers = tuple(
            (replace(c, available=False)
             if c.available and c.departs_before_pack is not None
             and c.departs_before_pack <= pack_id
             else c)
            for c in st.customers)
        holdings = st.holdings + tuple(
            HeldCard(f"P{pack_id}#{i}", card)
            for i, card in enumerate(content))
        nst = replace(st, holdings=holdings, customers=customers,
                      opened=st.opened | {pack_id},
                      opens_used=st.opens_used + 1, hypotheses=hypotheses)
        # 结算押柜信封（若约定的正是这一包）。
        if st.escrow is not None and st.escrow.settle_pack == pack_id:
            nst, settle_events = self._settle_escrow(nst, st.escrow, content)
            events += settle_events
        return nst, events

    @staticmethod
    def _settle_escrow(st: State, esc: Escrow,
                       content: Tuple[Card, ...]) -> Tuple[State, List[dict]]:
        hit = any(esc.condition.matches(c) for c in content)
        if hit:
            # 成交：户卡入柜可装册；柜台卡归户；换权不退还。
            holdings = st.holdings + (esc.customer.card,)
            customers = tuple(
                replace(c, available=False)
                if c.customer_id == esc.customer.customer_id else c
                for c in st.customers)
            nst = replace(st, holdings=holdings, customers=customers,
                          escrow=None)
            ev = {"event": "escrow_settled", "escrow_id": esc.escrow_id,
                  "counter_card_id": esc.counter_card.card_id,
                  "customer_card_id": esc.customer.card.card_id,
                  "customer_id": esc.customer.customer_id,
                  "right_id": esc.right_id, "dealt": True}
            return nst, [ev, {"event": "note",
                              "text": f"信封 {esc.escrow_id} 成交："
                                      f"{esc.counter_card.card_id} 归"
                                      f"{esc.customer.customer_id}，"
                                      f"{esc.customer.card.card_id} 入柜"}]
        # 落空：两张原卡退还，换权释放。
        holdings = st.holdings + (esc.counter_card,)
        customers = tuple(
            replace(c, available=False)
            if c.customer_id == esc.customer.customer_id else c
            for c in st.customers)
        nst = replace(st, holdings=holdings, customers=customers,
                      escrow=None, rights=tuple(sorted(st.rights +
                                                       (esc.right_id,))))
        ev = {"event": "escrow_failed", "escrow_id": esc.escrow_id,
              "counter_card_id": esc.counter_card.card_id,
              "customer_card_id": esc.customer.card.card_id,
              "customer_id": esc.customer.customer_id,
              "right_id": esc.right_id, "dealt": False}
        return nst, [ev, {"event": "note",
                          "text": f"信封 {esc.escrow_id} 落空：原卡退还，"
                                  f"换权 {esc.right_id} 释放"}]

    def _move_open(self, st: State) -> List[Tuple[int, List[PolicyNode], dict]]:
        """返回 (pack_id, 每个观察分支的办理树, 描述)；分支由 solve 求。"""
        if st.opens_used >= self.spec.max_opens:
            return []
        out = []
        for pack_id in self.spec.pack_ids:
            if pack_id in st.opened:
                continue
            outcomes = self._pack_outcomes(st, pack_id)
            branches: List[Tuple[str, PolicyNode]] = []
            ok = True
            for content in outcomes:
                nst, _ = self._open_branch(st, pack_id, content)
                sub = self.solve(nst)
                if sub is None:
                    ok = False
                    break
                label = "、".join(c.label() for c in content)
                branches.append((label, sub))
            if ok:
                text = f"开第 {pack_id} 包（{len(outcomes)} 种与回条相容的包况）"
                out.append((pack_id, branches, {"event": {
                    "event": "choose_open", "pack_id": pack_id},
                    "text": text}))
        return out

    # -- 递归 AND-OR -----------------------------------------------------
    def solve(self, st: State) -> Optional[PolicyNode]:
        if st in self._memo:
            return self._memo[st]
        if self._all_filled(st):
            node = PolicyNode("done", {"text": "已签各册全部补齐，办结"})
            self._memo[st] = node
            return node
        if not self._still_possible(st):
            self._memo[st] = None
            return None
        # 先试确定性动作（贴册/对调/押换/即换），再试开包机会结点。
        for movers in (self._moves_mount, self._moves_listed_swap,
                       self._moves_escrow, self._moves_instant):
            for nst, detail in movers(st):
                sub = self.solve(nst)
                if sub is not None:
                    node = PolicyNode("action", detail, next=sub)
                    self._memo[st] = node
                    return node
        for pack_id, branches, detail in self._move_open(st):
            node = PolicyNode(
                "chance", detail,
                branches=tuple((k, v) for k, v in branches))
            self._memo[st] = node
            return node
        self._memo[st] = None
        return None

    # -- 最大可签册子集 --------------------------------------------------
    def plan_max(self) -> Tuple[Tuple[str, ...], PolicyNode]:
        """枚举册子集（册数从大到小、字典序），返回能事先签下的最多册。"""
        n = len(self.spec.album_ids)
        for size in range(n, 0, -1):
            for subset in _combinations(self.spec.album_ids, size):
                node = self.solve(self.initial_state(subset))
                if node is not None:
                    return tuple(subset), node
        return (), PolicyNode("done", {"text": "一册都无法事先保证"})


def _combinations(items: Tuple[str, ...], size: int
                  ) -> List[Tuple[str, ...]]:
    if size == 0:
        return [()]
    out: List[Tuple[str, ...]] = []
    for i in range(len(items) - size + 1):
        for rest in _combinations(items[i + 1:], size - 1):
            out.append((items[i],) + rest)
    return out


def _condition_matchers(contents: Sequence[Tuple[Card, ...]]
                        ) -> Tuple[Matcher, ...]:
    """一包可能揭出的可成交条件：整卡、只指定图案、只指定亮/普面。

    只保留至少被一种真实包况中的某张卡满足的条件——不可能成交的信封
    （例如包内全是普面却约定“揭出亮面”）不会成为办理动作。
    """
    full: Dict[Tuple[Optional[str], Optional[bool]], None] = {}
    designs: Dict[str, None] = {}
    foils: Dict[bool, None] = {}
    for content in contents:
        for c in content:
            full[(c.design, c.foil)] = None
            designs[c.design] = None
            foils[c.foil] = None
    all_cards = [c for content in contents for c in content]
    candidates = ([Matcher(d, f) for d, f in full]
                  + [Matcher(d, None) for d in designs]
                  + [Matcher(None, f) for f in foils])
    return tuple(m for m in candidates
                 if any(m.matches(c) for c in all_cards))


def _matcher_dict(m: Matcher) -> dict:
    return {"design": m.design, "foil": m.foil}


# ---------------------------------------------------------------------------
# 保证单台账：一笔新册单从签保证单到揭包，全程可按单号/卡号/换权号追查
# ---------------------------------------------------------------------------
@dataclass
class OrderLedger:
    order_id: str
    slip_albums: Tuple[str, ...]
    events: List[dict] = field(default_factory=list)

    def append(self, event: dict) -> None:
        self.events.append({"seq": len(self.events), **event})

    def commitment(self) -> List[dict]:
        """开包前承诺给谁。"""
        return [e for e in self.events if e["event"] == "slip_issued"]

    def card_trace(self, card_id: str) -> List[dict]:
        """按原卡号重查：这张具名实卡去了哪里（含对调衍生卡）。"""
        out = []
        for e in self.events:
            blob = json.dumps(e, ensure_ascii=False, sort_keys=True)
            if card_id in blob:
                out.append(e)
        return out

    def right_trace(self, right_id: str) -> List[dict]:
        return [e for e in self.events if e.get("right_id") == right_id]

    def album_destination(self, album_id: str) -> List[dict]:
        """揭包后实卡装进该册的全部记录。"""
        return [e for e in self.events
                if e["event"] == "mount" and e.get("album_id") == album_id]

    def as_dict(self) -> dict:
        return {"order_id": self.order_id, "slip_albums": list(self.slip_albums),
                "events": self.events}


class OrderRunner:
    """按办理树在某个指定世界（一条可行解释）下执行并记账。"""

    def __init__(self, planner: CounterPlanner, order_id: str,
                 slip_albums: Sequence[str]):
        self.planner = planner
        self.spec = planner.spec
        self.ledger = OrderLedger(order_id, tuple(slip_albums))
        self.ledger.append({"event": "order_created",
                            "slip_albums": list(slip_albums)})
        for aid in slip_albums:
            self.ledger.append({"event": "slip_issued", "album_id": aid})

    def run(self, node: PolicyNode, st: State,
            world_key: Tuple[str, int, int]) -> State:
        if node.kind == "done":
            self.ledger.append({"event": "order_completed",
                                "albums": sorted(st.signed)})
            return st
        if node.kind == "action":
            ev = dict(node.detail["event"])
            nst = self._apply_event(st, ev)
            self.ledger.append(ev)
            return self.run(node.next, nst, world_key)
        if node.kind == "chance":
            pack_id = node.detail["event"]["pack_id"]
            content = self.planner._hyp[world_key].pack_content(
                pack_id, self.planner.belief.pack_size)
            label = "、".join(c.label() for c in content)
            branch = dict(node.branches)
            if label not in branch:
                raise CounterError(
                    f"policy has no branch for observation {label!r}")
            nst, open_events = self.planner._open_branch(st, pack_id, content)
            for ev in open_events:
                self.ledger.append(ev)
            return self.run(branch[label], nst, world_key)
        raise CounterError(f"unknown policy node kind: {node.kind}")

    def _apply_event(self, st: State, ev: dict) -> State:
        kind = ev["event"]
        if kind == "mount":
            holdings = tuple(h for h in st.holdings
                             if h.card_id != ev["card_id"])
            remaining = [list(slots) for slots in st.remaining]
            ai = self.spec.album_ids.index(ev["album_id"])
            target = Card.from_dict(ev["card"])
            remaining[ai].remove(target)
            return replace(st, holdings=holdings,
                           remaining=tuple(tuple(x) for x in remaining))
        if kind == "listed_swap":
            new_card = HeldCard(ev["to_card_id"],
                                Card.from_dict(ev["to_card"]))
            holdings = tuple(new_card if h.card_id == ev["from_card_id"]
                             else h for h in st.holdings)
            return replace(st, holdings=holdings,
                           rights=tuple(r for r in st.rights
                                        if r != ev["right_id"]))
        if kind == "instant_swap":
            holdings = tuple(h for h in st.holdings
                             if h.card_id != ev["gave_card_id"])
            got = HeldCard(ev["received_card_id"],
                           Card.from_dict(ev["received_card"]))
            holdings = holdings + (got,)
            customers = tuple(
                replace(c, available=False)
                if c.customer_id == ev["customer_id"] else c
                for c in st.customers)
            return replace(st, holdings=holdings, customers=customers,
                           rights=tuple(r for r in st.rights
                                        if r != ev["right_id"]))
        if kind == "escrow_placed":
            cust = next(c for c in st.customers
                        if c.customer_id == ev["customer_id"])
            counter_card = next(h for h in st.holdings
                                if h.card_id == ev["counter_card_id"])
            cond = Matcher.from_dict(ev["condition"])
            escrow = Escrow(ev["escrow_id"], counter_card, cust, cond,
                            ev["settle_pack"], ev["right_id"])
            holdings = tuple(h for h in st.holdings
                             if h.card_id != counter_card.card_id)
            customers = tuple(
                replace(c, available=False)
                if c.customer_id == cust.customer_id else c
                for c in st.customers)
            return replace(st, holdings=holdings, customers=customers,
                           rights=tuple(r for r in st.rights
                                        if r != ev["right_id"]),
                           escrow=escrow, escrow_seq=st.escrow_seq + 1)
        raise CounterError(f"policy action not replayable: {kind}")


# ---------------------------------------------------------------------------
# 整包规格（制带室 + 柜台共用一份 JSON）
# ---------------------------------------------------------------------------
def build_from_spec(spec: Mapping) -> Tuple[TapeRoom, CounterSpec, Belief,
                                            CounterPlanner]:
    room = TapeRoom.from_dict(spec)
    constraints = spec.get("constraints", {})
    start_cells = constraints.get("start_cells")
    dir_names = constraints.get("directions")
    directions = (tuple(NAME_DIRECTION[d] for d in dir_names)
                  if dir_names else None)
    hypotheses = room.feasible_hypotheses(
        start_cells=None if start_cells is None else tuple(start_cells),
        directions=directions or (1, -1))
    belief = Belief(hypotheses, spec.get("pack_size", 1), room.receipts)
    counter = CounterSpec.from_dict(
                {**spec.get("counter", {}),
                 "pack_size": spec.get("pack_size", 1)})
    for pack_id in counter.pack_ids:
        if pack_id in belief.observed_pack_indices():
            raise CounterError(
                f"pack {pack_id} already has a sampled receipt; only "
                "unopened packs can be handed to the counter")
    planner = CounterPlanner(counter, belief)
    return room, counter, belief, planner


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _json_default(obj):
    if isinstance(obj, Card):
        return obj.as_dict()
    raise TypeError(f"not JSON serializable: {obj!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wrsource.counter",
        description="换卡柜：回条信念下的最大保证册数与办理树。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--spec", required=True)
    p.add_argument("--out")
    p.add_argument("--order-id", default="ORD-1")
    args = parser.parse_args(argv)

    with open(args.spec, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    room, counter, belief, planner = build_from_spec(spec)
    slips, policy = planner.plan_max()

    # 每个可行世界跑一遍台账，证明同一组已签册在任一合法包况下都补齐。
    ledgers = []
    for h in belief.hypotheses:
        runner = OrderRunner(planner, args.order_id, slips)
        runner.run(policy, planner.initial_state(slips), h.key)
        ledgers.append({"world": h.as_dict(),
                        "ledger": runner.ledger.as_dict()})
    result = {
        "tape": belief.as_dict(),
        "guaranteed_slips": list(slips),
        "max_albums": len(slips),
        "policy": policy.to_dict(),
        "policy_text": policy.walk_lines(),
        "per_world_ledgers": ledgers,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2,
                      default=_json_default)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
