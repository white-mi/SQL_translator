"""Local runner: the arena evaluation loop without the arena, for development and baselines.

For every example: run the candidate exactly like the arena runner does (fresh process,
`python -E -s solve.py`, JSON on stdin, answer on stdout), then score the answer with the same
metric code. Results are aggregated per direction, template and feature class.
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
    solver = Path(solver).resolve()
    rows = [e for e in dataset if directions is None or meta[e["id"]]["direction"] in directions]
    if limit:
        rows = rows[:limit]

    def one(example: dict) -> dict:
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
    def agg(key_fn):
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
