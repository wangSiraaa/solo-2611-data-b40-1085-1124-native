"""故事盒带读求解层。

这是在原有“书目/预约/取件”台账之上新增的纯领域层，不改变目录与预约的
既有不变量。核心分成五块：

1. 香片、留香与有向遮蔽表（``ScentBox`` / ``analyze_turn`` / ``replay_record``）；
2. 两位读者各自独立的页单、时窗、惊喜页与残留（``Page`` / ``plan_guaranteed``）；
3. 实牌唯一性、转手拍数、小盘固定座位与唯一送片员容量；
4. 已付费自由页的对抗性时窗：读者两拍任选，保证必须对两个选择都成立；
5. 交班与续办（``ReadingSession``）：小盘归还前不能偷看揭牌拍，现场送片的
   实际时点当场可见且记录只能追加，不能事后抹掉残留。

输入既可以用 dataclass，也可以直接传普通 ``dict`` / 列表。返回中的集合保留为
``set``/``frozenset``，需要落 SQLite/JSON 时用 :func:`jsonable` 转换。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from itertools import combinations, product
from typing import Any, Iterable, Mapping

from .errors import IdempotencyConflict

HORIZON = 10
MAX_CARDS = 8
MAX_SCHEDULES = 5000


class StoryBoxError(ValueError):
    """故事盒领域的硬校验错误。"""


# ---------------------------------------------------------------------------
# 通用取值/归一化
# ---------------------------------------------------------------------------

def canonical_mode(mode: Any) -> str:
    """把常见中英文业务叫法归一为 tray / delivery。"""
    text = str(mode or "tray").strip().lower().replace("-", "_").replace(" ", "_")
    if text in {
        "delivery", "on_site", "onsite", "courier", "courier_service",
        "live", "on_site_delivery", "现场", "现场送片", "送片", "送片员",
    }:
        return "delivery"
    if text in {
        "tray", "small_tray", "tray_service", "plates", "plate",
        "small_plate", "xiaopan", "小盘", "小盘业务", "托盘",
    }:
        return "tray"
    return text


def _mapping(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    raise TypeError(f"无法把 {type(obj).__name__} 归一化为映射")


def _pick(d: Mapping[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, (set, frozenset, list, tuple)):
        return tuple(value)
    return (value,)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in _as_tuple(value))


def _as_int_tuple(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, range):
        return tuple(int(x) for x in value)
    if isinstance(value, int):
        return (int(value),)
    vals = _as_tuple(value)
    if not vals:
        return None
    out: list[int] = []
    for v in vals:
        if isinstance(v, str) and ":" in v:
            a, b = v.split(":", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(v))
    return tuple(out)


def jsonable(obj: Any) -> Any:
    """把结果里的 set/dataclass 转成可 JSON 序列化结构。"""
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(jsonable(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return jsonable(obj.to_dict())
    if hasattr(obj, "__dataclass_fields__"):
        return {k: jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


# ---------------------------------------------------------------------------
# 香片、试闻表与故事盒
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScentCard:
    """一张实物气味牌。

    ``linger`` 表示揭牌当拍之后仍可参与遮蔽计算的拍数。例如 ``linger=1``
    时，第 0 拍的牌仍会遮第 1 拍的新味，但不遮第 2 拍。
    若输入使用常见的 ``duration``（含揭牌拍在内的总寿命），归一化为
    ``linger=duration-1``。
    """

    id: str
    labels: frozenset[str] = frozenset()
    linger: int = 1
    color: str | None = None
    seat: str | None = None

    @classmethod
    def from_value(cls, value: Any, default_id: str | None = None) -> "ScentCard":
        if isinstance(value, ScentCard):
            return value
        if isinstance(value, str):
            # 允许 {"青": ["松"]} 的键由调用方补 default_id。
            return cls(id=default_id or value, labels=frozenset({value}))
        d = _mapping(value)
        cid = _pick(d, ("id", "card_id", "name", "piece", "牌", "牌名"), default_id)
        if cid is None:
            raise StoryBoxError("气味牌缺少 id/card_id")
        labels = _pick(
            d,
            ("labels", "tags", "scents", "notes", "aromas", "odors", "标签", "气味"),
            (),
        )
        linger = _pick(
            d,
            (
                "linger",
                "linger_beats",
                "residual_beats",
                "after_beats",
                "留香",
                "留香拍数",
            ),
        )
        if linger is None:
            duration = _pick(
                d,
                ("duration", "lasts", "ttl", "life", "whole_beats", "整拍留香"),
            )
            linger = max(0, int(duration) - 1) if duration is not None else 1
        return cls(
            id=str(cid),
            labels=frozenset(str(x) for x in _as_tuple(labels)),
            linger=max(0, int(linger)),
            color=_pick(d, ("color", "颜色")),
            seat=_pick(d, ("seat", "seat_id", "座", "座位")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "labels": sorted(self.labels),
            "linger": self.linger,
            "duration": self.linger + 1,
            "color": self.color,
            "seat": self.seat,
        }


Card = ScentCard


@dataclass(frozen=True)
class MaskRule:
    """有向遮蔽规则。

    典型四元组语义为 ``(旧味, 新味, 时间差, 被遮标签)``；省略被遮标签时
    默认遮掉新味。``lags`` 可以给多个允许时间差。
    """

    old: str
    hidden: str
    lags: frozenset[int] = frozenset({1})
    new: str | None = None
    rule_id: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> "MaskRule":
        if isinstance(value, MaskRule):
            return value
        if isinstance(value, (tuple, list)):
            if len(value) == 3:
                old, new, lag = value
                hidden = new
            elif len(value) >= 4:
                old, new, lag, hidden = value[:4]
            else:
                raise StoryBoxError(f"遮蔽规则元组长度不足: {value!r}")
            return cls(
                old=str(old),
                new=None if new is None else str(new),
                hidden=str(hidden),
                lags=frozenset(_as_int_tuple(lag) or ()),
            )

        d = _mapping(value)
        cond = d.get("when") or d.get("if") or d.get("condition")
        if isinstance(cond, Mapping):
            merged = dict(cond)
            merged.update({k: v for k, v in d.items() if k not in ("when", "if", "condition")})
            d = merged

        old = _pick(
            d,
            ("old", "old_scent", "old_label", "source", "source_label", "from",
             "prior", "older", "旧味", "旧标签", "旧"),
        )
        new = _pick(
            d,
            ("new", "new_scent", "new_label", "current", "target_scent", "to",
             "younger", "新味", "新标签", "新"),
        )
        hidden = _pick(
            d,
            ("hidden", "hidden_label", "blocked", "blocked_label", "masked",
             "masked_label", "target", "target_label", "label", "hides",
             "mask", "遮掉", "被遮标签"),
            new,
        )
        if old is None or hidden is None:
            raise StoryBoxError(f"遮蔽规则必须包含旧味和被遮标签: {value!r}")

        lags = _pick(d, ("lags", "allowed_lags", "time_diffs", "时间差集合"))
        if lags is not None:
            lag_set = frozenset(int(x) for x in _as_int_tuple(lags))
        else:
            lag = _pick(
                d,
                ("lag", "gap", "dt", "delay", "offset", "time_diff", "beat_gap",
                 "时间差", "拍差"),
            )
            if lag is not None:
                lag_set = frozenset(_as_int_tuple(lag) or ())
            else:
                lo = _pick(d, ("min_lag", "lag_min", "from_lag"))
                hi = _pick(d, ("max_lag", "lag_max", "through", "to_lag"))
                if lo is not None or hi is not None:
                    lo_i = int(lo) if lo is not None else 1
                    hi_i = int(hi) if hi is not None else lo_i
                    lag_set = frozenset(range(lo_i, hi_i + 1))
                else:
                    lag_set = frozenset({1})
        return cls(
            old=str(old),
            new=None if new is None else str(new),
            hidden=str(hidden),
            lags=lag_set,
            rule_id=_pick(d, ("id", "rule_id", "name")),
        )

    def matches(self, age: int, old_label: str, current_labels: Iterable[str]) -> bool:
        if age not in self.lags or old_label != self.old:
            return False
        if self.new is None:
            return True
        return self.new in set(current_labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "old": self.old,
            "new": self.new,
            "hidden": self.hidden,
            "lags": sorted(self.lags),
        }


MaskChart = MaskRule  # 仅用于类型/文档语义；真正的表是 tuple[MaskRule, ...]


@dataclass
class ScentBox:
    """一只实盒号对应的香片与同批试闻遮蔽表。"""

    box_id: str
    cards: dict[str, ScentCard] = field(default_factory=dict)
    rules: tuple[MaskRule, ...] = ()
    work_id: str | None = None
    copy_id: str | None = None

    @classmethod
    def from_value(cls, value: Any, rules: Any = None) -> "ScentBox":
        if isinstance(value, ScentBox):
            return value
        if isinstance(value, str):
            raise StoryBoxError(f"只给了盒号 {value!r}，但没有可加载的盒子资料")
        d = _mapping(value)
        box_id = _pick(d, ("box_id", "id", "copy_id", "实盒号", "盒号"), "BOX")
        work_id = _pick(d, ("work_id", "canonical_bib", "work", "作品"))
        copy_id = _pick(d, ("copy_id", "copy", "实盒号"))

        raw_cards = _pick(d, ("cards", "scent_cards", "pieces", "香片", "气味牌"), {})
        cards: dict[str, ScentCard] = {}
        if isinstance(raw_cards, Mapping):
            for cid, card_value in raw_cards.items():
                if isinstance(card_value, (list, tuple, set, frozenset)):
                    card = ScentCard.from_value(
                        {"id": cid, "labels": card_value}
                    )
                elif isinstance(card_value, ScentCard):
                    card = card_value
                else:
                    cd = _mapping(card_value)
                    cd.setdefault("id", cid)
                    card = ScentCard.from_value(cd)
                cards[card.id] = card
        else:
            for idx, card_value in enumerate(raw_cards):
                card = ScentCard.from_value(card_value, f"C{idx}")
                cards[card.id] = card

        raw_rules = _pick(
            d,
            ("rules", "masks", "mask_rules", "chart", "smell_chart",
             "masking_table", "试闻表", "遮蔽表"),
            (),
        )
        if rules is not None:
            raw_rules = rules
        if isinstance(raw_rules, MaskRule):
            rule_tuple = (raw_rules,)
        else:
            rule_tuple = tuple(MaskRule.from_value(r) for r in raw_rules)
        return cls(
            box_id=str(box_id),
            cards=cards,
            rules=rule_tuple,
            work_id=None if work_id is None else str(work_id),
            copy_id=None if copy_id is None else str(copy_id),
        )

    def card(self, card_id: str) -> ScentCard:
        if card_id not in self.cards:
            raise StoryBoxError(
                f"盒子 {self.box_id} 没有实片 {card_id!r}；可用片：{sorted(self.cards)}"
            )
        return self.cards[card_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "box_id": self.box_id,
            "work_id": self.work_id,
            "copy_id": self.copy_id,
            "cards": [c.to_dict() for c in self.cards.values()],
            "rules": [r.to_dict() for r in self.rules],
        }


def coerce_box(box: Any, rules: Any = None) -> ScentBox:
    return ScentBox.from_value(box, rules)


# ---------------------------------------------------------------------------
# 翻页记录与逐拍察觉
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reveal:
    beat: int
    card: str
    reader: str | None = None
    page: str | None = None

    @classmethod
    def from_value(cls, value: Any, default_reader: str | None = None) -> "Reveal":
        if isinstance(value, Reveal):
            return value
        d = _mapping(value)
        beat = _pick(d, ("beat", "at", "time", "t", "拍", "拍点"))
        card = _pick(d, ("card", "card_id", "piece", "香片", "实片", "牌"))
        if beat is None or card is None:
            raise StoryBoxError(f"翻页记录缺少 beat/card: {value!r}")
        return cls(
            beat=int(beat),
            card=str(card),
            reader=None if _pick(d, ("reader", "reader_id", "seat", "name", "读者", "座")) is None
            else str(_pick(d, ("reader", "reader_id", "seat", "name", "读者", "座"))),
            page=None if _pick(d, ("page", "page_id", "页", "页码")) is None
            else str(_pick(d, ("page", "page_id", "页", "页码"))),
        )


def coerce_record(record: Any, default_reader: str | None = None) -> list[Reveal]:
    if record is None:
        return []
    if isinstance(record, Mapping):
        # {0: "青", 2: "白"} 或 {"turns": [...]}
        if "turns" in record or "record" in record:
            return coerce_record(record.get("turns") or record.get("record"), default_reader)
        out = []
        for beat, card in record.items():
            if isinstance(card, Mapping):
                d = dict(card)
                d.setdefault("beat", beat)
                out.append(Reveal.from_value(d, default_reader))
            else:
                out.append(Reveal(int(beat), str(card), default_reader))
        return sorted(out, key=lambda r: (r.beat, r.reader or "", r.card))
    return sorted(
        (Reveal.from_value(x, default_reader) for x in record),
        key=lambda r: (r.beat, r.reader or "", r.card),
    )


def active_residues(
    box: Any,
    at_beat: int,
    history: Iterable[Reveal | Mapping],
    reader: str | None = None,
) -> list[dict[str, Any]]:
    """返回第 ``at_beat`` 拍仍未散的鼻端残留。"""
    b = coerce_box(box)
    residues: list[dict[str, Any]] = []
    for raw in history:
        rv = raw if isinstance(raw, Reveal) else Reveal.from_value(raw)
        if rv.beat >= at_beat:
            continue
        if reader is not None and rv.reader is not None and rv.reader != reader:
            continue
        card = b.card(rv.card)
        age = at_beat - rv.beat
        if age <= card.linger:
            for label in card.labels:
                residues.append(
                    {
                        "reader": rv.reader,
                        "source_beat": rv.beat,
                        "source_card": rv.card,
                        "label": label,
                        "age": age,
                        "linger": card.linger,
                        "remaining_beats": max(0, card.linger - age),
                        "expires_after_beat": rv.beat + card.linger,
                    }
                )
    return residues


def analyze_turn(
    box: Any,
    beat: int,
    card_id: str,
    history: Iterable[Reveal | Mapping] = (),
    reader: str | None = None,
    page: str | None = None,
) -> dict[str, Any]:
    """计算一位读者在一拍揭牌后实际能察觉的标签，并给出遮蔽证据。"""
    b = coerce_box(box)
    card = b.card(str(card_id))
    hist = list(history)
    blockers: list[dict[str, Any]] = []
    blocked_labels: set[str] = set()

    for residue in active_residues(b, beat, hist, reader):
        age = int(residue["age"])
        old_label = str(residue["label"])
        for rule in b.rules:
            if not rule.matches(age, old_label, card.labels):
                continue
            if rule.hidden not in card.labels:
                continue
            blocked_labels.add(rule.hidden)
            blockers.append(
                {
                    "label": rule.hidden,
                    "blocked_by": old_label,
                    "source_beat": residue["source_beat"],
                    "source_card": residue["source_card"],
                    "reader": residue["reader"],
                    "age": age,
                    "lag": age,
                    "rule_id": rule.rule_id,
                    "rule": rule.to_dict(),
                    "explanation": (
                        f"第{residue['source_beat']}拍 {residue['source_card']} "
                        f"牌的{old_label}味残留（时差{age}拍）遮掉本拍{rule.hidden}"
                    ),
                }
            )

    perceived = set(card.labels) - blocked_labels
    return {
        "beat": int(beat),
        "reader": reader,
        "page": page,
        "card": card.id,
        "labels": set(card.labels),
        "all_labels": set(card.labels),
        "perceived": perceived,
        "detected": perceived,
        "actual_labels": perceived,
        "blocked": blocked_labels,
        "blockers": blockers,
        "maskings": blockers,
        "residues": active_residues(b, beat, hist, reader),
    }


def replay_record(
    box: Any,
    record: Any,
    reader: str | None = None,
) -> dict[str, Any]:
    """重放一段翻页记录。

    当记录中有 ``reader`` 字段时，默认按读者分别计算残留；显式传 ``reader``
    可只取一条故事线（无 reader 字段的旧记录也按该读者处理）。
    """
    b = coerce_box(box)
    reveals = coerce_record(record, reader)
    turns: list[dict[str, Any]] = []
    all_maskings: list[dict[str, Any]] = []

    if reader is not None:
        selected = [
            r for r in reveals
            if r.reader == reader or (r.reader is None and not any(x.reader for x in reveals))
        ]
        history: list[Reveal] = []
        for rv in selected:
            turn = analyze_turn(b, rv.beat, rv.card, history, reader, rv.page)
            turns.append(turn)
            all_maskings.extend(turn["blockers"])
            history.append(rv)
        perceived = {t["beat"]: set(t["perceived"]) for t in turns}
        return {
            "status": "ok",
            "box_id": b.box_id,
            "reader": reader,
            "turns": turns,
            "perceived": perceived,
            "maskings": all_maskings,
        }

    readers = sorted({r.reader for r in reveals if r.reader is not None})
    if not readers:
        # 单读者记录：所有事件属于同一条鼻端残留线。
        return _replay_single(b, reveals, None)

    by_reader: dict[str, dict[str, Any]] = {}
    for rid in readers:
        rr = [r for r in reveals if r.reader == rid]
        by_reader[rid] = replay_record(b, rr, rid)
        all_maskings.extend(by_reader[rid]["maskings"])
    return {
        "status": "ok",
        "box_id": b.box_id,
        "readers": {rid: result["perceived"] for rid, result in by_reader.items()},
        "reader_results": by_reader,
        "turns": [t for result in by_reader.values() for t in result["turns"]],
        "maskings": all_maskings,
    }


def _replay_single(box: ScentBox, reveals: list[Reveal], reader: str | None) -> dict[str, Any]:
    history: list[Reveal] = []
    turns: list[dict[str, Any]] = []
    masks: list[dict[str, Any]] = []
    for rv in reveals:
        turn = analyze_turn(box, rv.beat, rv.card, history, reader, rv.page)
        turns.append(turn)
        masks.extend(turn["blockers"])
        history.append(rv)
    return {
        "status": "ok",
        "box_id": box.box_id,
        "reader": reader,
        "turns": turns,
        "perceived": {t["beat"]: set(t["perceived"]) for t in turns},
        "maskings": masks,
    }


# ---------------------------------------------------------------------------
# 页单和本地可行 schedule
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    id: str
    need: frozenset[str] = frozenset()
    cards: frozenset[str] = frozenset()
    beats: tuple[int, ...] | None = None
    free: bool = False
    need_any: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()
    no_early: Mapping[str, int] = field(default_factory=dict)
    window: tuple[int, int] | None = None

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "Page":
        if isinstance(value, Page):
            return value
        if isinstance(value, str):
            return cls(id=value, need=frozenset({value}))
        d = _mapping(value)
        pid = str(_pick(d, ("id", "page_id", "page", "name", "页"), f"p{index}"))
        need = _pick(d, ("need", "needs", "require", "requires", "label", "labels",
                         "must_smell", "target", "读到", "指定标签"), ())
        need_any = _pick(d, ("need_any", "require_any", "any_label", "任一标签"), ())
        cards = _pick(d, ("cards", "usable_cards", "allowed_cards", "choices_cards",
                          "可用牌", "规定牌"), ())
        beats = _pick(d, ("beats", "allowed_beats", "beat_options", "times",
                          "可用拍", "时窗拍点"))
        fixed_beat = _pick(d, ("beat", "at", "time", "固定拍"))
        if beats is None and fixed_beat is not None:
            beats = fixed_beat
        window = _pick(d, ("window", "beat_window", "时窗"))
        win_tuple: tuple[int, int] | None = None
        if window is not None:
            if isinstance(window, Mapping):
                lo = _pick(window, ("start", "lo", "earliest", "from", "begin"), 0)
                hi = _pick(window, ("end", "hi", "latest", "to", "deadline"), HORIZON - 1)
                win_tuple = (int(lo), int(hi))
            else:
                win_tuple = (int(window[0]), int(window[1]))
        else:
            lo = _pick(d, ("earliest", "start", "min_beat"))
            hi = _pick(d, ("latest", "deadline", "end", "max_beat"))
            if lo is not None or hi is not None:
                win_tuple = (int(lo or 0), int(hi if hi is not None else HORIZON - 1))

        options = d.get("options") or d.get("choices")
        option_beats: list[int] = []
        option_cards: list[str] = []
        if options:
            for opt in options:
                od = _mapping(opt)
                b = _pick(od, ("beat", "at", "time"))
                if b is not None:
                    option_beats.append(int(b))
                c = _pick(od, ("card", "card_id", "piece"))
                if c is not None:
                    option_cards.append(str(c))
            if beats is None and option_beats:
                beats = option_beats
            if not cards and option_cards:
                cards = option_cards

        free = bool(_pick(d, ("free", "is_free", "paid", "reader_choice",
                              "adversarial", "自由揭", "付费自由页"), False))
        no_early = d.get("no_early") or d.get("not_before") or {}
        if not isinstance(no_early, Mapping):
            no_early = {str(x): 0 for x in _as_tuple(no_early)}
        return cls(
            id=pid,
            need=frozenset(str(x) for x in _as_tuple(need)),
            need_any=frozenset(str(x) for x in _as_tuple(need_any)),
            cards=frozenset(str(x) for x in _as_tuple(cards) or option_cards),
            beats=_as_int_tuple(beats),
            free=free,
            forbidden=frozenset(
                str(x) for x in _as_tuple(
                    _pick(d, ("forbidden", "forbidden_labels", "must_not_smell",
                              "cannot_smell", "禁闻"), ())
                )
            ),
            no_early={str(k): int(v) for k, v in dict(no_early).items()},
            window=win_tuple,
        )

    def candidate_beats(self, horizon: int) -> tuple[int, ...]:
        if self.beats is not None:
            return tuple(b for b in self.beats if 0 <= b < horizon)
        lo, hi = self.window or (0, horizon - 1)
        return tuple(range(max(0, lo), min(horizon - 1, hi) + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "need": sorted(self.need),
            "need_any": sorted(self.need_any),
            "cards": sorted(self.cards),
            "beats": list(self.beats) if self.beats is not None else None,
            "window": self.window,
            "free": self.free,
            "forbidden": sorted(self.forbidden),
            "no_early": dict(self.no_early),
        }


@dataclass(frozen=True)
class Reader:
    id: str
    pages: tuple[Page, ...]
    name: str | None = None
    early_bound: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "Reader":
        if isinstance(value, Reader):
            return value
        if isinstance(value, str):
            return cls(id=value, pages=())
        d = _mapping(value)
        rid = str(_pick(d, ("id", "reader_id", "seat", "name", "读者"), f"R{index}"))
        pages_raw = _pick(d, ("pages", "page_list", "sheet", "页单", "故事线"), [])
        pages = tuple(Page.from_value(p, i) for i, p in enumerate(pages_raw))
        early = d.get("early_bound") or d.get("no_early") or {}
        if not isinstance(early, Mapping):
            early = {str(x): 0 for x in _as_tuple(early)}
        return cls(
            id=rid,
            name=_pick(d, ("name", "reader_name", "姓名")),
            pages=pages,
            early_bound={str(k): int(v) for k, v in dict(early).items()},
        )


def normalize_readers(guide: Any = None, readers: Any = None) -> list[Reader]:
    if readers is None and guide is not None:
        g = _mapping(guide)
        readers = _pick(g, ("readers", "seats", "people", "读者", "两座"))
    if readers is None:
        return []
    if isinstance(readers, Mapping):
        readers = [dict(v, id=k) if isinstance(v, Mapping) else v for k, v in readers.items()]
    return [Reader.from_value(r, i) for i, r in enumerate(readers)]


def normalize_box(box: Any = None, guide: Any = None) -> ScentBox:
    if box is not None:
        return coerce_box(box)
    g = _mapping(guide)
    b = _pick(g, ("box", "storybox", "material", "盒子", "实盒资料"))
    if b is None:
        raise StoryBoxError("plan_guaranteed 需要 box 或 guide['box']")
    return coerce_box(b)


def adversarial_realizations(
    readers: list[Reader], guide: Any = None
) -> list[dict[tuple[str, str], int]]:
    """所有已付费自由页两拍选择的笛卡尔积。

    柜台不能只验证有利分支，所以这些选择全部作为对抗性 realization。
    也允许在 guide 中直接传 ``free_choices`` / ``realizations``。
    """
    g = _mapping(guide)
    explicit = g.get("realizations") or g.get("scenarios")
    if explicit:
        out = []
        for real in explicit:
            norm: dict[tuple[str, str], int] = {}
            for key, beat in dict(real).items():
                if isinstance(key, tuple):
                    norm[key] = int(beat)
                elif "." in str(key):
                    r, p = str(key).split(".", 1)
                    norm[(r, p)] = int(beat)
                else:
                    norm[("", str(key))] = int(beat)
            out.append(norm)
        return out

    variables: list[tuple[tuple[str, str], tuple[int, ...]]] = []
    # guide 可集中声明 free_pages=[{"reader":"A","page":"g","beats":[0,1]}]
    for fp in g.get("free_pages", []) or []:
        fd = _mapping(fp)
        rid = str(_pick(fd, ("reader", "seat", "reader_id"), ""))
        pid = str(_pick(fd, ("page", "page_id", "id"), ""))
        beats = _as_int_tuple(_pick(fd, ("beats", "options", "beat_options"), (0, 1))) or (0, 1)
        variables.append(((rid, pid), tuple(beats)))

    for r in readers:
        for p in r.pages:
            if p.free and p.beats and len(p.beats) > 1:
                key = (r.id, p.id)
                if not any(k == key for k, _ in variables):
                    variables.append((key, tuple(p.beats)))

    if not variables:
        return [{}]
    keys = [k for k, _ in variables]
    vals = [v for _, v in variables]
    return [dict(zip(keys, choice)) for choice in product(*vals)]


def _real_beat(page: Page, reader_id: str, real: Mapping[tuple[str, str], int]) -> tuple[int, ...]:
    chosen = real.get((reader_id, page.id))
    if chosen is None:
        # 也允许字符串键，便于外部直接构造 {"A.green": 0}
        chosen = real.get(f"{reader_id}.{page.id}")
    if chosen is None:
        return page.beats or ()
    return (int(chosen),)


def _first_need_page(readers: Reader) -> dict[str, int]:
    first: dict[str, int] = {}
    for i, p in enumerate(readers.pages):
        for label in p.need:
            first.setdefault(label, i)
    return first


def local_schedules(
    box: Any,
    reader: Reader | Mapping,
    realization: Mapping[tuple[str, str], int | str] | None = None,
    horizon: int = HORIZON,
    fixed_cards: Mapping[str, str] | None = None,
    history: Iterable[Reveal | Mapping] = (),
    limit: int = MAX_SCHEDULES,
) -> list[dict[str, Any]]:
    """枚举/搜索一位读者在固定自由选择下能读完全部页单的安排。"""
    b = coerce_box(box)
    r = Reader.from_value(reader)
    real = dict(realization or {})
    fixed = dict(fixed_cards or {})
    prior_reveals = [x if isinstance(x, Reveal) else Reveal.from_value(x, r.id) for x in history]
    first_need = _first_need_page(r)
    results: list[dict[str, Any]] = []

    # 已发生历史占用的最高拍点；续办时后续页必须在其后。
    min_beat = max((x.beat for x in prior_reveals), default=-1)

    def dfs(i: int, after_beat: int, actions: list[dict[str, Any]], reveals: list[Reveal]) -> None:
        if len(results) >= limit:
            return
        if i >= len(r.pages):
            results.append(
                {
                    "reader": r.id,
                    "name": r.name,
                    "actions": [dict(a) for a in actions],
                    "promise": tuple(
                        sorted((r.id, a["page"], a["card"]) for a in actions)
                    ),
                    "beats": tuple(a["beat"] for a in actions),
                    "complete": True,
                }
            )
            return

        page = r.pages[i]
        beats = _real_beat(page, r.id, real)
        if not beats:
            beats = page.candidate_beats(horizon)
        cards = (fixed[page.id],) if page.id in fixed else tuple(
            c for c in (page.cards or tuple(b.cards)) if c in b.cards
        )
        if not cards:
            return

        for beat in beats:
            # 页单严格依次；续办时第一页也要晚于全部已发生历史。
            if beat <= after_beat:
                continue
            if beat >= horizon:
                continue
            for card_id in cards:
                rv = Reveal(beat, card_id, r.id, page.id)
                turn = analyze_turn(b, beat, card_id, reveals + prior_reveals, r.id, page.id)
                perceived = set(turn["perceived"])

                if page.need and not page.need.issubset(perceived):
                    continue
                if page.need_any and not (page.need_any & perceived):
                    continue
                if page.forbidden & perceived:
                    continue

                early_bad = False
                for label in perceived:
                    bound = page.no_early.get(label, r.early_bound.get(label))
                    if bound is not None and beat < int(bound):
                        early_bad = True
                        break
                    first_idx = first_need.get(label)
                    if first_idx is not None and i < first_idx:
                        early_bad = True
                        break
                if early_bad:
                    continue

                action = {
                    "reader": r.id,
                    "page": page.id,
                    "beat": beat,
                    "card": card_id,
                    "perceived": set(perceived),
                    "blocked": set(turn["blocked"]),
                    "blockers": list(turn["blockers"]),
                }
                actions.append(action)
                reveals.append(rv)
                dfs(i + 1, beat, actions, reveals)
                reveals.pop()
                actions.pop()
                if len(results) >= limit:
                    return

    dfs(0, min_beat, [], [])
    return results


# ---------------------------------------------------------------------------
# 两座物理资源约束与联合排程
# ---------------------------------------------------------------------------

def compatibility_conflicts(
    schedules: Iterable[Mapping[str, Any]],
    mode: str = "tray",
    transfer_beats: int = 1,
    courier_capacity: int = 1,
    tray_locked: bool = True,
) -> list[dict[str, Any]]:
    """检查多个读者 schedule 合并后是否可由实物执行。"""
    mode = canonical_mode(mode)
    actions = [a for s in schedules for a in s.get("actions", [])]
    conflicts: list[dict[str, Any]] = []

    # 同一拍同一张实片不能给两个人。
    card_beat: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for a in actions:
        card_beat.setdefault((int(a["beat"]), str(a["card"])), []).append(a)
    for (beat, card), uses in sorted(card_beat.items()):
        readers = sorted({str(u["reader"]) for u in uses})
        if len(uses) > 1:
            conflicts.append(
                {
                    "kind": "card_busy",
                    "beat": beat,
                    "card": card,
                    "readers": readers,
                    "reason": f"第{beat}拍实片{card}同时被 {','.join(readers)} 使用",
                }
            )

    # 小盘业务：一只盘固定在一个座位，场中不换盘，因此同一张实片不能跨座。
    if mode == "tray" and tray_locked:
        by_reader_cards: dict[str, set[str]] = {}
        for a in actions:
            by_reader_cards.setdefault(str(a["reader"]), set()).add(str(a["card"]))
        rids = list(by_reader_cards)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                shared = by_reader_cards[rids[i]] & by_reader_cards[rids[j]]
                for card in sorted(shared):
                    conflicts.append(
                        {
                            "kind": "tray_card_crossing",
                            "card": card,
                            "readers": [rids[i], rids[j]],
                            "reason": f"小盘固定座位后，实片{card}不能在两座间换盘",
                        }
                    )

    # 同一张片相邻两次使用若换座，必须满足转手拍数。
    uses_by_card: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        uses_by_card.setdefault(str(a["card"]), []).append(a)
    for card, uses in uses_by_card.items():
        uses = sorted(uses, key=lambda a: (int(a["beat"]), str(a["reader"])))
        for prev, nxt in zip(uses, uses[1:]):
            if prev["reader"] == nxt["reader"]:
                continue
            gap = int(nxt["beat"]) - int(prev["beat"])
            if gap < int(transfer_beats):
                conflicts.append(
                    {
                        "kind": "transfer_too_fast",
                        "card": card,
                        "from_reader": prev["reader"],
                        "to_reader": nxt["reader"],
                        "from_beat": prev["beat"],
                        "to_beat": nxt["beat"],
                        "gap": gap,
                        "required_gap": transfer_beats,
                        "reason": (
                            f"实片{card}从{prev['reader']}座转到{nxt['reader']}座"
                            f"只有{gap}拍，不足规定的{transfer_beats}拍"
                        ),
                    }
                )

    # 现场送片：唯一送片员每拍只能到一座，且不能提前放片。
    if mode == "delivery":
        by_beat: dict[int, set[str]] = {}
        for a in actions:
            by_beat.setdefault(int(a["beat"]), set()).add(str(a["reader"]))
        for beat, seats in sorted(by_beat.items()):
            if len(seats) > courier_capacity:
                conflicts.append(
                    {
                        "kind": "courier_busy",
                        "beat": beat,
                        "seats": sorted(seats),
                        "capacity": courier_capacity,
                        "reason": f"第{beat}拍送片员无法同时到 {','.join(sorted(seats))} 座",
                    }
                )
    return conflicts


def _actions_from_run(run: Any, actions: Any = None, schedules: Any = None) -> list[dict[str, Any]]:
    if actions is not None:
        raw_actions = list(actions)
    elif schedules is not None:
        raw_actions = [a for s in schedules for a in s.get("actions", [])]
    elif isinstance(run, Mapping):
        if run.get("schedules"):
            raw_actions = [a for s in run["schedules"] for a in s.get("actions", [])]
        elif run.get("actions"):
            raw_actions = list(run["actions"])
        elif run.get("branches"):
            # 显式校验合同时默认校验每个分支；调用方也可传入 branch。
            raw_actions = [
                a for br in run["branches"]
                for s in br.get("schedules", []) for a in s.get("actions", [])
            ]
        else:
            raw_actions = []
    elif isinstance(run, (list, tuple)):
        raw_actions = list(run)
    else:
        raw_actions = []
    return [dict(a) for a in raw_actions]


def verify_run(
    box: Any = None,
    run: Any = None,
    *,
    actions: Any = None,
    schedules: Any = None,
    readers: Any = None,
    guide: Any = None,
    mode: str = "tray",
    transfer_beats: int = 1,
    courier_capacity: int = 1,
    tray_locked: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """核对一张发片/转交/翻页表是否真的可执行且每页闻到指定标签。"""
    if (
        actions is None and schedules is None and isinstance(run, Mapping)
        and run.get("branches") and not run.get("actions") and not run.get("schedules")
    ):
        effective_box = box if box is not None else _mapping(run).get("box")
        if "branch" in kwargs:
            chosen = kwargs["branch"]
            branch_schedules = chosen.get("schedules", []) if isinstance(chosen, Mapping) else chosen
            return verify_run(
                effective_box, schedules=branch_schedules, readers=readers, guide=guide,
                mode=mode, transfer_beats=transfer_beats,
                courier_capacity=courier_capacity, tray_locked=tray_locked,
            )
        if "branch_index" in kwargs:
            idx = int(kwargs["branch_index"])
            return verify_run(
                effective_box, schedules=run["branches"][idx].get("schedules", []),
                readers=readers, guide=guide, mode=mode,
                transfer_beats=transfer_beats, courier_capacity=courier_capacity,
                tray_locked=tray_locked,
            )
        branch_results = [
            verify_run(
                effective_box, schedules=br.get("schedules", []), readers=readers, guide=guide,
                mode=mode, transfer_beats=transfer_beats,
                courier_capacity=courier_capacity, tray_locked=tray_locked,
            )
            for br in run["branches"]
        ]
        ok = all(r["ok"] for r in branch_results)
        return {
            "ok": ok,
            "status": "ok" if ok else "rejected",
            "branch_results": branch_results,
        }

    b = normalize_box(box, guide if box is None else None) if box is not None or guide is not None else coerce_box(
        _mapping(run).get("box") or _mapping(run).get("box_id")
    )
    if isinstance(box, str):
        # 允许 verify_run("BOX", run, desk=...) 的简单形式由调用方先 load。
        b = coerce_box(box)
    reader_specs = {r.id: r for r in normalize_readers(guide, readers)}
    raw_actions = _actions_from_run(run, actions, schedules)
    norm_actions: list[dict[str, Any]] = []
    for i, a in enumerate(raw_actions):
        d = _mapping(a)
        norm_actions.append(
            {
                "reader": str(_pick(d, ("reader", "seat", "reader_id"), f"R{i}")),
                "page": None if _pick(d, ("page", "page_id")) is None else str(_pick(d, ("page", "page_id"))),
                "beat": int(_pick(d, ("beat", "at", "time"))),
                "card": str(_pick(d, ("card", "card_id", "piece"))),
            }
        )

    errors: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    by_reader: dict[str, list[dict[str, Any]]] = {}
    for a in sorted(norm_actions, key=lambda x: (x["beat"], x["reader"])):
        rid = a["reader"]
        prior = [
            Reveal(x["beat"], x["card"], rid, x.get("page"))
            for x in by_reader.get(rid, [])
        ]
        try:
            turn = analyze_turn(b, a["beat"], a["card"], prior, rid, a.get("page"))
        except StoryBoxError as exc:
            errors.append({"kind": "unknown_card", "action": a, "reason": str(exc)})
            continue
        page_spec = None
        if rid in reader_specs and a.get("page"):
            page_spec = next((p for p in reader_specs[rid].pages if p.id == a["page"]), None)
        if page_spec is not None:
            missing = page_spec.need - turn["perceived"]
            if missing:
                errors.append(
                    {
                        "kind": "required_label_masked",
                        "reader": rid,
                        "page": page_spec.id,
                        "beat": a["beat"],
                        "card": a["card"],
                        "missing": sorted(missing),
                        "blockers": turn["blockers"],
                    }
                )
            forbidden = page_spec.forbidden & turn["perceived"]
            if forbidden:
                errors.append(
                    {
                        "kind": "forbidden_label_smelled",
                        "reader": rid,
                        "page": page_spec.id,
                        "beat": a["beat"],
                        "labels": sorted(forbidden),
                    }
                )
        action_result = {**a, "perceived": set(turn["perceived"]), "blockers": turn["blockers"]}
        by_reader.setdefault(rid, []).append(action_result)
        turns.append(turn)

    schedule_like = [{"reader": rid, "actions": acts} for rid, acts in by_reader.items()]
    conflicts = compatibility_conflicts(
        schedule_like, mode,
        int(kwargs.get("transfer_beats", transfer_beats)),
        int(kwargs.get("courier_capacity", courier_capacity)),
        bool(kwargs.get("tray_locked", tray_locked)),
    )
    ok = not errors and not conflicts
    return {
        "ok": ok,
        "status": "ok" if ok else "rejected",
        "box_id": b.box_id,
        "turns": turns,
        "perceived": {
            rid: {a["beat"]: set(a["perceived"]) for a in acts}
            for rid, acts in by_reader.items()
        },
        "errors": errors,
        "conflicts": conflicts,
    }


verify_tables = verify_run
verify_schedule = verify_run
check_run = verify_run


def _matches_promise(schedule: Mapping[str, Any], promise: tuple) -> bool:
    target = {(r, p): c for r, p, c in promise}
    return all(target.get((a["reader"], a["page"])) == a["card"] for a in schedule["actions"])


def _normalize_locks(locks: Any) -> list[dict[str, Any]]:
    """预约/取件台已经锁住的实片时点；排程不能把它当成可用片。"""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(locks or []):
        d = _mapping(item)
        beat = _pick(d, ("beat", "at", "time"))
        card = _pick(d, ("card", "card_id", "piece"))
        if beat is None or card is None:
            continue
        out.append({
            "reader": str(_pick(d, ("reader", "seat", "holder", "name"), f"lock-{i}")),
            "page": _pick(d, ("page", "page_id")),
            "beat": int(beat),
            "card": str(card),
        })
    return out


def iter_joint_plans(
    box: ScentBox,
    readers: list[Reader],
    realization: Mapping[tuple[str, str], int],
    mode: str,
    promise: tuple | None = None,
    horizon: int = HORIZON,
    transfer_beats: int = 1,
    courier_capacity: int = 1,
    tray_locked: bool = True,
    locks: Any = (),
    limit: int = MAX_SCHEDULES,
):
    """枚举兼容的联合安排；robust 搜索用它尝试不同的实片 promise。"""
    schedules_by_reader: list[list[dict[str, Any]]] = []
    fixed_by_reader: dict[str, dict[str, str]] = {}
    if promise:
        for rid, pid, cid in promise:
            fixed_by_reader.setdefault(rid, {})[pid] = cid

    for r in readers:
        fixed = fixed_by_reader.get(r.id)
        ss = local_schedules(box, r, realization, horizon, fixed_cards=fixed)
        if promise is not None:
            ss = [s for s in ss if _matches_promise(s, promise)]
        if not ss:
            return
        schedules_by_reader.append(ss)

    lock_schedules = [
        {"reader": lock["reader"], "actions": [lock]}
        for lock in _normalize_locks(locks)
    ]

    yielded = 0
    for combo in product(*schedules_by_reader):
        conflicts = compatibility_conflicts(
            [*combo, *lock_schedules], mode, transfer_beats, courier_capacity, tray_locked
        )
        if not conflicts:
            yield {"schedules": list(combo), "conflicts": []}
            yielded += 1
            if yielded >= limit:
                return


def find_joint_plan(
    box: ScentBox,
    readers: list[Reader],
    realization: Mapping[tuple[str, str], int],
    mode: str,
    promise: tuple | None = None,
    horizon: int = HORIZON,
    transfer_beats: int = 1,
    courier_capacity: int = 1,
    tray_locked: bool = True,
    locks: Any = (),
) -> dict[str, Any] | None:
    """找一组兼容的多读者 schedule；给定 promise 时实片选择必须一致。"""
    return next(
        iter_joint_plans(
            box, readers, realization, mode, promise, horizon,
            transfer_beats, courier_capacity, tray_locked, locks, limit=1,
        ),
        None,
    )


def _promised_cards(promise: tuple) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for rid, _pid, cid in promise:
        if cid not in out.setdefault(rid, []):
            out[rid].append(cid)
    return out


def plan_guaranteed(
    box: Any = None,
    guide: Any = None,
    readers: Any = None,
    seats: list[str] | None = None,
    mode: str = "tray",
    horizon: int = HORIZON,
    transfer_beats: int = 1,
    courier_capacity: int = 1,
    tray_locked: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """事先保证所选具名座位读完。

    对所有对抗性自由页选择都必须存在可执行安排，并且各分支许诺给座位的实片
    身份（promise）保持一致。返回第一个按读者/页顺序搜索到的安排。
    """
    b = normalize_box(box, guide)
    g = _mapping(guide)
    all_readers = normalize_readers(g, readers)
    if seats is not None:
        wanted = set(seats)
        selected = [r for r in all_readers if r.id in wanted]
        # 保持调用方给定的具名顺序。
        order = {rid: i for i, rid in enumerate(seats)}
        selected.sort(key=lambda r: order.get(r.id, 999))
    else:
        selected = all_readers

    horizon = int(extra.get("max_beats", g.get("horizon", g.get("max_beats", horizon))))
    transfer_beats = int(_pick(g, ("transfer_beats", "pass_beats", "min_card_gap"), transfer_beats))
    courier_capacity = int(_pick(g, ("courier_capacity", "deliverers"), courier_capacity))
    mode = canonical_mode(_pick(g, ("mode", "service_mode", "method", "带读法"), mode))
    if "tray_locked" in g:
        tray_locked = bool(g["tray_locked"])
    locks = _normalize_locks(
        extra.get("locks", _pick(g, ("locks", "card_locks", "locked_cards", "reservations"), ()))
    )

    if not selected:
        return {
            "status": "infeasible",
            "ok": False,
            "mode": mode,
            "seats": [],
            "reason": "no_readers",
            "branches": [],
        }

    realisations = adversarial_realizations(all_readers, g)
    # realization 可能包含未选座位的自由页；保留即可，_real_beat 按读者取。
    base = realisations[0]

    tried_promises: list[tuple] = []
    base_found = False

    # 一个 promise 是所有入选座位全部页/实片身份的联合元组。逐个尝试 base
    # realization 中可行的不同实片组合，避免只取第一组而漏掉跨自由选择稳健的方案。
    for base_joint in iter_joint_plans(
        b, selected, base, mode, None, horizon, transfer_beats,
        courier_capacity, tray_locked, locks, limit=MAX_SCHEDULES,
    ):
        base_found = True
        promise = tuple(
            sorted(item for s in base_joint["schedules"] for item in s["promise"])
        )
        if not promise or promise in tried_promises:
            continue
        tried_promises.append(promise)

        branches: list[dict[str, Any]] = []
        all_ok = True
        for real in realisations:
            joint = find_joint_plan(
                b, selected, real, mode, promise, horizon, transfer_beats,
                courier_capacity, tray_locked, locks,
            )
            if joint is None:
                all_ok = False
                break
            choices = {
                f"{rid}.{pid}": beat for (rid, pid), beat in real.items()
                if any(r.id == rid for r in selected)
            }
            branches.append(
                {
                    "choices": choices,
                    "realization": {f"{rid}.{pid}": beat for (rid, pid), beat in real.items()},
                    "schedules": joint["schedules"],
                    "conflicts": joint["conflicts"],
                }
            )
        if all_ok:
            return {
                "status": "guaranteed",
                "ok": True,
                "mode": mode,
                "box_id": b.box_id,
                "box": b,
                "seats": [r.id for r in selected],
                "promise": list(promise),
                "promised_cards": _promised_cards(promise),
                "branches": branches,
                "free_choice_count": len(realisations),
                "transfer_beats": transfer_beats,
                "courier_capacity": courier_capacity,
                "tray_locked": tray_locked,
            }

    if not base_found:
        # 给出单读者层面的首个失败原因，方便柜台解释。
        diagnostics = _diagnose(b, selected, base, horizon)
        return {
            "status": "infeasible",
            "ok": False,
            "mode": mode,
            "seats": [r.id for r in selected],
            "reason": diagnostics.get("reason", "no_executable_joint_plan"),
            "diagnostics": diagnostics,
            "branches": [],
        }

    return {
        "status": "not_guaranteed",
        "ok": False,
        "mode": mode,
        "box_id": b.box_id,
        "box": b,
        "seats": [r.id for r in selected],
        "reason": "some_reader_choice_breaks_plan",
        "branches": [],
        "tried_promises": [list(p) for p in tried_promises],
    }


def _diagnose(box: ScentBox, readers: list[Reader], real: Mapping, horizon: int) -> dict[str, Any]:
    for r in readers:
        ss = local_schedules(box, r, real, horizon, limit=1)
        if not ss:
            # 找到第一页无法满足的具体标签/牌，供演示和错误说明。
            for p in r.pages:
                for beat in _real_beat(p, r.id, real) or p.candidate_beats(horizon):
                    for cid in p.cards or tuple(box.cards):
                        try:
                            turn = analyze_turn(box, beat, cid, [], r.id, p.id)
                        except StoryBoxError:
                            continue
                        if p.need and not p.need.issubset(turn["perceived"]):
                            return {
                                "reason": "perception_blocked",
                                "reader": r.id,
                                "page": p.id,
                                "beat": beat,
                                "card": cid,
                                "missing": sorted(p.need - turn["perceived"]),
                                "blockers": turn["blockers"],
                            }
            return {"reason": "reader_program_infeasible", "reader": r.id}
    return {"reason": "physical_resource_conflict"}


def maximum_guaranteed_seats(
    box: Any = None,
    guide: Any = None,
    readers: Any = None,
    mode: str = "tray",
    **kwargs: Any,
) -> dict[str, Any]:
    """按预约单具名顺序求可事先保证读完的最多席位。"""
    b = normalize_box(box, guide)
    g = _mapping(guide)
    rs = normalize_readers(g, readers)
    ids = [r.id for r in rs]

    full = plan_guaranteed(b, g, rs, ids, mode, **kwargs)
    if full["ok"]:
        return {
            "status": "guaranteed",
            "max_seats": len(ids),
            "seats": ids,
            "options": [ids],
            "plan": full,
            "plans": {",".join(ids): full},
        }

    feasible_singletons: list[str] = []
    plans: dict[str, dict[str, Any]] = {}
    for rid in ids:
        plan = plan_guaranteed(b, g, rs, [rid], mode, **kwargs)
        if plan["ok"]:
            feasible_singletons.append(rid)
            plans[rid] = plan

    if feasible_singletons:
        return {
            "status": "partial",
            "max_seats": 1,
            "seats": [feasible_singletons[0]],
            "named_seats": feasible_singletons,
            "options": [[rid] for rid in feasible_singletons],
            "plan": plans[feasible_singletons[0]],
            "plans": plans,
            "full_plan": full,
        }
    return {
        "status": "infeasible",
        "max_seats": 0,
        "seats": [],
        "named_seats": [],
        "options": [],
        "plan": full,
        "plans": {},
        "full_plan": full,
    }


# 更贴近题面/常见英文命名的别名
guaranteed_seats = maximum_guaranteed_seats
maximum_named_seats = maximum_guaranteed_seats
plan_reading = plan_guaranteed
resolve_scents = replay_record
scents_at = analyze_turn


def continue_from_on_site_record(sheet: Mapping[str, Any], record: Iterable[Mapping[str, Any]],
                                 box: Any = None) -> dict[str, Any]:
    """用同一张已签单和一份现场追加记录直接交班续办。"""
    session = ReadingSession(sheet, box)
    for e in record:
        d = _mapping(e)
        session.record_actual(
            str(_pick(d, ("op_id", "id"), f"ev-{len(session.events)}")),
            int(_pick(d, ("beat", "at"))),
            str(_pick(d, ("reader", "seat"))),
            None if _pick(d, ("card", "card_id")) is None else str(_pick(d, ("card", "card_id"))),
            None if _pick(d, ("page", "page_id")) is None else str(_pick(d, ("page", "page_id"))),
        )
    return session.continue_remaining()


plan_session = plan_guaranteed


# ---------------------------------------------------------------------------
# 签约、小盘 epistemic state 与交班续办
# ---------------------------------------------------------------------------

def sign_booking(
    booking: Any = None,
    box: Any = None,
    readers: Any = None,
    mode: str | None = None,
    sheet_id: str | None = None,
    op_id: str | None = None,
    allow_partial: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """受理含若干具名席的预约单并选择一种带读办法签约。

    tray/小盘：可以在开场把实片放入各座小盘；签约前仍要求所有自由页分支可保证。
    delivery/现场送片：唯一送片员容量进入排程，因此两座同时揭牌会使最大保席降为 1。
    """
    b = normalize_box(box, booking) if box is not None or _mapping(booking).get("box") else coerce_box(box)
    g = _mapping(booking)
    service_mode = canonical_mode(mode or g.get("mode") or g.get("service_mode") or g.get("method") or "tray")
    max_result = maximum_guaranteed_seats(b, g, readers, service_mode, **kwargs)
    requested = [r.id for r in normalize_readers(g, readers)]
    guaranteed = max_result["seats"]
    status = "signed" if max_result["max_seats"] == len(requested) else (
        "partial" if max_result["max_seats"] else "rejected"
    )
    if status != "signed" and not allow_partial:
        status = "rejected"

    sheet = {
        "status": status,
        "ok": status in ("signed", "partial"),
        "sheet_id": sheet_id or g.get("sheet_id") or op_id,
        "op_id": op_id,
        "mode": service_mode,
        "requested_seats": requested,
        "signed_seats": guaranteed if allow_partial else [],
        "guaranteed_seats": guaranteed,
        "alternative_seat_options": max_result.get("options", []),
        "promised_cards": max_result.get("plan", {}).get("promised_cards", {}),
        "guarantee": max_result,
        "box_id": b.box_id,
        "box": b,
        "booking": g,
    }
    return sheet


sign_sheet = sign_booking


class ReadingSession:
    """一场已签带读的追加式现场记录与交班入口。"""

    def __init__(self, signed: Mapping[str, Any], box: Any = None) -> None:
        self.signed = dict(signed)
        guarantee = signed.get("guarantee", signed)
        if isinstance(guarantee, Mapping) and "branches" not in guarantee \
                and isinstance(guarantee.get("plan"), Mapping):
            guarantee = guarantee["plan"]
        self.guarantee = guarantee
        self.box = coerce_box(box or signed.get("box") or guarantee.get("box_id") or signed.get("box_id")) \
            if box is not None or signed.get("box") is not None else None
        self.mode = canonical_mode(signed.get("mode", "tray"))
        self.events: list[dict[str, Any]] = []
        self.returned_trays: set[str] = set()
        self._op_index: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ records
    def record_actual(
        self,
        op_id: str,
        beat: int,
        reader: str,
        card: str | None = None,
        page: str | None = None,
        observed: bool = True,
    ) -> dict[str, Any]:
        """追加一笔当班确实见到的揭牌/送片记录。

        同 op_id 同文重放返回原记录；异文抛 :class:`IdempotencyConflict`。
        不提供修改或删除入口，旧事件参与所有残留计算。
        """
        payload = {
            "op_id": op_id, "beat": int(beat), "reader": reader,
            "card": None if card is None else str(card),
            "page": None if page is None else str(page),
        }
        fp_payload = {k: v for k, v in payload.items() if k != "op_id"}
        if op_id in self._op_index:
            old = self._op_index[op_id]
            if {k: old[k] for k in fp_payload} != fp_payload:
                raise IdempotencyConflict(op_id, "actual_reading_event")
            return dict(old)

        # 同一读者同一页只能有一笔现场实际拍点。换拍“补记”会留下抹去旧残留
        # 的口子；同拍重复登记也没有第二份事实，必须走原 op_id 重放。
        for e in self.events:
            if e["reader"] == reader and page is not None and e.get("page") == page:
                raise StoryBoxError(
                    f"actual_already_recorded: {reader}/{page} 已由 {e['op_id']} "
                    f"记录在第{e['beat']}拍；不能用 {op_id} 再记一次或改拍"
                )

        # 现场送片的追加记录也要当场满足唯一送片员容量。
        if self.mode == "delivery":
            seats = {e["reader"] for e in self.events if e["beat"] == beat and e.get("observed", True)}
            if reader in seats or len(seats) >= 1:
                if reader not in seats:
                    raise StoryBoxError(
                        f"courier_busy: 第{beat}拍送片员已去 {sorted(seats)} 座，不能再到{reader}座"
                    )

        event = {
            **payload,
            "observed": bool(observed),
            "source": "on_site" if observed else "sealed_tray",
        }
        self.events.append(event)
        self._op_index[op_id] = event
        return dict(event)

    # 语义别名
    deliver = record_actual

    def preposition_card(self, *args: Any, **kwargs: Any) -> None:
        if self.mode == "delivery":
            raise StoryBoxError(
                "early_placement_forbidden: 现场送片必须在揭牌当拍到座，未到点不能先放"
            )
        # 小盘模式的开场放置由签约/托盘分配表达，也不允许通过该方法伪造时点。
        raise StoryBoxError("use sign_booking/promised_cards to assign tray cards before opening")

    def deliver_at_reveal(
        self, op_id: str, beat: int, reader: str, card: str,
        reveal_beat: int | None = None, page: str | None = None,
    ) -> dict[str, Any]:
        reveal = beat if reveal_beat is None else int(reveal_beat)
        if int(beat) != reveal:
            raise StoryBoxError(
                f"delivery_not_at_reveal: 送片到座拍 {beat} 不等于实际揭牌拍 {reveal}"
            )
        return self.record_actual(op_id, beat, reader, card, page, True)

    def return_tray(
        self,
        reader: str,
        actuals: Mapping[str, int] | Iterable[Mapping[str, Any]],
        op_id_prefix: str = "tray-return",
    ) -> list[dict[str, Any]]:
        """小盘归还时才登记实际揭牌拍；归还前这些时点不可知。"""
        events: list[dict[str, Any]] = []
        if isinstance(actuals, Mapping):
            items = [{"page": p, "beat": b} for p, b in actuals.items()]
        else:
            items = list(actuals)
        for i, item in enumerate(items):
            d = _mapping(item)
            beat = int(_pick(d, ("beat", "at")))
            page = _pick(d, ("page", "page_id", "id"))
            card = _pick(d, ("card", "card_id"))
            op_id = f"{op_id_prefix}-{reader}-{page or i}"
            events.append(
                self.record_actual(op_id, beat, reader, card, str(page) if page else None, True)
            )
        self.returned_trays.add(reader)
        return events

    def erase_event(self, *args: Any, **kwargs: Any) -> None:
        raise StoryBoxError("append_only_actual_log: 不能删除或事后抹掉已闻到的残留")

    def backdate_event(self, *args: Any, **kwargs: Any) -> None:
        raise StoryBoxError("retroactive_edit_forbidden: 不能补记/改记拍点来消除残留")

    # ------------------------------------------------------------ views
    def known_events(self) -> list[dict[str, Any]]:
        """接班人在当前交班点依法可知的事件。"""
        out = []
        for e in self.events:
            if self.mode == "tray" and e["reader"] not in self.returned_trays:
                # 未归还小盘里的实际拍点不能进入 known view。
                continue
            out.append(dict(e))
        return out

    def hidden_tray_facts(self) -> list[dict[str, Any]]:
        if self.mode != "tray":
            return []
        return [dict(e) for e in self.events if e["reader"] not in self.returned_trays]

    def possible_choices(self) -> list[dict[str, Any]]:
        branches = self.guarantee.get("branches", []) if isinstance(self.guarantee, Mapping) else []
        known = self.known_events()
        possible = []
        for br in branches:
            choices = dict(br.get("choices", {}))
            ok = True
            for e in known:
                # 已观测到的自由页拍点会消去不一致世界。
                for key, beat in choices.items():
                    rid, pid = key.split(".", 1)
                    if e["reader"] == rid and (e.get("page") in (pid, None)) and e["beat"] != beat:
                        ok = False
            if ok:
                possible.append(choices)
        return possible

    def decay_report(self, at_beat: int, reader: str | None = None) -> dict[str, Any]:
        if self.box is None:
            raise StoryBoxError("decay_report 需要 box 资料")
        known = self.known_events()
        history = [Reveal(e["beat"], e["card"], e["reader"], e.get("page"))
                   for e in known if e.get("card")]
        active = active_residues(self.box, at_beat, history, reader)
        return {
            "at_beat": at_beat,
            "reader": reader,
            "active_residues": active,
            "known_event_count": len(history),
            "note": "中断不重置留香；拍数差照常计算",
        }

    def handover_brief(self, at_beat: int | None = None) -> dict[str, Any]:
        """交班单：小盘未还时只含馆员早已知情的合同/时窗/实片身份。"""
        possible = self.possible_choices()
        return {
            "mode": self.mode,
            "known_events": self.known_events(),
            "hidden_tray_events": self.hidden_tray_facts(),
            "possible_choices": possible,
            "promised_cards": self.signed.get("promised_cards", {}),
            "requested_seats": self.signed.get("requested_seats", []),
            "guaranteed_seats": self.signed.get("guaranteed_seats", []),
            "trays_returned": sorted(self.returned_trays),
            "epistemic_rule": (
                "tray_actual_beat_unknown_until_return"
                if self.mode == "tray" else "delivery_actual_beat_observed"
            ),
            "decay": None if at_beat is None else self.decay_report(at_beat),
        }

    def brief_for_seat(self, reader: str, at_beat: int | None = None) -> dict[str, Any]:
        """给接班人/某一座的交代：只含该时点依法可知的事实。"""
        full = self.handover_brief(at_beat)
        common = self.continue_remaining(reader, at_beat) if at_beat is not None else None
        return {
            "reader": reader,
            "mode": full["mode"],
            "known_events": [
                e for e in full["known_events"] if e["reader"] == reader
            ],
            "promised_cards": self.signed.get("promised_cards", {}).get(reader, []),
            "possible_choices": full["possible_choices"],
            "tray_returned": reader in self.returned_trays,
            "common_remaining": None if common is None else common.get("common_remaining", common.get("remaining")),
            "do_not_use": [] if full["mode"] != "tray" or reader in self.returned_trays
            else ["other_seat_hidden_reveal_beat"],
            "decay": full["decay"],
        }

    # ------------------------------------------------------------ continue
    def continue_remaining(
        self,
        reader: str | None = None,
        after_beat: int | None = None,
        known_only: bool = True,
    ) -> dict[str, Any]:
        """从交班点继续余页。

        - delivery：实际时点已在 ``events``，选择与记录相符的已保证分支；
        - tray 且小盘未还：不猜实际拍，返回对所有可能世界都成立的余页；
        - tray 归还后：按实际拍点选分支。
        """
        branches = list(self.guarantee.get("branches", [])) if isinstance(self.guarantee, Mapping) else []
        if reader is not None:
            target_readers = [reader]
        else:
            target_readers = list(self.signed.get("guaranteed_seats", []))

        if self.mode == "tray" and known_only and any(r not in self.returned_trays for r in target_readers):
            possible = self.possible_choices()
            continuations = []
            for br in branches:
                if dict(br.get("choices", {})) in possible:
                    cont = _branch_remaining(br, target_readers, after_beat)
                    if cont:
                        continuations.append({"choices": br.get("choices", {}), "remaining": cont})
            common = _common_remaining(continuations)
            return {
                "status": "robust_until_tray_return",
                "mode": self.mode,
                "known_only": True,
                "possible_choice_count": len(continuations),
                "continuations": continuations,
                "common_remaining": common,
                "brief": self.handover_brief(after_beat),
            }

        known = self.known_events()
        chosen = None
        for br in branches:
            choices = br.get("choices", {})
            match = True
            for e in known:
                for key, beat in choices.items():
                    rid, pid = key.split(".", 1)
                    if e["reader"] == rid and e.get("page") == pid and e["beat"] != beat:
                        match = False
            if match:
                chosen = br
                break
        if chosen is None and branches:
            chosen = branches[0]
        if chosen is None:
            return {"status": "no_signed_branch", "remaining": []}
        remaining = _branch_remaining(chosen, target_readers, after_beat)
        return {
            "status": "ok",
            "mode": self.mode,
            "known_only": False,
            "choices": chosen.get("choices", {}),
            "remaining": remaining,
            "known_events": known,
        }


def _branch_remaining(
    branch: Mapping[str, Any], readers: list[str], after_beat: int | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sch in branch.get("schedules", []):
        if readers and sch.get("reader") not in readers:
            continue
        for a in sch.get("actions", []):
            if after_beat is not None and int(a["beat"]) <= int(after_beat):
                continue
            out.append(dict(a))
    return sorted(out, key=lambda a: (a["beat"], a["reader"]))


def _common_remaining(continuations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各未知分支中身份相同的余页安排（通常拍点在自由页之后已固定）。"""
    if not continuations:
        return []
    base = continuations[0]["remaining"]
    common = []
    for a in base:
        same = True
        for other in continuations[1:]:
            if not any(
                x["reader"] == a["reader"] and x["page"] == a["page"]
                and x["card"] == a["card"] and x["beat"] == a["beat"]
                for x in other["remaining"]
            ):
                same = False
                break
        if same:
            common.append(a)
    return common


