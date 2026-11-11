"""Arena metric command: one example on stdin, {"score": ..., "statistics": {...}} on stdout.

The arena evaluator runs it as `python score.py` (metric.json) once per example. It puts its own
directory first on sys.path, so the `sqltr_metric` package next to it is the one imported.

stdin:  {"example": {"id", "input", "reference"}, "answer": "<translated script>"}
input:  JSON string {"direction": "gp2trino" | "trino2gp", "script": "<source script>"}
reference: JSON string {"target", "states", "outputs": {"wrk.t": {"hashes": [...], "rows": [...]}}}
stdout: one JSON line {"score": 1.0 | 0.0, "statistics": {...}}; keys in evaluate.score_answer

Settings come from config.load(): metric_config.json overridden by SQLTR_* variables.
Exit code is non-zero only when the measurement itself fails (engine down, bad configuration,
malformed stdin); any property of the translation itself is expressed as score 0 with
diagnostics in statistics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqltr_metric import config  # noqa: E402
from sqltr_metric.evaluate import PgCache, score_answer  # noqa: E402


def main() -> int:
    """Score the example on stdin, print the result as one JSON line and return exit code 0.

    Opens the score cache (evaluate.PgCache) when cfg.cache is set and closes it afterwards.
    InfraError and malformed input are not caught: they propagate, the process exits non-zero
    and the arena records no score instead of a wrong zero.
    """
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
