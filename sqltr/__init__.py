"""SQLTR: dataset generator, reference builder and local runner for the SQL translation case."""

import sys
from pathlib import Path

# the metric package lives inside the arena case; make it importable in a plain checkout
_METRIC = Path(__file__).resolve().parent.parent / "case" / "metric"
if str(_METRIC) not in sys.path:
    sys.path.insert(0, str(_METRIC))

AS_OF = "2026-06-30"  # fixed "today" of the synthetic warehouse: scripts never use now()
