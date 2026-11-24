"""Signed handoff records (已签记录) for shift changeover.

When a worker hands a partially-inked job to the next worker, the next worker
may continue **only** from a signed record (『谁接手时据哪张已签记录才可继续』).
The record binds:

* the observations actually registered (their instrument buckets and the
  underlying binary32 originals, if supplied);
* the decision the record authorises (which pen draws the body);
* the carried-forward chamber state — ink already spent on the test stroke is
  *gone* and must not be restored (『接手要保留实际落笔的线，不还原以前的墨』).

``Handoff`` produces a content digest over all of that, so any later tampering
— restoring ink, swapping the chosen pen, or rewriting an observation — changes
the digest and the record no longer verifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Hashable, Mapping, Sequence

from obsdelivery.canonical import digest

from .model import State


def _s(v: Fraction) -> str:
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


@dataclass(frozen=True)
class ObservationRecord:
    tag: str
    bucket: Hashable
    bits: str | None = None

    def canonical(self) -> dict:
        return {"tag": self.tag, "bucket": repr(self.bucket), "bits": self.bits}


@dataclass(frozen=True)
class SignedHandoff:
    trial: str
    instrument: str
    observations: tuple[ObservationRecord, ...]
    chosen_pen: str
    carried_states: Mapping[str, State]
    spent_test_ink: Mapping[str, Fraction]
    successor: str
    digest: str

    def canonical(self) -> dict:
        return {
            "trial": self.trial,
            "instrument": self.instrument,
            "observations": [o.canonical() for o in self.observations],
            "chosen_pen": self.chosen_pen,
            "carried_states": {
                p: {"nib": _s(s.nib), "belly": _s(s.belly)}
                for p, s in sorted(self.carried_states.items())
            },
            "spent_test_ink": {p: _s(v) for p, v in sorted(self.spent_test_ink.items())},
            "successor": self.successor,
        }

    def as_json(self) -> dict:
        body = self.canonical()
        body["digest"] = self.digest
        return body


def sign_handoff(
    *,
    trial: str,
    instrument: str,
    observations: Sequence[ObservationRecord],
    chosen_pen: str,
    carried_states: Mapping[str, State],
    spent_test_ink: Mapping[str, Fraction],
    successor: str,
) -> SignedHandoff:
    body = {
        "trial": trial,
        "instrument": instrument,
        "observations": [o.canonical() for o in observations],
        "chosen_pen": chosen_pen,
        "carried_states": {
            p: {"nib": _s(s.nib), "belly": _s(s.belly)}
            for p, s in sorted(carried_states.items())
        },
        "spent_test_ink": {p: _s(v) for p, v in sorted(spent_test_ink.items())},
        "successor": successor,
    }
    return SignedHandoff(
        trial=trial,
        instrument=instrument,
        observations=tuple(observations),
        chosen_pen=chosen_pen,
        carried_states=dict(carried_states),
        spent_test_ink=dict(spent_test_ink),
        successor=successor,
        digest=digest(body),
    )


class HandoffError(Exception):
    pass


def continue_from(
    record: SignedHandoff,
    *,
    carried_states: Mapping[str, State],
    spent_test_ink: Mapping[str, Fraction] | None = None,
    chosen_pen: str | None = None,
) -> SignedHandoff:
    """Authorise continuation only if it matches the signed record exactly.

    Refuses to continue if the carried-over chambers were *restored* (more ink
    than the record allows), if the chosen pen differs, or if the spent-test-ink
    accounting was rolled back.
    """
    for pen, state in carried_states.items():
        allowed = record.carried_states.get(pen)
        if allowed is None:
            raise HandoffError(f"pen {pen!r} is not on the signed record")
        if state.nib > allowed.nib or state.belly > allowed.belly:
            raise HandoffError(
                f"pen {pen!r} carries more ink ({_s(state.nib)}/{_s(state.belly)}) "
                f"than the signed record ({_s(allowed.nib)}/{_s(allowed.belly)}): "
                "ink may not be restored"
            )
    if spent_test_ink is not None:
        for pen, spent in spent_test_ink.items():
            if Fraction(spent) < Fraction(record.spent_test_ink.get(pen, 0)):
                raise HandoffError(
                    f"spent test ink for {pen!r} was rolled back"
                )
    if chosen_pen is not None and chosen_pen != record.chosen_pen:
        raise HandoffError(
            f"chosen pen {chosen_pen!r} disagrees with the signed record "
            f"({record.chosen_pen!r})"
        )
    if digest(record.canonical()) != record.digest:
        raise HandoffError("signed record digest does not verify")
    return record
