"""Core measurement: run a script on K data states in isolated schemas and hash output tables.

Used both by the frozen metric (score a translation against reference hashes) and by the
trusted reference builder (compute reference hashes). One code path, one set of rules.

Entry points: score_answer (called once per example by score.py and run-local), run_on_states
(also used by sqltr/references.py) and PgCache (the score cache in PostgreSQL).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field

from . import METRIC_VERSION
from .config import Config
from .engines import InfraError, make_engine
from .normalize import table_hash
from .rewrite import gp_incompatible, gp_shim, isolate, outside_writes

# dialect -> engine that executes it
DIALECT_ENGINE = {"gp": "postgres", "trino": "trino"}
# direction -> (source dialect, target dialect)
DIRECTIONS = {"gp2trino": ("gp", "trino"), "trino2gp": ("trino", "gp")}
# prefix of the answer the harness adapter (case/harness/main.py) returns when the candidate fails
# (crash, timeout, bad output); keep it equal to MARKER there
HARNESS_FAULT = "-- sqltr-harness-error:"


@dataclass
class StateRun:
    """Result of running one script on one data state (one item of run_on_states()).

    Attributes:
        ok: the script ran without error; when False, hashes and rows stay empty.
        hashes: output name as given in `outputs` (e.g. "wrk.t") -> normalize.table_hash of the
            table, or None if the script did not create it.
        rows: output name -> row count, or None if the table is missing.
        error_kind, error, statement_index, statements, seconds: kind, message, statement_index,
            statements and seconds of the engine's ExecOutcome.
    """

    state: str
    ok: bool
    hashes: dict[str, str | None] = field(default_factory=dict)
    rows: dict[str, int | None] = field(default_factory=dict)
    error_kind: str = ""
    error: str = ""
    statement_index: int = -1
    statements: int = 0
    seconds: float = 0.0


def prepare_script(script: str, dialect: str) -> str:
    """Return the script as the measurer executes it: Greenplum through gp_shim, Trino unchanged."""
    return gp_shim(script) if dialect == "gp" else script


def run_on_states(script: str, dialect: str, outputs: list[str], cfg: Config,
                  states: list[str] | None = None, stop_on_failure: bool = False,
                  engine=None) -> list[StateRun]:
    """Execute `script` on each data state in fresh schemas and hash its output tables.

    For each state: create physical writable schemas `<logical>_<run id>_<state>`, map the source
    schema to cfg.physical_source(state), rewrite qualified names with rewrite.isolate, execute
    with the last writable schema as the default schema, hash every output table, and drop the
    schemas again, also on errors. All states share one deadline, cfg.example_timeout_s from the
    start of the call. Called by score_answer for answers and by sqltr/references.py for
    references and the self-check.

    Args:
        dialect: "gp" (runs on PostgreSQL after gp_shim) or "trino".
        outputs: logical output tables as "schema.table" (e.g. "wrk.t"), read after a
            successful run.
        states: defaults to cfg.states.
        stop_on_failure: stop after the first state on which the script fails.
        engine: an open engine for `dialect` to reuse across calls; the caller owns and closes
            it. Without it, one is created and closed here.

    Returns:
        One StateRun per state run, in order.

    Raises:
        InfraError: an engine or connection failure. Driver errors outside the script itself
            (creating schemas, reading tables) propagate unwrapped.
    """
    states = states or cfg.states
    own_engine = engine is None
    engine = engine or make_engine(DIALECT_ENGINE[dialect], cfg)
    prepared = prepare_script(script, dialect)
    run_id = uuid.uuid4().hex[:10]
    results: list[StateRun] = []
    deadline = time.monotonic() + cfg.example_timeout_s
    try:
        for state in states:
            tag = f"{run_id}_{state}"
            mapping = {cfg.source_schema: cfg.physical_source(state)}
            physical = {}
            for logical in cfg.writable_schemas:
                physical[logical] = f"{logical}_{tag}"
            mapping.update(physical)
            engine.create_schemas(list(physical.values()))
            try:
                outcome = engine.execute(isolate(prepared, mapping), physical[cfg.writable_schemas[-1]],
                                         deadline)
                run = StateRun(state, outcome.ok, error_kind=outcome.kind, error=outcome.message,
                               statement_index=outcome.statement_index, statements=outcome.statements,
                               seconds=outcome.seconds)
                if outcome.ok:
                    for name in outputs:
                        schema, table = name.split(".", 1)
                        data = engine.read_table(mapping.get(schema, schema), table)
                        if data is None:
                            run.hashes[name], run.rows[name] = None, None
                        else:
                            run.hashes[name] = table_hash(data.columns, data.rows)
                            run.rows[name] = len(data.rows)
                results.append(run)
            finally:
                engine.drop_schemas(list(physical.values()))
            if stop_on_failure and not results[-1].ok:
                break
    finally:
        if own_engine:
            engine.close()
    return results


# ---------------------------------------------------------------- scoring


@dataclass
class Score:
    """Metric result for one answer: `score` 1.0 or 0.0 and JSON-ready `statistics`."""

    score: float
    statistics: dict


def score_answer(example_id: str, task: dict, reference: dict, answer: str, cfg: Config,
                 cache=None) -> Score:
    """Score one translated script: 1.0 if every output table matches on every state, else 0.0.

    Checks run in order; the first that fails gives 0 with its reason in statistics["failure"]:
    empty answer ("empty_answer"); harness fault marker ({"kind": "candidate_fault", "fault",
    "detail"}); a write outside the writable schemas, see rewrite.outside_writes
    ("write_outside_allowed_schemas", with "details"); for a Greenplum target,
    rewrite.gp_incompatible ("target_dialect_gate", with "details"). Then the answer runs on the
    target engine over the reference's states and stops at the first failing state. The first
    problem found is recorded as {"state", "kind", "statement", "error"} for an execution error,
    or {"state", "kind": "missing_table" | "mismatch", "table", "rows_expected", "rows_got"} for
    an output table. Called once per example by score.py and sqltr/runlocal.py.

    Args:
        example_id: dataset id; part of the cache key.
        task: decoded example input; only "direction" is used.
        reference: decoded reference {"target", "states", "outputs": {name: {"hashes": [one per
            state], "rows": [...]}}}; its "states" override cfg.states.
        answer: the candidate's script, or the harness fault marker.
        cache: PgCache or another object with get/put, or None to always execute.

    Returns:
        Score. statistics always has "direction", "target" and "metric_version"; once the answer
        was executed also "states_passed", "tables_matched", "tables_checked" (over the states
        compared), "statements" and "engine_seconds". Executed results are cached; a result
        served from the cache has "cache": "hit".

    Raises:
        InfraError: engine or cache failure; the caller must not record a score.
    """
    direction = task["direction"]
    _, target = DIRECTIONS[direction]
    outputs = list(reference["outputs"])
    states = reference.get("states", cfg.states)
    stats: dict = {"direction": direction, "target": target, "metric_version": METRIC_VERSION}

    if not isinstance(answer, str) or not answer.strip():
        stats.update(failure="empty_answer")
        return Score(0.0, stats)
    if answer.lstrip().startswith(HARNESS_FAULT):
        # the harness adapter reports a crash/timeout of the candidate program as a marked answer
        first, _, rest = answer.lstrip().partition("\n")
        stats.update(failure={"kind": "candidate_fault", "fault": first[len(HARNESS_FAULT):].strip(),
                              "detail": rest[:1500]})
        return Score(0.0, stats)

    outside = outside_writes(answer, set(cfg.writable_schemas))
    if outside:
        stats.update(failure="write_outside_allowed_schemas", details=outside[:5])
        return Score(0.0, stats)
    if target == "gp":
        issues = gp_incompatible(answer)
        if issues:
            stats.update(failure="target_dialect_gate", details=issues)
            return Score(0.0, stats)

    key = hashlib.sha256(f"{METRIC_VERSION}|{example_id}|{answer}".encode()).hexdigest()
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            hit["statistics"]["cache"] = "hit"
            return Score(hit["score"], hit["statistics"])

    runs = run_on_states(answer, target, outputs, cfg, states=states, stop_on_failure=True)
    matched_tables = 0
    total_tables = 0
    states_passed = 0
    failure = None
    for run in runs:
        if not run.ok:
            failure = {"state": run.state, "kind": run.error_kind, "statement": run.statement_index,
                       "error": run.error}
            break
        state_index = states.index(run.state)
        state_ok = True
        for name in outputs:
            total_tables += 1
            expected = reference["outputs"][name]["hashes"][state_index]
            got = run.hashes.get(name)
            if got is not None and got == expected:
                matched_tables += 1
                continue
            state_ok = False
            if failure is None:
                rows_expected = reference["outputs"][name].get("rows", [None] * len(states))
                failure = {"state": run.state, "kind": "missing_table" if got is None else "mismatch",
                           "table": name, "rows_expected": rows_expected[state_index],
                           "rows_got": run.rows.get(name)}
        if not state_ok:
            break
        states_passed += 1
    ok = failure is None and states_passed == len(states)
    stats.update(
        states_passed=states_passed,
        tables_matched=matched_tables, tables_checked=total_tables,
        statements=runs[0].statements if runs else 0,
        engine_seconds=round(sum(r.seconds for r in runs), 3),
    )
    if failure:
        stats["failure"] = failure
    result = Score(1.0 if ok else 0.0, stats)
    if cache is not None:
        cache.put(key, {"score": result.score, "statistics": result.statistics})
    return result


# ---------------------------------------------------------------- cache in PostgreSQL


class PgCache:
    """Score cache in the PostgreSQL table sqltr_meta.score_cache.

    The metric is deterministic, so an identical (METRIC_VERSION, example id, answer) gives an
    identical result; score_answer hashes the three into the key. While the scoring rules
    change, disable the cache (SQLTR_CACHE=0) or change METRIC_VERSION, or stale scores are
    served. Create one per process when cfg.cache is set and close() it when done.
    """

    def __init__(self, cfg: Config):
        """Connect as the admin role and create sqltr_meta.score_cache if it is missing.

        Raises InfraError if PostgreSQL cannot be reached.
        """
        import psycopg

        pg = cfg.postgres
        try:
            self.conn = psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                                        password=pg.admin_password or None, autocommit=True,
                                        connect_timeout=10)
        except psycopg.OperationalError as error:
            raise InfraError(f"cache connection failed: {error}") from error
        with self.conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS sqltr_meta")
            cur.execute("CREATE TABLE IF NOT EXISTS sqltr_meta.score_cache ("
                        "key text PRIMARY KEY, result jsonb NOT NULL, created_at timestamptz DEFAULT now())")

    def get(self, key: str):
        """Return the cached {"score", "statistics"} for `key`, or None if there is none."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT result FROM sqltr_meta.score_cache WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else row[0]

    def put(self, key: str, value: dict) -> None:
        """Store `value` under `key`; an existing entry is kept, so the first result wins."""
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO sqltr_meta.score_cache (key, result) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO NOTHING", (key, json.dumps(value, ensure_ascii=False)))

    def close(self) -> None:
        """Close the cache connection."""
        self.conn.close()


__all__ = ["DIRECTIONS", "DIALECT_ENGINE", "InfraError", "PgCache", "Score", "StateRun",
           "prepare_script", "run_on_states", "score_answer"]
