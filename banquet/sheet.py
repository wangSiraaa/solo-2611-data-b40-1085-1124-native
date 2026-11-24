"""The interpretation sheet (传译单): the one deliverable on the real chair map.

Given a chosen control, produce a single reviewable document that contains:

* the actual seating chart in force (real chairs, with empty gaps shown);
* for every round, each relayed conversation's path, per-segment shared
  language and interpreter, plus any infeasibility reason;
* the accumulated clue ledger (who heard which named clue, and why);
* the secrecy agreement: every breach, naming either the single interpreter or
  the designated pair that gathers a forbidden clue group;
* venue-facing diagnostics that point at exactly which incidental hearing or
  which boundary crossing would make the evening's promise fail.

``build_sheet`` never rewrites history: it reports the union of clues actually
heard under the control (including prior spoken rounds), so a spoken segment
cannot be "unheard" by changing the chart or an interpreter afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from .control import (
    Control,
    Night,
    NightResult,
    NoControl,
    listener_hears,
    simulate_night,
)


@dataclass(frozen=True)
class Sheet:
    control_kind: str
    result: NightResult
    diagnostics: tuple[dict, ...]

    @property
    def signed(self) -> bool:
        """A sheet can be signed only when every round is feasible, no breach
        is found, and no control error applies."""

        return self.result.valid

    def as_dict(self) -> dict:
        return {
            "control": self.control_kind,
            "signed": self.signed,
            "status": self.result.status,
            "rounds": [r.as_dict() for r in self.result.round_reports],
            "clues": self.result.ledger.as_dict(),
            "breaches": [
                {
                    "group": b.group_id,
                    "actor": list(b.actor),
                    "gathered": sorted(b.gathered),
                    "via": b.via,
                }
                for b in self.result.breaches
            ],
            "diagnostics": list(self.diagnostics),
            "notes": list(self.result.notes),
        }


def _diagnostics(night: Night, control: Control) -> tuple[dict, ...]:
    """For each incidental round/conversation and each would-be listener,
    report whether they hear it under this control, so the venue can point at
    the offending hearing (or confirm it is sealed)."""

    out: list[dict] = []
    for ri, rnd in enumerate(night.rounds):
        if not rnd.is_incidental:
            continue
        for conv in rnd.conversations:
            if not conv.incidental and not rnd.is_incidental:
                continue
            for guest_id in night.guests:
                if guest_id in (conv.speaker_a, conv.speaker_b):
                    continue
                hears = listener_hears(night, ri, conv.conv_id, guest_id, control=control)
                if hears:
                    out.append(
                        {
                            "round_index": ri,
                            "round_id": rnd.round_id,
                            "conv_id": conv.conv_id,
                            "listener": guest_id,
                            "hears": True,
                            "kind": "incidental",
                        }
                    )
    return tuple(out)


def build_sheet(night: Night, control: Control = NoControl()) -> Sheet:
    """Simulate the night and assemble the interpretation sheet."""

    result = simulate_night(night, control)
    diags = _diagnostics(night, control)
    return Sheet(control.kind, result, diags)


# ---------------------------------------------------------------------------
# Comparison helper: screen vs swap, for the "which control is bookable" task
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlComparison:
    screen: Sheet | None
    screen_reason: str
    swap: Sheet | None
    swap_reason: str

    def as_dict(self) -> dict:
        return {
            "screen": self.screen.as_dict() if self.screen else None,
            "screen_reason": self.screen_reason,
            "swap": self.swap.as_dict() if self.swap else None,
            "swap_reason": self.swap_reason,
        }
