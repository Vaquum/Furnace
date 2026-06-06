"""run_round must cap BLAS/OpenMP threads for the `limen run` subprocess so
concurrent sklearn fits don't oversubscribe the cores (profiled: 186 threads on
48 cores, ~35s/permutation for a logreg that should take ~1s)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_run_round_caps_blas_threads() -> None:
    """The limen subprocess env pins each thread-pool library to one thread.
    Quote-agnostic so reformatting (e.g. ruff's single-quote style) doesn't
    break the check while the behaviour is unchanged."""
    src = (REPO_ROOT / "furnace" / "run_round.py").read_text(encoding="utf-8")
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert re.search(rf"""['"]{var}['"]\s*:\s*['"]1['"]""", src), (
            f"run_round.py does not pin {var}=1"
        )
