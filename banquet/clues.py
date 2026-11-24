"""Clues, incidental overhearing, and the secrecy agreement.

A *clue* is produced per relay **segment**: the two chairs joined by a segment
each hear that segment and receive a named clue ``(conv_id, segment_index)``.
A toast may be pieced together from several whisper segments, so a *secret*
label is fully held only once a person has heard **every** segment-clue of
**every** conversation carrying that label.

Ordinary rounds have no incidental hearing: the audience is exactly the chosen
relay path.  A round/conversation marked *incidental* (顺带听闻) additionally
lets a chair's immediate neighbours across an **unscreened** seam hear the
segments touching that chair.  A sound screen blocks the seam entirely.

Clues are monotonic: once a segment has been spoken, the hearer keeps the clue
even if the chart or interpreter changes afterwards -- "已经开口的一段不能因
追改席图或换译员而'没听见'".  We therefore *accumulate* into a persistent
per-person ledger and never remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .model import Conversation, Edge, Round, SeatChart, SecretGroup
from .relay import RoundPlan, Route

ClueId = tuple[str, int]   # (conv_id, segment index)


@dataclass
class ClueLedger:
    """Accumulated clues per person; never shrinks."""

    heard: dict[str, set[ClueId]] = field(default_factory=dict)
    interpreters: set[str] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)

    def grant(self, guest_id: str, clue: ClueId, *, reason: str) -> None:
        bucket = self.heard.setdefault(guest_id, set())
        if clue not in bucket:
            bucket.add(clue)
            self.events.append(
                {"hearer": guest_id, "clue": list(clue), "reason": reason}
            )

    def clues_of(self, guest_id: str) -> frozenset[ClueId]:
        return frozenset(self.heard.get(guest_id, set()))

    def as_dict(self) -> dict:
        return {
            "heard": {g: sorted(c) for g, c in sorted(self.heard.items())},
            "interpreters": sorted(self.interpreters),
            "events": list(self.events),
        }


@dataclass(frozen=True)
class SecretRequirement:
    """All clue ids that must be heard to fully hold a secret label."""

    label: str
    clues: frozenset[ClueId]


def secret_requirements(
    rounds: Iterable[Round], plans: Iterable[RoundPlan]
) -> dict[str, SecretRequirement]:
    """Map secret label -> all its required segment-clues across the night."""

    per_conv_segments: dict[str, int] = {}
    for plan in plans:
        for route in plan.routes:
            per_conv_segments[route.conv_id] = len(route.segments)

    label_clues: dict[str, set[ClueId]] = {}
    for rnd in rounds:
        for conv in rnd.conversations:
            if not conv.secret:
                continue
            n = per_conv_segments.get(conv.conv_id, 0)
            label_clues.setdefault(conv.secret, set()).update(
                (conv.conv_id, k) for k in range(n)
            )
    return {
        label: SecretRequirement(label, frozenset(clues))
        for label, clues in label_clues.items()
    }


def _segment_hearers(
    chart: SeatChart,
    route: Route,
    seg_index: int,
    *,
    incidental: bool,
    blocked: frozenset[Edge],
) -> dict[str, str]:
    """Return ``{hearers: reason}`` for one segment."""

    seg = route.segments[seg_index]
    out: dict[str, str] = {
        seg.frm: "path_endpoint",
        seg.to: "path_endpoint",
    }
    if incidental:
        for endpoint in (seg.frm, seg.to):
            for nb in chart.neighbours(endpoint):
                seam = Edge(*sorted((endpoint, nb)))
                if seam in blocked:
                    continue
                out.setdefault(nb, f"incidental:{endpoint}")
    return out


def record_round_clues(
    ledger: ClueLedger,
    chart: SeatChart,
    rnd: Round,
    plan: RoundPlan,
    *,
    blocked: Iterable[Edge] = (),
) -> None:
    """Accumulate segment-clues for every valid route in ``plan``."""

    if not plan.ok:
        return
    blocked_set = frozenset(blocked)
    conv_map: dict[str, Conversation] = {c.conv_id: c for c in rnd.conversations}
    for route in plan.routes:
        conv = conv_map[route.conv_id]
        ledger.interpreters.update(route.interpreters)
        incidental = conv.incidental or rnd.is_incidental
        for k in range(len(route.segments)):
            hearers = _segment_hearers(
                chart, route, k, incidental=incidental, blocked=blocked_set
            )
            for hearer, reason in hearers.items():
                ledger.grant(hearer, (route.conv_id, k), reason=reason)


# ---------------------------------------------------------------------------
# Secrecy evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Breach:
    group_id: str
    actor: tuple[str, ...]      # one person, or a designated pair
    gathered: frozenset[str]    # secret labels fully gathered
    via: str                    # "single_interpreter" | "designated_pair"


def _full_labels(
    clues: frozenset[ClueId],
    requirements: Mapping[str, SecretRequirement],
) -> frozenset[str]:
    return frozenset(
        label
        for label, req in requirements.items()
        if req.clues and req.clues <= clues
    )


def evaluate_secrecy(
    ledger: ClueLedger,
    *,
    requirements: Mapping[str, SecretRequirement],
    groups: Iterable[SecretGroup],
    designated_pairs: Iterable[frozenset[str]] = (),
) -> list[Breach]:
    """Find every secrecy-group that a single interpreter or a designated pair
    can fully gather.

    * the individual actors checked are the night's interpreters;
    * the pair actors are the registered mutually-communicating pairs, whose
      clues are unioned (they can pool what each has heard).
    """

    breaches: list[Breach] = []
    individuals = sorted(ledger.interpreters)
    pairs = [frozenset(p) for p in designated_pairs if len(p) == 2]

    for group in groups:
        labels = frozenset(group.secrets)
        if not labels:
            continue
        for person in individuals:
            full = _full_labels(ledger.clues_of(person), requirements)
            if labels <= full:
                breaches.append(
                    Breach(group.group_id, (person,), frozenset(labels), "single_interpreter")
                )
        for pair in pairs:
            pooled: set[ClueId] = set()
            for p in pair:
                pooled |= ledger.clues_of(p)
            full = _full_labels(frozenset(pooled), requirements)
            if labels <= full:
                breaches.append(
                    Breach(
                        group.group_id,
                        tuple(sorted(pair)),
                        frozenset(labels),
                        "designated_pair",
                    )
                )
    return breaches


def secrecy_ok(breaches: Iterable[Breach]) -> bool:
    return not list(breaches)
