"""Unit tests of the metric building blocks (no database needed).

Covers statement splitting, schema isolation, the Greenplum shim, the static write and dialect
gates, value normalization and table hashing, and the classification of Trino errors (which needs
the trino package, but no server).
"""

import datetime as dt
from decimal import Decimal

from sqltr_metric.normalize import table_hash, value
from sqltr_metric.rewrite import gp_incompatible, gp_shim, isolate, outside_writes, write_targets
from sqltr_metric.sqltext import split_statements


# ------------------------------------------------------------------ splitting

def test_split_respects_literals_and_comments():
    sql = ("CREATE TABLE wrk.a AS SELECT ';' AS x; -- comment; with semicolon\n"
           "SELECT $$a;b$$ AS y; /* block; */ SELECT E'it''s; \\' ok' AS z;")
    parts = split_statements(sql)
    assert len(parts) == 3
    assert parts[0].startswith("CREATE TABLE wrk.a")
    assert "$$a;b$$" in parts[1]


def test_split_drops_comment_only_fragments():
    assert split_statements("-- only a comment\n;\n  ;SELECT 1;") == ["SELECT 1"]


# ------------------------------------------------------------------ isolation

def test_isolate_rewrites_code_but_not_strings():
    sql = "INSERT INTO wrk.t SELECT 'wrk.t', x FROM retail.orders o JOIN \"wrk\".u ON true"
    out = isolate(sql, {"wrk": "wrk_r1", "retail": "retail_s2"})
    assert "INSERT INTO wrk_r1.t" in out
    assert "'wrk.t'" in out
    assert "FROM retail_s2.orders" in out
    assert "JOIN wrk_r1.u" in out


def test_isolate_keeps_catalog_and_ignores_columns():
    out = isolate("SELECT o.wrk FROM iceberg.wrk.t o", {"wrk": "wrk_r1"})
    assert out == "SELECT o.wrk FROM iceberg.wrk_r1.t o"


# ------------------------------------------------------------------ Greenplum shim

def test_gp_shim_removes_storage_and_distribution():
    sql = ("CREATE TABLE wrk.t WITH (appendonly=true, orientation=column, compresstype=zstd) AS\n"
           "SELECT 1 AS a\nDISTRIBUTED BY (a);\nCREATE TEMP TABLE x AS SELECT 2 DISTRIBUTED RANDOMLY;")
    out = gp_shim(sql)
    assert "appendonly" not in out and "DISTRIBUTED" not in out
    assert "CREATE TABLE wrk.t AS" in " ".join(out.split())


def test_gp_shim_removes_inline_partitions_with_literals():
    sql = ("CREATE TABLE wrk.f (d date, v int)\nWITH (appendonly=true)\nDISTRIBUTED BY (v)\n"
           "PARTITION BY RANGE (d)\n(START (DATE '2020-01-01') INCLUSIVE END (DATE '2030-01-01') EXCLUSIVE "
           "EVERY (INTERVAL '1 month'));")
    out = gp_shim(sql)
    assert out.split() == ["CREATE", "TABLE", "wrk.f", "(d", "date,", "v", "int)", ";"]


def test_gp_shim_keeps_cte_with_clause():
    sql = "WITH base AS (SELECT 1) SELECT * FROM base"
    assert gp_shim(sql) == sql


# ------------------------------------------------------------------ static gates

def test_write_targets_and_outside_writes():
    sql = ("CREATE TEMP TABLE tmp AS SELECT 1; INSERT INTO wrk.t SELECT 1; "
           "DELETE FROM retail.orders; DROP TABLE iceberg.public.x; UPDATE sandbox.s SET a = 1")
    targets = write_targets(sql)
    assert ("INSERT INTO", "wrk.t") in targets
    bad = outside_writes(sql, {"wrk", "sandbox"})
    assert bad == ["DELETE FROM retail.orders", "DROP TABLE iceberg.public.x"]


def test_outside_writes_ignores_strings():
    assert outside_writes("SELECT 'DELETE FROM retail.orders'", {"wrk"}) == []


def test_gp_incompatible_merge():
    assert gp_incompatible("MERGE INTO wrk.t USING x ON true WHEN MATCHED THEN DELETE")
    assert not gp_incompatible("SELECT 'MERGE INTO'")


# ------------------------------------------------------------------ normalization

def test_numbers_normalize_across_types():
    assert value(5) == value(Decimal("5.000")) == value(5.0) == "5"
    assert value(Decimal("0.1234564")) == "0.123456"
    assert value(Decimal("-0.0000001")) == "0"
    assert value(0.30000000000000004) == "0.3"


def test_temporal_and_null_values():
    assert value(dt.datetime(2026, 1, 1, 0, 0)) == "2026-01-01 00:00:00"
    assert value(dt.date(2024, 2, 29)) == "2024-02-29"
    assert value(None) == "\\N"
    assert value(True) == "t"
    assert value(dt.timedelta(minutes=2)) == "120"


def test_table_hash_ignores_row_and_column_order():
    a = table_hash(["A", "b"], [(1, "x"), (2, "y")])
    b = table_hash(["b", "a"], [("y", 2), ("x", 1)])
    assert a == b
    assert a != table_hash(["a", "b"], [(1, "x"), (2, "z")])
    assert a != table_hash(["a", "c"], [(1, "x"), (2, "y")])


def test_table_hash_is_a_multiset():
    assert table_hash(["a"], [(1,), (1,)]) != table_hash(["a"], [(1,)])


# ------------------------------------------------------------------ Trino error classification

def test_trino_error_classification():
    """Trino errors map to translation faults (sql_error, permission, timeout) or to infra.

    The engine is created without `__init__`, which would connect to Trino: `_classify` needs only
    the trino exceptions module in `_exc`.
    """
    import trino.exceptions as exc

    from sqltr_metric.engines import TrinoEngine

    engine = object.__new__(TrinoEngine)
    engine._exc = exc

    def err(name, etype):
        """Return a TrinoQueryError with the given errorName and errorType."""
        return exc.TrinoQueryError({"errorName": name, "errorType": etype, "message": name})

    assert engine._classify(err("SYNTAX_ERROR", "USER_ERROR")) == "sql_error"
    assert engine._classify(err("FUNCTION_NOT_FOUND", "USER_ERROR")) == "sql_error"
    assert engine._classify(err("PERMISSION_DENIED", "USER_ERROR")) == "permission"
    assert engine._classify(err("EXCEEDED_TIME_LIMIT", "INSUFFICIENT_RESOURCES")) == "timeout"
    assert engine._classify(err("EXCEEDED_LOCAL_MEMORY_LIMIT", "INSUFFICIENT_RESOURCES")) == "timeout"
    assert engine._classify(err("NO_NODES_AVAILABLE", "INTERNAL_ERROR")) == "infra"
    assert engine._classify(err("CATALOG_UNAVAILABLE", "USER_ERROR")) == "infra"
    assert engine._classify(err("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR")) == "infra"
    assert engine._classify(err("ICEBERG_FILESYSTEM_ERROR", "EXTERNAL")) == "infra"
