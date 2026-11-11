"""Engine adapters: run a script in an isolated namespace and read result tables.

Error policy (one of the key metric rules):
- an SQL error caused by the script itself (syntax, unknown function, type error, permission
  denied, timeout) is a property of the translation and yields score 0;
- so does a per-script resource limit: Trino's per-query limits (TrinoEngine._LIMITS) and
  PostgreSQL's temp_file_limit, which `python -m sqltr setup-pg` sets on the runner role
  (RUNNER_TEMP_FILE_LIMIT in sqltr/warehouse.py), so a runaway answer scores 0 on both engines;
- a failure of the infrastructure (connection refused, engine down, internal storage error, the
  whole server out of memory or disk) raises InfraError, so the evaluation fails loudly instead
  of recording a wrong zero.

Get an engine from make_engine() and close() it when done. evaluate.run_on_states drives each
run: create_schemas, execute, read_table for every output, drop_schemas.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import Config
from .sqltext import split_statements


class InfraError(RuntimeError):
    """The engine or the network failed; the measurement is not valid.

    Raised by the engines and by evaluate.PgCache. score.py lets it propagate, so the process
    exits non-zero and no score is recorded instead of a wrong zero.
    """


@dataclass
class ExecOutcome:
    """Outcome of executing one script on one engine (returned by the engines' execute()).

    Attributes:
        ok: every statement succeeded.
        kind: "" when ok, otherwise "sql_error", "timeout" or "permission".
        message: shortened error text, "" when ok; PostgreSQL errors start with their SQLSTATE.
        statement_index: 0-based index, in sqltext.split_statements(script), of the statement
            that failed or was not started because time ran out; -1 when ok.
        statements: number of statements in the script.
        seconds: wall time from just before the runner connection to the end of the script.
    """

    ok: bool
    kind: str = ""  # "", "sql_error", "timeout", "permission"
    message: str = ""
    statement_index: int = -1
    statements: int = 0
    seconds: float = 0.0


@dataclass
class TableData:
    """Contents of one table read back after a script ran (returned by read_table()).

    Attributes:
        columns: column names as the engine reports them.
        rows: row tuples of driver values, in no particular order.
    """

    columns: list[str]
    rows: list[tuple] = field(default_factory=list)


def _pg_error_kind(sqlstate: str, operational: bool) -> str:
    """Return the kind of a PostgreSQL script error: "timeout", "permission", "sql_error" or "infra".

    The PostgreSQL counterpart of TrinoEngine._classify, used by PostgresEngine.execute. 57014
    (statement timeout or cancel) and 53400 (configuration_limit_exceeded: the script went over
    the runner's temp_file_limit) are limits the script hit itself, kind "timeout". 42501 is
    "permission". The classes 08 (connection), 53 otherwise (the server out of memory, disk or
    connections), 58 (system) and XX (internal), and an OperationalError without SQLSTATE
    (`operational` true, `sqlstate` empty), are "infra". Anything else is "sql_error".
    """
    if sqlstate in ("57014", "53400"):
        return "timeout"
    if sqlstate == "42501":
        return "permission"
    if sqlstate[:2] in ("08", "53", "58", "XX") or (operational and not sqlstate):
        return "infra"
    return "sql_error"


def _short(text: str, limit: int = 600) -> str:
    """Return `text` on one line with whitespace collapsed, cut to `limit` characters with "..."."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ======================================================================= PostgreSQL


class PostgresEngine:
    """PostgreSQL adapter: executes Greenplum-dialect scripts as the runner role, reads results.

    Holds an autocommit admin connection for schema management and reads; each execute() opens
    its own connection as the runner role. Create it with make_engine("postgres", cfg) and call
    close() when done.
    """

    name = "postgres"

    def __init__(self, cfg: Config):
        """Open the admin connection; raise InfraError if PostgreSQL cannot be reached.

        Needs the psycopg driver, imported here so the package loads without it.
        """
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
        """Close the admin connection."""
        self.admin.close()

    def _ident(self, name: str):
        """Return `name` as a quoted identifier for psycopg.sql composition."""
        return self._psycopg.sql.Identifier(name)

    def create_schemas(self, names: list[str]) -> None:
        """Create each schema owned by the runner role, so the measured script can write there."""
        sql = self._psycopg.sql
        with self.admin.cursor() as cur:
            for name in names:
                cur.execute(
                    sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                        self._ident(name), self._ident(self.cfg.postgres.runner_user)
                    )
                )

    def drop_schemas(self, names: list[str]) -> None:
        """Drop each schema with everything in it (DROP SCHEMA IF EXISTS ... CASCADE)."""
        sql = self._psycopg.sql
        with self.admin.cursor() as cur:
            for name in names:
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(self._ident(name)))

    def execute(self, script: str, default_schema: str, deadline: float) -> ExecOutcome:
        """Run `script` statement by statement as the runner role and report how it ended.

        The session runs in UTC with statement_timeout = cfg.statement_timeout_s and
        search_path = default_schema, pg_temp. A timer cancels the running statement at
        `deadline`. The first failing statement ends the script. The runner role must have a
        temp_file_limit (set by `python -m sqltr setup-pg`), so a runaway script stops at its own
        limit instead of filling the server's disk.

        Args:
            script: script with physical schema names (already isolated).
            default_schema: physical schema for unqualified names.
            deadline: time.monotonic() value at which the script is stopped as a "timeout".

        Returns:
            ExecOutcome with the kind from _pg_error_kind: "timeout" for SQLSTATE 57014 (cancel
            or statement timeout) and 53400 (over temp_file_limit), "permission" for 42501,
            "sql_error" for any other script error.

        Raises:
            InfraError: the runner connection fails, the runner role has no temp_file_limit, or
                _pg_error_kind returns "infra" (SQLSTATE class 08, 53 except 53400, 58 or XX, or
                an OperationalError without SQLSTATE).
        """
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
                cur.execute("SELECT current_setting('temp_file_limit')")
                if cur.fetchone()[0] == "-1":
                    raise InfraError(f"postgres role {pg.runner_user} has no temp_file_limit; "
                                     "run python -m sqltr setup-pg")
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
                        kind = _pg_error_kind(state, isinstance(error, psycopg.OperationalError))
                        if kind == "infra":
                            raise InfraError(f"postgres failure {state}: {_short(error)}") from error
                        return ExecOutcome(False, kind, f"{state}: {_short(error)}", idx,
                                           len(statements), time.monotonic() - started)
            return ExecOutcome(True, statements=len(statements), seconds=time.monotonic() - started)
        finally:
            timer.cancel()
            conn.close()

    def read_table(self, schema: str, table: str) -> TableData | None:
        """Return the rows of `schema.table` (physical names), or None if it does not exist.

        Reads through the admin connection; a view counts as a table. Other errors are raised
        as psycopg errors.
        """
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
    """Trino adapter: executes Trino-dialect scripts as the runner user and reads result tables.

    Holds an admin connection for schema management and reads; each execute() opens its own
    connection as the runner user. Create it with make_engine("trino", cfg) and call close()
    when done.
    """

    name = "trino"

    def __init__(self, cfg: Config):
        """Connect as admin and probe with SELECT 1; raise InfraError if Trino does not answer.

        Needs the trino driver, imported here so the package loads without it.
        """
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
        """Close the admin connection."""
        self.admin.close()

    def _admin_exec(self, statement: str):
        """Run one statement as the admin user and return (cursor, all rows); errors propagate."""
        cur = self.admin.cursor()
        cur.execute(statement)
        rows = cur.fetchall()
        return cur, rows

    def _q(self, name: str) -> str:
        """Return `name` as a double-quoted Trino identifier."""
        return '"' + name.replace('"', '""') + '"'

    def create_schemas(self, names: list[str]) -> None:
        """Create each schema in the configured catalog as the admin user."""
        for name in names:
            self._admin_exec(f"CREATE SCHEMA {self.cfg.trino.catalog}.{self._q(name)}")

    def drop_schemas(self, names: list[str]) -> None:
        """Drop each schema after dropping the tables and views in it.

        Query errors are ignored, so a failed drop leaves the object behind; connection errors
        still propagate. Called from a `finally` block in evaluate.run_on_states.
        """
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
                pass  # the schema stays behind; it never affects other runs (unique names)

    # error names from io.trino.spi.StandardErrorCode: _INFRA means the cluster failed (InfraError),
    # _LIMITS means the script hit a resource or time limit (kind "timeout", a translation fault)
    _INFRA = {"SERVER_STARTING_UP", "SERVER_SHUTTING_DOWN", "NO_NODES_AVAILABLE", "REMOTE_TASK_ERROR",
              "REMOTE_HOST_GONE", "PAGE_TRANSPORT_ERROR", "PAGE_TRANSPORT_TIMEOUT", "TOO_MANY_REQUESTS_FAILED",
              "CATALOG_NOT_AVAILABLE", "CATALOG_STORE_ERROR", "CATALOG_UNAVAILABLE", "OUT_OF_SPILL_SPACE",
              "ABANDONED_QUERY", "CLUSTER_OUT_OF_MEMORY", "QUERY_QUEUE_FULL", "REMOTE_TASK_MISMATCH",
              "SERVER_SHUTTING_DOWN", "GENERIC_INSUFFICIENT_RESOURCES"}
    _LIMITS = {"EXCEEDED_TIME_LIMIT", "EXCEEDED_CPU_LIMIT", "EXCEEDED_LOCAL_MEMORY_LIMIT",
               "EXCEEDED_GLOBAL_MEMORY_LIMIT", "EXCEEDED_SCAN_LIMIT", "EXCEEDED_WRITE_LIMIT",
               "EXCEEDED_SPILL_LIMIT", "ADMINISTRATIVELY_KILLED", "USER_CANCELED"}

    def _classify(self, error) -> str:
        """Return the kind of a Trino query error: "infra", "permission", "timeout" or "sql_error".

        The error name decides first (_INFRA, PERMISSION_DENIED, _LIMITS), then the error type:
        a user error is "sql_error" and anything else "infra". execute() raises InfraError for
        "infra" and reports the other kinds as a failed ExecOutcome.
        """
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
        """Run `script` statement by statement as the runner user and report how it ended.

        The session's default catalog and schema are cfg.trino.catalog and `default_schema`,
        and query_max_run_time is cfg.statement_timeout_s. Each statement's result is fetched in
        full; a timer cancels the running statement at `deadline`. The first failing statement
        ends the script.

        Args:
            script: script with physical schema names (already isolated).
            default_schema: physical schema for unqualified names.
            deadline: time.monotonic() value at which the script is stopped as a "timeout".

        Returns:
            ExecOutcome; the kind of a failure comes from _classify().

        Raises:
            InfraError: _classify() returns "infra", or the connection fails before `deadline`.
        """
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
        """Return the rows of `schema.table` (physical names), or None if it does not exist.

        Reads through the admin connection. Errors other than TABLE_NOT_FOUND and
        SCHEMA_NOT_FOUND are raised as trino client exceptions.
        """
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
    """Return a connected engine for "postgres" or "trino"; the caller must close() it.

    Raises InfraError if the engine cannot be reached and ValueError for any other name.
    """
    if name == "postgres":
        return PostgresEngine(cfg)
    if name == "trino":
        return TrinoEngine(cfg)
    raise ValueError(f"unknown engine {name}")
