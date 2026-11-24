"""Stable error codes for the lacquer workshop."""

from __future__ import annotations

from typing import Any


class LacquerError(Exception):
    """An expected, explainable failure with a machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# Packing / work-order errors
NO_FEASIBLE_PACKING = "no_feasible_packing"
NOT_ROBUST = "packing_not_robust"
UNKNOWN_PART = "unknown_part"
UNKNOWN_STATION = "unknown_station"
BUDGET_EXHAUSTED = "beat_budget_exhausted"
TONGUE_UNAVAILABLE = "tongue_unavailable"
ORIGINAL_TONGUE_REQUIRED = "original_tongue_required"
TEST_NOT_OFFERED = "test_not_offered"
COMMITMENT_BLOCKED = "commitment_blocked"
INVALID_ORDER = "invalid_order"
WITNESS_VIOLATION = "witness_violation"
