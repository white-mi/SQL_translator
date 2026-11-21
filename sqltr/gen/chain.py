"""Composite templates for campaign v2: two scenario scripts linked by a shared key.

A composite family renders two existing templates and adds a linker that joins their outputs,
so one script carries several statements that cross-reference tables and mix divergence classes
from both scenarios plus the linker's own. The world stops being a checklist of single classes:
rules must survive combinations (a date fix next to a string aggregation, an UPDATE over a join).

Each composite targets one key pool - store, customer or month - and every template in a pool is
described by the columns its output always has, whatever its parameters drew. The linker uses
only `R` methods, so the two renderings stay equivalent by construction; `self-check` on the
stand verifies it on real engines, as for every other template.

The composite templates are appended to `templates.TEMPLATES`; samplers call the member
samplers, so a seed reproduces a split exactly as before. `family_id` covers both member
parameter sets, keeping composite families distinct and excludable like any other.
"""

from __future__ import annotations

import random
from typing import Callable

from .render import R
from .templates import Template


class Desc:
    """Output contract of one pool member: columns the linker may rely on.

    Attributes:
        key: The member's own join-key column, always in the output (cohorts calls it
            `cohort_month` where the month pool aliases it to `month_start`).
        num: Always-present numeric column, or None.
        num2: Second numeric column, or None.
        text: Always-present text column safe for aggregation and regex, or None.
        date: Always-present DATE column, or None.
        last: Take the LAST output table (fact with the pinned rollup) instead of the first.
    """

    def __init__(self, key: str, num: str | None = None, num2: str | None = None,
                 text: str | None = None, date: str | None = None, last: bool = False):
        """Store the column contract; see the class docstring for the fields."""
        self.key, self.num, self.num2, self.text, self.date, self.last = key, num, num2, text, date, last


# One entry per pool member; the dict key is the template name.
STORE = {
    "daily_sales": Desc("store_id", num="revenue", num2="units", date="sale_date"),
    "stock_alerts": Desc("store_id", num="on_hand", text="status", date="snapshot_date"),
    "payments": Desc("store_id", num="order_total", num2="minutes_to_pay", text="status"),
    "calendar": Desc("store_id", num="orders_cnt", date="cal_day"),
    "store_rank": Desc("store_id", num="paid", text="region"),
    "fact": Desc("store_id", num="revenue", text="channel", date="sale_date"),
}
CUSTOMER = {
    "last_order": Desc("customer_id", text="channel"),
    "rfm": Desc("customer_id", num="monetary", num2="frequency", text="value_band"),
    "contacts": Desc("customer_id", text="customer_key"),
}
MONTH = {
    "category_monthly": Desc("month_start", num="revenue", text="month_label"),
    "cohorts": Desc("cohort_month", num="active_customers"),
    "fact": Desc("month_start", num="revenue", text="channel", last=True),
}
POOLS = {"store": STORE, "customer": CUSTOMER, "month": MONTH}
# The key column the linker exposes in the chain table; members join through their own Desc.key.
POOL_KEY = {"store": "store_id", "customer": "customer_id", "month": "month_start"}

# Member options the composite pins for contract stability: fact only exposes sales_monthly with
# the rollup on, so the month pool forces it after sampling.
PIN = {"month": {"fact": {"rollup": True}}}


def _sample_chain(pool: str) -> Callable[[random.Random], dict]:
    """Return the sampler of one pool's composite template.

    Draws two distinct member templates and their parameters, applies the pool's pins and draws
    the linker knobs: a day-of-week column, a label aggregation, a regex-match count prefix, a
    HAVING minimum and an UPDATE through a helper table.
    """

    def sample(r: random.Random) -> dict:
        """Draw two members with their parameters and the linker knobs; see `_sample_chain`."""
        a, b = r.sample(sorted(POOLS[pool]), 2)
        pa, pb = _member(a, pool).sample(r), _member(b, pool).sample(r)
        for name, params in ((a, pa), (b, pb)):
            params.update(PIN.get(pool, {}).get(name, {}))
        return {"a": a, "pa": pa, "b": b, "pb": pb,
                "dow": r.random() < 0.5, "labels": r.random() < 0.7,
                "prefix": r.choice(["ok", "lo", "hi", "web", "app", "retail", "vip", "mid"]),
                "having": r.choice([None, None, 1, 2]), "update": r.random() < 0.5}
    return sample


