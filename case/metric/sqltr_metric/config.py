"""Measurer configuration: defaults from metric_config.json, overridden by SQLTR_* variables.

Passwords are never stored in the package: they come from environment variables set by the
operator for the evaluator container (or are empty for local trust authentication).

Build the settings with load() and pass the Config to engines and evaluate functions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PostgresConfig:
    """Connection settings for the PostgreSQL that executes Greenplum-dialect scripts.

    Attributes:
        admin_user: role that creates and drops per-run schemas, reads output tables and holds
            the score cache.
        runner_user: unprivileged role that executes the scripts being measured.
        admin_password, runner_password: empty means connecting without a password.
    """

    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "sqltr"
    admin_user: str = "sqltr_admin"
    admin_password: str = ""
    runner_user: str = "sqltr_runner"
    runner_password: str = ""


@dataclass
class TrinoConfig:
    """Connection settings for the Trino that executes Trino-dialect scripts (HTTP, no password).

    Attributes:
        catalog: catalog of the source data and the per-run schemas; also the default catalog of
            the runner's sessions.
        admin_user: user that creates and drops per-run schemas and reads output tables.
        runner_user: user that executes the scripts being measured.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    catalog: str = "iceberg"
    admin_user: str = "sqltr_admin"
    runner_user: str = "sqltr_runner"


@dataclass
class Config:
    """Metric settings shared by reference building and scoring; build one with load().

    Attributes:
        states: data states every script runs on; state `s` reads the physical source schema
            `<source_schema>_<s>` (physical_source).
        source_schema: logical read-only schema of the source data.
        writable_schemas: logical schemas a script may write; every run gets fresh physical
            copies of them, and the last one is the default schema for unqualified names.
        statement_timeout_s: limit for one statement, set as the engine's session timeout.
        example_timeout_s: limit for one script over all states (evaluate.run_on_states).
        cache: whether score.py and run-local reuse scores from evaluate.PgCache.
    """

    states: list[str] = field(default_factory=lambda: ["s1", "s2", "s3", "s4"])
    source_schema: str = "retail"
    writable_schemas: list[str] = field(default_factory=lambda: ["wrk", "sandbox"])
    statement_timeout_s: int = 30
    example_timeout_s: int = 240
    cache: bool = True
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    trino: TrinoConfig = field(default_factory=TrinoConfig)

    def physical_source(self, state: str) -> str:
        """Return the physical source schema that holds data state `state`, e.g. `retail_s1`."""
        return f"{self.source_schema}_{state}"


def _env(name: str, default):
    """Return variable `name` cast to the type of `default`, or `default` if unset or empty.

    For a bool default, "0", "false" and "no" mean False and any other value True.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return type(default)(raw) if not isinstance(default, bool) else raw not in ("0", "false", "no")


def load(path: Path | None = None) -> Config:
    """Return the Config: built-in defaults, then the JSON file, then SQLTR_* variables.

    Args:
        path: settings file; defaults to metric_config.json next to the package. A missing file
            leaves the built-in defaults.

    Variables: SQLTR_PG_HOST, SQLTR_PG_PORT, SQLTR_PG_DB, SQLTR_PG_ADMIN_USER,
    SQLTR_PG_ADMIN_PASSWORD, SQLTR_PG_RUNNER_USER, SQLTR_PG_RUNNER_PASSWORD, SQLTR_TRINO_HOST,
    SQLTR_TRINO_PORT, SQLTR_TRINO_CATALOG, SQLTR_CACHE (0, false or no disables the cache) and
    SQLTR_STATES (comma-separated state names). Called by score.py, the sqltr CLI (whose --local
    flag then points both hosts at 127.0.0.1) and the tests.

    Raises:
        TypeError: an unknown key in the file's "postgres" or "trino" section.
        ValueError: invalid JSON in the file, or a port variable that is not an integer.
    """
    data: dict = {}
    if path is None:
        path = Path(__file__).resolve().parent.parent / "metric_config.json"
    if path.exists():
        data = json.loads(path.read_text())
    pg = PostgresConfig(**data.get("postgres", {}))
    tr = TrinoConfig(**data.get("trino", {}))
    cfg = Config(
        states=data.get("states", Config().states),
        source_schema=data.get("source_schema", "retail"),
        writable_schemas=data.get("writable_schemas", ["wrk", "sandbox"]),
        statement_timeout_s=data.get("statement_timeout_s", 30),
        example_timeout_s=data.get("example_timeout_s", 240),
        cache=data.get("cache", True),
        postgres=pg,
        trino=tr,
    )
    pg.host = _env("SQLTR_PG_HOST", pg.host)
    pg.port = _env("SQLTR_PG_PORT", pg.port)
    pg.dbname = _env("SQLTR_PG_DB", pg.dbname)
    pg.admin_user = _env("SQLTR_PG_ADMIN_USER", pg.admin_user)
    pg.admin_password = _env("SQLTR_PG_ADMIN_PASSWORD", pg.admin_password)
    pg.runner_user = _env("SQLTR_PG_RUNNER_USER", pg.runner_user)
    pg.runner_password = _env("SQLTR_PG_RUNNER_PASSWORD", pg.runner_password)
    tr.host = _env("SQLTR_TRINO_HOST", tr.host)
    tr.port = _env("SQLTR_TRINO_PORT", tr.port)
    tr.catalog = _env("SQLTR_TRINO_CATALOG", tr.catalog)
    cfg.cache = _env("SQLTR_CACHE", cfg.cache)
    states = os.environ.get("SQLTR_STATES")
    if states:
        cfg.states = [s.strip() for s in states.split(",") if s.strip()]
    return cfg
