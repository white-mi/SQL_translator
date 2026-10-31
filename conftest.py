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
    """Metric config pointing at a PostgreSQL with loaded data states (local dev or the VM stand)."""
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
