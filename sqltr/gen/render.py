"""Paired rendering helpers: one method per dialect divergence point.

Every helper returns the Greenplum (`gp`) or the Trino (`trino`) form of the same computation.
The pair must be semantically identical on all data states. `python -m sqltr self-check` on the
stand executes both renderings on their engines and reports the families whose outputs differ;
such families must be removed from the dataset.

Feature tags collected in `R.features` describe which divergence classes a script contains; they
are used for the per-class breakdown of results (where rules suffice, where they fail).

`sample_style` draws the paraphrase knobs of one variant; `templates.render` builds an `R` from
the dialect and that style and hands it to a template's renderer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# Logical type names accepted by R.cast, mapped to each dialect's type.
GP_TYPES = {"text": "text", "varchar": "varchar", "int": "integer", "bigint": "bigint",
            "numeric": "numeric", "float": "float8", "date": "date", "timestamp": "timestamp"}
TRINO_TYPES = {"text": "VARCHAR", "varchar": "VARCHAR", "int": "INTEGER", "bigint": "BIGINT",
               "numeric": "DECIMAL", "float": "DOUBLE", "date": "DATE", "timestamp": "TIMESTAMP(6)"}


def sample_style(r: random.Random) -> dict:
    """Draw the paraphrase knobs of one style variant; both renderings of the variant share them.

    `build_split` calls it once per variant. It always makes the same number of draws from `r`.

    Returns:
        A dict read by `R` methods: `gp_cast` ("colon" for `::`, `->>` and `'...'::date`, or
        "cast"), `gp_date` (date arithmetic: "int" for `d + N`, or "interval"), `gp_storage`
        (CTAS storage clause: "aoco", "ao" or "none"), `gp_dist` (DISTRIBUTED "by" a key or
        "random"), `trino_date` ("date_add" with format_datetime, or "interval" with
        date_format), `trino_catalog` (prefix table names with `iceberg.`), `trino_storage` (CTAS
        properties: "parquet" or "none"), `trino_agg` (string aggregation: "array_join" or
        "listagg") and `comments` (start the script with a `--` comment).
    """
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
    """Renderer state for one script in one dialect, with a method per divergent construct.

    `templates.render` creates `R(dialect, style)` and passes it to a template's renderer as `x`.
    The renderer composes the script from the methods' SQL fragments: `x.t(schema, table)` names
    a table it writes, `x.src(table)` a `retail` source table, the expression methods return SQL
    expressions, and the statement methods (`ctas`, `partitioned_table`, `temp_table`,
    `update_from`, `delete_matching`, `analyze`) return whole statements ending in `;`. Where no
    method fits, the renderer branches on `x.gp` and records the divergence with `x.f(tag)`, as
    `r_last_order` does for DISTINCT ON.

    Each method records its feature tag as a side effect, some in one dialect only; `build_split`
    takes the union over both renderings. Nothing is escaped: literals and patterns passed in must
    not contain single quotes.

    Attributes:
        d: "gp" or "trino".
        st: Knobs from `sample_style`. Both renderings of a variant get the same dict; each reads
            its own dialect's keys and `comments`.
        features: Feature tags recorded so far.
    """

    d: str  # "gp" | "trino"
    st: dict
    features: set = field(default_factory=set)

    # -------------------------------------------------------------- basics
    @property
    def gp(self) -> bool:
        """True when rendering the Greenplum dialect."""
        return self.d == "gp"

    def f(self, *tags: str) -> None:
        """Record feature tags; a renderer calls it where it branches on `gp` itself."""
        self.features.update(tags)

    def t(self, schema: str, table: str) -> str:
        """Return the name of a table: `iceberg.schema.table` in Trino under `trino_catalog`.

        Use it for every table the script writes; the renderer's returned outputs list the bare
        `schema.table` form.
        """
        if not self.gp and self.st.get("trino_catalog"):
            return f"iceberg.{schema}.{table}"
        return f"{schema}.{table}"

    def src(self, table: str) -> str:
        """Return the name of the `retail` source table `table`."""
        return self.t("retail", table)

    def comment(self, text: str) -> str:
        """Return a `-- text` line to prefix a statement with, or "" when the style has none."""
        return f"-- {text}\n" if self.st.get("comments") else ""

    def date_lit(self, iso: str) -> str:
        """Return a date literal: `'iso'::date` in the Greenplum colon style, else `DATE 'iso'`."""
        if self.gp and self.st["gp_cast"] == "colon":
            return f"'{iso}'::date"
        return f"DATE '{iso}'"

    def cast(self, expr: str, typ: str, args: str = "") -> str:
        """Return `expr` cast to the logical type `typ` (a `GP_TYPES` key), with `args` like '12,2'.

        Greenplum uses `::` in the colon style, parenthesising `expr` unless it is a bare
        (dotted) name or number, and CAST otherwise. Trino always uses CAST and drops `args`
        for `timestamp`, which is TIMESTAMP(6) there.
        """
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
        """Return `expr` as text: unchanged in Greenplum, CAST to VARCHAR in Trino.

        Trino's `||` and concat() take only strings; PostgreSQL converts other operands itself.
        """
        if self.gp:
            return expr
        return f"CAST({expr} AS VARCHAR)"

    def strlit(self, literal: str) -> str:
        """Return a string literal for a CTAS column, CAST to unbounded VARCHAR in Trino.

        A bare literal would give the column type VARCHAR(n), and Iceberg needs unbounded varchar.
        """
        return f"'{literal}'" if self.gp else f"CAST('{literal}' AS VARCHAR)"

    # -------------------------------------------------------------- dates
    def to_date(self, ts: str) -> str:
        """Return the timestamp `ts` cast to date."""
        self.f("date_cast")
        if self.gp:
            return f"{ts}::date" if self.st["gp_cast"] == "colon" else f"CAST({ts} AS date)"
        return f"CAST({ts} AS DATE)"

    def month_start(self, ts: str) -> str:
        """Return the first day of the month of `ts`, as a date."""
        self.f("date_trunc")
        if self.gp:
            return f"date_trunc('month', {ts})::date" if self.st["gp_cast"] == "colon" else \
                f"CAST(date_trunc('month', {ts}) AS date)"
        return f"CAST(date_trunc('month', {ts}) AS DATE)"

    def fmt_month(self, ts: str) -> str:
        """Return `ts` formatted as 'YYYY-MM' text.

        Greenplum uses to_char; Trino uses format_datetime with a Joda-Time pattern or
        date_format with a MySQL pattern, by the `trino_date` style.
        """
        self.f("date_format")
        if self.gp:
            return f"to_char({ts}, 'YYYY-MM')"
        return f"format_datetime({ts}, 'yyyy-MM')" if self.st["trino_date"] == "date_add" else \
            f"date_format({ts}, '%Y-%m')"

    def date_shift(self, dexpr: str, days: int) -> str:
        """Return the date `dexpr` shifted by `days` (negative goes back), still a date.

        `dexpr` must be a date: the Greenplum integer form `d - N` does not apply to timestamps.
        """
        self.f("date_arith")
        if self.gp:
            if self.st["gp_date"] == "int":
                return f"{dexpr} {'+' if days >= 0 else '-'} {abs(days)}"
            return f"({dexpr} {'+' if days >= 0 else '-'} interval '{abs(days)} days')::date"
        if self.st["trino_date"] == "date_add":
            return f"date_add('day', {days}, {dexpr})"
        return f"{dexpr} {'+' if days >= 0 else '-'} INTERVAL '{abs(days)}' DAY"

    def days_between(self, d1: str, d2: str) -> str:
        """Return the number of days from date `d1` to date `d2`.

        Both must be dates: in Greenplum `d2 - d1` of timestamps is an interval, not a number.
        """
        self.f("date_diff")
        return f"({d2} - {d1})" if self.gp else f"date_diff('day', {d1}, {d2})"

    def minutes_between(self, t1: str, t2: str) -> str:
        """Return the whole minutes from timestamp `t1` to `t2`.

        Greenplum floors the epoch seconds of `t2 - t1` divided by 60; Trino uses
        date_diff('minute'). They agree for `t2 >= t1`, as in `r_payments`, where a payment
        follows its order; for a negative span floor() rounds down, date_diff toward zero.
        """
        self.f("epoch")
        if self.gp:
            return f"floor(extract(epoch from ({t2} - {t1})) / 60)"
        return f"date_diff('minute', {t1}, {t2})"

    def years_between(self, d1: str, d2: str) -> str:
        """Return the full years from `d1` to `d2` as PostgreSQL age() counts them.

        Trino date_diff('year') balances leap days differently (2024-02-29 -> 2025-02-28 is 1
        there, 0 in PostgreSQL), so the Trino form is spelled out from year, month and day.
        """
        self.f("age")
        if self.gp:
            return f"date_part('year', age({d2}, {d1}))"
        return (f"(year({d2}) - year({d1}) - if(month({d2}) * 100 + day({d2}) < "
                f"month({d1}) * 100 + day({d1}), 1, 0))")

    def months_between(self, d1: str, d2: str) -> str:
        """Return the full months from `d1` to `d2` as PostgreSQL age() counts them; needs d2 >= d1.

        Trino date_diff('month') clamps month ends (2026-03-31 -> 2026-04-30 is 1 there, 0 in
        PostgreSQL), so the Trino form is spelled out from year, month and day.
        """
        self.f("age")
        if self.gp:
            return f"(date_part('year', age({d2}, {d1})) * 12 + date_part('month', age({d2}, {d1})))"
        return (f"((year({d2}) - year({d1})) * 12 + month({d2}) - month({d1}) - "
                f"if(day({d2}) < day({d1}), 1, 0))")

    def dow(self, dexpr: str) -> str:
        """Return the day of week of `dexpr`, 0 for Sunday through 6 for Saturday.

        Trino's day_of_week is ISO (Monday 1 to Sunday 7), so `% 7` maps it onto extract(dow).
        """
        self.f("dow")
        return f"extract(dow from {dexpr})" if self.gp else f"day_of_week({dexpr}) % 7"

    def year(self, dexpr: str) -> str:
        """Return the calendar year of `dexpr`: date_part in Greenplum, year() in Trino."""
        self.f("date_part")
        return f"date_part('year', {dexpr})" if self.gp else f"year({dexpr})"

    def series_days(self, start_iso: str, end_iso: str, alias: str, col: str) -> tuple[str, str]:
        """Return a FROM item with one row per day from `start_iso` to `end_iso`, both included.

        Greenplum runs generate_series over timestamps into the column `<col>_ts` and casts it
        back to date; Trino unnests sequence() into the column `col`.

        Returns:
            `(from_item, day_expr)`: put `from_item` after FROM and select `day_expr`, a date.
        """
        self.f("generate_series")
        if self.gp:
            return (f"generate_series({self.date_lit(start_iso)}, {self.date_lit(end_iso)}, interval '1 day') "
                    f"AS {alias}({col}_ts)", f"{alias}.{col}_ts::date")
        return (f"UNNEST(sequence(DATE '{start_iso}', DATE '{end_iso}', INTERVAL '1' DAY)) AS {alias}({col})",
                f"{alias}.{col}")

    # -------------------------------------------------------------- strings
    def ilike(self, col: str, pattern: str) -> str:
        """Return a case-insensitive LIKE predicate; Trino lower-cases the column and pattern."""
        self.f("ilike")
        return f"{col} ILIKE '{pattern}'" if self.gp else f"lower({col}) LIKE '{pattern.lower()}'"

    def regex(self, col: str, pattern: str) -> str:
        """Return a regular-expression match predicate: `~` in Greenplum, regexp_like in Trino."""
        self.f("regex")
        return f"{col} ~ '{pattern}'" if self.gp else f"regexp_like({col}, '{pattern}')"

    def label(self, *parts: str, sep: str = "-") -> str:
        """Return the parts joined by `||` with the separator `sep`; any NULL part gives NULL.

        id || '-' || text: PostgreSQL casts non-text operands implicitly, Trino does not, so
        Trino casts each part to VARCHAR.
        """
        self.f("concat_types")
        glue = f" || '{sep}' || "
        return glue.join(parts if self.gp else [self.text(p) for p in parts])

    def concat_nullsafe(self, *parts: str, sep: str = "-") -> str:
        """Return concat() of the parts separated by `sep`, with NULL parts read as ''.

        concat() skips NULLs in PostgreSQL but returns NULL in Trino, so Trino wraps each part in
        coalesce(..., ''). Separators stay: a NULL last part leaves a trailing `sep`.
        """
        self.f("concat_null")
        items = []
        for idx, p in enumerate(parts):
            if idx:
                items.append(f"'{sep}'")
            items.append(p if self.gp else f"coalesce({self.text(p)}, '')")
        return f"concat({', '.join(items)})"

    def agg_join(self, col: str, sep: str, order: str | None = None, distinct: bool = False) -> str:
        """Return the non-NULL `col` values of a group joined by `sep`, sorted by `order`.

        Greenplum uses string_agg. Trino uses array_join(array_agg) or listagg by the `trino_agg`
        style, filtered so that a group of only NULLs stays NULL; with `distinct` it sorts the
        distinct values itself. Leave `order` unset with `distinct`: PostgreSQL requires a
        DISTINCT aggregate to be ordered by its argument.
        """
        self.f("string_agg")
        order = order or col
        if self.gp:
            dist = "DISTINCT " if distinct else ""
            return f"string_agg({dist}{col}, '{sep}' ORDER BY {order})"
        # FILTER keeps the all-NULL group NULL, as string_agg does (array_join would give '')
        nonnull = f"FILTER (WHERE {col} IS NOT NULL)"
        if distinct:
            return f"array_join(array_sort(array_distinct(array_agg({col}) {nonnull})), '{sep}')"
        if self.st["trino_agg"] == "listagg":
            return f"listagg({col}, '{sep}') WITHIN GROUP (ORDER BY {order}) {nonnull}"
        return f"array_join(array_agg({col} ORDER BY {order}) {nonnull}, '{sep}')"

    def left(self, s: str, n: int) -> str:
        """Return the first `n` characters of `s`: left() in Greenplum, substr() in Trino."""
        self.f("substr")
        return f"left({s}, {n})" if self.gp else f"substr({s}, 1, {n})"

    def right(self, s: str, n: int) -> str:
        """Return the last `n` characters of `s`, or all of `s` if it is shorter.

        Trino has no right(), and substr() from a start clamped to 1 keeps a short string whole.
        """
        self.f("substr")
        # substr(s, -n) is '' for strings shorter than n; right() returns the whole string
        return f"right({s}, {n})" if self.gp else f"substr({s}, greatest(length({s}) - {n} + 1, 1))"

    def md5hex(self, s: str) -> str:
        """Return the lower-case hex MD5 of the text `s`; Trino hashes its UTF-8 bytes."""
        self.f("md5")
        return f"md5({s})" if self.gp else f"lower(to_hex(md5(to_utf8({s}))))"

    def json_get(self, col: str, key: str) -> str:
        """Return the value of the top-level `key` of the JSON text in `col`, as text.

        Greenplum uses `->>` in the colon style, else json_extract_path_text; Trino uses
        json_extract_scalar. They agree for scalar values; for an object or array Trino gives NULL.
        """
        self.f("json")
        if self.gp:
            return f"({col}::json ->> '{key}')" if self.st["gp_cast"] == "colon" else \
                f"json_extract_path_text(CAST({col} AS json), '{key}')"
        return f"json_extract_scalar({col}, '$.{key}')"

    def try_bigint(self, col: str) -> str:
        """Return the text `col` as bigint, or NULL when it is not a string of digits.

        Greenplum guards the cast with the regex `^[0-9]+$`; Trino uses try_cast. They agree on
        unsigned digit strings that fit in bigint: try_cast also accepts a sign, and the Greenplum
        cast raises on overflow.
        """
        self.f("try_cast")
        if self.gp:
            return f"CASE WHEN {col} ~ '^[0-9]+$' THEN {self.cast(col, 'bigint')} END"
        return f"try_cast({col} AS BIGINT)"

    def iff(self, cond: str, a: str, b: str) -> str:
        """Return `a` if `cond` holds, else `b`: CASE WHEN in Greenplum, if() in Trino."""
        if self.gp:
            return f"CASE WHEN {cond} THEN {a} ELSE {b} END"
        self.f("if")
        return f"if({cond}, {a}, {b})"

    # -------------------------------------------------------------- arrays from comma lists
    def tag_count(self, col: str) -> str:
        """Return the number of items in the comma-separated list `col`; NULL for NULL or ''.

        PostgreSQL splits '' into an empty array, whose array_length is NULL, so Trino
        special-cases ''.
        """
        self.f("arrays")
        if self.gp:
            return f"array_length(string_to_array({col}, ','), 1)"
        return f"if({col} = '', NULL, cardinality(split({col}, ',')))"  # '' is an empty array in PostgreSQL

    def tags_upper(self, col: str) -> str:
        """Return the comma-separated list `col` upper-cased and re-joined with '|'.

        Greenplum rewrites the string; Trino maps a lambda over the split list.
        """
        self.f("lambda")
        return f"upper(replace({col}, ',', '|'))" if self.gp else \
            f"array_join(transform(split({col}, ','), x -> upper(x)), '|')"

    def tags_matching(self, col: str, prefix: str) -> str:
        """Return how many items of the comma-separated `col` start with `prefix`; NULL for NULL.

        Greenplum counts unnested items in a subquery; Trino filters the split list with a lambda.
        """
        self.f("lambda")
        if self.gp:
            return (f"CASE WHEN {col} IS NULL THEN NULL ELSE (SELECT count(*) FROM unnest(string_to_array({col}, ',')) "
                    f"AS u(x) WHERE u.x LIKE '{prefix}%') END")
        return f"cardinality(filter(split({col}, ','), x -> x LIKE '{prefix}%'))"

    def unnest_tags(self, col: str, alias: str, out: str) -> str:
        """Return a join fragment, starting with CROSS JOIN, giving one row per item of `col`.

        Each item is the column `alias.out`; rows with a NULL `col` drop out. An empty string gives
        no row in Greenplum but one empty item in Trino; the warehouse never stores ''.
        """
        self.f("unnest")
        if self.gp:
            return f"CROSS JOIN LATERAL unnest(string_to_array({col}, ',')) AS {alias}({out})"
        return f"CROSS JOIN UNNEST(split({col}, ',')) AS {alias}({out})"

    # -------------------------------------------------------------- numbers and nulls
    def ratio(self, num: str, den: str, places: int = 4) -> str:
        """Return `num / den` rounded to `places` decimals, NULL when `den` is 0.

        Greenplum divides as numeric, Trino as DOUBLE; integer division would truncate in both.
        """
        self.f("division")
        if self.gp:
            return f"round({self.cast(num, 'numeric')} / NULLIF({den}, 0), {places})"
        return f"round(CAST({num} AS DOUBLE) / NULLIF({den}, 0), {places})"

    def greatest2(self, a: str, b: str) -> str:
        """Return the larger of `a` and `b`, ignoring a NULL operand as PostgreSQL's greatest().

        Trino's greatest() is NULL if any operand is, so Trino compares coalesced operands.
        """
        self.f("greatest_null")
        return f"greatest({a}, {b})" if self.gp else f"greatest(coalesce({a}, {b}), coalesce({b}, {a}))"

    def least2(self, a: str, b: str) -> str:
        """Return the smaller of `a` and `b`, ignoring a NULL operand; see `greatest2`."""
        self.f("greatest_null")
        return f"least({a}, {b})" if self.gp else f"least(coalesce({a}, {b}), coalesce({b}, {a}))"

    def desc(self, expr: str) -> str:
        """Return an ORDER BY item sorting `expr` descending with PostgreSQL's NULL placement.

        PostgreSQL puts NULLs first in DESC order, Trino last, so Trino adds NULLS FIRST.
        """
        self.f("null_order")
        return f"{expr} DESC" if self.gp else f"{expr} DESC NULLS FIRST"

    def decimal(self, expr: str, args: str = "14,2") -> str:
        """Return `expr` for an INSERT into a DECIMAL(`args`) column.

        Greenplum narrows the value on assignment; Trino needs an explicit narrowing cast.
        """
        return expr if self.gp else f"CAST({expr} AS DECIMAL({args}))"

    # -------------------------------------------------------------- statements
    def ctas(self, name: str, select_sql: str, dist: str | None = None,
             partition_col: str | None = None) -> str:
        """Return CREATE TABLE `name` AS `select_sql` with the style's storage options.

        The options do not change the table's contents: Greenplum gets an append-optimized
        WITH clause by `gp_storage` and a DISTRIBUTED clause by `gp_dist`; Trino gets
        WITH (format = 'PARQUET', ...) when `trino_storage` is "parquet".

        Args:
            name: Table name from `t()`.
            dist: Greenplum distribution key for the "by" style; without it that style adds no
                clause, and the "random" style ignores it.
            partition_col: Column Trino partitions by month, only together with the Parquet
                properties; Greenplum ignores it.
        """
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
        """Return CREATE TABLE `name` with explicit columns, partitioned by month of `part_col`.

        The style does not apply. Greenplum gets an append-optimized column table DISTRIBUTED BY
        `dist` with classic monthly range partitions covering 2020-01 to 2029-12, so rows outside
        that range cannot be inserted; Trino gets Iceberg `partitioning = ARRAY['month(...)']`.

        Args:
            columns: `(name, type)` pairs with the type spelled as SQL for both dialects, such as
                `integer`, `varchar` or `decimal(14,2)`: Greenplum renames decimal to numeric,
                Trino upper-cases the type. These are not the logical type names of `cast()`.
        """
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
        """Return the statements and the name of a helper table used only within the script.

        Greenplum creates a TEMP table, DISTRIBUTED BY `dist` under the "by" style. Trino on
        Iceberg has no temporary tables, so it creates a regular table in the `sandbox` schema,
        which only the drop statement removes.

        Returns:
            `(create, name, drop)`: run `create` first, refer to the table as `name`, and place
            `drop` after its last use.
        """
        self.f("temp_table")
        if self.gp:
            tail = f"\nDISTRIBUTED BY ({dist})" if dist and self.st["gp_dist"] == "by" else ""
            return f"CREATE TEMP TABLE {short} AS\n{select_sql}{tail};", short, f"DROP TABLE {short};"
        name = self.t("sandbox", short)
        return f"CREATE TABLE {name} AS\n{select_sql};", name, f"DROP TABLE {name};"

    def update_from(self, target: str, alias: str, source: str, salias: str, on: str,
                    sets: list[tuple[str, str]]) -> str:
        """Return an UPDATE of `target` from the rows of `source` that match it on `on`.

        Greenplum uses UPDATE ... FROM; Trino has no join in UPDATE and uses MERGE ... WHEN
        MATCHED THEN UPDATE. Each target row must match at most one source row; otherwise MERGE
        fails, while UPDATE ... FROM silently uses one of the matches.

        Args:
            alias: Alias of `target`; `salias` is the alias of `source`. `on` may use both.
            sets: `(column, expression)` assignments; columns unqualified, expressions may use
                both aliases.
        """
        self.f("update_join")
        if self.gp:
            assign = ", ".join(f"{c} = {e}" for c, e in sets)
            return f"UPDATE {target} {alias}\nSET {assign}\nFROM {source} {salias}\nWHERE {on};"
        assign = ", ".join(f"{c} = {e}" for c, e in sets)
        return (f"MERGE INTO {target} {alias}\nUSING {source} {salias}\nON {on}\n"
                f"WHEN MATCHED THEN UPDATE SET {assign};")

    def delete_matching(self, target: str, alias: str, source: str, salias: str, key: str) -> str:
        """Return a DELETE of the rows of `target` whose column `key` matches a row of `source`.

        Greenplum uses DELETE ... USING; Trino has no USING, so it deletes `WHERE key IN
        (subquery)` and does not alias `target`. `key` must name a column in both tables.
        """
        self.f("delete_join")
        if self.gp:
            return f"DELETE FROM {target} {alias}\nUSING {source} {salias}\nWHERE {alias}.{key} = {salias}.{key};"
        return f"DELETE FROM {target}\nWHERE {key} IN (SELECT {salias}.{key} FROM {source} {salias});"

    def analyze(self, name: str) -> str:
        """Return `ANALYZE name;` in Greenplum and "" in Trino, which `templates._join` drops."""
        return f"ANALYZE {name};" if self.gp else ""
