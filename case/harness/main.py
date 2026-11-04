"""SQLTR harness adapter: the arena runner protocol for model-free candidates, crash-tolerant.

The stock arena runner turns any candidate failure (non-zero exit, timeout, invalid JSON) into a
runner failure, and one runner failure stops a whole private series without results. A translation
program that crashes on one script is a bad translation of that script, not a broken measurement,
so this adapter turns candidate faults into an answer that the metric scores 0:

    -- sqltr-harness-error: <kind>
    -- <first lines of the candidate's stderr>

Faults of the task itself (malformed stdin from the evaluator, unsupported operation) still exit
non-zero: those are infrastructure problems and must stay loud.

Protocol (same as the arena's runner/main.py, non-model mode):
  stdin  {"input": str, "uses_model": false, "parameters"?: {...}, "prepare_timeout": float}
  stdout {"answer": str}
The candidate is run as `python -E -s /candidate/solve.py` with cwd /tmp and gets
{"input", "assets_dir": "/assets", "parameters"?} on stdin.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 1024 * 1024
MAX_TASK_BYTES = 8 * 1024 * 1024
SOLVE_TIMEOUT = 120.0  # seconds per example; the evaluator's ARENA_RUNNER_TIMEOUT must be larger
MARKER = "-- sqltr-harness-error:"
CANDIDATE = Path("/candidate")
ASSETS = "/assets"


def fault(kind: str, detail: str = "") -> str:
    lines = [f"{MARKER} {kind}"]
    lines += ["-- " + line for line in detail.strip().splitlines()[-12:] if line.strip()]
    return "\n".join(lines)[:2000] + "\n"


def run_candidate(payload: dict) -> str:
    solve = CANDIDATE / "solve.py"
    if not solve.is_file():
        return fault("missing_solve_py")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(
                [sys.executable, "-E", "-s", str(solve)],
                input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    errors="backslashreplace"),
                stdout=out, stderr=err, timeout=SOLVE_TIMEOUT, cwd="/tmp", check=False,
            )
        except subprocess.TimeoutExpired:
            return fault("timeout", f"solve.py exceeded {SOLVE_TIMEOUT:.0f} s")
        err.seek(0)
        stderr = err.read(64 * 1024).decode(errors="replace")
        if completed.returncode != 0:
            return fault("exit_code", f"solve.py exited with {completed.returncode}\n{stderr}")
        out.seek(0)
        raw = out.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return fault("output_too_large")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return fault("invalid_json", raw[:500].decode(errors="replace"))
    if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
        return fault("invalid_answer", "stdout must be {\"answer\": \"...\"}")
    return result["answer"]


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_TASK_BYTES + 1)
        if len(raw) > MAX_TASK_BYTES:
            raise ValueError("task too large")
        task = json.loads(raw)
        if (not isinstance(task, dict) or task.get("uses_model", True) is not False
                or task.get("operation", "solve") != "solve" or not isinstance(task.get("input"), str)
                or not isinstance(task.get("parameters", {}), dict)):
            raise ValueError("invalid task for the model-free sqltr harness")
    except Exception:  # noqa: BLE001 - evaluator-side problem: fail loudly
        print("runner failed: invalid task", file=sys.stderr)
        return 1
    payload = {"input": task["input"], "assets_dir": ASSETS}
    if "parameters" in task:
        payload["parameters"] = task["parameters"]
    answer = run_candidate(payload)
    output = json.dumps({"answer": answer}, ensure_ascii=False)
    if len(output.encode()) + 1 > MAX_BYTES:
        output = json.dumps({"answer": fault("answer_too_large")})
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
