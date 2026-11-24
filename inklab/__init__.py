"""inklab — dip-pen ink-flow simulation, rate solving and pen scheduling.

Built on the existing ``obsdelivery`` exact binary32 observation store.

Modules:

* ``model``       two-chamber flow, exact Fractions, per-direction supply rates
* ``instrument``  instrument resolution / "same reading" indistinguishability
* ``solver``      which fixed rate combinations explain every chamber weighing
* ``ledger``      register weighings as true bit patterns; verify a re-openable
                  supply table by the exact difference of a canonical product
* ``planner``     pen choice / test-stroke / dry-clamp scheduling under ambiguity
* ``scenarios``   every worked claim in the exhibit brief, computed exactly
* ``demo``        human- and machine-readable run of all scenarios
"""

from .bits import decode_bits, encode_bits
from .handoff import (
    HandoffError,
    ObservationRecord,
    SignedHandoff,
    continue_from,
    sign_handoff,
)
from .instrument import (
    Instrument,
    contract_instrument,
    exact_instrument,
    group_instrument,
)
from .ledger import InkLedger, Weighing
from .model import (
    BELLY,
    CHAMBERS,
    DIRECTIONS,
    H,
    NIB,
    V,
    Beat,
    BeatResult,
    Pen,
    Rates,
    RunResult,
    State,
    beat,
    dip,
    run,
)
from .planner import (
    Body,
    DryFixture,
    DryTest,
    Dip,
    PlanReport,
    Test,
    World,
    best_guaranteed,
    evaluate,
    indistinguishable_worlds,
    worth_discriminating,
)
from .solver import Explanation, Observation, SolveResult, Trial, solve
from .trialgen import MAX_BEATS, MAX_PENS, Originals, OriginalRecord, generate_originals

__all__ = [
    "decode_bits", "encode_bits",
    "HandoffError", "ObservationRecord", "SignedHandoff", "continue_from", "sign_handoff",
    "Instrument", "contract_instrument", "exact_instrument", "group_instrument",
    "InkLedger", "Weighing",
    "BELLY", "CHAMBERS", "DIRECTIONS", "H", "NIB", "V",
    "Beat", "BeatResult", "Pen", "Rates", "RunResult", "State", "beat", "dip", "run",
    "Body", "DryFixture", "DryTest", "Dip", "PlanReport", "Test", "World",
    "best_guaranteed", "evaluate", "indistinguishable_worlds", "worth_discriminating",
    "Explanation", "Observation", "SolveResult", "Trial", "solve",
    "MAX_BEATS", "MAX_PENS", "Originals", "OriginalRecord", "generate_originals",
]
