"""Revisionable weighted record source and reproducible weighted sampler.

Two independently runnable modules live in this package:

* :mod:`wrsource.source`  -- SQLite backed, versioned source service.
* :mod:`wrsource.dataset` -- reproducible weighted sample / manifest producer.

The newspaper extension adds:

* :mod:`wrsource.steno`     -- practice-card cuts, stroke semantics and
  manuscript readings (pure logic).
* :mod:`wrsource.printshop` -- type-stick editor, lock safety and the two
  morning/evening press processes (pure logic).
* :mod:`wrsource.proofdesk` -- binding of the proof desk to the source service
  and sampled dataset directories, plus a CLI.

The implementation intentionally uses only the Python 3.11 standard library.
"""

__all__ = ["source", "sampling", "dataset", "steno", "printshop",
           "proofdesk"]
__version__ = "1.1.0"
