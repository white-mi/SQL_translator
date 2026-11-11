"""Synthetic retail warehouse: schema, K deterministic data states and loaders for both engines.

States:
  s1 - baseline data;
  s2 - NULLs in nullable columns, ties in amounts and timestamps, Cyrillic names and cities;
  s3 - date edge cases (month ends, leap day, year boundary, midnight) and large numbers;
  s4 - tiny tables, some of them empty.
Several states catch translations that are right on typical data but wrong on edge cases.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass
from decimal import Decimal

# ------------------------------------------------------------------ schema

# logical type -> (postgres type, trino/iceberg type)
TYPES = {
    "int": ("integer", "integer"),
    "bigint": ("bigint", "bigint"),
    "text": ("varchar", "varchar"),
    "money": ("numeric(12,2)", "decimal(12,2)"),
    "rate": ("numeric(5,4)", "decimal(5,4)"),
    "date": ("date", "date"),
    "ts": ("timestamp", "timestamp(6)"),
    "bool": ("boolean", "boolean"),
}

TABLES: dict[str, list[tuple[str, str]]] = {
    "customers": [("customer_id", "int"), ("full_name", "text"), ("city", "text"), ("segment", "text"),
                  ("signup_date", "date"), ("birth_date", "date"), ("email", "text"),
                  ("loyalty_code", "text")],
    "products": [("product_id", "int"), ("sku", "text"), ("title", "text"), ("category", "text"),
                 ("price", "money"), ("is_active", "bool"), ("tags", "text"), ("attrs", "text")],
    "stores": [("store_id", "int"), ("region", "text"), ("city", "text"), ("opened_date", "date")],
    "orders": [("order_id", "bigint"), ("customer_id", "int"), ("store_id", "int"), ("order_ts", "ts"),
               ("status", "text"), ("channel", "text"), ("promo_code", "text"), ("attrs", "text")],
    "order_items": [("order_id", "bigint"), ("line_no", "int"), ("product_id", "int"), ("qty", "int"),
                    ("unit_price", "money"), ("discount", "rate")],
    "payments": [("payment_id", "bigint"), ("order_id", "bigint"), ("paid_ts", "ts"), ("amount", "money"),
                 ("method", "text")],
    "inventory": [("store_id", "int"), ("product_id", "int"), ("snapshot_date", "date"), ("on_hand", "int")],
}


@dataclass(frozen=True)
class StateSpec:
    """Recipe of one data state: its random seed, table sizes and the edge cases it injects.

    Attributes:
        seed: seed of the state's random generator; the same spec always gives the same data.
        customers, products, orders: rows generated in those tables (stores are always 12).
        nulls: probability that a value of a nullable column is NULL; non-zero also lets
            discounts be NULL.
        cyrillic: share of customers with Cyrillic names and cities.
        ties: create ties: a timestamp shared by some orders, prices of 499.00, payments of 1000.00.
        edge_dates: draw 30% of dates and timestamps from EDGE_DATES and EDGE_TS.
        big_numbers: allow some prices up to 99999 and some quantities up to 500.
        empty: tables left empty.
    """

    seed: int
    customers: int
    products: int
    orders: int
    nulls: float = 0.0
    cyrillic: float = 0.0
    ties: bool = False
    edge_dates: bool = False
    big_numbers: bool = False
    empty: tuple[str, ...] = ()


STATES: dict[str, StateSpec] = {
    "s1": StateSpec(seed=101, customers=150, products=60, orders=900),
    "s2": StateSpec(seed=202, customers=150, products=60, orders=900, nulls=0.12, cyrillic=0.3, ties=True),
    "s3": StateSpec(seed=303, customers=120, products=50, orders=700, edge_dates=True, big_numbers=True),
    "s4": StateSpec(seed=404, customers=20, products=10, orders=30, empty=("payments", "inventory")),
}

AS_OF = dt.date(2026, 6, 30)
ORDERS_FROM = dt.datetime(2025, 7, 1)

FIRST = ["Anna", "Boris", "Clara", "Denis", "Elena", "Fedor", "Galina", "Igor", "Kira", "Lev",
         "Maria", "Nikita", "Olga", "Pavel", "Rita", "Sergey", "Tatiana", "Victor", "Yana", "Zakhar"]
LAST = ["Ivanova", "Petrov", "Smirnova", "Kuznetsov", "Popova", "Volkov", "Sokolova", "Lebedev",
        "Kozlova", "Novikov", "Morozova", "Orlov"]
FIRST_RU = ["Анна", "Борис", "Вера", "Глеб", "Дарья", "Егор", "Зоя", "Илья"]
LAST_RU = ["Иванова", "Петров", "Смирнова", "Кузнецов", "Попова", "Волков"]
CITIES = ["Moscow", "Kazan", "Samara", "Perm", "Tver", "Omsk", "Sochi", "Ufa"]
CITIES_RU = ["Москва", "Казань", "Самара", "Пермь"]
REGIONS = {"Moscow": "Center", "Tver": "Center", "Kazan": "Volga", "Samara": "Volga", "Ufa": "Volga",
           "Perm": "Ural", "Omsk": "Siberia", "Sochi": "South"}
SEGMENTS = ["retail", "wholesale", "vip"]
CATEGORIES = ["grocery", "home", "toys", "garden", "books", "sport"]
TAGS = ["eco", "new", "sale", "kids", "bulk", "premium", "local", "gift"]
COLORS = ["red", "green", "blue", "black", "white"]
STATUSES = ["new", "paid", "shipped", "cancelled", "returned"]
CHANNELS = ["web", "app", "store"]
METHODS = ["card", "cash", "sbp"]
DEVICES = ["ios", "android", "desktop"]
COUPONS = ["SPRING10", "VIP20", "WELCOME5", "FLASH15"]
PROMOS = ["SPRING10", "spring-sale", "VIP20", "WELCOME5", "flash15", "BLACKFRIDAY"]

EDGE_TS = [dt.datetime(2025, 12, 31, 23, 59, 59), dt.datetime(2026, 1, 1, 0, 0, 0),
           dt.datetime(2026, 2, 28, 23, 59, 59), dt.datetime(2026, 3, 1, 0, 0, 0),
           dt.datetime(2026, 3, 31, 12, 0, 0), dt.datetime(2026, 4, 30, 0, 0, 0),
           dt.datetime(2026, 6, 30, 23, 59, 59), dt.datetime(2025, 7, 1, 0, 0, 0)]
EDGE_DATES = [dt.date(2024, 2, 29), dt.date(2025, 12, 31), dt.date(2026, 1, 1), dt.date(2026, 2, 28),
              dt.date(2026, 3, 31), dt.date(2026, 6, 30)]


def _maybe_null(r: random.Random, spec: StateSpec, value):
    """Return None with probability `spec.nulls`, else `value`; draws a number only if nulls > 0."""
    return None if spec.nulls and r.random() < spec.nulls else value


def _money(r: random.Random, lo: float, hi: float) -> Decimal:
    """Return a random amount between lo and hi, rounded to cents."""
    return Decimal(f"{r.uniform(lo, hi):.2f}")


def _rand_ts(r: random.Random, spec: StateSpec) -> dt.datetime:
    """Return a random timestamp from ORDERS_FROM to the last second of AS_OF.

    With `spec.edge_dates`, 30% of the draws are an EDGE_TS value instead.
    """
    if spec.edge_dates and r.random() < 0.3:
        return r.choice(EDGE_TS)
    span = int((dt.datetime.combine(AS_OF, dt.time(23, 59, 59)) - ORDERS_FROM).total_seconds())
    return ORDERS_FROM + dt.timedelta(seconds=r.randrange(span))


def _rand_date(r: random.Random, lo: dt.date, hi: dt.date, spec: StateSpec) -> dt.date:
    """Return a random date from lo to hi inclusive.

    With `spec.edge_dates`, 30% of the draws are an EDGE_DATES value instead, which may fall
    outside [lo, hi] (in s3: birth dates in 2024-2026, store openings in 2026).
    """
    if spec.edge_dates and r.random() < 0.3:
        return r.choice(EDGE_DATES)
    return lo + dt.timedelta(days=r.randrange((hi - lo).days + 1))


def generate_state(name: str) -> dict[str, list[tuple]]:
    """Generate the rows of every table for the state `name`, a key of STATES.

    Deterministic: the state's seed fixes the data, so `load_postgres` and `load_trino`, which call
    this separately, load identical copies. Changing the order or number of random draws here
    changes the data of the state, and with it every reference hash. Returns table name -> rows,
    each row a tuple in TABLES column order with Python values (int, str, Decimal, date,
    datetime, bool or None); tables in `spec.empty` are empty lists.
    """
    spec = STATES[name]
    r = random.Random(spec.seed)
    data: dict[str, list[tuple]] = {}

    stores = []
    for sid in range(1, 13):
        city = CITIES[(sid - 1) % len(CITIES)]
        stores.append((sid, REGIONS[city], city, _rand_date(r, dt.date(2018, 1, 1), dt.date(2025, 12, 31), spec)))
    data["stores"] = stores

    customers = []
    for cid in range(1, spec.customers + 1):
        if spec.cyrillic and r.random() < spec.cyrillic:
            full_name = f"{r.choice(FIRST_RU)} {r.choice(LAST_RU)}"
            city = r.choice(CITIES_RU)
        else:
            full_name = f"{r.choice(FIRST)} {r.choice(LAST)}"
            city = r.choice(CITIES)
        handle = f"user{cid}" if r.random() > 0.08 else f"user {cid}"  # some invalid emails
        domain = r.choice(["mail.ru", "yandex.ru", "gmail.com", "example"])
        email = f"{handle}@{domain}"
        code = r.random()
        loyalty = (str(r.randrange(10**5, 10**9)) if code < 0.7 else
                   r.choice(["N/A", "12A", "", "007"]) if code < 0.9 else None)
        customers.append((cid, full_name, _maybe_null(r, spec, city), _maybe_null(r, spec, r.choice(SEGMENTS)),
                          _rand_date(r, dt.date(2023, 1, 1), AS_OF, spec),
                          _maybe_null(r, spec, _rand_date(r, dt.date(1960, 1, 1), dt.date(2006, 12, 31), spec)),
                          email, _maybe_null(r, spec, loyalty)))
    data["customers"] = customers

    products = []
    for pid in range(1, spec.products + 1):
        hi = 99999.0 if spec.big_numbers and r.random() < 0.2 else 5000.0
        price = _money(r, 1.0, hi)
        if spec.ties and r.random() < 0.3:
            price = Decimal("499.00")
        tags = ",".join(r.sample(TAGS, r.randint(1, 3))) if r.random() > 0.15 else None
        attrs = {"color": r.choice(COLORS), "weight": round(r.uniform(0.1, 20.0), 2)}
        if r.random() < 0.2:
            attrs.pop("color")
        products.append((pid, f"SKU-{pid:04d}", f"{r.choice(CATEGORIES).title()} item {pid}",
                         r.choice(CATEGORIES), price, r.random() > 0.1, _maybe_null(r, spec, tags),
                         _maybe_null(r, spec, json.dumps(attrs, ensure_ascii=False))))
    data["products"] = products

    orders, items, payments = [], [], []
    payment_id = 1
    tie_ts = _rand_ts(r, spec)
    for oid in range(1, spec.orders + 1):
        order_id = 100000 + oid
        ts = tie_ts if spec.ties and r.random() < 0.1 else _rand_ts(r, spec)
        cust = r.randint(1, spec.customers)
        status = r.choices(STATUSES, weights=[1, 4, 4, 1, 1])[0]
        promo = r.choice(PROMOS) if r.random() < 0.25 else None
        attrs = {"device": r.choice(DEVICES)}
        if r.random() < 0.3:
            attrs["coupon"] = r.choice(COUPONS)
        orders.append((order_id, _maybe_null(r, spec, cust), r.randint(1, 12), ts, status, r.choice(CHANNELS),
                       promo, json.dumps(attrs)))
        total = Decimal("0")
        for line in range(1, r.randint(1, 4) + 1):
            prod = products[r.randrange(len(products))]
            qty = r.randint(1, 500 if spec.big_numbers and r.random() < 0.1 else 5)
            discount = r.choice([None, Decimal("0.0500"), Decimal("0.1000"), Decimal("0.0000")])
            if discount is None and not spec.nulls:
                discount = Decimal("0.0000")
            items.append((order_id, line, prod[0], qty, prod[4], discount))
            total += prod[4] * qty * (1 - (discount or 0))
        if status in ("paid", "shipped", "returned") and "payments" not in spec.empty:
            parts = 2 if r.random() < 0.1 else 1
            for part in range(parts):
                amount = (total / parts).quantize(Decimal("0.01"))
                if spec.ties and r.random() < 0.2:
                    amount = Decimal("1000.00")
                paid = ts + dt.timedelta(minutes=r.randint(1, 72 * 60))
                payments.append((payment_id, order_id, paid, amount, _maybe_null(r, spec, r.choice(METHODS))))
                payment_id += 1
    data["orders"], data["order_items"], data["payments"] = orders, items, payments

    inventory = []
    if "inventory" not in spec.empty:
        for snap in (dt.date(2026, 4, 30), dt.date(2026, 5, 31), dt.date(2026, 6, 30)):
            for store in range(1, 13):
                for prod in products:
                    if r.random() < 0.5:
                        inventory.append((store, prod[0], snap, r.randint(0, 200)))
    data["inventory"] = inventory

    for name_empty in spec.empty:
        data[name_empty] = []
    return data


# ------------------------------------------------------------------ DDL


def ddl(engine: str, schema: str, table: str, catalog: str = "iceberg") -> str:
    """Return the CREATE TABLE statement of a warehouse table for one engine.

    `engine` "postgres" gives PostgreSQL types and `schema.table`; any other value gives the
    Trino/Iceberg form `catalog.schema.table` stored as Parquet.
    """
    idx = 0 if engine == "postgres" else 1
    cols = ", ".join(f"{c} {TYPES[t][idx]}" for c, t in TABLES[table])
    if engine == "postgres":
        return f"CREATE TABLE {schema}.{table} ({cols})"
    return f"CREATE TABLE {catalog}.{schema}.{table} ({cols}) WITH (format = 'PARQUET')"


def trino_literal(value, logical: str) -> str:
    """Render a Python value as a Trino SQL literal of the logical type `logical` (a key of TYPES).

    Used by `load_trino` to build INSERT ... VALUES; None becomes NULL, and text is single-quoted
    with quotes doubled.
    """
    if value is None:
        return "NULL"
    if logical in ("int", "bigint"):
        return str(value)
    if logical in ("money", "rate"):
        return f"DECIMAL '{value}'"
    if logical == "date":
        return f"DATE '{value.isoformat()}'"
    if logical == "ts":
        return f"TIMESTAMP '{value.isoformat(sep=' ', timespec='microseconds')}'"
    if logical == "bool":
        return "true" if value else "false"
    return "'" + str(value).replace("'", "''") + "'"


# ------------------------------------------------------------------ loaders


def load_postgres(conn, states: list[str], runner_user: str) -> dict[str, dict[str, int]]:
    """Create the retail_<state> schemas in PostgreSQL with data; grant read access to the runner.

    Destructive: each schema is dropped with CASCADE and recreated, then filled with COPY. The
    runner role gets USAGE on the schema and SELECT on its tables, so it must exist already
    (`setup_postgres_roles`). Callers: `python -m sqltr load-states` and the `pg_cfg` test fixture.

    Args:
        conn: psycopg connection as an admin that may create schemas, in autocommit mode: the
            function never commits.
        states: state names, keys of STATES.
        runner_user: the role that executes translations.

    Returns:
        Row counts, `{state: {table: rows}}`.
    """
    from psycopg import sql

    counts: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        for state in states:
            schema = f"retail_{state}"
            data = generate_state(state)
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            counts[state] = {}
            for table, rows in data.items():
                cur.execute(ddl("postgres", schema, table))
                cols = [c for c, _ in TABLES[table]]
                with cur.copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                        sql.Identifier(schema), sql.Identifier(table),
                        sql.SQL(", ").join(sql.Identifier(c) for c in cols))) as copy:
                    for row in rows:
                        copy.write_row(row)
                counts[state][table] = len(rows)
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(runner_user)))
            cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(runner_user)))
    return counts


def load_trino(conn, states: list[str], catalog: str = "iceberg", batch: int = 250) -> dict[str, dict[str, int]]:
    """Create the retail_<state> schemas in a Trino catalog with the same data as `load_postgres`.

    Destructive: an existing schema loses its tables one by one and is then dropped and recreated.
    Rows go in as `INSERT ... VALUES` literals (`trino_literal`), `batch` rows per statement.
    Unlike `load_postgres`, it grants nothing. Caller: `python -m sqltr load-states`.

    Args:
        conn: Trino DB-API connection (`trino.dbapi.connect`) as the admin user.
        states: state names, keys of STATES.
        catalog: the Iceberg catalog that holds the schemas.
        batch: rows per INSERT statement.

    Returns:
        Row counts, `{state: {table: rows}}`.
    """
    counts: dict[str, dict[str, int]] = {}
    cur = conn.cursor()

    def run(statement: str):
        """Execute one statement and return all its rows; fetching waits for it to finish."""
        cur.execute(statement)
        return cur.fetchall()

    for state in states:
        schema = f"retail_{state}"
        data = generate_state(state)
        existing = [r[0] for r in run(f"SHOW SCHEMAS FROM {catalog}")]
        if schema in existing:
            for (table,) in run(f"SHOW TABLES FROM {catalog}.{schema}"):
                run(f"DROP TABLE {catalog}.{schema}.{table}")
            run(f"DROP SCHEMA {catalog}.{schema}")
        run(f"CREATE SCHEMA {catalog}.{schema}")
        counts[state] = {}
        for table, rows in data.items():
            run(ddl("trino", schema, table, catalog))
            types = [t for _, t in TABLES[table]]
            for start in range(0, len(rows), batch):
                chunk = rows[start:start + batch]
                values = ",\n".join(
                    "(" + ", ".join(trino_literal(v, t) for v, t in zip(row, types)) + ")" for row in chunk)
                run(f"INSERT INTO {catalog}.{schema}.{table} VALUES {values}")
            counts[state][table] = len(rows)
    return counts


# Most temporary-file space one runner session may use (sorts, hashes, temp tables). A correct
# translation of a script over the synthetic states needs a few megabytes; a runaway join stops
# here with SQLSTATE 53400 and scores 0 instead of filling the server's disk (sqltr_metric.engines).
RUNNER_TEMP_FILE_LIMIT = "1GB"


def setup_postgres_roles(conn, runner_user: str, runner_password: str | None) -> None:
    """Create the unprivileged runner role used to execute translations, and its database grants.

    Idempotent: an existing role is kept and its password is not changed. Without
    `runner_password` the role gets no password (local trust authentication). Sets the role's
    temp_file_limit to RUNNER_TEMP_FILE_LIMIT every time; the metric refuses to run without it.
    Also revokes CREATE on schema public from PUBLIC and grants the runner CONNECT and TEMPORARY
    on the current database; read access to the data comes from `load_postgres`. `conn` must be
    an autocommit superuser connection (temp_file_limit is a superuser setting). Callers:
    `python -m sqltr setup-pg` and the `pg_cfg` test fixture.
    """
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (runner_user,))
        if cur.fetchone() is None:
            if runner_password:
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(runner_user), sql.Literal(runner_password)))
            else:
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(runner_user)))
        cur.execute(sql.SQL("ALTER ROLE {} SET temp_file_limit = {}").format(
            sql.Identifier(runner_user), sql.Literal(RUNNER_TEMP_FILE_LIMIT)))
        cur.execute("SELECT current_database()")
        (db,) = cur.fetchone()
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        cur.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(db), sql.Identifier(runner_user)))
