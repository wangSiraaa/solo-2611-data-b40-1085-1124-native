"""Cheese-model quality desk and guaranteed sample-cutting planner.

The public surface is stable and JSON/JSONL oriented:

* :func:`quality_check` filters candidate internal models from probe samples.
* :func:`explain_plane` explains whether a plane is guaranteed hole-free.
* :func:`plan_order` chooses sealed-label or inspection-table delivery.
* :class:`CheeseDesk` persists source/sample/cut/label audit state.
"""

from .geometry import CheeseInputError
from .planner import (decide_remaining_boxes, plan_order, plan_remaining,
                      plan_sealed, plan_table, plan_slices, plan_cuts,
                      qc_and_plan, reconstruct_pieces)
from .qc import (cuts_revealing_holes, check_block, explain_plane,
                 filter_models, quality_check, run_qc)
from .sample_io import (case_from_sample, quality_check_dataset,
                        quality_check_sample)
from .audit import (CheeseDesk, build_weighted_audit, open_desk,
                    replay_original_case)

qc_from_sample = quality_check_sample
qc_from_dataset = quality_check_dataset

__all__ = [
    "CheeseDesk",
    "CheeseInputError",
    "build_weighted_audit",
    "case_from_sample",
    "decide_remaining_boxes",
    "explain_plane",
    "filter_models",
    "open_desk",
    "plan_cuts",
    "plan_order",
    "plan_remaining",
    "plan_sealed",
    "plan_slices",
    "plan_table",
    "qc_and_plan",
    "qc_from_dataset",
    "qc_from_sample",
    "quality_check",
    "quality_check_dataset",
    "quality_check_sample",
    "reconstruct_pieces",
    "replay_original_case",
    "run_qc",
]
