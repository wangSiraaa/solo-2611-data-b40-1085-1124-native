"""Banquet reconstruction layer built on top of ``citation-product/1``.

The citation catalog (task 726) answers *what pages concretely exist, in what
stable identity and current order*.  This package uses those products to
reconstruct an old banquet:

* a **PINNED** citation is the sign-in sheet (who actually came);
* a **RANGE** citation is the ordinance, whose pages take effect one by one
  (ordering, adjacency, substitution, and single-clause revocations);
* from attendance + ordinance we enumerate every compliant seating chart, or
  produce a reviewable "no seating" basis, and suspend any session whose
  citations are missing or unresolved;
* on a chosen chart the interpretation team routes each round's private
  conversations along adjacent chairs, assigning a shared language and an
  interpreter to every segment, and tracks named clues, incidental
  overhearing and the secrecy agreement;
* the ritual office may take exactly one control measure for the event: a
  sound screen (installed in an empty round) or per-round seat swaps.

See ``banquet/README.md`` and ``scripts/demo_banquet.py``.
"""

from .clues import (
    Breach,
    ClueLedger,
    evaluate_secrecy,
    record_round_clues,
    secret_requirements,
)
from .control import (
    Control,
    ControlError,
    Night,
    NoControl,
    ScreenControl,
    SeatSwap,
    SwapControl,
    book_swap,
    can_install_screen,
    chart_for_round,
    find_swap_seats,
    listener_hears,
    simulate_night,
)
from .decode import DecodedSession, Script, decode_ordinance
from .model import (
    AdjacencyClause,
    Clause,
    Conversation,
    Edge,
    Guest,
    OrderClause,
    PageOrdinance,
    Round,
    SeatChart,
    SecretGroup,
    SubstituteClause,
    build_ordinance,
    enumerate_compliant_charts,
)
from .products import ProductError, ProductRow, ProductSet, load_product_files
from .relay import RoundPlan, Route, Segment, candidate_routes, plan_round
from .seating import SeatingResult, attendance_from_products, seat_session
from .sheet import Sheet, build_sheet

__all__ = [
    "AdjacencyClause",
    "Breach",
    "Clause",
    "ClueLedger",
    "Control",
    "ControlError",
    "Conversation",
    "DecodedSession",
    "Edge",
    "Guest",
    "Night",
    "NoControl",
    "OrderClause",
    "PageOrdinance",
    "ProductError",
    "ProductRow",
    "ProductSet",
    "Round",
    "RoundPlan",
    "Route",
    "ScreenControl",
    "Script",
    "SeatChart",
    "SeatSwap",
    "SecretGroup",
    "Segment",
    "SeatingResult",
    "Sheet",
    "SubstituteClause",
    "SwapControl",
    "attendance_from_products",
    "book_swap",
    "build_ordinance",
    "build_sheet",
    "can_install_screen",
    "candidate_routes",
    "chart_for_round",
    "decode_ordinance",
    "enumerate_compliant_charts",
    "evaluate_secrecy",
    "find_swap_seats",
    "listener_hears",
    "load_product_files",
    "plan_round",
    "record_round_clues",
    "seat_session",
    "secret_requirements",
    "simulate_night",
]
