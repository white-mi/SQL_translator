"""SQLTR harness adapter: the arena runner protocol for model-free candidates, crash-tolerant.

The stock arena runner turns any candidate failure (non-zero exit, timeout, invalid JSON) into a
runner failure, and one runner failure stops a whole private series without results. A translation
program that crashes on one script is a bad translation of that script, not a broken measurement,
so this adapter turns candidate faults into an answer that the metric scores 0:

    -- sqltr-harness-error: <kind>
    -- <last lines of the detail, such as the candidate's stderr>

Faults of the task itself (malformed stdin from the evaluator, unsupported operation) still exit
non-zero: those are infrastructure problems and must stay loud.

Protocol (same as the arena's runner/main.py, non-model mode):
  stdin  {"input": str, "uses_model": false, "operation"?: "solve", "parameters"?: {...}, ...}
  stdout {"answer": str}
Other keys, such as prepare_timeout, are ignored; the "build" operation is not supported.
The candidate is run as `python -E -s /candidate/solve.py` with cwd /tmp and gets
{"input", "assets_dir": "/assets", "parameters"?} on stdin; it must print {"answer": str}
within SOLVE_TIMEOUT seconds. main() is the entry point of the runner image (runner.Dockerfile).
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 1024 * 1024  # limit for the candidate's stdout and for this adapter's output line
MAX_TASK_BYTES = 8 * 1024 * 1024  # limit for the task read from stdin
SOLVE_TIMEOUT = 120.0  # seconds per example; the evaluator's ARENA_RUNNER_TIMEOUT must be larger
# prefix of a fault answer; keep it equal to HARNESS_FAULT in case/metric/sqltr_metric/evaluate.py
MARKER = "-- sqltr-harness-error:"
CANDIDATE = Path("/candidate")
ASSETS = "/assets"


def fault(kind: str, detail: str = "") -> str:
    """Return the answer that reports a candidate fault of kind `kind`; the metric scores it 0.

    The first line is "-- sqltr-harness-error: <kind>", followed by the last 12 non-empty lines
    of `detail` as SQL comments; the text is cut to 2000 characters and ends with a newline.
    evaluate.score_answer in the metric recognizes the marker (failure "candidate_fault").
    """
    lines = [f"{MARKER} {kind}"]
    lines += ["-- " + line for line in detail.strip().splitlines()[-12:] if line.strip()]
    return "\n".join(lines)[:2000] + "\n"


def run_candidate(payload: dict) -> str:
    """Run /candidate/solve.py once with `payload` as JSON on stdin and return its answer.

    A candidate fault becomes a fault() answer instead of an exception: solve.py missing,
    running longer than SOLVE_TIMEOUT, exiting non-zero (the end of its stderr is kept), printing
    more than MAX_BYTES, printing invalid JSON, or JSON without a string "answer".
    """
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
    """Handle one runner task from stdin and print {"answer": ...} as one JSON line.

    Returns 0 whenever an answer was printed, fault answers included; an answer longer than
    MAX_BYTES is replaced by the "answer_too_large" fault. Returns 1, with a message on stderr
    and no answer, for an invalid task: over MAX_TASK_BYTES, not JSON, uses_model not false,
    an operation other than "solve", a non-string input, or parameters that are not an object.
    """
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
