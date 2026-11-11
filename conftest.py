"""Pytest setup shared by tests/ and case/metric/tests/.

Puts the repository root and case/metric at the front of sys.path, so `sqltr` and `sqltr_metric`
import from a plain checkout without installing the package.

Tests that need PostgreSQL take the session fixture `pg_cfg`. When the database is not reachable,
those tests are skipped, not failed: a green run without PostgreSQL has not run them, so check the
skip count.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
METRIC = ROOT / "case" / "metric"
for path in (METRIC, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _pg_available(cfg) -> bool:
    """Return True if the admin user connects to the configured PostgreSQL within 3 seconds.

    Any error, including a missing psycopg, gives False.
    """
    try:
        import psycopg

        pg = cfg.postgres
        with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                             password=pg.admin_password or None, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def pg_cfg():
    """Return a metric config pointing at a PostgreSQL with the data states loaded.

    Session-scoped. The host defaults to 127.0.0.1: the fixture sets `SQLTR_PG_HOST` in this
    process's environment when it is unset, so later subprocesses inherit it; everything else comes
    from metric_config.json and the `SQLTR_*` variables. The metric cache is off. If the admin
    connection fails, every test that uses the fixture is skipped. Otherwise it creates the runner
    role and reloads the states, which drops and recreates the retail_<state> schemas of that
    database (local dev or the VM stand).
    """
    from sqltr_metric import config

    os.environ.setdefault("SQLTR_PG_HOST", "127.0.0.1")
    cfg = config.load()
    cfg.cache = False
    if not _pg_available(cfg):
        pytest.skip("PostgreSQL with the sqltr database is not reachable")
    import psycopg

    from sqltr import warehouse

    pg = cfg.postgres
    with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                         password=pg.admin_password or None, autocommit=True) as conn:
        warehouse.setup_postgres_roles(conn, pg.runner_user, pg.runner_password or None)
        warehouse.load_postgres(conn, cfg.states, pg.runner_user)
    return cfg
