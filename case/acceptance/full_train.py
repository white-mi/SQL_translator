"""What one candidate costs: the full train split through the arena, with the real runner and metric.

Runs in a throwaway directory with its own journal and evaluators; it never touches the campaign.
Operator only: the perfect candidate is built from the oracle translations in data/train/instances.jsonl,
so this tool stays out of the researcher workspace (ADR 0009).

Three measurements on the same 500 examples:
  perfect          every translation is right, metric cache off: the most expensive metric run;
                   the score must be exactly 1.0, which also proves the chain end to end
  baseline         the real starting candidate, metric cache on
  baseline-repeat  the same answers again: what the metric cache saves a descendant

    python case/acceptance/full_train.py --arena /opt/arena --work /srv/sqltr-fulltrain-1 --report out.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import ROOT, Acceptance  # noqa: E402

PERFECT_SOLVE = """\
import json
import sys
from pathlib import Path

task = json.loads(json.load(sys.stdin)["input"])
answers = json.loads((Path(__file__).parent / "answers.json").read_text())
print(json.dumps({"answer": answers[task["direction"] + "\\n" + task["script"]]}))
"""


class MemorySampler(threading.Thread):
    """Lowest MemAvailable seen. Reads /proc only, so the measurement adds no real load."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lowest_mb = None
        self.done = threading.Event()

    def run(self):
        while not self.done.wait(5):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) // 1024
                    self.lowest_mb = available if self.lowest_mb is None else min(self.lowest_mb, available)


def trino_peak_mb() -> int | None:
    proc = subprocess.run(["docker", "exec", "sqltr-trino-1", "cat", "/sys/fs/cgroup/memory.peak"],
                          capture_output=True, text=True)
    return int(proc.stdout) // 2**20 if proc.returncode == 0 and proc.stdout.strip().isdigit() else None


def build_candidates(work: Path) -> dict[str, Path]:
    ways = work / "ways"
    shutil.copytree(ROOT / "case" / "ways" / "baseline", ways / "baseline", ignore=shutil.ignore_patterns("__pycache__"))
    # a different snapshot with the same behaviour: the arena measures it again, the metric cache answers
    shutil.copytree(ways / "baseline", ways / "baseline-repeat")
    (ways / "baseline-repeat" / "REPEAT.txt").write_text("same answers as baseline\n")
    (ways / "perfect").mkdir()
    (ways / "perfect" / "solve.py").write_text(PERFECT_SOLVE)
    answers = {}
    for line in (ROOT / "data" / "train" / "instances.jsonl").read_text().splitlines():
        inst = json.loads(line)
        answers[inst["direction"] + "\n" + inst["source_script"]] = inst["oracle_script"]
    (ways / "perfect" / "answers.json").write_text(json.dumps(answers, ensure_ascii=False))
    return {p.name: p for p in ways.iterdir()}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arena", required=True, help="pinned arena checkout with .venv")
    p.add_argument("--work", required=True, help="new empty directory for this run")
    p.add_argument("--report", required=True, help="where to write the JSON report")
    p.add_argument("--runner-image", default="sqltr-runner:1")
    p.add_argument("--base-port", type=int, default=18281)
    p.add_argument("--cli-timeout", type=float, default=900)
    p.add_argument("--wait-timeout", type=float, default=7200)
    args = p.parse_args()

    run = Acceptance(args)
    run.poll_seconds = 30  # each poll starts a Python process, which would distort the timing on 2 vCPU
    work = run.work
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"{work} must be new or empty")
    (work / "logs").mkdir(parents=True, exist_ok=True)
    data = work / "data"
    (data / "train").mkdir(parents=True)
    shutil.copy2(ROOT / "data" / "train" / "dataset.jsonl", data / "train" / "dataset.jsonl")
    run.data = data

    sampler = MemorySampler()
    sampler.start()
    measured = []
    try:
        metric = run.freeze_metric("metric")
        ways = build_candidates(work)
        (work / "srv" / "search").mkdir(parents=True, exist_ok=True)
        journal = work / "srv" / "search" / "search.sqlite3"
        run.arena_cli("evaluator.search", "--journal", journal, "init", "--max-solutions", 3)
        common = {"ARENA_METRIC_PATH": str(metric), "ARENA_DATASET_PATH": str(data / "train" / "dataset.jsonl"),
                  "ARENA_CAMPAIGN_ID": "sqltr-full-train", "ARENA_CASE_ID": "sqltr", "ARENA_DATASET_ID": "train-full",
                  "ARENA_TRAIN_TRACKING": "false"}
        services = {
            "off": run.start("train-cache-off", "train", {**common, "ARENA_CONDITIONS_ID": "cache-off", "SQLTR_CACHE": "0"}),
            "on": run.start("train-cache-on", "train", {**common, "ARENA_CONDITIONS_ID": "cache-on"}),
        }
        flat = ["--hypothesis", "measurement", "--change", "measurement", "--tokens", "0", "--seconds", "0"]
        run.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", "baseline",
                      "--directory", ways["baseline"], *flat)
        for cid in ("baseline-repeat", "perfect"):
            run.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", cid,
                          "--directory", ways[cid], "--parent", "baseline", *flat)

        for cid, cache in (("perfect", "off"), ("baseline", "on"), ("baseline-repeat", "on")):
            token = run.token(f"train-cache-{cache}")
            result = run.train(services[cache], token, journal, cid, f"{cid}-full", "train-full", f"cache-{cache}")
            metrics = result.get("metrics") or {}
            failures: dict[str, int] = {}
            for example in result.get("examples") or []:
                failure = (example.get("statistics") or {}).get("failure")
                if failure:
                    kind = failure.get("kind") if isinstance(failure, dict) else failure
                    failures[kind] = failures.get(kind, 0) + 1
            row = {"candidate": cid, "metric_cache": cache, "status": result["status"], "score": metrics.get("score"),
                   "examples": len(result.get("examples") or []), "failures": failures,
                   "solver_seconds": round(metrics.get("solver_seconds") or 0, 1),
                   "metric_seconds": round(metrics.get("judge_seconds") or 0, 1),
                   "wall_minutes": round((metrics.get("wall_seconds") or 0) / 60, 1)}
            measured.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        run.stop_all()
        sampler.done.set()

    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runner_image": args.runner_image,
              "measurements": measured, "lowest_available_memory_mb": sampler.lowest_mb, "trino_peak_memory_mb": trino_peak_mb()}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    perfect = next((m for m in measured if m["candidate"] == "perfect"), None)
    return 0 if perfect and perfect["score"] == 1.0 and perfect["examples"] == 500 else 1


if __name__ == "__main__":
    raise SystemExit(main())
