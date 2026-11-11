"""The baseline candidate honours the arena runner contract and never crashes."""

import json
import subprocess
import sys
from pathlib import Path

SOLVER = Path(__file__).resolve().parents[1] / "case" / "ways" / "baseline" / "solve.py"


def _run(stdin: bytes) -> dict:
    """Run the baseline solve.py like the arena runner, feed it `stdin`, return its parsed stdout.

    Asserts that the process exits with 0. It runs with cwd /tmp, so it needs Linux or WSL.
    """
    proc = subprocess.run([sys.executable, "-E", "-s", str(SOLVER)], input=stdin, capture_output=True,
                          cwd="/tmp", timeout=60)
    assert proc.returncode == 0
    return json.loads(proc.stdout)


def test_translates_trino_to_postgres():
    """Smoke test: a trino2gp task yields an answer that still contains CREATE TABLE.

    Whether the translation is right is the metric's job; this checks only that one comes back.
    """
    task = {"direction": "trino2gp", "script": "CREATE TABLE wrk.t AS SELECT try_cast(x AS BIGINT) AS v FROM retail.a;"}
    out = _run(json.dumps({"input": json.dumps(task), "assets_dir": "/assets", "uses_model": False}).encode())
    assert "CREATE TABLE" in out["answer"].upper()


def test_garbage_input_still_returns_an_answer():
    assert "answer" in _run(b"not json at all")
    task = {"direction": "gp2trino", "script": "CREATE TABLE ((( broken"}
    out = _run(json.dumps({"input": json.dumps(task)}).encode())
    assert isinstance(out["answer"], str)
