"""Engine adapters: run a script in an isolated namespace and read result tables.

Error policy (one of the key metric rules):
- an SQL error caused by the script itself (syntax, unknown function, type error, permission
  denied, timeout) is a property of the translation and yields score 0;
- a failure of the infrastructure (connection refused, engine down, internal storage error)
  raises InfraError, so the evaluation fails loudly instead of recording a wrong zero.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import Config
from .sqltext import split_statements


class InfraError(RuntimeError):
    """The engine or the network failed; the measurement is not valid."""


@dataclass
class ExecOutcome:
    ok: bool
    kind: str = ""  # "", "sql_error", "timeout", "permission"
    message: str = ""
    statement_index: int = -1
    statements: int = 0
    seconds: float = 0.0


@dataclass
class TableData:
    columns: list[str]
    rows: list[tuple] = field(default_factory=list)


def _short(text: str, limit: int = 600) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ======================================================================= PostgreSQL


class PostgresEngine:
    name = "postgres"

    def __init__(self, cfg: Config):
        import psycopg  # imported lazily so the package loads without drivers

        self._psycopg = psycopg
        self.cfg = cfg
        pg = cfg.postgres
        try:
            self.admin = psycopg.connect(
                host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                password=pg.admin_password or None, autocommit=True, connect_timeout=10,
            )
        except psycopg.OperationalError as error:
            raise InfraError(f"postgres admin connection failed: {_short(error)}") from error

    def close(self) -> None:
        self.admin.close()

    def _ident(self, name: str):
        return self._psycopg.sql.Identifier(name)

    def create_schemas(self, names: list[str]) -> None:
        sql = self._psycopg.sql
        with self.admin.cursor() as cur:
            for name in names:
                cur.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        self._ident(name), self._ident(self.cfg.postgres.runner_user)
                    )
                )

    def drop_schemas(self, names: list[str]) -> None:
        sql = self._psycopg.sql
        with self.admin.cursor() as cur:
            for name in names:
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(self._ident(name)))

    def execute(self, script: str, default_schema: str, deadline: float) -> ExecOutcome:
        psycopg = self._psycopg
        pg = self.cfg.postgres
        statements = split_statements(script)
        started = time.monotonic()
        try:
            conn = psycopg.connect(
                host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.runner_user,
                password=pg.runner_password or None, autocommit=True, connect_timeout=10,
            )
        except psycopg.OperationalError as error:
            raise InfraError(f"postgres runner connection failed: {_short(error)}") from error
        timer = threading.Timer(max(0.1, deadline - time.monotonic()), conn.cancel)
        timer.daemon = True
        timer.start()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    psycopg.sql.SQL("SET statement_timeout = {}").format(
                        psycopg.sql.Literal(int(self.cfg.statement_timeout_s * 1000))
                    )
                )
                cur.execute("SET TIME ZONE 'UTC'")
                cur.execute(
                    psycopg.sql.SQL("SET search_path = {}, pg_temp").format(self._ident(default_schema))
                )
                for idx, stmt in enumerate(statements):
                    if time.monotonic() > deadline:
                        return ExecOutcome(False, "timeout", "example time limit exceeded", idx,
                                           len(statements), time.monotonic() - started)
                    try:
                        cur.execute(stmt.encode())
                    except psycopg.Error as error:
                        state = getattr(error, "sqlstate", None) or ""
                        if state == "57014":
                            kind = "timeout"
                        elif state == "42501":
                            kind = "permission"
                        elif state[:2] in ("08", "53", "58", "XX") or (
                            isinstance(error, psycopg.OperationalError) and not state
                        ):
                            raise InfraError(f"postgres failure {state}: {_short(error)}") from error
                        else:
                            kind = "sql_error"
                        return ExecOutcome(False, kind, f"{state}: {_short(error)}", idx,
                                           len(statements), time.monotonic() - started)
            return ExecOutcome(True, statements=len(statements), seconds=time.monotonic() - started)
        finally:
            timer.cancel()
            conn.close()

    def read_table(self, schema: str, table: str) -> TableData | None:
        sql = self._psycopg.sql
        with self.admin.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(sql.SQL("SELECT * FROM {}.{}").format(self._ident(schema), self._ident(table)))
            columns = [d.name for d in cur.description]
            return TableData(columns, cur.fetchall())


# ======================================================================= Trino


class TrinoEngine:
    name = "trino"

    def __init__(self, cfg: Config):
        import trino  # noqa: F401  (lazy import)
        from trino import dbapi, exceptions

        self._dbapi = dbapi
        self._exc = exceptions
        self.cfg = cfg
        tr = cfg.trino
        self.admin = dbapi.connect(host=tr.host, port=tr.port, user=tr.admin_user, catalog=tr.catalog,
                                   http_scheme="http", request_timeout=60)
        try:
            self._admin_exec("SELECT 1")
        except Exception as error:  # noqa: BLE001
            raise InfraError(f"trino admin connection failed: {_short(error)}") from error

    def close(self) -> None:
        self.admin.close()

    def _admin_exec(self, statement: str):
        cur = self.admin.cursor()
        cur.execute(statement)
        rows = cur.fetchall()
        return cur, rows

    def _q(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def create_schemas(self, names: list[str]) -> None:
        for name in names:
            self._admin_exec(f"CREATE SCHEMA {self.cfg.trino.catalog}.{self._q(name)}")

    def drop_schemas(self, names: list[str]) -> None:
        """Drop tables and views created by a script, then the schema. Never raises on cleanup."""
        cat = self.cfg.trino.catalog
        for name in names:
            try:
                _, objects = self._admin_exec(
                    f"SELECT table_name, table_type FROM {cat}.information_schema.tables "
                    f"WHERE table_schema = '{name}'")
            except self._exc.TrinoQueryError:
                objects = []
            for table, kind in objects:
                verb = "VIEW" if str(kind).upper() == "VIEW" else "TABLE"
                try:
                    self._admin_exec(f"DROP {verb} IF EXISTS {cat}.{self._q(name)}.{self._q(table)}")
                except self._exc.TrinoQueryError:
                    pass
            try:
                self._admin_exec(f"DROP SCHEMA IF EXISTS {cat}.{self._q(name)}")
            except self._exc.TrinoQueryError:
                pass  # left for the periodic cleanup; it never affects other runs (unique names)

    # error names from io.trino.spi.StandardErrorCode
    _INFRA = {"SERVER_STARTING_UP", "SERVER_SHUTTING_DOWN", "NO_NODES_AVAILABLE", "REMOTE_TASK_ERROR",
              "REMOTE_HOST_GONE", "PAGE_TRANSPORT_ERROR", "PAGE_TRANSPORT_TIMEOUT", "TOO_MANY_REQUESTS_FAILED",
              "CATALOG_NOT_AVAILABLE", "CATALOG_STORE_ERROR", "CATALOG_UNAVAILABLE", "OUT_OF_SPILL_SPACE",
              "ABANDONED_QUERY", "CLUSTER_OUT_OF_MEMORY", "QUERY_QUEUE_FULL", "REMOTE_TASK_MISMATCH",
              "SERVER_SHUTTING_DOWN", "GENERIC_INSUFFICIENT_RESOURCES"}
    _LIMITS = {"EXCEEDED_TIME_LIMIT", "EXCEEDED_CPU_LIMIT", "EXCEEDED_LOCAL_MEMORY_LIMIT",
               "EXCEEDED_GLOBAL_MEMORY_LIMIT", "EXCEEDED_SCAN_LIMIT", "EXCEEDED_WRITE_LIMIT",
               "EXCEEDED_SPILL_LIMIT", "ADMINISTRATIVELY_KILLED", "USER_CANCELED"}

    def _classify(self, error) -> str:
        """Translation fault (score 0) vs infrastructure failure (raise). Names first, then type."""
        name = getattr(error, "error_name", "") or ""
        etype = getattr(error, "error_type", "") or ""
        if name in self._INFRA:
            return "infra"
        if name == "PERMISSION_DENIED":
            return "permission"
        if name in self._LIMITS:
            return "timeout"  # resource limits hit by the script itself (runaway joins, long queries)
        if etype == "USER_ERROR" or isinstance(error, self._exc.TrinoUserError):
            return "sql_error"
        return "infra"

    def execute(self, script: str, default_schema: str, deadline: float) -> ExecOutcome:
        tr = self.cfg.trino
        statements = split_statements(script)
        started = time.monotonic()
        conn = self._dbapi.connect(
            host=tr.host, port=tr.port, user=tr.runner_user, catalog=tr.catalog,
            schema=default_schema, http_scheme="http", request_timeout=self.cfg.example_timeout_s,
            session_properties={"query_max_run_time": f"{self.cfg.statement_timeout_s}s"},
        )
        try:
            for idx, stmt in enumerate(statements):
                if time.monotonic() > deadline:
                    return ExecOutcome(False, "timeout", "example time limit exceeded", idx,
                                       len(statements), time.monotonic() - started)
                cur = conn.cursor()
                timer = threading.Timer(max(0.1, deadline - time.monotonic()), cur.cancel)
                timer.daemon = True
                timer.start()
                try:
                    cur.execute(stmt)
                    cur.fetchall()
                except self._exc.TrinoQueryError as error:
                    kind = self._classify(error)
                    if kind == "infra":
                        raise InfraError(f"trino failure: {_short(error)}") from error
                    return ExecOutcome(False, kind, _short(getattr(error, "message", error)), idx,
                                       len(statements), time.monotonic() - started)
                except (self._exc.TrinoConnectionError, self._exc.HttpError, self._exc.OperationalError,
                        OSError) as error:
                    if time.monotonic() > deadline:
                        return ExecOutcome(False, "timeout", "example time limit exceeded", idx,
                                           len(statements), time.monotonic() - started)
                    raise InfraError(f"trino connection failure: {_short(error)}") from error
                finally:
                    timer.cancel()
            return ExecOutcome(True, statements=len(statements), seconds=time.monotonic() - started)
        finally:
            conn.close()

    def read_table(self, schema: str, table: str) -> TableData | None:
        cat = self.cfg.trino.catalog
        try:
            cur, rows = self._admin_exec(f"SELECT * FROM {cat}.{self._q(schema)}.{self._q(table)}")
        except self._exc.TrinoUserError as error:
            if getattr(error, "error_name", "") in ("TABLE_NOT_FOUND", "SCHEMA_NOT_FOUND"):
                return None
            raise
        columns = [d[0] for d in cur.description]
        return TableData(columns, [tuple(r) for r in rows])


def make_engine(name: str, cfg: Config):
    if name == "postgres":
        return PostgresEngine(cfg)
    if name == "trino":
        return TrinoEngine(cfg)
    raise ValueError(f"unknown engine {name}")
