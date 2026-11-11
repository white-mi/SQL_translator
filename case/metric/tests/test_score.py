"""Integration tests of the metric on PostgreSQL, including cheating translations it must catch.

The trino2gp direction is fully testable on PostgreSQL alone: the target engine is PostgreSQL and
the reference comes from the paired Greenplum rendering. Trino-side checks run on the VM stand.
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from sqltr.gen.build import build_split
from sqltr.references import build_references
from sqltr_metric.evaluate import run_on_states, score_answer

METRIC = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cases(pg_cfg):
    """Return one trino2gp case per template: `{template: (instance, arena example)}`.

    Builds 40 dev families with seed 7 and their references in paired mode on PostgreSQL
    (`trino=False`), and asserts that none was skipped. Tests pick cases by template name ("tags",
    "loyalty", "stock_alerts"), so a change of seed, family count or template sampling that drops
    one of those templates fails them with KeyError.
    """
    instances = [i for i in build_split("dev", 40, seed=7) if i.direction == "trino2gp"]
    examples, meta, report = build_references(instances, pg_cfg, trino=False)
    assert not report.skipped
    by_id = {i.id: i for i in instances}
    out = {}
    for ex in examples:
        inst = by_id[ex["id"]]
        out.setdefault(inst.template, (inst, ex))
    return out


def _score(cfg, inst, ex, answer):
    """Score `answer` against the reference of the arena example `ex`; `inst` is unused."""
    return score_answer(ex["id"], json.loads(ex["input"]), json.loads(ex["reference"]), answer, cfg)


def test_oracle_translation_scores_one(pg_cfg, cases):
    for inst, ex in cases.values():
        result = _score(pg_cfg, inst, ex, inst.oracle_script)
        assert result.score == 1.0, (inst.template, result.statistics)


def test_identity_translation_fails_on_trino_only_constructs(pg_cfg, cases):
    inst, ex = cases["tags"]  # split/cardinality/transform lambdas do not exist in PostgreSQL
    result = _score(pg_cfg, inst, ex, inst.source_script)
    assert result.score == 0.0
    assert result.statistics["failure"]["kind"] == "sql_error"


def test_hardcoded_results_fail_on_other_states(pg_cfg, cases):
    """Cheating: create the output table and INSERT the values seen on state s1."""
    inst, ex = cases["loyalty"]
    runs = run_on_states(inst.oracle_script, "gp", inst.outputs, pg_cfg, states=["s1"])
    assert runs[0].ok
    import psycopg

    pg = pg_cfg.postgres
    with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                         password=pg.admin_password or None, autocommit=True) as c:
        from sqltr_metric.rewrite import gp_shim

        select = gp_shim(inst.oracle_script.split(" AS\n", 1)[1].split(";")[0])
        cur = c.execute(select.replace("retail.", "retail_s1."))
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    def lit(v):
        """Render a value as an SQL literal for the cheating INSERT (strings are not escaped)."""
        return "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else str(v))

    values = ", ".join("(" + ", ".join(lit(v) for v in row) + ")" for row in rows)
    cheat = (f"CREATE TABLE wrk.loyalty_check ({', '.join(c + ' ' + ('text' if c == 'segment' else 'bigint') for c in cols)});\n"
             f"INSERT INTO wrk.loyalty_check VALUES {values};")
    result = _score(pg_cfg, inst, ex, cheat)
    assert result.score == 0.0
    assert result.statistics["states_passed"] == 1  # right on s1, wrong on the mutated states


def test_dropping_statements_fails(pg_cfg, cases):
    """Cheating: keep only the first statement of a multi-statement script."""
    inst, ex = cases["stock_alerts"]
    first = inst.oracle_script.split(";")[0] + ";"
    result = _score(pg_cfg, inst, ex, first)
    assert result.score == 0.0
    assert result.statistics["failure"]["kind"] in ("mismatch", "missing_table")


def test_write_outside_allowed_schemas_is_rejected(pg_cfg, cases):
    inst, ex = cases["loyalty"]
    result = _score(pg_cfg, inst, ex, inst.oracle_script + "\nDELETE FROM retail.customers;")
    assert result.score == 0.0
    assert result.statistics["failure"] == "write_outside_allowed_schemas"


def test_runner_cannot_modify_source_data_even_when_static_check_is_evaded(pg_cfg, cases):
    """Unqualified write to a source table via search_path tricks is stopped by privileges."""
    inst, ex = cases["loyalty"]
    sneaky = "SET search_path = retail_s1; DELETE FROM customers;\n" + inst.oracle_script
    result = _score(pg_cfg, inst, ex, sneaky)
    assert result.score == 0.0
    assert result.statistics["failure"]["kind"] in ("permission", "sql_error")


def test_merge_is_rejected_for_greenplum_target(pg_cfg, cases):
    inst, ex = cases["stock_alerts"]
    answer = inst.oracle_script + "\nMERGE INTO wrk.stock_alerts a USING wrk.stock_alerts b ON false WHEN MATCHED THEN DELETE;"
    result = _score(pg_cfg, inst, ex, answer)
    assert result.statistics["failure"] == "target_dialect_gate"


def test_empty_and_broken_answers_score_zero(pg_cfg, cases):
    inst, ex = cases["loyalty"]
    assert _score(pg_cfg, inst, ex, "").statistics["failure"] == "empty_answer"
    broken = _score(pg_cfg, inst, ex, "CREATE TABLE wrk.loyalty_check AS SELEC 1;")
    assert broken.score == 0.0 and broken.statistics["failure"]["kind"] == "sql_error"


def test_metric_command_protocol(pg_cfg, cases):
    """score.py reads one example from stdin and prints a single JSON object."""
    inst, ex = cases["loyalty"]
    env = {**os.environ, "SQLTR_PG_HOST": pg_cfg.postgres.host, "SQLTR_CACHE": "0"}
    payload = json.dumps({"example": ex, "answer": inst.oracle_script})
    proc = subprocess.run([sys.executable, str(METRIC / "score.py")], input=payload.encode(), env=env,
                          capture_output=True, check=True, cwd="/")
    out = json.loads(proc.stdout)
    assert out["score"] == 1.0 and "statistics" in out


def test_infrastructure_failure_is_loud(pg_cfg, cases):
    """Engine down => non-zero exit, never a silent score of 0."""
    inst, ex = cases["loyalty"]
    env = {**os.environ, "SQLTR_PG_HOST": "127.0.0.1", "SQLTR_PG_PORT": "1", "SQLTR_CACHE": "0"}
    payload = json.dumps({"example": ex, "answer": inst.oracle_script})
    proc = subprocess.run([sys.executable, str(METRIC / "score.py")], input=payload.encode(), env=env,
                          capture_output=True, cwd="/")
    assert proc.returncode != 0
    assert b"score" not in proc.stdout


def test_metric_is_deterministic(pg_cfg, cases):
    inst, ex = random.Random(1).choice(list(cases.values()))
    first = _score(pg_cfg, inst, ex, inst.oracle_script)
    second = _score(pg_cfg, inst, ex, inst.oracle_script)
    assert first.score == second.score == 1.0
