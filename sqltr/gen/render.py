"""Paired rendering helpers: one method per dialect divergence point.

Every helper returns the Greenplum (`gp`) or the Trino (`trino`) form of the same computation.
The pair must be semantically identical on all data states; the VM self-check executes both
renderings on their engines and drops instances whose outputs differ.

Feature tags collected in `R.features` describe which divergence classes a script contains; they
are used for the per-class breakdown of results (where rules suffice, where they fail).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

GP_TYPES = {"text": "text", "varchar": "varchar", "int": "integer", "bigint": "bigint",
            "numeric": "numeric", "float": "float8", "date": "date", "timestamp": "timestamp"}
TRINO_TYPES = {"text": "VARCHAR", "varchar": "VARCHAR", "int": "INTEGER", "bigint": "BIGINT",
               "numeric": "DECIMAL", "float": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP(6)"}


def sample_style(r: random.Random) -> dict:
    return {
        "gp_cast": r.choice(["colon", "colon", "cast"]),
        "gp_date": r.choice(["int", "interval"]),
        "gp_storage": r.choice(["aoco", "ao", "none"]),
        "gp_dist": r.choice(["by", "by", "random"]),
        "trino_date": r.choice(["date_add", "interval"]),
        "trino_catalog": r.random() < 0.35,
        "trino_storage": r.choice(["parquet", "none"]),
        "trino_agg": r.choice(["array_join", "listagg"]),
        "comments": r.random() < 0.6,
    }


@dataclass
class R:
    d: str  # "gp" | "trino"
    st: dict
    features: set = field(default_factory=set)

    # -------------------------------------------------------------- basics
    @property
    def gp(self) -> bool:
        return self.d == "gp"

    def f(self, *tags: str) -> None:
        self.features.update(tags)

    def t(self, schema: str, table: str) -> str:
        if not self.gp and self.st.get("trino_catalog"):
            return f"iceberg.{schema}.{table}"
        return f"{schema}.{table}"

    def src(self, table: str) -> str:
        return self.t("retail", table)

    def comment(self, text: str) -> str:
        return f"-- {text}\n" if self.st.get("comments") else ""

    def date_lit(self, iso: str) -> str:
        if self.gp and self.st["gp_cast"] == "colon":
            return f"'{iso}'::date"
        return f"DATE '{iso}'"

    def cast(self, expr: str, typ: str, args: str = "") -> str:
        """Explicit cast; typ is a logical type name, args like '12,2'."""
        suffix = f"({args})" if args else ""
        if self.gp:
            name = GP_TYPES[typ] + suffix
            if self.st["gp_cast"] == "colon":
                needs = not (expr.replace("_", "").replace(".", "").isalnum())
                return f"({expr})::{name}" if needs else f"{expr}::{name}"
            return f"CAST({expr} AS {name})"
        self.f("cast")
        name = TRINO_TYPES[typ] + suffix if typ != "timestamp" else TRINO_TYPES[typ]
        return f"CAST({expr} AS {name})"

    def text(self, expr: str) -> str:
        """Value as text, needed for concatenation in Trino."""
        if self.gp:
            return expr
        return f"CAST({expr} AS VARCHAR)"

    def strlit(self, literal: str) -> str:
        """String literal used as a CTAS column (Iceberg needs unbounded varchar)."""
        return f"'{literal}'" if self.gp else f"CAST('{literal}' AS VARCHAR)"

    # -------------------------------------------------------------- dates
    def to_date(self, ts: str) -> str:
        self.f("date_cast")
        if self.gp:
            return f"{ts}::date" if self.st["gp_cast"] == "colon" else f"CAST({ts} AS date)"
        return f"CAST({ts} AS DATE)"

    def month_start(self, ts: str) -> str:
        self.f("date_trunc")
        if self.gp:
            return f"date_trunc('month', {ts})::date" if self.st["gp_cast"] == "colon" else \
                f"CAST(date_trunc('month', {ts}) AS date)"
        return f"CAST(date_trunc('month', {ts}) AS DATE)"

    def fmt_month(self, ts: str) -> str:
        self.f("date_format")
        if self.gp:
            return f"to_char({ts}, 'YYYY-MM')"
        return f"format_datetime({ts}, 'yyyy-MM')" if self.st["trino_date"] == "date_add" else \
            f"date_format({ts}, '%Y-%m')"

    def date_shift(self, dexpr: str, days: int) -> str:
        """date +/- N days, result type date."""
        self.f("date_arith")
        if self.gp:
            if self.st["gp_date"] == "int":
                return f"{dexpr} {'+' if days >= 0 else '-'} {abs(days)}"
            return f"({dexpr} {'+' if days >= 0 else '-'} interval '{abs(days)} days')::date"
        if self.st["trino_date"] == "date_add":
            return f"date_add('day', {days}, {dexpr})"
        return f"{dexpr} {'+' if days >= 0 else '-'} INTERVAL '{abs(days)}' DAY"

    def days_between(self, d1: str, d2: str) -> str:
        self.f("date_diff")
        return f"({d2} - {d1})" if self.gp else f"date_diff('day', {d1}, {d2})"

    def minutes_between(self, t1: str, t2: str) -> str:
        self.f("epoch")
        if self.gp:
            return f"floor(extract(epoch from ({t2} - {t1})) / 60)"
        return f"date_diff('minute', {t1}, {t2})"

    def years_between(self, d1: str, d2: str) -> str:
        self.f("age")
        return f"date_part('year', age({d2}, {d1}))" if self.gp else f"date_diff('year', {d1}, {d2})"

    def months_between(self, d1: str, d2: str) -> str:
        self.f("age")
        if self.gp:
            return f"(date_part('year', age({d2}, {d1})) * 12 + date_part('month', age({d2}, {d1})))"
        return f"date_diff('month', {d1}, {d2})"

    def dow(self, dexpr: str) -> str:
        self.f("dow")
        return f"extract(dow from {dexpr})" if self.gp else f"day_of_week({dexpr}) % 7"

    def year(self, dexpr: str) -> str:
        self.f("date_part")
        return f"date_part('year', {dexpr})" if self.gp else f"year({dexpr})"

    def series_days(self, start_iso: str, end_iso: str, alias: str, col: str) -> tuple[str, str]:
        """FROM-item producing one row per day; returns (from_item, day_expression)."""
        self.f("generate_series")
        if self.gp:
            return (f"generate_series({self.date_lit(start_iso)}, {self.date_lit(end_iso)}, interval '1 day') "
                    f"AS {alias}({col}_ts)", f"{alias}.{col}_ts::date")
        return (f"UNNEST(sequence(DATE '{start_iso}', DATE '{end_iso}', INTERVAL '1' DAY)) AS {alias}({col})",
                f"{alias}.{col}")

    # -------------------------------------------------------------- strings
    def ilike(self, col: str, pattern: str) -> str:
        self.f("ilike")
        return f"{col} ILIKE '{pattern}'" if self.gp else f"lower({col}) LIKE '{pattern.lower()}'"

    def regex(self, col: str, pattern: str) -> str:
        self.f("regex")
        return f"{col} ~ '{pattern}'" if self.gp else f"regexp_like({col}, '{pattern}')"

    def label(self, *parts: str, sep: str = "-") -> str:
        """id || '-' || text: PostgreSQL casts non-text operands implicitly, Trino does not."""
        self.f("concat_types")
        glue = f" || '{sep}' || "
        return glue.join(parts if self.gp else [self.text(p) for p in parts])

    def concat_nullsafe(self, *parts: str, sep: str = "-") -> str:
        """concat() skips NULLs in PostgreSQL but returns NULL in Trino."""
        self.f("concat_null")
        items = []
        for idx, p in enumerate(parts):
            if idx:
                items.append(f"'{sep}'")
            items.append(p if self.gp else f"coalesce({self.text(p)}, '')")
        return f"concat({', '.join(items)})"

    def agg_join(self, col: str, sep: str, order: str | None = None, distinct: bool = False) -> str:
        self.f("string_agg")
        order = order or col
        if self.gp:
            dist = "DISTINCT " if distinct else ""
            return f"string_agg({dist}{col}, '{sep}' ORDER BY {order})"
        if distinct:
            return f"array_join(array_sort(array_distinct(array_agg({col}))), '{sep}')"
        if self.st["trino_agg"] == "listagg":
            return f"listagg({col}, '{sep}') WITHIN GROUP (ORDER BY {order})"
        return f"array_join(array_agg({col} ORDER BY {order}), '{sep}')"

    def left(self, s: str, n: int) -> str:
        self.f("substr")
        return f"left({s}, {n})" if self.gp else f"substr({s}, 1, {n})"

    def right(self, s: str, n: int) -> str:
        self.f("substr")
        return f"right({s}, {n})" if self.gp else f"substr({s}, -{n})"

    def md5hex(self, s: str) -> str:
        self.f("md5")
        return f"md5({s})" if self.gp else f"lower(to_hex(md5(to_utf8({s}))))"

    def json_get(self, col: str, key: str) -> str:
        self.f("json")
        if self.gp:
            return f"({col}::json ->> '{key}')" if self.st["gp_cast"] == "colon" else \
                f"json_extract_path_text(CAST({col} AS json), '{key}')"
        return f"json_extract_scalar({col}, '$.{key}')"

    def try_bigint(self, col: str) -> str:
        self.f("try_cast")
        if self.gp:
            return f"CASE WHEN {col} ~ '^[0-9]+$' THEN {self.cast(col, 'bigint')} END"
        return f"try_cast({col} AS BIGINT)"

    def iff(self, cond: str, a: str, b: str) -> str:
        if self.gp:
            return f"CASE WHEN {cond} THEN {a} ELSE {b} END"
        self.f("if")
        return f"if({cond}, {a}, {b})"

    # -------------------------------------------------------------- arrays from comma lists
    def tag_count(self, col: str) -> str:
        self.f("arrays")
        return f"array_length(string_to_array({col}, ','), 1)" if self.gp else f"cardinality(split({col}, ','))"

    def tags_upper(self, col: str) -> str:
        self.f("lambda")
        return f"upper(replace({col}, ',', '|'))" if self.gp else \
            f"array_join(transform(split({col}, ','), x -> upper(x)), '|')"

    def tags_matching(self, col: str, prefix: str) -> str:
        self.f("lambda")
        if self.gp:
            return (f"CASE WHEN {col} IS NULL THEN NULL ELSE (SELECT count(*) FROM unnest(string_to_array({col}, ',')) "
                    f"AS u(x) WHERE u.x LIKE '{prefix}%') END")
        return f"cardinality(filter(split({col}, ','), x -> x LIKE '{prefix}%'))"

    def unnest_tags(self, col: str, alias: str, out: str) -> str:
        """Join fragment (with leading CROSS JOIN) producing one row per tag."""
        self.f("unnest")
        if self.gp:
            return f"CROSS JOIN LATERAL unnest(string_to_array({col}, ',')) AS {alias}({out})"
        return f"CROSS JOIN UNNEST(split({col}, ',')) AS {alias}({out})"

    # -------------------------------------------------------------- numbers and nulls
    def ratio(self, num: str, den: str, places: int = 4) -> str:
        self.f("division")
        if self.gp:
            return f"round({self.cast(num, 'numeric')} / NULLIF({den}, 0), {places})"
        return f"round(CAST({num} AS DOUBLE) / NULLIF({den}, 0), {places})"

    def greatest2(self, a: str, b: str) -> str:
        self.f("greatest_null")
        return f"greatest({a}, {b})" if self.gp else f"greatest(coalesce({a}, {b}), coalesce({b}, {a}))"

    def least2(self, a: str, b: str) -> str:
        self.f("greatest_null")
        return f"least({a}, {b})" if self.gp else f"least(coalesce({a}, {b}), coalesce({b}, {a}))"

    def desc(self, expr: str) -> str:
        """ORDER BY expr DESC with PostgreSQL null placement (NULLs first)."""
        self.f("null_order")
        return f"{expr} DESC" if self.gp else f"{expr} DESC NULLS FIRST"

    def decimal(self, expr: str, args: str = "14,2") -> str:
        """Value stored into a decimal column: Trino needs an explicit narrowing cast."""
        return expr if self.gp else f"CAST({expr} AS DECIMAL({args}))"

    # -------------------------------------------------------------- statements
    def ctas(self, name: str, select_sql: str, dist: str | None = None,
             partition_col: str | None = None) -> str:
        if self.gp:
            storage = {"aoco": " WITH (appendonly=true, orientation=column, compresstype=zstd)",
                       "ao": " WITH (appendonly=true)", "none": ""}[self.st["gp_storage"]]
            if storage:
                self.f("gp_storage")
            tail = ""
            if dist and self.st["gp_dist"] == "by":
                tail = f"\nDISTRIBUTED BY ({dist})"
            elif self.st["gp_dist"] == "random":
                tail = "\nDISTRIBUTED RANDOMLY"
            if tail:
                self.f("gp_distribution")
            return f"CREATE TABLE {name}{storage} AS\n{select_sql}{tail};"
        props = []
        if self.st["trino_storage"] == "parquet":
            props.append("format = 'PARQUET'")
        if partition_col and self.st["trino_storage"] == "parquet":
            props.append(f"partitioning = ARRAY['month({partition_col})']")
        with_ = f" WITH ({', '.join(props)})" if props else ""
        if props:
            self.f("iceberg_props")
        return f"CREATE TABLE {name}{with_} AS\n{select_sql};"

    def partitioned_table(self, name: str, columns: list[tuple[str, str]], part_col: str, dist: str) -> str:
        """Explicit DDL of a month-partitioned table. columns: (name, logical type with args)."""
        self.f("partition_ddl")
        if self.gp:
            cols = ", ".join(f"{c} {t.replace('decimal', 'numeric')}" for c, t in columns)
            return (f"CREATE TABLE {name} ({cols})\nWITH (appendonly=true, orientation=column)\n"
                    f"DISTRIBUTED BY ({dist})\nPARTITION BY RANGE ({part_col})\n"
                    f"(START (DATE '2020-01-01') INCLUSIVE END (DATE '2030-01-01') EXCLUSIVE "
                    f"EVERY (INTERVAL '1 month'));")
        cols = ", ".join(f"{c} {t.upper().replace('NUMERIC', 'DECIMAL')}" for c, t in columns)
        return f"CREATE TABLE {name} ({cols})\nWITH (partitioning = ARRAY['month({part_col})']);"

    def temp_table(self, short: str, select_sql: str, dist: str | None = None) -> tuple[str, str, str]:
        """Session-scoped helper table. Returns (create statement, reference name, drop statement)."""
        self.f("temp_table")
        if self.gp:
            tail = f"\nDISTRIBUTED BY ({dist})" if dist and self.st["gp_dist"] == "by" else ""
            return f"CREATE TEMP TABLE {short} AS\n{select_sql}{tail};", short, f"DROP TABLE {short};"
        name = self.t("sandbox", short)
        return f"CREATE TABLE {name} AS\n{select_sql};", name, f"DROP TABLE {name};"

    def update_from(self, target: str, alias: str, source: str, salias: str, on: str,
                    sets: list[tuple[str, str]]) -> str:
        self.f("update_join")
        if self.gp:
            assign = ", ".join(f"{c} = {e}" for c, e in sets)
            return f"UPDATE {target} {alias}\nSET {assign}\nFROM {source} {salias}\nWHERE {on};"
        assign = ", ".join(f"{c} = {e}" for c, e in sets)
        return (f"MERGE INTO {target} {alias}\nUSING {source} {salias}\nON {on}\n"
                f"WHEN MATCHED THEN UPDATE SET {assign};")

    def delete_matching(self, target: str, alias: str, source: str, salias: str, key: str) -> str:
        self.f("delete_join")
        if self.gp:
            return f"DELETE FROM {target} {alias}\nUSING {source} {salias}\nWHERE {alias}.{key} = {salias}.{key};"
        return f"DELETE FROM {target}\nWHERE {key} IN (SELECT {salias}.{key} FROM {source} {salias});"

    def analyze(self, name: str) -> str:
        return f"ANALYZE {name};" if self.gp else ""
