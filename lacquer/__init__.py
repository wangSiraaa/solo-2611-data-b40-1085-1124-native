"""Lacquer stacking-box workshop.

The package mirrors the physical workflow:

* :mod:`lacquer.notebook` -- stable-identity gauge journals, append-only page
  revisions, closed-walk reconstruction and the finite worn-base candidates;
* :mod:`lacquer.sampling` -- sampling only over usable segments of an old
  ``citation-product/1`` export;
* :mod:`lacquer.witness` -- fixed vertical-line witnesses;
* :mod:`lacquer.packing` -- joint fitting and robust packing sheets that fit
  every still-possible configuration;
* :mod:`lacquer.workbench` -- gauge/trial-bite questions, beat budgets and
  named one-time wax tongues;
* :mod:`lacquer.planner` -- the adaptive verification plan and the go/no-go
  release decision for one fixed sheet.
"""

from __future__ import annotations

from .errors import LacquerError
from .model import (
    Base,
    Lid,
    Ring,
    Vessel,
    VesselConfiguration,
    lower_lugs_fit_upper,
    ring_from_features,
)
from .notebook import (
    Journal,
    JournalBook,
    Page,
    Reconstruction,
    CELL_COLLISION,
    CLOSURE_FAILURE,
    NO_USABLE_PAGES,
)
from .packing import (
    PackingProblem,
    PackingSheet,
    Seat,
    enumerate_worlds,
    find_robust_sheets,
    find_seatings_for_world,
    worlds_fit_sheet,
)
from .planner import PlanNode, build_plan, evaluate_all_worlds, simulate_shift
from .witness import Witness
from .workbench import (
    GAUGE,
    TRIAL_BITE,
    GaugeQuestion,
    ResourceLedger,
    StationOffer,
    TrialBiteQuestion,
    WorkOrder,
    answer_question,
    run_question,
)

__all__ = [
    "LacquerError",
    "Base",
    "Lid",
    "Ring",
    "Vessel",
    "VesselConfiguration",
    "lower_lugs_fit_upper",
    "ring_from_features",
    "Journal",
    "JournalBook",
    "Page",
    "Reconstruction",
    "CELL_COLLISION",
    "CLOSURE_FAILURE",
    "NO_USABLE_PAGES",
    "PackingProblem",
    "PackingSheet",
    "Seat",
    "enumerate_worlds",
    "find_robust_sheets",
    "find_seatings_for_world",
    "worlds_fit_sheet",
    "PlanNode",
    "build_plan",
    "evaluate_all_worlds",
    "simulate_shift",
    "Witness",
    "GAUGE",
    "TRIAL_BITE",
    "GaugeQuestion",
    "ResourceLedger",
    "StationOffer",
    "TrialBiteQuestion",
    "WorkOrder",
    "answer_question",
    "run_question",
]
