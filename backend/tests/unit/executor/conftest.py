"""Makes the shared in-memory harness (tests/unit/planner/erasure_harness.py) importable."""

from __future__ import annotations

import sys
from pathlib import Path

_HARNESS = str(Path(__file__).resolve().parent.parent / "planner")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)
