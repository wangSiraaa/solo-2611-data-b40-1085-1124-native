"""Independent observation-semantics service and decimal delivery library.

Two responsibilities with a real boundary:

* ``obsdelivery.semantics`` / ``obsdelivery.service`` interpret original
  binary32 bit patterns exactly and judge them against references.  This works
  with no delivery library present.
* ``obsdelivery.delivery_algo`` / ``obsdelivery.delivery`` take a *published*
  semantic product and an independent reader contract and decide a concrete
  decimal delivery, fail-closed and all-or-nothing.
"""

from .contracts import ReaderContract
from .delivery import DeliveryService
from .delivery_algo import ScaleAttempt, Selection, round_half_even, select_representation
from .errors import (
    BatchRejected,
    DeliveryIdConflict,
    DeliveryNotFound,
    DeliveryRejected,
    IntegrityError,
    ProductConflict,
    ProductNotFound,
    ReferenceConflict,
    ReferenceNotFound,
    ReportConflict,
    ReportNotFound,
    ValidationError,
)
from .semantics import decode, interpret, judge, parse_bits
from .service import SemanticsService
from .storage import Store
from .twist import (
    PointReading,
    PrintedResidual,
    Segment,
    TwistLedger,
    build_ledger,
    combine_residuals,
    encode_bits,
    print_residual,
    verify_against_original,
    verify_ledger,
)
from .ropes import (
    HEAD,
    TAIL,
    Axis,
    InchRow,
    Interval,
    PairEvaluation,
    Span,
    evaluate_pair,
    make_intervals,
)
from .workshop import (
    BENCH_A,
    BENCH_B,
    BLOCKED,
    CUT,
    CUT_NOW,
    WAIT,
    CutOrder,
    InchId,
    PrepOrder,
    SpanRef,
    Ticket,
    Workshop,
    Yarn,
)

__all__ = [
    "ReaderContract",
    "DeliveryService",
    "ScaleAttempt",
    "Selection",
    "round_half_even",
    "select_representation",
    "BatchRejected",
    "DeliveryIdConflict",
    "DeliveryNotFound",
    "DeliveryRejected",
    "IntegrityError",
    "ProductConflict",
    "ProductNotFound",
    "ReferenceConflict",
    "ReferenceNotFound",
    "ReportConflict",
    "ReportNotFound",
    "ValidationError",
    "decode",
    "interpret",
    "judge",
    "parse_bits",
    "SemanticsService",
    "Store",
    # twist ledger
    "PointReading",
    "PrintedResidual",
    "Segment",
    "TwistLedger",
    "build_ledger",
    "combine_residuals",
    "encode_bits",
    "print_residual",
    "verify_against_original",
    "verify_ledger",
    # ropes
    "HEAD",
    "TAIL",
    "Axis",
    "InchRow",
    "Interval",
    "PairEvaluation",
    "Span",
    "evaluate_pair",
    "make_intervals",
    # workshop
    "BENCH_A",
    "BENCH_B",
    "BLOCKED",
    "CUT",
    "CUT_NOW",
    "WAIT",
    "CutOrder",
    "InchId",
    "PrepOrder",
    "SpanRef",
    "Ticket",
    "Workshop",
    "Yarn",
]
