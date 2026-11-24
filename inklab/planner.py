"""Pen scheduling under rate ambiguity (开笔/换笔安排).

We are given:

* an inventory of pens, each with a *current* state and an unknown rate
  assignment drawn from a finite set of ``World`` s (one world per possible
  assignment, e.g. "A fast, B slow" vs "A slow, B fast");
* a *plan*: an ordered list of actions.  ``Test`` draws one directed stroke with
  a pen and records the nib reading in an instrument bucket; ``DryTest``
  compares a nib against a named reference nib at the bench without spending
  ink; ``Dip`` sets chambers to desk-card amounts; ``Body`` draws a run of
  beats that must all complete, with the drawing pen chosen by a selector that
  sees *only recorded observations* — never the true rates (『不得偷分』).

Execution is per world.  A plan *guarantees* completion when the body run
completes in every world.  Its cost is the worst case over worlds of the ink
spent on probe strokes (『以各可能率中的最多试划耗墨计』), because that ink is
discarded relative to the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Hashable, Mapping, Sequence

from .instrument import Instrument
from .model import Beat, Pen, State, beat


@dataclass(frozen=True)
class World:
    name: str
    rates: Mapping[str, Mapping[str, Fraction]]  # pen -> direction -> rate

    def rate(self, pen: str, direction: str) -> Fraction:
        return Fraction(self.rates[pen][direction])


@dataclass(frozen=True)
class Obs:
    """A recorded observation: an action tag and the bucket it registered."""

    tag: str
    bucket: Hashable


@dataclass(frozen=True)
class DryFixture:
    """Bench fixture comparing a nib to a named reference nib.

    ``reference_rate`` is the fast-or-slow rate of the reference nib; the desk
    card says what "same tier" / "different tier" mean for each rate pair.
    """

    reference_rate: Fraction
    rate_values: tuple[Fraction, ...]  # the two possible rates, e.g. (0, 1)
    available: bool = True
    name: str = "bench"

    def compare(self, nib_rate: Fraction) -> Hashable | None:
        if not self.available:
            return None  # fixture removed: no evidence
        same = Fraction(nib_rate) == Fraction(self.reference_rate)
        return ("tier", "same" if same else "different")


# -- actions ---------------------------------------------------------------

@dataclass(frozen=True)
class Test:
    tag: str
    pen: str
    beat: Beat
    __test__ = False  # not a pytest test class despite the name


@dataclass(frozen=True)
class DryTest:
    tag: str
    pen: str
    fixture: DryFixture


@dataclass(frozen=True)
class Dip:
    tag: str
    pen: str
    nib: Fraction
    belly: Fraction


@dataclass(frozen=True)
class Body:
    tag: str
    beats: tuple[Beat, ...]
    select: Callable[[tuple[Obs, ...]], "str | None"]  # observations -> pen name

    def __post_init__(self) -> None:
        object.__setattr__(self, "beats", tuple(self.beats))


Action = Test | DryTest | Dip | Body


@dataclass
class WorldOutcome:
    world: str
    completed: bool
    observations: tuple[Obs, ...]
    chosen_pen: str | None
    test_ink: Fraction
    body_ink: Fraction
    leftover: Fraction
    failure: str | None = None


@dataclass
class PlanReport:
    name: str
    outcomes: dict[str, WorldOutcome]
    guaranteed: bool
    max_test_ink: Fraction
    max_leftover: Fraction

    def summary(self) -> dict:
        return {
            "name": self.name,
            "guaranteed": self.guaranteed,
            "max_test_ink": _s(self.max_test_ink),
            "max_leftover": _s(self.max_leftover),
            "worlds": {
                w: {
                    "completed": o.completed,
                    "chosen_pen": o.chosen_pen,
                    "test_ink": _s(o.test_ink),
                    "body_ink": _s(o.body_ink),
                    "leftover": _s(o.leftover),
                    "failure": o.failure,
                    "observations": [(ob.tag, ob.bucket) for ob in o.observations],
                }
                for w, o in self.outcomes.items()
            },
        }


def _s(v: Fraction) -> str:
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def evaluate(
    name: str,
    pens: Mapping[str, Pen],
    initial: Mapping[str, State],
    worlds: Sequence[World],
    plan: Sequence[Action],
    instrument: Instrument,
) -> PlanReport:
    """Execute ``plan`` in every world and summarise guarantee + cost."""
    outcomes: dict[str, WorldOutcome] = {}
    for world in worlds:
        states = {p: State(initial[p].nib, initial[p].belly) for p in pens}
        observations: list[Obs] = []
        test_ink = Fraction(0)
        body_ink = Fraction(0)
        chosen: str | None = None
        completed = True
        failure: str | None = None

        for action in plan:
            if isinstance(action, Test):
                pen = pens[action.pen]
                rate = world.rate(action.pen, action.beat.direction)
                res = beat(pen, states[action.pen], action.beat, rate)
                states[action.pen] = res.after
                if not res.drew:
                    completed = False
                    failure = f"test {action.tag} broke"
                    break
                test_ink += action.beat.cost
                observations.append(Obs(action.tag, instrument.key(res.after.nib)))
            elif isinstance(action, DryTest):
                bucket = action.fixture.compare(world.rate(action.pen, "h"))
                if bucket is not None:
                    observations.append(Obs(action.tag, bucket))
            elif isinstance(action, Dip):
                pen = pens[action.pen]
                states[action.pen] = State(
                    min(Fraction(action.nib), pen.nib_cap),
                    min(Fraction(action.belly), pen.belly_cap),
                )
            elif isinstance(action, Body):
                chosen = action.select(tuple(observations))
                if chosen is None:
                    completed = False
                    failure = f"body {action.tag}: no pen selected"
                    break
                pen = pens[chosen]
                state = states[chosen]
                for b in action.beats:
                    rate = world.rate(chosen, b.direction)
                    res = beat(pen, state, b, rate)
                    if not res.drew:
                        completed = False
                        failure = (
                            f"body {action.tag}: {chosen} broke on {b.direction} "
                            f"cost {_s(b.cost)}"
                        )
                        break
                    body_ink += b.cost
                    state = res.after
                states[chosen] = state
                if not completed:
                    break
            else:  # pragma: no cover
                raise TypeError(f"unknown action {action!r}")

        leftover = sum((states[p].total for p in pens), Fraction(0))
        outcomes[world.name] = WorldOutcome(
            world.name, completed, tuple(observations), chosen,
            test_ink, body_ink, leftover, failure,
        )

    guaranteed = all(o.completed for o in outcomes.values())
    max_test = max((o.test_ink for o in outcomes.values()), default=Fraction(0))
    max_left = max((o.leftover for o in outcomes.values()), default=Fraction(0))
    return PlanReport(name, outcomes, guaranteed, max_test, max_left)


def best_guaranteed(reports: Sequence[PlanReport]) -> PlanReport | None:
    """Guaranteed plan with the least worst-case probe ink (ties: least leftover)."""
    guaranteed = [r for r in reports if r.guaranteed]
    if not guaranteed:
        return None
    return min(guaranteed, key=lambda r: (r.max_test_ink, r.max_leftover))


def indistinguishable_worlds(
    pens: Mapping[str, Pen],
    initial: Mapping[str, State],
    worlds: Sequence[World],
    plan: Sequence[Action],
    instrument: Instrument,
) -> list[list[str]]:
    """Group worlds whose recorded observations are identical.

    If two worlds with *different* bodies end up with the same observation
    trail, no selector can tell them apart — that is the formal meaning of
    『登记为同一读数的两种供墨率不得偷分』.
    """
    groups: dict[tuple, list[str]] = {}
    for world in worlds:
        states = {p: State(initial[p].nib, initial[p].belly) for p in pens}
        trail: list[tuple[str, Hashable]] = []
        for action in plan:
            if isinstance(action, Test):
                res = beat(pens[action.pen], states[action.pen], action.beat,
                           world.rate(action.pen, action.beat.direction))
                states[action.pen] = res.after
                trail.append((action.tag, instrument.key(res.after.nib)))
            elif isinstance(action, DryTest):
                b = action.fixture.compare(world.rate(action.pen, "h"))
                if b is not None:
                    trail.append((action.tag, b))
        groups.setdefault(tuple(trail), []).append(world.name)
    return [g for g in groups.values() if len(g) > 1]


def worth_discriminating(
    with_probe: Sequence[PlanReport],
    without_probe: Sequence[PlanReport],
) -> dict:
    """『何时值得花墨去辨别』 — is the probe ink ever recovered?

    A probe is worth its ink when it turns a *non-guaranteed* baseline into a
    guaranteed plan, or when it lets a guaranteed plan draw the body with a
    cheaper pen.  Concretely:

    * ``baseline_guaranteed`` — does anything work with no evidence at all?
    * ``probe_guaranteed``    — does the evidence make some plan guaranteed?
    * ``worth_it``            — probe needed to guarantee, i.e. not baseline but
      yes with probe.
    * ``wasted``              — guaranteed already without the probe AND the
      probe costs ink (so the ink is pure waste).
    """
    base_best = best_guaranteed(without_probe)
    probe_best = best_guaranteed(with_probe)
    baseline_guaranteed = base_best is not None
    probe_guaranteed = probe_best is not None
    probe_ink = min((r.max_test_ink for r in with_probe if r.guaranteed),
                    default=Fraction(0))
    return {
        "baseline_guaranteed": baseline_guaranteed,
        "probe_guaranteed": probe_guaranteed,
        "worth_it": (not baseline_guaranteed) and probe_guaranteed,
        "wasted": baseline_guaranteed and probe_ink > 0,
        "probe_ink": probe_ink,
    }