def _member(name: str, pool: str) -> Template:
    """Return the member template `name` of `pool`; imported here to avoid a cycle at load time."""
    from .templates import BY_NAME
    return BY_NAME[name]


def _table(logical: str, x: R) -> str:
    """Return the physical name of a logical `schema.table` output in dialect `x`."""
    schema, table = logical.split(".", 1)
    return x.t(schema, table)


def _render_chain(pool: str) -> Callable[[dict, R], tuple[str, list[str]]]:
    """Return the renderer of one pool's composite template."""

    def render(p: dict, x: R) -> tuple[str, list[str]]:
        """Render both members and the linker over their outputs; see `_render_chain`.

        The linker joins `a`'s and `b`'s first (or pinned last) output tables on the pool key and
        aggregates: sums of both numeric columns with their ratio, optionally the day of week of
        `a`'s date column, `b`'s text values joined into a label, and a regex-match count. A
        HAVING minimum and an UPDATE of the sum through a per-key helper table come from the
        knobs. Every construct goes through `R` methods, so both dialects stay equivalent.
        """
        descs = POOLS[pool]
        alias = POOL_KEY[pool]
        script_a, out_a = _member(p["a"], pool).render(p["pa"], x)
        script_b, out_b = _member(p["b"], pool).render(p["pb"], x)
        da, db = descs[p["a"]], descs[p["b"]]
        ta = _table(out_a[-1] if da.last else out_a[0], x)
        tb = _table(out_b[-1] if db.last else out_b[0], x)
        link = x.t("wrk", f"chain_{pool}")
        cols, group = [f"a.{da.key} AS {alias}"], ["1"]
        if da.date and p["dow"]:
            cols.append(f"{x.dow('a.' + da.date)} AS link_dow")
            group.append(str(len(group) + 1))
        sum_a = f"round({x.cast('sum(a.' + da.num + ')', 'numeric')}, 2)" if da.num else None
        sum_b = f"round({x.cast('sum(b.' + db.num + ')', 'numeric')}, 2)" if db.num else None
        if sum_a:
            cols.append(f"{sum_a} AS sum_a")
        if sum_b:
            cols.append(f"{sum_b} AS sum_b")
        if sum_a and sum_b:
            # exact decimal division in both engines: DOUBLE arithmetic in Trino rounds
            # differently from PostgreSQL numeric at the 4th decimal (38/250 families diverged)
            x.f("division")
            num_dec, den_dec = x.cast(f"sum(a.{da.num})", "numeric"), x.cast(f"sum(b.{db.num})", "numeric")
            cols.append(f"round({num_dec} / NULLIF({den_dec}, 0), 2) AS share_b")
        if db.text and p["labels"]:
            cols.append(f"{x.agg_join('b.' + db.text, '|')} AS b_texts")
        if db.text:
            cols.append(f"count(*) FILTER (WHERE {x.regex('b.' + db.text, '^' + p['prefix'])}) AS b_match")
        having = f"\nHAVING count(*) >= {p['having']}" if p["having"] else ""
        select = (f"SELECT {',\n       '.join(cols)}\nFROM {ta} a\nJOIN {tb} b ON a.{da.key} = b.{db.key}"
                  f"\nGROUP BY {', '.join(group)}{having}")
        parts = [script_a, script_b, x.comment(f"сводка цепочки {p['a']} + {p['b']}") + x.ctas(link, select)]
        outputs = out_a + out_b + [f"wrk.chain_{pool}"]
        if p["update"] and sum_b:
            agg = (f"SELECT b.{db.key} AS {db.key}, round({x.cast('sum(b.' + db.num + ')', 'numeric')}, 2) AS b_total\n"
                   f"FROM {tb} b GROUP BY 1")
            create_tmp, tmp, drop_tmp = x.temp_table(f"chain_src_{pool}", agg, dist=db.key)
            update = x.update_from(link, "c", tmp, "t", f"c.{alias} = t.{db.key}", [("sum_b", "t.b_total")])
            parts += [create_tmp, update, drop_tmp]
        elif p["update"] and sum_a:
            parts.append(x.analyze(link))
        return "\n\n".join(part for part in parts if part) + "\n", outputs
    return render


COMPOSITE = [Template(f"chain_{pool}", _sample_chain(pool), _render_chain(pool)) for pool in POOLS]
