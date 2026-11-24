"""Stenography card proofreading: ligature cuts, stroke semantics and readings.

Domain model (fixed by the task, everything is recorded locally as strings)

* A *manual* (手册) owns a lexicon: every ligature (连笔, one or more gesture
  symbols) can be cut into one or more alternative short-stroke sequences.
  At most six distinct short strokes exist; a manuscript carries at most
  twenty gesture symbols.
* Every short stroke (短笔形) explains exactly one editor command in the whole
  manual.  Assigning a second meaning to a stroke is an explicit replacement,
  never a silent override.
* A practice card (练习卡) couples a gesture string with the command string
  actually printed on its proof.  A card is supported only when a *complete*
  cut of the whole gesture string translates, stroke by stroke, into the whole
  printed string.  Readings that only match a suffix are never accepted.
* Proofreading an as-yet-untypeset manuscript enumerates every complete cut
  whose strokes all have known meanings, translates it, groups identical
  command strings and records which cuts / meanings support each one.  When no
  complete cut exists, the first ligature at which every cut dies is reported.

The module is pure logic (no I/O); :mod:`wrsource.proofdesk` binds it to the
SQLite source and the sampled dataset directories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

MAX_STROKES = 6
MAX_GESTURES = 20

INSERT = "insert"
BACK = "back"
DELETE = "delete"
COMMIT = "commit"
COMMAND_OPS = (INSERT, BACK, DELETE, COMMIT)

CARD_BODY_KIND = "practice_card"


class StenoError(ValueError):
    pass


class Command(NamedTuple):
    """One editor instruction.  ``char`` is set only for ``insert``."""

    op: str
    char: Optional[str] = None

    def token(self) -> str:
        """Compact single-string recording used inside card bodies."""
        if self.op == INSERT:
            if self.char is None or len(self.char) != 1:
                raise StenoError("insert command needs exactly one char")
            return f"{INSERT}:{self.char}"
        if self.op not in (BACK, DELETE, COMMIT):
            raise StenoError(f"unknown command op: {self.op!r}")
        return self.op

    @classmethod
    def parse(cls, token: str) -> "Command":
        if token.startswith(INSERT + ":"):
            char = token[len(INSERT) + 1:]
            if len(char) != 1:
                raise StenoError(f"bad insert token: {token!r}")
            return cls(INSERT, char)
        if token in (BACK, DELETE, COMMIT):
            return cls(token)
        raise StenoError(f"unknown command token: {token!r}")

    def as_dict(self) -> dict:
        return {"op": self.op, "char": self.char}


def encode_commands(commands: Sequence[Command]) -> List[str]:
    return [c.token() for c in commands]


def decode_commands(tokens: Sequence[str]) -> List[Command]:
    return [Command.parse(t) for t in tokens]


# A cut records, per consumed ligature, which stroke sequence it was cut into.
Cut = List[Tuple[str, Tuple[str, ...]]]


@dataclass(frozen=True)
class Reading:
    """One legal command-string reading of a manuscript."""

    commands: Tuple[Command, ...]
    cuts: Tuple[Cut, ...]          # every cut supporting this command string
    token_key: Tuple[str, ...]

    def tokens(self) -> List[str]:
        return [c.token() for c in self.commands]

    def boundary_index(self, deadline_ligatures: int) -> int:
        """Number of commands consumed after the first k ligatures."""
        total = 0
        for ligature, strokes in self.cuts[0][:deadline_ligatures]:
            total += len(strokes)
        return total

    def as_dict(self) -> dict:
        return {
            "commands": [c.as_dict() for c in self.commands],
            "tokens": list(self.token_key),
            "cuts": [[[lig, list(strokes)] for lig, strokes in cut]
                     for cut in self.cuts],
        }


@dataclass
class Manual:
    """Lexicon plus the one-command-per-stroke semantics table."""

    lexicon: Dict[str, List[Tuple[str, ...]]]
    semantics: Dict[str, Command] = field(default_factory=dict)

    def __post_init__(self) -> None:
        norm: Dict[str, List[Tuple[str, ...]]] = {}
        for ligature, alternatives in self.lexicon.items():
            if not isinstance(ligature, str) or not ligature:
                raise StenoError("lexicon keys must be non-empty strings")
            alts: List[Tuple[str, ...]] = []
            for seq in alternatives:
                if not seq or not all(isinstance(s, str) and s for s in seq):
                    raise StenoError(
                        f"lexicon entry for {ligature!r} needs non-empty "
                        "stroke strings")
                alts.append(tuple(seq))
            if not alts:
                raise StenoError(f"lexicon entry for {ligature!r} is empty")
            norm[ligature] = alts
        self.lexicon = norm
        sem: Dict[str, Command] = {}
        for stroke, cmd in self.semantics.items():
            sem[stroke] = cmd if isinstance(cmd, Command) else Command.parse(cmd)
        self.semantics = sem
        self.validate_limits()

    # -- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> "Manual":
        if not isinstance(data, dict) or "lexicon" not in data:
            raise StenoError("manual needs a lexicon object")
        lexicon = {
            lig: [tuple(seq) for seq in alts]
            for lig, alts in data["lexicon"].items()
        }
        semantics = {
            stroke: Command.parse(token) if isinstance(token, str)
            else Command(**token)
            for stroke, token in data.get("semantics", {}).items()
        }
        return cls(lexicon, semantics)

    @classmethod
    def from_json(cls, text: str) -> "Manual":
        return cls.from_dict(json.loads(text))

    def to_dict(self) -> dict:
        return {
            "lexicon": {lig: [list(seq) for seq in alts]
                        for lig, alts in self.lexicon.items()},
            "semantics": {s: c.token() for s, c in
                          sorted(self.semantics.items())},
        }

    def validate_limits(self) -> None:
        strokes = set()
        for alts in self.lexicon.values():
            for seq in alts:
                strokes.update(seq)
        strokes.update(self.semantics)
        if len(strokes) > MAX_STROKES:
            raise StenoError(
                f"a manual defines at most {MAX_STROKES} strokes, "
                f"found {len(strokes)}: {sorted(strokes)}")

    # -- semantics ---------------------------------------------------------
    def define(self, stroke: str, command: Command, *, replace: bool = False
               ) -> None:
        """Assign the single meaning of a stroke (one command per stroke)."""
        if not isinstance(stroke, str) or not stroke:
            raise StenoError("stroke must be a non-empty string")
        existing = self.semantics.get(stroke)
        if existing is not None and existing != command and not replace:
            raise StenoError(
                f"stroke {stroke!r} already means {existing.token()!r}; "
                "pass replace=True to re-define it")
        self.semantics[stroke] = command
        self.validate_limits()

    def meaning(self, stroke: str) -> Optional[Command]:
        return self.semantics.get(stroke)

    # -- cutting -----------------------------------------------------------
    def cut_partitions(self, gestures: str) -> List[Cut]:
        """Every complete cut of the whole gesture string.

        A cut is a list of ``(ligature, strokes)`` pairs covering every
        gesture symbol, so a suffix-only "reading" can never appear here.
        """
        if not gestures:
            return []
        if len(gestures) > MAX_GESTURES:
            raise StenoError(
                f"a manuscript carries at most {MAX_GESTURES} gestures")
        n = len(gestures)
        results: List[Cut] = []

        def dfs(pos: int, acc: Cut) -> None:
            if pos == n:
                results.append(list(acc))
                return
            for ligature, alternatives in self.lexicon.items():
                if gestures.startswith(ligature, pos):
                    for seq in alternatives:
                        acc.append((ligature, seq))
                        dfs(pos + len(ligature), acc)
                        acc.pop()

        dfs(0, [])
        return results

    def partitions(self, gestures: str) -> List[Tuple[str, ...]]:
        """Flat stroke tuples for every complete cut."""
        seen = set()
        out: List[Tuple[str, ...]] = []
        for cut in self.cut_partitions(gestures):
            strokes: Tuple[str, ...] = tuple(
                s for _, seq in cut for s in seq)
            if strokes not in seen:
                seen.add(strokes)
                out.append(strokes)
        return out

    def first_stuck(self, gestures: str) -> Optional[dict]:
        """Earliest ligature where every partial cut dies.

        Returns ``None`` when at least one complete cut exists.  Otherwise the
        smallest reachable position that has no lexicon continuation is the
        first stuck stroke, together with the unconsumed suffix.
        """
        n = len(gestures)
        reachable = [False] * (n + 1)
        reachable[0] = True
        for i in range(n):
            if not reachable[i]:
                continue
            advanced = False
            for ligature in self.lexicon:
                if gestures.startswith(ligature, i):
                    reachable[i + len(ligature)] = True
                    advanced = True
            if not advanced:
                return {"index": i, "suffix": gestures[i:]}
        return None if reachable[n] else {"index": n, "suffix": ""}


@dataclass(frozen=True)
class Card:
    """One sampled practice card: gestures plus the printed proof commands."""

    card_id: str
    gestures: str
    printed: Tuple[Command, ...]

    @classmethod
    def from_row(cls, row: dict) -> "Card":
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict) or body.get("kind") != CARD_BODY_KIND:
            raise StenoError(
                f"row {row.get('row_id')!r} is not a practice card body")
        gestures = body.get("gestures")
        tokens = body.get("printed")
        if not isinstance(gestures, str) or not gestures:
            raise StenoError(f"card {row.get('row_id')!r} needs gestures")
        if not isinstance(tokens, list) or not tokens:
            raise StenoError(f"card {row.get('row_id')!r} needs printed ops")
        if len(gestures) > MAX_GESTURES:
            raise StenoError(
                f"card {row.get('row_id')!r} exceeds {MAX_GESTURES} gestures")
        return cls(row["row_id"], gestures,
                   tuple(decode_commands(tokens)))

    def body_dict(self) -> dict:
        return {
            "kind": CARD_BODY_KIND,
            "gestures": self.gestures,
            "printed": encode_commands(self.printed),
        }

    def body_json(self) -> str:
        return json.dumps(self.body_dict(), ensure_ascii=False,
                          sort_keys=True)


def witness_cuts(card: Card, manual: Manual) -> List[Cut]:
    """Complete cuts whose stroke count matches the printed command count."""
    out = []
    for cut in manual.cut_partitions(card.gestures):
        n_strokes = sum(len(seq) for _, seq in cut)
        if n_strokes == len(card.printed):
            out.append(cut)
    return out


@dataclass
class SemanticsReport:
    semantics: Dict[str, Command]
    determined: List[str]
    conflicts: Dict[str, dict]       # stroke -> {tokens, card_ids}
    unknown_strokes: List[str]
    unsupported_cards: List[str]

    def as_dict(self) -> dict:
        return {
            "semantics": {s: c.token() for s, c in
                          sorted(self.semantics.items())},
            "determined": sorted(self.determined),
            "conflicts": self.conflicts,
            "unknown_strokes": sorted(self.unknown_strokes),
            "unsupported_cards": sorted(self.unsupported_cards),
        }


def induce_semantics(cards: Sequence[Card], manual: Manual
                     ) -> SemanticsReport:
    """Infer the one-command-per-stroke table from sampled practice cards.

    Every complete cut whose length matches the printed proof pairs strokes
    with the command printed at the same position.  A stroke seen with two
    different commands is reported as a conflict (with the card ids) rather
    than guessed.  Cards that no complete cut can support are listed too.
    """
    evidence: Dict[str, Dict[str, set]] = {}
    all_strokes = set()
    unsupported: List[str] = []

    for card in cards:
        cuts = witness_cuts(card, manual)
        supported = False
        for cut in cuts:
            strokes = [s for _, seq in cut for s in seq]
            all_strokes.update(strokes)
            pairs = list(zip(strokes, card.printed))
            table: Dict[str, str] = {}
            consistent = True
            for stroke, cmd in pairs:
                token = cmd.token()
                if stroke in table and table[stroke] != token:
                    consistent = False  # one cut assigns two meanings
                    break
                table[stroke] = token
            if not consistent:
                continue
            supported = True
            for stroke, cmd in pairs:
                evidence.setdefault(stroke, {}).setdefault(
                    cmd.token(), set()).add(card.card_id)
        if not supported:
            unsupported.append(card.card_id)

    semantics: Dict[str, Command] = {}
    determined: List[str] = []
    conflicts: Dict[str, dict] = {}
    for stroke, by_token in evidence.items():
        if len(by_token) == 1:
            token = next(iter(by_token))
            semantics[stroke] = Command.parse(token)
            determined.append(stroke)
        else:
            conflicts[stroke] = {
                "tokens": sorted(by_token),
                "card_ids": sorted({cid for ids in by_token.values()
                                    for cid in ids}),
            }
    unknown = sorted(all_strokes - set(semantics) - set(conflicts))
    return SemanticsReport(semantics, sorted(determined), conflicts,
                           unknown, sorted(unsupported))


def apply_semantics(manual: Manual, report: SemanticsReport) -> Manual:
    """Return ``manual`` with every determined meaning installed."""
    for stroke, cmd in report.semantics.items():
        manual.define(stroke, cmd, replace=True)
    return manual


@dataclass
class ProofResult:
    gestures: str
    readings: List[Reading]
    stuck: Optional[dict]
    unknown_strokes: List[str]

    def as_dict(self) -> dict:
        return {
            "gestures": self.gestures,
            "readings": [r.as_dict() for r in self.readings],
            "stuck": self.stuck,
            "unknown_strokes": sorted(self.unknown_strokes),
        }


def proofread(manual: Manual, gestures: str) -> ProofResult:
    """Enumerate every legal command-string reading of a new manuscript.

    Only complete cuts whose strokes all carry a meaning contribute.  Each
    distinct command string is one :class:`Reading` and keeps every cut that
    supports it, so the desk can show the meanings and cuts behind it.
    """
    stuck = manual.first_stuck(gestures)
    grouped: Dict[Tuple[str, ...], List[Cut]] = {}
    unknown: set = set()
    for cut in manual.cut_partitions(gestures):
        strokes = [s for _, seq in cut for s in seq]
        missing = [s for s in strokes if s not in manual.semantics]
        if missing:
            unknown.update(missing)
            continue
        grouped.setdefault(tuple(strokes), []).append(cut)

    readings: List[Reading] = []
    for stroke_key in sorted(grouped):
        cuts = grouped[stroke_key]
        commands = tuple(manual.semantics[s] for s in stroke_key)
        token_key = tuple(c.token() for c in commands)
        readings.append(Reading(commands, tuple(cuts), token_key))
    return ProofResult(gestures, readings, stuck, sorted(unknown))
