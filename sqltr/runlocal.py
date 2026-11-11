"""Local runner: the arena evaluation loop without the arena, for development and baselines.

For every example: run the candidate like the arena runner does (fresh process,
`python -E -s solve.py`, JSON on stdin, answer on stdout), then score the answer with the same
metric code (`sqltr_metric.evaluate.score_answer`). Results are aggregated per direction, template
and feature class. Entry point: `python -m sqltr run-local`.

Differences from the runner image (`case/harness/main.py`): the payload also carries
`"uses_model": false`, the output size is not limited, and a crash is recorded as
`statistics.failure = "solver_failure"` instead of a marked answer. The candidate runs with cwd
`/tmp`, so this works only on Linux or WSL.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqltr_metric.config import Config
from sqltr_metric.evaluate import score_answer


def run_candidate(solver: Path, example_input: str, timeout: float = 120) -> tuple[str, float, str]:
    """Run the candidate on one example in a fresh process and return its answer.

    Args:
        solver: the candidate's solve.py; it runs under this interpreter as `python -E -s`, with
            cwd `/tmp`.
        example_input: the example's `input` string (JSON of direction and script), passed on as is.
        timeout: seconds before the process is killed.

    Returns:
        `(answer, seconds, error)`. On success error is "" and answer is the `answer` field of the
        candidate's stdout, unchecked. On any failure (non-zero exit, timeout, invalid JSON, no
        `answer` field, a process that cannot start) answer is "" and error is
        `"solver_failure: <exception type>"`; nothing is raised.
    """
    payload = json.dumps({"input": example_input, "assets_dir": "/assets", "uses_model": False},
                         ensure_ascii=False)
    started = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-E", "-s", str(solver)], input=payload.encode(),
                              capture_output=True, timeout=timeout, cwd="/tmp", check=True)
        answer = json.loads(proc.stdout)["answer"]
        return answer, time.monotonic() - started, ""
    except Exception as error:  # noqa: BLE001 - a crash is a candidate failure
        return "", time.monotonic() - started, f"solver_failure: {type(error).__name__}"


def evaluate_split(dataset: list[dict], meta: dict[str, dict], solver: Path, cfg: Config, workers: int = 4,
                   directions: set[str] | None = None, limit: int | None = None, cache=None) -> list[dict]:
    """Run the candidate on every selected example and score each answer.

    Args:
        dataset: rows of dataset.jsonl (`id`, `input`, `reference`).
        meta: meta.jsonl rows by example id; must cover every example of `dataset`.
        solver: the candidate's solve.py; resolved to an absolute path first.
        workers: threads; each runs one example at a time, candidate process and then metric.
        directions: keep only examples of these directions, e.g. `{"trino2gp"}`; None keeps all.
        limit: keep only the first N examples after the direction filter; None or 0 keeps all.
        cache: a `sqltr_metric.evaluate.PgCache`, or None to execute every answer.

    Returns:
        One row per example, in dataset order: id, direction, template, family, features, score
        (0 or 1), the metric's statistics, solver_seconds, metric_seconds, solver_error and the
        answer. A crashed candidate scores 0 with `statistics["failure"] = "solver_failure"`.

    Raises:
        InfraError: an engine fault in the metric; it aborts the whole run.
    """
    solver = Path(solver).resolve()
    rows = [e for e in dataset if directions is None or meta[e["id"]]["direction"] in directions]
    if limit:
        rows = rows[:limit]

    def one(example: dict) -> dict:
        """Run the candidate on one example, score its answer and return the result row."""
        answer, solve_s, solver_error = run_candidate(solver, example["input"])
        task = json.loads(example["input"])
        reference = json.loads(example["reference"])
        started = time.monotonic()
        result = score_answer(example["id"], task, reference, answer, cfg, cache)
        if solver_error:
            result.statistics["failure"] = "solver_failure"
        info = meta[example["id"]]
        return {"id": example["id"], "direction": info["direction"], "template": info["template"],
                "family": info["family"], "features": info["features"], "score": result.score,
                "statistics": result.statistics, "solver_seconds": round(solve_s, 3),
                "metric_seconds": round(time.monotonic() - started, 3), "solver_error": solver_error,
                "answer": answer}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, rows))


def summarize(results: list[dict]) -> dict:
    """Aggregate `evaluate_split` rows into the summary that run-local saves as summary.json.

    Returns:
        A dict with `examples`; `score`, the mean of the per-direction scores, so each direction
        weighs the same whatever its example count (None without rows); `by_direction`,
        `by_template` (keys `direction/template`) and `by_feature` (keys `direction/feature`; an
        example counts once per feature), each `{key: {"n", "score"}}`; `failures`, the count per
        failure kind (a string failure, or the `kind` of a dict one), most frequent first; and
        `solver_seconds_mean` and `metric_seconds_mean` per example.
    """

    def agg(key_fn):
        """Group scores by the keys `key_fn` gives each row; return `{key: {"n", "score"}}`."""
        groups: dict[str, list[float]] = defaultdict(list)
        for row in results:
            for key in key_fn(row):
                groups[key].append(row["score"])
        return {k: {"n": len(v), "score": round(sum(v) / len(v), 4)} for k, v in sorted(groups.items())}

    failures: dict[str, int] = defaultdict(int)
    for row in results:
        failure = row["statistics"].get("failure")
        if failure:
            kind = failure if isinstance(failure, str) else failure.get("kind", "?")
            failures[kind] += 1
    by_direction = agg(lambda r: [r["direction"]])
    directions = [v["score"] for v in by_direction.values()]
    return {
        "examples": len(results),
        "score": round(sum(directions) / len(directions), 4) if directions else None,
        "by_direction": by_direction,
        "by_template": agg(lambda r: [f"{r['direction']}/{r['template']}"]),
        "by_feature": agg(lambda r: [f"{r['direction']}/{f}" for f in r["features"]]),
        "failures": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "solver_seconds_mean": round(sum(r["solver_seconds"] for r in results) / max(1, len(results)), 3),
        "metric_seconds_mean": round(sum(r["metric_seconds"] for r in results) / max(1, len(results)), 3),
    }
