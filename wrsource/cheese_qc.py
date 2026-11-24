"""Compatibility alias for :mod:`wrsource.cheese.qc`."""
from .cheese.qc import *  # noqa: F401,F403

if __name__ == "__main__":
    from .cheese.cli import main
    raise SystemExit(main(["qc"] + __import__("sys").argv[1:]))
