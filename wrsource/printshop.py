"""Composing room: type sticks, a real type box and the two press processes.

Editor semantics (fixed by the task)

* A stick (铅字栏) is a text string with a moving cursor and a locked prefix
  length.  The locked prefix cannot be moved, deleted or overwritten.
* Commands from :mod:`wrsource.steno`:

  - ``insert:字``: put one sort *before* the cursor; the cursor steps past
    the new sort.  Refused when the cursor is inside the locked prefix.
  - ``back``: move the cursor one cell left.  Moving back onto or across the
    lock boundary is allowed (the prefix itself is not altered); running past
    the left edge is a no-op stop.
  - ``delete``: remove the sort before the cursor.  Refused when it would
    touch the locked prefix (cursor at or before the lock length) or at the
    edge.
  - ``commit``: finalize through the cursor; the locked prefix extends to the
    current cursor position.  Committing inside the existing lock is a no-op.

* A refused command *stops the run*: the offending step and everything after
  it never happen.  Each step is recorded, including the first violation.

Physical sorts
--------------
A :class:`TypeBox` is a real, countable inventory.  Inserts draw one sort,
deletes return it.  A reserve stick claims real sorts from the same box: once
claimed the main stick cannot borrow them until the reserve is released.
Printed sheets are handed to morning readers and their sorts never come back.

Press processes
---------------
* ``plan_shared_column`` -- the shared-form process.  The morning edition may
  lock only a prefix every legal continuation can still safely continue from;
  the night edition continues on the same stick.
* ``plan_reserve_column`` -- the duplicate-form process.  A second stick is
  cast from real sorts before printing; at the last segment one form is chosen
  and the unused reserve released.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .steno import (BACK, COMMIT, DELETE, INSERT, Command, Reading)


class PressError(RuntimeError):
    pass


class LockViolation(PressError):
    """A command tried to alter the finalized prefix."""


class OutOfSorts(PressError):
    """The physical type box has no spare sort of the required face."""


# ---------------------------------------------------------------------------
# Physical type box
# ---------------------------------------------------------------------------

class TypeBox:
    """A countable box of lead sorts.  Counts are never allowed below zero."""

    def __init__(self, sorts: Optional[Dict[str, int]] = None):
        self.counts: Dict[str, int] = {}
        if sorts:
            for char, count in sorts.items():
                if not isinstance(char, str) or len(char) != 1:
                    raise PressError("type box keys must be single chars")
                if not isinstance(count, int) or isinstance(count, bool) \
                        or count < 0:
                    raise PressError("type box counts must be non-negative ints")
                if count:
                    self.counts[char] = count

    def available(self, char: str) -> int:
        return self.counts.get(char, 0)

    def can_supply(self, needs: Dict[str, int]) -> bool:
        return all(self.available(ch) >= n for ch, n in needs.items())

    def draw(self, char: str) -> None:
        if self.available(char) <= 0:
            raise OutOfSorts(f"no spare sort for {char!r}")
        self.counts[char] -= 1
        if self.counts[char] == 0:
            del self.counts[char]

    def return_sort(self, char: str) -> None:
        self.counts[char] = self.counts.get(char, 0) + 1

    def draw_needs(self, needs: Dict[str, int]) -> None:
        if not self.can_supply(needs):
            missing = {ch: n - self.available(ch) for ch, n in needs.items()
                       if n > self.available(ch)}
            raise OutOfSorts(f"box cannot supply {missing}")
        for char, n in needs.items():
            for _ in range(n):
                self.draw(char)

    def snapshot(self) -> Dict[str, int]:
        return dict(sorted(self.counts.items()))

    def copy(self) -> "TypeBox":
        return TypeBox(self.counts)


def needs_of(text: str) -> Dict[str, int]:
    needs: Dict[str, int] = {}
    for ch in text:
        needs[ch] = needs.get(ch, 0) + 1
    return needs


# ---------------------------------------------------------------------------
# Type stick state machine
# ---------------------------------------------------------------------------

@dataclass
class StickEvent:
    step: int
    command: str
    status: str                    # applied | boundary | refused
    detail: Optional[str] = None
    text: str = ""
    cursor: int = 0
    locked: int = 0

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "command": self.command,
            "status": self.status,
            "detail": self.detail,
            "text": self.text,
            "cursor": self.cursor,
            "locked": self.locked,
        }


@dataclass
class Stick:
    """One composing stick: text, cursor, locked prefix length and its box."""

    text: str = ""
    cursor: int = 0
    locked: int = 0
    box: Optional[TypeBox] = None
    events: List[StickEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (0 <= self.locked <= len(self.text)):
            raise PressError("locked prefix longer than the stick text")
        if not (0 <= self.cursor <= len(self.text)):
            raise PressError("cursor outside the stick")
        if self.box is None:
            self.box = TypeBox()

    def copy(self) -> "Stick":
        return Stick(self.text, self.cursor, self.locked, self.box.copy(),
                     [StickEvent(**e.__dict__) for e in self.events])

    # -- single command semantics ------------------------------------------
    def apply(self, command: Command) -> StickEvent:
        step = len(self.events)
        token = command.token()
        if command.op == INSERT:
            if self.cursor < self.locked:
                ev = self._refuse(step, token,
                                  "insert inside locked prefix")
                raise LockViolation(ev.detail)
            assert command.char is not None
            if self.box is not None:
                # Surface the physical shortage as a stop too.
                if self.box.available(command.char) <= 0:
                    ev = self._refuse(step, token,
                                      f"out of sorts {command.char!r}")
                    raise OutOfSorts(ev.detail)
                self.box.draw(command.char)
            self.text = self.text[:self.cursor] + command.char + \
                self.text[self.cursor:]
            self.cursor += 1
            ev = StickEvent(step, token, "applied", None, self.text,
                            self.cursor, self.locked)
        elif command.op == BACK:
            if self.cursor <= 0:
                ev = StickEvent(step, token, "boundary",
                                "cursor already at left edge", self.text,
                                self.cursor, self.locked)
            else:
                self.cursor -= 1
                ev = StickEvent(step, token, "applied", None, self.text,
                                self.cursor, self.locked)
        elif command.op == DELETE:
            if self.cursor <= 0:
                ev = self._refuse(step, token,
                                  "delete at left edge")
                raise LockViolation(ev.detail)
            if self.cursor <= self.locked:
                ev = self._refuse(step, token,
                                  "delete would touch locked prefix")
                raise LockViolation(ev.detail)
            removed = self.text[self.cursor - 1]
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
            if self.box is not None:
                self.box.return_sort(removed)
            ev = StickEvent(step, token, "applied", None, self.text,
                            self.cursor, self.locked)
        elif command.op == COMMIT:
            if self.cursor < self.locked:
                ev = self._refuse(step, token,
                                  "commit point inside locked prefix")
                raise LockViolation(ev.detail)
            self.locked = max(self.locked, self.cursor)
            ev = StickEvent(step, token, "applied", None, self.text,
                            self.cursor, self.locked)
        else:  # pragma: no cover - guarded by Command.parse
            raise PressError(f"unknown command {token!r}")
        self.events.append(ev)
        return ev

    def _refuse(self, step: int, token: str, detail: str) -> StickEvent:
        ev = StickEvent(step, token, "refused", detail, self.text,
                        self.cursor, self.locked)
        self.events.append(ev)
        return ev

    # -- runs ---------------------------------------------------------------
    def run(self, commands: Sequence[Command]) -> dict:
        """Run until completion or the first stop.  Reports the outcome."""
        for command in commands:
            try:
                self.apply(command)
            except PressError as exc:
                return self._outcome(stopped=True,
                                     error=type(exc).__name__,
                                     detail=str(exc))
        return self._outcome(stopped=False)

    def _outcome(self, stopped: bool, error: Optional[str] = None,
                 detail: Optional[str] = None) -> dict:
        return {
            "text": self.text,
            "cursor": self.cursor,
            "locked": self.locked,
            "stopped": stopped,
            "error": error,
            "detail": detail,
            "events": [e.as_dict() for e in self.events],
            "box": self.box.snapshot(),
        }

    def lock_printed_prefix(self, length: int) -> None:
        """Hand the first ``length`` sorts to the morning readers."""
        if not (0 <= length <= len(self.text)):
            raise PressError("cannot lock beyond the stick")
        self.locked = max(self.locked, length)


def simulate(initial_text: str, commands: Sequence[Command],
             box: Optional[TypeBox] = None, *, cursor: Optional[int] = None,
             locked: int = 0) -> dict:
    """Convenience wrapper used by the desk and the tests."""
    stick = Stick(initial_text,
                  len(initial_text) if cursor is None else cursor,
                  locked, box.copy() if box is not None else None)
    return stick.run(commands)


# ---------------------------------------------------------------------------
# Safety analysis across all legal readings
# ---------------------------------------------------------------------------

@dataclass
class ReadingVerdict:
    index: int
    tokens: Tuple[str, ...]
    safe: bool
    final_text: str
    stopped_step: Optional[int]
    error: Optional[str]
    detail: Optional[str]
    events: List[dict]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "tokens": list(self.tokens),
            "safe": self.safe,
            "final_text": self.final_text,
            "stopped_step": self.stopped_step,
            "error": self.error,
            "detail": self.detail,
            "events": self.events,
        }


@dataclass
class SafetyReport:
    initial_text: str
    locked: int
    target_title: str
    all_safe: bool
    all_meet_title: bool
    verdicts: List[ReadingVerdict]
    earliest_violation: Optional[dict]
    opposite_reading: Optional[dict]

    def as_dict(self) -> dict:
        return {
            "initial_text": self.initial_text,
            "locked": self.locked,
            "target_title": self.target_title,
            "all_safe": self.all_safe,
            "all_meet_title": self.all_meet_title,
            "verdicts": [v.as_dict() for v in self.verdicts],
            "earliest_violation": self.earliest_violation,
            "opposite_reading": self.opposite_reading,
        }


def analyze_safety(initial_text: str, readings: Sequence[Reading],
                   target_title: str, *, locked: int = 0,
                   box: Optional[TypeBox] = None) -> SafetyReport:
    """Run every legal reading from the same locked starting stick.

    For each reading the whole command string is replayed on a fresh copy, so
    two readings that reach the same final text can still differ in whether
    they touch the locked prefix mid-run.  The report keeps the earliest
    layout-breaking step across all readings and one opposite (safe, title
    matching) reading when a break exists.
    """
    verdicts: List[ReadingVerdict] = []
    for i, reading in enumerate(readings):
        stick = Stick(initial_text, len(initial_text), locked,
                      box.copy() if box is not None else None)
        outcome = stick.run(reading.commands)
        verdicts.append(ReadingVerdict(
            index=i,
            tokens=reading.token_key,
            safe=not outcome["stopped"],
            final_text=outcome["text"],
            stopped_step=(outcome["events"][-1]["step"]
                          if outcome["stopped"] else None),
            error=outcome["error"],
            detail=outcome["detail"],
            events=outcome["events"],
        ))

    all_safe = all(v.safe for v in verdicts)
    all_meet = all(v.safe and v.final_text == target_title
                   for v in verdicts)

    earliest: Optional[dict] = None
    for v in verdicts:
        if not v.safe and (earliest is None
                           or (v.stopped_step or 0) <
                           earliest["stopped_step"]
                           or ((v.stopped_step or 0) ==
                               earliest["stopped_step"]
                               and v.index < earliest["reading_index"])):
            earliest = {
                "reading_index": v.index,
                "tokens": list(v.tokens),
                "stopped_step": v.stopped_step,
                "command": v.tokens[v.stopped_step]
                if v.stopped_step is not None else None,
                "error": v.error,
                "detail": v.detail,
            }

    opposite = None
    if earliest is not None:
        for v in verdicts:
            if v.safe and v.final_text == target_title:
                opposite = v.as_dict()
                break
    return SafetyReport(initial_text, locked, target_title, all_safe,
                        all_meet, verdicts, earliest, opposite)


# ---------------------------------------------------------------------------
# Helpers shared by the two press processes
# ---------------------------------------------------------------------------

def _evening_commands(reading: Reading, cut_index: int
                      ) -> List[Command]:
    return list(reading.commands[cut_index:])


# ---------------------------------------------------------------------------
# Shared-form process (共版)
# ---------------------------------------------------------------------------

@dataclass
class SharedPlan:
    process: str
    morning_text: str
    morning_locked: int
    longest_safe_length: int
    box_after_morning: Dict[str, int]
    evening_possible: bool
    evening_readings: List[dict]
    morning_blocker: Optional[dict]

    def as_dict(self) -> dict:
        return {
            "process": self.process,
            "morning_text": self.morning_text,
            "morning_locked": self.morning_locked,
            "longest_safe_length": self.longest_safe_length,
            "box_after_morning": self.box_after_morning,
            "evening_possible": self.evening_possible,
            "evening_readings": self.evening_readings,
            "morning_blocker": self.morning_blocker,
        }


def plan_shared_column(initial_text: str, readings: Sequence[Reading],
                       target_title: str, cut_index: int,
                       box: TypeBox) -> SharedPlan:
    """Shared-form plan: morning lock length + same-stick night continuation.

    The longest morning prefix is the largest ``L`` (bounded by the requested
    title) such that *every* legal reading can continue from the same stick
    after its first ``L`` sorts are printed and locked, with the physical box
    covering the morning sorts.  L=0 (no morning sheet beyond the initial
    stick) is always considered.  Per-reading night outcomes are reported for
    the chosen length even when some reading then fails.
    """
    limit = min(len(target_title), len(initial_text))
    morning_blocker: Optional[dict] = None
    level_results: Dict[int, List[dict]] = {}
    chosen = 0

    def run_level(length: int) -> Tuple[bool, List[dict], TypeBox]:
        prefix = initial_text[:length]
        level_box = box.copy()
        level_box.draw_needs(needs_of(prefix))
        outcomes = []
        ok = True
        for i, reading in enumerate(readings):
            stick = Stick(initial_text, len(initial_text), length,
                          level_box.copy())
            outcome = dict(stick.run(_evening_commands(reading, cut_index)))
            outcome["reading_index"] = i
            outcome["tokens"] = list(reading.token_key)
            outcomes.append(outcome)
            if outcome["stopped"] or outcome["text"] != target_title:
                ok = False
        return ok, outcomes, level_box

    for length in range(limit, -1, -1):
        prefix = initial_text[:length]
        if not box.can_supply(needs_of(prefix)):
            if morning_blocker is None:
                morning_blocker = {
                    "length": length,
                    "reason": "type box cannot cover the morning sorts",
                }
            continue
        ok, outcomes, level_box = run_level(length)
        level_results[length] = outcomes
        if ok:
            chosen = length
            break
        if morning_blocker is None:
            for outcome in outcomes:
                if outcome["stopped"]:
                    morning_blocker = {
                        "length": length,
                        "reading_index": outcome["reading_index"],
                        "reason": outcome["detail"],
                    }
                    break

    chosen_outcomes = level_results.get(chosen, [])
    morning_box = box.copy()
    morning_box.draw_needs(needs_of(initial_text[:chosen]))
    evening = []
    for outcome in chosen_outcomes:
        evening.append({
            "reading_index": outcome["reading_index"],
            "tokens": outcome["tokens"],
            "delivered": (not outcome["stopped"]
                          and outcome["text"] == target_title),
            "final_text": outcome["text"],
            "stopped_step": (outcome["events"][-1]["step"]
                             if outcome["stopped"] else None),
            "detail": outcome["detail"],
        })
    return SharedPlan(
        process="shared",
        morning_text=initial_text[:chosen],
        morning_locked=chosen,
        longest_safe_length=chosen,
        box_after_morning=morning_box.snapshot(),
        evening_possible=bool(evening)
        and all(e["delivered"] for e in evening),
        evening_readings=evening,
        morning_blocker=morning_blocker,
    )


# ---------------------------------------------------------------------------
# Reserve-form process (副栏)
# ---------------------------------------------------------------------------

@dataclass
class ReservePlan:
    process: str
    feasible: bool
    reason: Optional[str]
    morning_text: str
    morning_locked: int
    reserve_text: str
    reserved_sorts: Dict[str, int]
    box_after_morning: Dict[str, int]
    evening: List[dict]
    all_delivered: bool

    def as_dict(self) -> dict:
        return {
            "process": self.process,
            "feasible": self.feasible,
            "reason": self.reason,
            "morning_text": self.morning_text,
            "morning_locked": self.morning_locked,
            "reserve_text": self.reserve_text,
            "reserved_sorts": self.reserved_sorts,
            "box_after_morning": self.box_after_morning,
            "evening_readings": self.evening,
            "all_delivered": self.all_delivered,
        }


def plan_reserve_column(initial_text: str, readings: Sequence[Reading],
                        target_title: str, cut_index: int,
                        box: TypeBox) -> ReservePlan:
    """Reserve-form plan: two real sticks cast from the same box.

    The main stick prints the morning sheet and keeps its locked prefix; the
    reserve duplicates the *whole initial stick* from real sorts and is held
    untouched until the last segment.  At night each reading picks a form on
    which its own continuation finishes safely, and sorts of the unused form
    are released back.  Printed morning sorts are never reclaimed.
    """
    needs = needs_of(initial_text)
    # Two full copies of the initial stick must physically exist.
    doubled = {ch: 2 * n for ch, n in needs.items()}
    if not box.can_supply(doubled):
        missing = {ch: n - box.available(ch) for ch, n in doubled.items()
                   if n > box.available(ch)}
        return ReservePlan(
            process="reserve", feasible=False,
            reason=f"box cannot cast a reserve stick, missing {missing}",
            morning_text=initial_text, morning_locked=len(initial_text),
            reserve_text=initial_text, reserved_sorts=needs,
            box_after_morning=box.snapshot(), evening=[],
            all_delivered=False)

    main_box = box.copy()
    main_box.draw_needs(needs)
    # The reserve claims a second full set of sorts from the same box; those
    # sorts cannot be lent to the main stick until the night form is chosen.
    reserve_box = box.copy()
    reserve_box.draw_needs(doubled)

    evening: List[dict] = []
    delivered = True
    for i, reading in enumerate(readings):
        # Try the main stick first (it carries the printed morning sheet).
        main_stick = Stick(initial_text, len(initial_text),
                           len(initial_text), main_box.copy())
        main_outcome = main_stick.run(
            _evening_commands(reading, cut_index))
        if not main_outcome["stopped"] \
                and main_outcome["text"] == target_title:
            evening.append({
                "reading_index": i,
                "tokens": list(reading.token_key),
                "form": "main",
                "delivered": True,
                "final_text": main_outcome["text"],
                "stopped_step": None,
                "detail": None,
            })
            continue
        # Otherwise this continuation needs the untouched reserve form.
        reserve_stick = Stick(initial_text, len(initial_text), 0,
                              reserve_box.copy())
        reserve_outcome = reserve_stick.run(
            _evening_commands(reading, cut_index))
        ok = (not reserve_outcome["stopped"]
              and reserve_outcome["text"] == target_title)
        delivered = delivered and ok
        evening.append({
            "reading_index": i,
            "tokens": list(reading.token_key),
            "form": "reserve",
            "delivered": ok,
            "final_text": reserve_outcome["text"],
            "stopped_step": (reserve_outcome["events"][-1]["step"]
                             if reserve_outcome["stopped"] else None),
            "detail": reserve_outcome["detail"],
        })

    # After the form decision the unused form's sorts return to the box; the
    # morning sheet's sorts remain with the readers.
    remaining = box.copy()
    remaining.draw_needs(needs)       # main stick keeps its sorts
    return ReservePlan(
        process="reserve", feasible=True, reason=None,
        morning_text=initial_text, morning_locked=len(initial_text),
        reserve_text=initial_text, reserved_sorts=needs,
        box_after_morning=remaining.snapshot(),
        evening=evening, all_delivered=delivered)


# ---------------------------------------------------------------------------
# Desk query: one manuscript, both processes
# ---------------------------------------------------------------------------

@dataclass
class ManuscriptOrder:
    """One manuscript in the physical delivery order.

    ``morning_ligatures`` is the number of its ligatures already visible at
    the morning deadline; the remaining ligatures arrive only with the last
    segment.
    """

    name: str
    initial_text: str
    readings: List[Reading]
    target_title: str
    morning_ligatures: int = 0
    box_sorts: Optional[Dict[str, int]] = None


def press_query(initial_text: str, readings: Sequence[Reading],
                target_title: str, deadline_ligatures: int,
                box_sorts: Dict[str, int]) -> dict:
    """Answer the desk's full query for a new manuscript with a deadline.

    Returns the morning state each process can actually hand over, the sorts
    left in the box, the night-form outcomes and, for the shared-form process,
    the first reading/step that breaks the layout.
    """
    if not readings:
        raise PressError("no legal readings: proofread the manuscript first")
    cut_index = readings[0].boundary_index(deadline_ligatures)
    for reading in readings:
        if reading.boundary_index(deadline_ligatures) != cut_index:
            raise PressError(
                "readings disagree on the deadline cut index; the deadline "
                "must fall on a ligature boundary common to every reading")
    box = TypeBox(box_sorts)
    shared = plan_shared_column(initial_text, readings, target_title,
                                cut_index, box)
    reserve = plan_reserve_column(initial_text, readings, target_title,
                                  cut_index, box)
    safety = analyze_safety(initial_text, readings, target_title,
                            locked=len(initial_text), box=box)
    return {
        "initial_text": initial_text,
        "target_title": target_title,
        "deadline_ligatures": deadline_ligatures,
        "cut_index": cut_index,
        "readings": [list(r.token_key) for r in readings],
        "box_initial": box.snapshot(),
        "shared": shared.as_dict(),
        "reserve": reserve.as_dict(),
        "safety_against_locked_morning": safety.as_dict(),
    }


# ---------------------------------------------------------------------------
# Multi-manuscript schedule over one physical box
# ---------------------------------------------------------------------------

def _validated_cut_index(readings: Sequence[Reading],
                         morning_ligatures: int) -> int:
    if not readings:
        raise PressError("no legal readings: proofread the manuscript first")
    cut_index = readings[0].boundary_index(morning_ligatures)
    for reading in readings:
        if reading.boundary_index(morning_ligatures) != cut_index:
            raise PressError(
                "readings disagree on the deadline cut index; the deadline "
                "must fall on a ligature boundary common to every reading")
    return cut_index


def run_press_schedule(box_sorts: Dict[str, int],
                       order: Sequence[ManuscriptOrder],
                       process: str) -> dict:
    """Run the whole morning/evening schedule over one shared type box.

    Manuscripts are handled in their physical delivery order.  Every night
    edition consumes real sorts, printed sheets keep their sorts with the
    readers, and sorts of an unchosen reserve form return to the box before
    the next manuscript is cast.  The result records, per manuscript, the
    longest morning prefix the chosen process could hand over, the sorts that
    had to be held back (reserve) and whether the night edition delivered.
    """
    if process not in ("shared", "reserve"):
        raise PressError("process must be 'shared' or 'reserve'")
    box = TypeBox(box_sorts)
    entries = []
    all_delivered = True

    for item in order:
        cut_index = _validated_cut_index(item.readings,
                                         item.morning_ligatures)
        entry = {
            "name": item.name,
            "target_title": item.target_title,
            "process": process,
            "readings": [list(r.token_key) for r in item.readings],
            "box_before": box.snapshot(),
        }
        if process == "shared":
            plan = plan_shared_column(item.initial_text, item.readings,
                                      item.target_title, cut_index, box)
            entry["morning_text"] = plan.morning_text
            entry["morning_locked"] = plan.morning_locked
            entry["longest_safe_length"] = plan.longest_safe_length
            entry["morning_blocker"] = plan.morning_blocker
            entry["evening_readings"] = plan.evening_readings
            entry["reserved_sorts"] = {}
            delivered = plan.evening_possible
            if delivered:
                # Every reading delivers the same title; its physical sorts
                # are exactly the title's sorts and stay with the readers.
                try:
                    box.draw_needs(needs_of(item.target_title))
                except OutOfSorts as exc:
                    delivered = False
                    entry["shortage"] = str(exc)
        else:
            plan = plan_reserve_column(item.initial_text, item.readings,
                                       item.target_title, cut_index, box)
            entry["feasible"] = plan.feasible
            entry["reason"] = plan.reason
            entry["morning_text"] = plan.morning_text
            entry["morning_locked"] = plan.morning_locked
            entry["reserved_sorts"] = plan.reserved_sorts
            entry["evening_readings"] = plan.evening
            delivered = plan.feasible and plan.all_delivered
            if delivered:
                # The chosen form prints one full title and keeps its sorts;
                # the unused form's sorts were released back to the box.
                try:
                    box.draw_needs(needs_of(item.target_title))
                except OutOfSorts as exc:
                    delivered = False
                    entry["shortage"] = str(exc)
        entry["delivered"] = delivered
        entry["box_after"] = box.snapshot()
        all_delivered = all_delivered and delivered
        entries.append(entry)

    return {
        "process": process,
        "box_initial": TypeBox(box_sorts).snapshot(),
        "box_final": box.snapshot(),
        "all_delivered": all_delivered,
        "manuscripts": entries,
    }
