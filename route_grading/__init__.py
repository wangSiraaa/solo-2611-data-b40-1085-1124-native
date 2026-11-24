"""Route-grading layer on top of the case-file citation product.

The package turns exported ``citation-product/1`` rows into gradable bouldering
routes (a RANGE opens the holds, a PINNED lists the start/finish signs), grades
them at a group's actual arm span, and plans multi-group rounds with lane
covers or relay numbers.

It never reads PDFs or images, and it never treats a colour name as a hold
identity: holds are identified by ``(volume_id, page_id)`` / a stable
``hold_id``.
"""

__version__ = "1.0.0"

from .model import (  # noqa: F401
    BOTH_HANDS,
    LEFT,
    RIGHT,
    ClimbPath,
    GradeResult,
    Hold,
    RequiredVisits,
    Route,
    Step,
    enumerate_min_paths,
    grade,
    make_hold,
    manhattan,
    min_steps,
)
