"""Revisionable weighted record source and reproducible weighted sampler.

Three independently runnable modules live in this package:

* :mod:`wrsource.source`   -- SQLite backed, versioned source service.
* :mod:`wrsource.dataset`  -- reproducible weighted sample / manifest producer.
* :mod:`wrsource.elevator` -- tabletop elevator drill: reconstruct boarding
  histories from sampled count rows and run a mid-route handover dispatch
  under identity-blind driver readings.

The implementation intentionally uses only the Python 3.11 standard library.
"""

__all__ = ["source", "sampling", "dataset", "elevator"]
__version__ = "1.1.0"
