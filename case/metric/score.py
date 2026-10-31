"""Arena metric command: one example on stdin, {"score": ..., "statistics": {...}} on stdout.

stdin:  {"example": {"id", "input", "reference"}, "answer": "<translated script>"}
input:  JSON string {"direction": "gp2trino" | "trino2gp", "script": "<source script>"}
reference: JSON string {"target", "states", "outputs": {"wrk.t": {"hashes": [...], "rows": [...]}}}

Exit code is non-zero only for infrastructure failures (engine down, bad configuration); any
property of the translation itself is expressed as score 0 with diagnostics in statistics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqltr_metric import config  # noqa: E402
from sqltr_metric.evaluate import PgCache, score_answer  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    example = payload["example"]
    task = json.loads(example["input"])
    reference = json.loads(example["reference"])
    cfg = config.load()
    cache = PgCache(cfg) if cfg.cache else None
    try:
        result = score_answer(example["id"], task, reference, payload.get("answer", ""), cfg, cache)
    finally:
        if cache is not None:
            cache.close()
    print(json.dumps({"score": result.score, "statistics": result.statistics}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
