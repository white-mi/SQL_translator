"""SQLTR: dataset generator, reference builder and local runner for the SQL translation case.

Run it as `python -m sqltr <command>` (see `sqltr/__main__.py`). Importing the package puts
`case/metric` on `sys.path`, so `sqltr_metric` imports from a plain checkout; a script that
uses `sqltr_metric` imports `sqltr` first.

`AS_OF` is the fixed "today" of the synthetic warehouse as an SQL date string: the templates in
`sqltr/gen` use it instead of `now()`. It must stay equal to `warehouse.AS_OF`.
"""

import sys
from pathlib import Path

# the metric package lives inside the arena case; make it importable in a plain checkout
_METRIC = Path(__file__).resolve().parent.parent / "case" / "metric"
if str(_METRIC) not in sys.path:
    sys.path.insert(0, str(_METRIC))

AS_OF = "2026-06-30"  # fixed "today" of the synthetic warehouse: scripts never use now()
