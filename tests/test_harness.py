"""Harness adapter: candidate faults become a marked answer; evaluator-side faults stay loud."""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sqltr_harness_main", ROOT / "case/harness/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setattr(module, "CANDIDATE", candidate)
    monkeypatch.setattr(module, "SOLVE_TIMEOUT", 5.0)

    def run(solve_source: str | None, task: dict | str) -> tuple[int, dict | None]:
        if solve_source is not None:
            (candidate / "solve.py").write_text(solve_source)
        raw = task if isinstance(task, str) else json.dumps(task)
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode())))
        monkeypatch.setattr(sys, "stdout", out)
        code = module.main()
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None)

    return module, run


TASK = {"input": "{\"direction\": \"trino2gp\", \"script\": \"SELECT 1\"}", "uses_model": False,
        "prepare_timeout": 60}


def test_normal_answer_passes_through(harness):
    _, run = harness
    code, out = run("import json,sys\nt=json.load(sys.stdin)\nprint(json.dumps({'answer': 'ok:' + t['assets_dir']}))\n",
                    TASK)
    assert code == 0 and out == {"answer": "ok:/assets"}


@pytest.mark.parametrize("source, kind", [
    ("raise RuntimeError('boom')\n", "exit_code"),
    ("import time\ntime.sleep(30)\n", "timeout"),
    ("print('not json')\n", "invalid_json"),
    ("print('{\"answer\": 5}')\n", "invalid_answer"),
    (None, "missing_solve_py"),
])
def test_candidate_faults_become_marked_answers(harness, source, kind):
    module, run = harness
    code, out = run(source, TASK)
    assert code == 0
    assert out["answer"].startswith(f"{module.MARKER} {kind}")


def test_crash_detail_carries_stderr_tail(harness):
    _, run = harness
    _, out = run("raise ValueError('unsupported construct LATERAL')\n", TASK)
    assert "unsupported construct LATERAL" in out["answer"]


@pytest.mark.parametrize("task", [
    "not json",
    {"input": "x", "uses_model": True, "model": "m", "model_base_url": "http://g/v1"},
    {"operation": "build", "uses_model": False},
    {"input": 5, "uses_model": False},
])
def test_invalid_tasks_fail_loudly(harness, task):
    _, run = harness
    code, out = run("print('{\"answer\": \"x\"}')\n", task)
    assert code == 1 and out is None


def test_metric_scores_marked_answer_zero_with_diagnostics():
    from sqltr_metric import config
    from sqltr_metric.evaluate import HARNESS_FAULT, score_answer

    answer = f"{HARNESS_FAULT} exit_code\n-- solve.py exited with 1\n-- KeyError: 'x'\n"
    reference = {"target": "gp", "states": ["s1"], "outputs": {"wrk.t": {"hashes": ["h"], "rows": [1]}}}
    result = score_answer("ex", {"direction": "trino2gp", "script": "SELECT 1"}, reference, answer,
                          config.load(), cache=None)
    assert result.score == 0.0
    failure = result.statistics["failure"]
    assert failure["kind"] == "candidate_fault" and failure["fault"] == "exit_code"
    assert "KeyError" in failure["detail"]
