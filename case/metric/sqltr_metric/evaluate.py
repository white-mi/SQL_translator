"""Core measurement: run a script on K data states in isolated schemas and hash output tables.

Used both by the frozen metric (score a translation against reference hashes) and by the
trusted reference builder (compute hashes of the source script). One code path, one set of rules.
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
DIRECTIONS = {"gp2trino": ("gp", "trino"), "trino2gp": ("trino", "gp")}


@dataclass
class StateRun:
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
    """Dialect-specific preprocessing done by the measurer before execution."""
    return gp_shim(script) if dialect == "gp" else script


def run_on_states(script: str, dialect: str, outputs: list[str], cfg: Config,
                  states: list[str] | None = None, stop_on_failure: bool = False,
                  engine=None) -> list[StateRun]:
    """Execute `script` (dialect `dialect`) on each state and return per-state output hashes."""
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
    score: float
    statistics: dict


def score_answer(example_id: str, task: dict, reference: dict, answer: str, cfg: Config,
                 cache=None) -> Score:
    direction = task["direction"]
    _, target = DIRECTIONS[direction]
    outputs = list(reference["outputs"])
    states = reference.get("states", cfg.states)
    stats: dict = {"direction": direction, "target": target, "metric_version": METRIC_VERSION}

    if not isinstance(answer, str) or not answer.strip():
        stats.update(failure="empty_answer")
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
    """Deterministic metric => identical (example, answer) pairs give identical results."""

    def __init__(self, cfg: Config):
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
        with self.conn.cursor() as cur:
            cur.execute("SELECT result FROM sqltr_meta.score_cache WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else row[0]

    def put(self, key: str, value: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO sqltr_meta.score_cache (key, result) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO NOTHING", (key, json.dumps(value, ensure_ascii=False)))

    def close(self) -> None:
        self.conn.close()


__all__ = ["DIRECTIONS", "DIALECT_ENGINE", "InfraError", "PgCache", "Score", "StateRun",
           "prepare_script", "run_on_states", "score_answer"]
