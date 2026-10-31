"""Measurer configuration: defaults from metric_config.json, overridden by SQLTR_* variables.

Passwords are never stored in the package: they come from environment variables set by the
operator for the evaluator container (or are empty for local trust authentication).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "sqltr"
    admin_user: str = "sqltr_admin"
    admin_password: str = ""
    runner_user: str = "sqltr_runner"
    runner_password: str = ""


@dataclass
class TrinoConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    catalog: str = "iceberg"
    admin_user: str = "sqltr_admin"
    runner_user: str = "sqltr_runner"


@dataclass
class Config:
    states: list[str] = field(default_factory=lambda: ["s1", "s2", "s3", "s4"])
    source_schema: str = "retail"
    writable_schemas: list[str] = field(default_factory=lambda: ["wrk", "sandbox"])
    statement_timeout_s: int = 30
    example_timeout_s: int = 240
    cache: bool = True
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    trino: TrinoConfig = field(default_factory=TrinoConfig)

    def physical_source(self, state: str) -> str:
        return f"{self.source_schema}_{state}"


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return type(default)(raw) if not isinstance(default, bool) else raw not in ("0", "false", "no")


def load(path: Path | None = None) -> Config:
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