# ---------------------------------------------------------------------------
# 与预约/取件回执和 SQLite 连接衔接的柜台
# ---------------------------------------------------------------------------

class StoryBoxDesk:
    """用取件回执上的实盒号加载资料，并可持久化签约单。"""

    def __init__(self, system_or_conn: Any = None) -> None:
        if system_or_conn is None:
            self.conn: sqlite3.Connection | None = None
            self._boxes: dict[str, ScentBox] = {}
            self._sheets: dict[str, dict[str, Any]] = {}
        elif hasattr(system_or_conn, "conn"):
            self.conn = system_or_conn.conn
        else:
            self.conn = system_or_conn
        if self.conn is not None:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS storybox_boxes (
                    box_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS storybox_sheets (
                    sheet_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )

    def register_box(self, box: Any = None, *, replace: bool = False, **kwargs: Any) -> ScentBox:
        if box is None:
            box = kwargs
        sb = coerce_box(box)
        if self.conn is None:
            if not replace and sb.box_id in self._boxes:
                raise StoryBoxError(f"box_exists: {sb.box_id}")
            self._boxes[sb.box_id] = sb
            return sb
        exists = self.conn.execute(
            "SELECT 1 FROM storybox_boxes WHERE box_id=?", (sb.box_id,)
        ).fetchone()
        if exists and not replace:
            raise StoryBoxError(f"box_exists: {sb.box_id}")
        self.conn.execute(
            "INSERT OR REPLACE INTO storybox_boxes(box_id,payload) VALUES(?,?)",
            (sb.box_id, json.dumps(jsonable(sb.to_dict()), ensure_ascii=False)),
        )
        return sb

    def load_box(self, box_id: str) -> ScentBox:
        if self.conn is None:
            if box_id not in self._boxes:
                raise StoryBoxError(f"unknown_box: {box_id}")
            return self._boxes[box_id]
        row = self.conn.execute(
            "SELECT payload FROM storybox_boxes WHERE box_id=?", (box_id,)
        ).fetchone()
        if row is None:
            raise StoryBoxError(f"unknown_box: {box_id}")
        payload = row["payload"] if isinstance(row, sqlite3.Row) else row[0]
        return coerce_box(json.loads(payload))

    def replay(self, box_id: str, record: Any, reader: str | None = None) -> dict[str, Any]:
        """按实盒号加载试闻表并重放翻页记录。"""
        return replay_record(self.load_box(box_id), record, reader)

    def analyze(self, box_id: str, beat: int, card_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return analyze_turn(self.load_box(box_id), beat, card_id, *args, **kwargs)

    def load_by_receipt(self, receipt: Any) -> ScentBox:
        """receipt 可取 pickup 结果 dict、pickup 操作 ID 或 hold_id。"""
        copy_id: str | None = None
        if isinstance(receipt, Mapping):
            copy_id = _pick(receipt, ("copy", "copy_id", "box_id", "实盒号"))
            if receipt.get("status") and receipt.get("status") != "ok":
                raise StoryBoxError(f"receipt_not_successful: {receipt}")
        elif self.conn is not None:
            op_id = str(receipt)
            row = self.conn.execute(
                "SELECT result FROM receipts WHERE op_id=? AND kind='pickup' "
                "ORDER BY receipt_id DESC LIMIT 1",
                (op_id,),
            ).fetchone()
            if row is not None:
                result = json.loads(row["result"] if not isinstance(row, sqlite3.Row) else row["result"])
                if result.get("status") != "ok":
                    raise StoryBoxError(f"receipt_not_successful: {op_id}")
                copy_id = result.get("copy") or result.get("copy_id")
            else:
                hold = self.conn.execute(
                    "SELECT copy_id,status FROM reservations WHERE hold_id=?", (op_id,)
                ).fetchone()
                if hold is not None:
                    status = hold["status"] if isinstance(hold, sqlite3.Row) else hold[1]
                    if status != "fulfilled":
                        raise StoryBoxError(
                            f"hold_not_picked_up: {op_id} status={status}"
                        )
                    copy_id = hold["copy_id"] if isinstance(hold, sqlite3.Row) else hold[0]
        else:
            raise StoryBoxError("内存 StoryBoxDesk 不能按操作 ID 查回执，请传 pickup 结果 dict")

        if copy_id is None:
            raise StoryBoxError(f"unknown_receipt: {receipt!r}")
        return self.load_box(str(copy_id))

    def save_sheet(self, sheet: Mapping[str, Any]) -> Mapping[str, Any]:
        sid = sheet.get("sheet_id") or sheet.get("op_id")
        if not sid:
            raise StoryBoxError("sheet 缺少 sheet_id/op_id")
        payload = json.dumps(jsonable(dict(sheet)), ensure_ascii=False)
        if self.conn is None:
            self._sheets[str(sid)] = dict(sheet)
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO storybox_sheets(sheet_id,payload) VALUES(?,?)",
                (str(sid), payload),
            )
        return sheet

    def load_sheet(self, sheet_id: str) -> dict[str, Any]:
        if self.conn is None:
            return dict(self._sheets[sheet_id])
        row = self.conn.execute(
            "SELECT payload FROM storybox_sheets WHERE sheet_id=?", (sheet_id,)
        ).fetchone()
        if row is None:
            raise StoryBoxError(f"unknown_sheet: {sheet_id}")
        return json.loads(row["payload"] if not isinstance(row, sqlite3.Row) else row["payload"])

    def sign_booking(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sheet = sign_booking(*args, **kwargs)
        if sheet.get("sheet_id"):
            self.save_sheet(sheet)
        return sheet

    def open_session(self, sheet_or_id: Any, box: Any = None) -> ReadingSession:
        if isinstance(sheet_or_id, str):
            sheet = self.load_sheet(sheet_or_id)
        else:
            sheet = dict(sheet_or_id)
        return ReadingSession(sheet, box)


ScentDesk = StoryBoxDesk
GuideDesk = StoryBoxDesk
ReadingDesk = StoryBoxDesk


# ---------------------------------------------------------------------------
# 题面中的固定样例，便于演示和人工复核
# ---------------------------------------------------------------------------

def songli_box(box_id: str = "SONGLI", mask_through: int = 1) -> ScentBox:
    """青牌松、白牌梨；青味默认只遮下一拍（lag=1）。"""
    lags = frozenset(range(1, max(1, int(mask_through)) + 1))
    return ScentBox(
        box_id=box_id,
        cards={
            "青": ScentCard("青", frozenset({"松"}), linger=3, color="青"),
            "白": ScentCard("白", frozenset({"梨"}), linger=3, color="白"),
        },
        rules=(MaskRule(old="松", new="梨", hidden="梨", lags=lags, rule_id="song-to-pear"),),
    )


def basic_a_reader() -> Reader:
    return Reader(
        "甲",
        (
            Page("松页", frozenset({"松"}), frozenset({"青"}), (0,)),
            Page("梨页", frozenset({"梨"}), frozenset({"白"}), (2,)),
        ),
        name="甲",
    )


def locked_b_reader() -> Reader:
    return Reader(
        "乙",
        (Page("乙锁白", frozenset({"梨"}), frozenset({"白"}), (2,)),),
        name="乙",
    )


def transfer_demo_guide() -> dict[str, Any]:
    """两位各自可读通，但唯一白牌在合桌后来不及转手的样例。"""
    box = ScentBox(
        "TRANSFER",
        {
            "青甲": ScentCard("青甲", frozenset({"松"}), 3),
            "青乙": ScentCard("青乙", frozenset({"松"}), 3),
            "白": ScentCard("白", frozenset({"梨"}), 3),
        },
        (MaskRule(old="松", new="梨", hidden="梨", lags=frozenset({1})) ,),
    )
    readers = [
        Reader("甲", (
            Page("松1", frozenset({"松"}), frozenset({"青甲"}), (0,)),
            Page("梨1", frozenset({"梨"}), frozenset({"白"}), (2,)),
        )),
        Reader("乙", (
            Page("松2", frozenset({"松"}), frozenset({"青乙"}), (1,)),
            Page("梨2", frozenset({"梨"}), frozenset({"白"}), (3,)),
        )),
    ]
    return {
        "box": box,
        "readers": readers,
        "mode": "tray",
        "tray_locked": False,
        "transfer_beats": 2,
        "horizon": 6,
    }


def booking_demo_guide(white_beat: int = 3) -> dict[str, Any]:
    """含两席的预约单：甲的青页可在 0/1 拍自由揭。"""
    box = ScentBox(
        "BOOK2",
        {
            "青": ScentCard("青", frozenset({"松"}), 3),
            "白": ScentCard("白", frozenset({"梨"}), 3),
            "赭": ScentCard("赭", frozenset({"杏"}), 3),
        },
        (MaskRule(old="松", new="梨", hidden="梨", lags=frozenset({1})),),
    )
    readers = [
        Reader("甲", (
            Page(
                "青页", frozenset({"松"}), frozenset({"青"}), (0, 1), free=True
            ),
            Page(
                "白页", frozenset({"梨"}), frozenset({"白"}), (int(white_beat),)
            ),
        )),
        Reader("乙", (
            Page(
                "乙揭牌", frozenset({"杏"}), frozenset({"赭"}), (int(white_beat),)
            ),
        )),
    ]
    return {
        "box": box,
        "readers": readers,
        "free_pages": [{"reader": "甲", "page": "青页", "beats": [0, 1]}],
        "horizon": 6,
    }


__all__ = [
    "StoryBoxError", "jsonable", "canonical_mode",
    "ScentCard", "Card", "MaskRule", "MaskChart", "ScentBox",
    "Reveal", "Page", "Reader",
    "coerce_box", "coerce_record", "normalize_readers",
    "active_residues", "analyze_turn", "replay_record",
    "local_schedules", "compatibility_conflicts",
    "verify_run", "verify_tables", "verify_schedule", "check_run",
    "plan_guaranteed", "maximum_guaranteed_seats",
    "guaranteed_seats", "plan_session", "maximum_named_seats", "plan_reading",
    "resolve_scents", "scents_at", "ScentDesk", "GuideDesk", "ReadingDesk",
    "continue_from_on_site_record",
    "sign_booking", "sign_sheet", "ReadingSession",
    "StoryBoxDesk",
    "songli_box", "basic_a_reader", "locked_b_reader",
    "transfer_demo_guide", "booking_demo_guide",
]
