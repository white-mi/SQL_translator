"""Scenario templates. Each template = parameter sampler + paired renderer.

A family is a template with fixed parameters; its style variants (paraphrases) stay in one split.
Rules that keep the two renderings equivalent and the results deterministic:
- every window ORDER BY ends with a unique key, so ties never make the result engine-dependent;
- string columns created from literals are cast to unbounded VARCHAR in Trino (Iceberg);
- numeric outputs are rounded explicitly; decimal values inserted into narrower columns are cast;
- the "current date" is the fixed AS_OF literal, never now().
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from .. import AS_OF
from .render import R

REV = "oi.qty * oi.unit_price * (1 - coalesce(oi.discount, 0))"
PAID_STATUSES = [("paid", "shipped"), ("paid", "shipped", "returned"), ("paid",)]


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _join(*statements: str) -> str:
    return "\n\n".join(s.strip() for s in statements if s and s.strip()) + "\n"


@dataclass(frozen=True)
class Template:
    name: str
    sample: Callable[[random.Random], dict]
    render: Callable[[dict, R], tuple[str, list[str]]]


# ---------------------------------------------------------------- 1. daily sales mart

def s_daily_sales(r):
    return {"days": r.randint(7, 120), "statuses": r.randrange(len(PAID_STATUSES)),
            "by_channel": r.random() < 0.5, "analyze": r.random() < 0.5,
            "min_units": r.choice([None, None, r.randint(2, 20)])}


def r_daily_sales(p, x: R):
    name = x.t("wrk", f"daily_sales_{p['days']}d")
    extra = ", o.channel" if p["by_channel"] else ""
    group = "1, 2, 3" if p["by_channel"] else "1, 2"
    select = (f"SELECT o.store_id, {x.to_date('o.order_ts')} AS sale_date{extra},\n"
              f"       count(DISTINCT o.order_id) AS orders_cnt,\n"
              f"       sum(oi.qty) AS units,\n"
              f"       round(sum({REV}), 2) AS revenue\n"
              f"FROM {x.src('orders')} o\n"
              f"JOIN {x.src('order_items')} oi ON oi.order_id = o.order_id\n"
              f"WHERE o.status IN {_in(PAID_STATUSES[p['statuses']])}\n"
              f"  AND o.order_ts >= {x.date_shift(x.date_lit(AS_OF), -p['days'])}\n"
              f"GROUP BY {group}" + (f"\nHAVING sum(oi.qty) >= {p['min_units']}" if p.get("min_units") else ""))
    script = _join(x.comment(f"витрина продаж по магазинам за {p['days']} дней") +
                   x.ctas(name, select, dist="store_id", partition_col="sale_date"),
                   x.analyze(name) if p["analyze"] else "")
    return script, [f"wrk.daily_sales_{p['days']}d"]


# ---------------------------------------------------------------- 2. monthly category report

def s_category_monthly(r):
    return {"months": r.randint(2, 12), "skus": r.random() < 0.5, "exclude": r.choice(["cancelled", "returned"]),
            "category": r.choice([None, None, "grocery", "home", "toys", "garden", "books", "sport"])}


def r_category_monthly(p, x: R):
    name = x.t("wrk", "category_monthly")
    skus = f",\n       {x.agg_join('p.sku', ',', distinct=True)} AS skus" if p["skus"] else ""
    select = (f"SELECT {x.month_start('o.order_ts')} AS month_start,\n"
              f"       {x.fmt_month('o.order_ts')} AS month_label,\n"
              f"       p.category,\n"
              f"       round(sum({REV}), 2) AS revenue,\n"
              f"       count(*) AS lines{skus}\n"
              f"FROM {x.src('orders')} o\n"
              f"JOIN {x.src('order_items')} oi ON oi.order_id = o.order_id\n"
              f"JOIN {x.src('products')} p ON p.product_id = oi.product_id\n"
              f"WHERE o.order_ts >= {x.date_shift(x.date_lit(AS_OF), -30 * p['months'])}\n"
              f"  AND o.status <> '{p['exclude']}'\n"
              + (f"  AND p.category = '{p['category']}'\n" if p.get("category") else "") +
              f"GROUP BY 1, 2, 3")
    return _join(x.comment("помесячная выручка по категориям") + x.ctas(name, select, dist="category")), \
        ["wrk.category_monthly"]


# ---------------------------------------------------------------- 3. last order per customer

def s_last_order(r):
    return {"city_filter": r.choice([None, "%an%", "%sk%", "%m%", "%a%", "%er%", "%o%", "%u%"]),
            "with_key": r.random() < 0.7, "channel": r.choice([None, None, "web", "app", "store"])}


def r_last_order(p, x: R):
    name = x.t("wrk", "customer_last_order")
    key = f",\n       {x.label('o.customer_id', 'c.segment')} AS customer_key" if p["with_key"] else ""
    where = "o.status <> 'cancelled'"
    if p["city_filter"]:
        where += f"\n  AND {x.ilike('c.city', p['city_filter'])}"
    if p.get("channel"):
        where += f"\n  AND o.channel = '{p['channel']}'"
    cols = "o.customer_id, o.order_id, o.order_ts, o.channel"
    if x.gp:
        x.f("distinct_on")
        select = (f"SELECT DISTINCT ON (o.customer_id) {cols}{key}\n"
                  f"FROM {x.src('orders')} o\n"
                  f"JOIN {x.src('customers')} c ON c.customer_id = o.customer_id\n"
                  f"WHERE {where}\n"
                  f"ORDER BY o.customer_id, o.order_ts DESC, o.order_id DESC")
    else:
        outer = "customer_id, order_id, order_ts, channel" + (", customer_key" if p["with_key"] else "")
        select = (f"SELECT {outer}\nFROM (\n"
                  f"  SELECT {cols}{key},\n"
                  f"         row_number() OVER (PARTITION BY o.customer_id ORDER BY o.order_ts DESC, o.order_id DESC) AS rn\n"
                  f"  FROM {x.src('orders')} o\n"
                  f"  JOIN {x.src('customers')} c ON c.customer_id = o.customer_id\n"
                  f"  WHERE {where}\n"
                  f") t\nWHERE rn = 1")
    return _join(x.comment("последний заказ каждого покупателя") + x.ctas(name, select, dist="customer_id")), \
        ["wrk.customer_last_order"]


# ---------------------------------------------------------------- 4. cohort retention

def s_cohorts(r):
    return {"horizon": r.randint(2, 12), "statuses": r.randrange(len(PAID_STATUSES)),
            "channel": r.choice([None, None, "web", "app", "store"])}


def r_cohorts(p, x: R):
    name = x.t("wrk", "cohort_retention")
    select = (f"WITH signups AS (\n"
              f"  SELECT c.customer_id, c.signup_date, {x.month_start('c.signup_date')} AS cohort_month\n"
              f"  FROM {x.src('customers')} c\n"
              f"), activity AS (\n"
              f"  SELECT s.cohort_month, o.customer_id,\n"
              f"         {x.months_between('s.signup_date', x.to_date('o.order_ts'))} AS month_n\n"
              f"  FROM signups s\n"
              f"  JOIN {x.src('orders')} o ON o.customer_id = s.customer_id\n"
              f"  WHERE o.status IN {_in(PAID_STATUSES[p['statuses']])}\n"
              f"    AND o.order_ts >= s.signup_date\n"
              + (f"    AND o.channel = '{p['channel']}'\n" if p.get("channel") else "") +
              f")\n"
              f"SELECT cohort_month, month_n, count(DISTINCT customer_id) AS active_customers\n"
              f"FROM activity\n"
              f"WHERE month_n <= {p['horizon']}\n"
              f"GROUP BY 1, 2")
    return _join(x.comment("удержание по когортам регистрации") + x.ctas(name, select)), ["wrk.cohort_retention"]


# ---------------------------------------------------------------- 5. stock alerts with update

def s_stock_alerts(r):
    return {"threshold": r.randint(1, 60), "region": r.choice([None, None, "Volga", "Center", "Ural", "South"])}


def r_stock_alerts(p, x: R):
    name = x.t("wrk", "stock_alerts")
    first = (f"SELECT i.store_id, i.product_id, i.on_hand, i.snapshot_date, {x.strlit('ok')} AS status\n"
             f"FROM {x.src('inventory')} i\n"
             + (f"JOIN {x.src('stores')} st ON st.store_id = i.store_id AND st.region = '{p['region']}'\n"
                if p.get("region") else "") +
             f"WHERE i.snapshot_date = (SELECT max(snapshot_date) FROM {x.src('inventory')})")
    low_sel = (f"SELECT s.store_id, s.product_id,\n"
               f"       CASE WHEN s.on_hand = 0 THEN {x.strlit('out')} ELSE {x.strlit('low')} END AS new_status\n"
               f"FROM {name} s\n"
               f"WHERE s.on_hand < {p['threshold']}")
    create_tmp, tmp, drop_tmp = x.temp_table("low_stock", low_sel, dist="store_id")
    update = x.update_from(name, "a", tmp, "l", "a.store_id = l.store_id AND a.product_id = l.product_id",
                           [("status", "l.new_status")])
    return _join(x.comment("остатки на последнюю дату") + x.ctas(name, first, dist="store_id"),
                 create_tmp, update, drop_tmp), ["wrk.stock_alerts"]


# ---------------------------------------------------------------- 6. payment reconciliation

def s_payments(r):
    return {"drop_cancelled": r.random() < 0.7, "days": r.randint(14, 365),
            "channel": r.choice([None, None, "web", "app", "store"])}


def r_payments(p, x: R):
    name = x.t("wrk", "payment_recon")
    select = (f"SELECT o.order_id, o.store_id, o.status,\n"
              f"       round(sum({REV}), 2) AS order_total,\n"
              f"       pay.paid_total, pay.first_paid_ts,\n"
              f"       {x.minutes_between('o.order_ts', 'pay.first_paid_ts')} AS minutes_to_pay\n"
              f"FROM {x.src('orders')} o\n"
              f"JOIN {x.src('order_items')} oi ON oi.order_id = o.order_id\n"
              f"LEFT JOIN (\n"
              f"  SELECT order_id, sum(amount) AS paid_total, min(paid_ts) AS first_paid_ts\n"
              f"  FROM {x.src('payments')}\n"
              f"  GROUP BY order_id\n"
              f") pay ON pay.order_id = o.order_id\n"
              f"WHERE o.order_ts >= {x.date_shift(x.date_lit(AS_OF), -p['days'])}\n"
              + (f"  AND o.channel = '{p['channel']}'\n" if p.get("channel") else "") +
              f"GROUP BY o.order_id, o.store_id, o.status, o.order_ts, pay.paid_total, pay.first_paid_ts")
    parts = [x.comment("сверка заказов и оплат") + x.ctas(name, select, dist="order_id")]
    if p["drop_cancelled"]:
        create_tmp, tmp, drop_tmp = x.temp_table(
            "cancelled_orders", f"SELECT order_id FROM {x.src('orders')} WHERE status = 'cancelled'", dist="order_id")
        parts += [create_tmp, x.delete_matching(name, "r", tmp, "c", "order_id"), drop_tmp]
    return _join(*parts), ["wrk.payment_recon"]


# ---------------------------------------------------------------- 7. product tags and attributes

def s_tags(r):
    return {"prefix": r.choice(["e", "s", "p", "k", "n", "b", "l", "g"]), "active_only": r.random() < 0.6,
            "profile": r.random() < 0.7, "min_products": r.choice([None, None, 2, 3, 5])}


def r_tags(p, x: R):
    stats = x.t("wrk", "tag_stats")
    where = "\nWHERE p.is_active" if p["active_only"] else ""
    sel1 = (f"SELECT t.tag, count(DISTINCT p.product_id) AS products,\n"
            f"       round(avg(p.price), 2) AS avg_price,\n"
            f"       {x.agg_join('p.category', ',', distinct=True)} AS categories\n"
            f"FROM {x.src('products')} p\n"
            f"{x.unnest_tags('p.tags', 't', 'tag')}{where}\n"
            f"GROUP BY t.tag"
            + (f"\nHAVING count(DISTINCT p.product_id) >= {p['min_products']}" if p.get("min_products") else ""))
    parts = [x.comment("статистика по тегам товаров") + x.ctas(stats, sel1)]
    outputs = ["wrk.tag_stats"]
    if p["profile"]:
        prof = x.t("wrk", "product_profile")
        sel2 = (f"SELECT p.product_id, p.sku,\n"
                f"       {x.left('p.sku', 3)} AS sku_prefix,\n"
                f"       {x.right('p.sku', 4)} AS sku_num,\n"
                f"       {x.tag_count('p.tags')} AS tag_cnt,\n"
                f"       {x.tags_upper('p.tags')} AS tags_upper,\n"
                f"       {x.tags_matching('p.tags', p['prefix'])} AS tags_with_prefix,\n"
                f"       {x.json_get('p.attrs', 'color')} AS color\n"
                f"FROM {x.src('products')} p")
        parts.append(x.ctas(prof, sel2, dist="product_id"))
        outputs.append("wrk.product_profile")
    return _join(*parts), outputs


# ---------------------------------------------------------------- 8. RFM segments

def s_rfm(r):
    lo = r.choice([300, 500, 1000, 1500, 2000, 3000])
    return {"lo": lo, "hi": lo * r.choice([3, 5, 10]), "statuses": r.randrange(len(PAID_STATUSES)),
            "with_age": r.random() < 0.6, "quantiles": r.choice([3, 4, 5, 10])}


def r_rfm(p, x: R):
    name = x.t("wrk", "rfm")
    age = f",\n       {x.years_between('c.birth_date', x.date_lit(AS_OF))} AS age_years" if p["with_age"] else ""
    select = (f"WITH base AS (\n"
              f"  SELECT o.customer_id, max({x.to_date('o.order_ts')}) AS last_date,\n"
              f"         count(DISTINCT o.order_id) AS frequency,\n"
              f"         round(sum({REV}), 2) AS monetary\n"
              f"  FROM {x.src('orders')} o\n"
              f"  JOIN {x.src('order_items')} oi ON oi.order_id = o.order_id\n"
              f"  WHERE o.status IN {_in(PAID_STATUSES[p['statuses']])} AND o.customer_id IS NOT NULL\n"
              f"  GROUP BY o.customer_id\n"
              f")\n"
              f"SELECT b.customer_id,\n"
              f"       {x.days_between('b.last_date', x.date_lit(AS_OF))} AS recency_days,\n"
              f"       b.frequency, b.monetary,\n"
              f"       CASE WHEN b.monetary >= {p['hi']} THEN {x.strlit('high')}\n"
              f"            WHEN b.monetary >= {p['lo']} THEN {x.strlit('mid')}\n"
              f"            ELSE {x.strlit('low')} END AS value_band,\n"
              f"       ntile({p.get('quantiles', 4)}) OVER (ORDER BY b.monetary DESC, b.customer_id) AS quartile,\n"
              f"       {x.md5hex('c.email')} AS email_hash{age}\n"
              f"FROM base b\n"
              f"JOIN {x.src('customers')} c ON c.customer_id = b.customer_id")
    return _join(x.comment("RFM-сегментация покупателей") + x.ctas(name, select, dist="customer_id")), ["wrk.rfm"]


# ---------------------------------------------------------------- 9. calendar fill

def s_calendar(r):
    return {"days": r.randint(5, 30), "region": r.choice([None, "Volga", "Center", "Ural", "South"])}


def r_calendar(p, x: R):
    name = x.t("wrk", "store_calendar")
    start = f"2026-06-{31 - p['days']:02d}"
    series, day = x.series_days(start, AS_OF, "g", "cal_day")
    region = f"\nWHERE s.region = '{p['region']}'" if p["region"] else ""
    select = (f"WITH days AS (\n"
              f"  SELECT {day} AS cal_day FROM {series}\n"
              f"), sales AS (\n"
              f"  SELECT o.store_id, {x.to_date('o.order_ts')} AS cal_day, count(*) AS orders_cnt\n"
              f"  FROM {x.src('orders')} o\n"
              f"  WHERE o.order_ts >= {x.date_lit(start)}\n"
              f"  GROUP BY 1, 2\n"
              f")\n"
              f"SELECT s.store_id, d.cal_day, {x.dow('d.cal_day')} AS dow, coalesce(z.orders_cnt, 0) AS orders_cnt\n"
              f"FROM days d\n"
              f"CROSS JOIN {x.src('stores')} s\n"
              f"LEFT JOIN sales z ON z.store_id = s.store_id AND z.cal_day = d.cal_day{region}")
    return _join(x.comment("календарь магазинов с нулями") + x.ctas(name, select, dist="store_id")), \
        ["wrk.store_calendar"]


# ---------------------------------------------------------------- 10. store ranking

def s_store_rank(r):
    return {"place": r.random() < 0.6, "worst": r.random() < 0.3,
            "method": r.choice([None, None, "card", "cash", "sbp"])}


def r_store_rank(p, x: R):
    name = x.t("wrk", "store_rank")
    order = f"{x.desc('t.paid')}, t.store_id" if not p["worst"] else "t.paid, t.store_id"
    place = f",\n       {x.concat_nullsafe('t.city', 't.region')} AS place" if p["place"] else ""
    select = (f"WITH totals AS (\n"
              f"  SELECT s.store_id, s.region, s.city, round(sum(pm.amount), 2) AS paid, max(pm.paid_ts) AS last_paid\n"
              f"  FROM {x.src('stores')} s\n"
              f"  LEFT JOIN {x.src('orders')} o ON o.store_id = s.store_id\n"
              f"  LEFT JOIN {x.src('payments')} pm ON pm.order_id = o.order_id"
              + (f" AND pm.method = '{p['method']}'" if p.get("method") else "") + "\n"
              f"  GROUP BY s.store_id, s.region, s.city\n"
              f")\n"
              f"SELECT t.store_id, t.region, t.paid, t.last_paid,\n"
              f"       dense_rank() OVER (PARTITION BY t.region ORDER BY {order}) AS region_rank{place}\n"
              f"FROM totals t")
    return _join(x.comment("рейтинг магазинов внутри региона") + x.ctas(name, select, dist="store_id")), \
        ["wrk.store_rank"]


# ---------------------------------------------------------------- 11. promo effect

def s_promo(r):
    return {"pattern": r.choice(["%spring%", "%vip%", "%flash%", "%sale%"]), "days": r.randint(30, 365),
            "coupon": r.random() < 0.6}


def r_promo(p, x: R):
    name = x.t("wrk", "promo_effect")
    promo = f"{x.ilike('o.promo_code', p['pattern'])} OR {x.regex('o.promo_code', '^[A-Z]+[0-9]+$')}"
    coupon = f",\n       count({x.json_get('o.attrs', 'coupon')}) AS coupon_orders" if p["coupon"] else ""
    select = (f"SELECT o.channel,\n"
              f"       {x.json_get('o.attrs', 'device')} AS device,\n"
              f"       count(*) AS orders_cnt,\n"
              f"       count(*) FILTER (WHERE {promo}) AS promo_orders,\n"
              f"       {x.ratio(f'count(*) FILTER (WHERE {promo})', 'count(*)')} AS promo_share{coupon}\n"
              f"FROM {x.src('orders')} o\n"
              f"WHERE o.order_ts >= {x.date_shift(x.date_lit(AS_OF), -p['days'])}\n"
              f"GROUP BY 1, 2")
    return _join(x.comment("эффект промокодов по каналам") + x.ctas(name, select)), ["wrk.promo_effect"]


# ---------------------------------------------------------------- 12. partitioned fact + monthly rollup

def s_fact(r):
    return {"statuses": r.randrange(len(PAID_STATUSES)), "rollup": r.random() < 0.7,
            "days": r.choice([None, r.randint(30, 365)])}


def r_fact(p, x: R):
    fact = x.t("wrk", "sales_fact")
    ddl = x.partitioned_table(fact, [("sale_date", "date"), ("store_id", "integer"), ("channel", "varchar"),
                                     ("revenue", "decimal(14,2)")], "sale_date", "store_id")
    insert = (f"INSERT INTO {fact}\n"
              f"SELECT {x.to_date('o.order_ts')}, o.store_id, o.channel, {x.decimal(f'round(sum({REV}), 2)')}\n"
              f"FROM {x.src('orders')} o\n"
              f"JOIN {x.src('order_items')} oi ON oi.order_id = o.order_id\n"
              f"WHERE o.status IN {_in(PAID_STATUSES[p['statuses']])}\n"
              + (f"  AND o.order_ts >= {x.date_shift(x.date_lit(AS_OF), -p['days'])}\n" if p.get("days") else "") +
              f"GROUP BY 1, 2, 3;")
    parts = [x.comment("партиционированный факт продаж") + ddl, insert, x.analyze(fact)]
    outputs = ["wrk.sales_fact"]
    if p["rollup"]:
        roll = x.t("wrk", "sales_monthly")
        sel = (f"SELECT {x.month_start('f.sale_date')} AS month_start, f.channel, sum(f.revenue) AS revenue\n"
               f"FROM {fact} f\nGROUP BY 1, 2")
        parts.append(x.ctas(roll, sel))
        outputs.append("wrk.sales_monthly")
    return _join(*parts), outputs


# ---------------------------------------------------------------- 13. loyalty codes

def s_loyalty(r):
    return {"premium": r.randint(1, 9) * 100000000, "since": r.choice([None, "2024-01-01", "2025-01-01", "2025-07-01"])}


def r_loyalty(p, x: R):
    name = x.t("wrk", "loyalty_check")
    code = x.try_bigint("c.loyalty_code")
    select = (f"SELECT c.segment,\n"
              f"       count(*) AS customers,\n"
              f"       count({code}) AS valid_codes,\n"
              f"       sum({x.iff(f'{code} > {p['premium']}', '1', '0')}) AS premium_codes\n"
              f"FROM {x.src('customers')} c\n"
              + (f"WHERE c.signup_date >= {x.date_lit(p['since'])}\n" if p.get("since") else "") +
              f"GROUP BY c.segment")
    return _join(x.comment("проверка кодов лояльности") + x.ctas(name, select)), ["wrk.loyalty_check"]


# ---------------------------------------------------------------- 14. customer contacts

def s_contacts(r):
    return {"city_filter": r.choice([None, "%a%", "%o%", "%an%", "%m%", "%r%"]), "tenure": r.random() < 0.6,
            "segment": r.choice([None, None, "retail", "wholesale", "vip"])}


def r_contacts(p, x: R):
    name = x.t("wrk", "customer_contacts")
    conds = []
    if p["city_filter"]:
        conds.append(x.ilike("c.city", p["city_filter"]))
    if p.get("segment"):
        conds.append(f"c.segment = '{p['segment']}'")
    where = ("\nWHERE " + "\n  AND ".join(conds)) if conds else ""
    tenure = f",\n       {x.years_between('c.signup_date', x.date_lit(AS_OF))} AS tenure_years" if p["tenure"] else ""
    select = (f"SELECT c.customer_id,\n"
              f"       {x.concat_nullsafe('c.full_name', 'c.city', sep=', ')} AS display_name,\n"
              f"       {x.regex('c.email', '^[a-z0-9._]+@[a-z0-9.]+[.][a-z]+$')} AS email_valid,\n"
              f"       {x.md5hex('c.email')} AS email_hash,\n"
              f"       {x.label('c.customer_id', 'coalesce(c.segment, ' + chr(39) + 'none' + chr(39) + ')')} AS customer_key{tenure}\n"
              f"FROM {x.src('customers')} c{where}")
    return _join(x.comment("контакты покупателей") + x.ctas(name, select, dist="customer_id")), \
        ["wrk.customer_contacts"]


TEMPLATES: list[Template] = [
    Template("daily_sales", s_daily_sales, r_daily_sales),
    Template("category_monthly", s_category_monthly, r_category_monthly),
    Template("last_order", s_last_order, r_last_order),
    Template("cohorts", s_cohorts, r_cohorts),
    Template("stock_alerts", s_stock_alerts, r_stock_alerts),
    Template("payments", s_payments, r_payments),
    Template("tags", s_tags, r_tags),
    Template("rfm", s_rfm, r_rfm),
    Template("calendar", s_calendar, r_calendar),
    Template("store_rank", s_store_rank, r_store_rank),
    Template("promo", s_promo, r_promo),
    Template("fact", s_fact, r_fact),
    Template("loyalty", s_loyalty, r_loyalty),
    Template("contacts", s_contacts, r_contacts),
]
BY_NAME = {t.name: t for t in TEMPLATES}


def render(template: str, params: dict, dialect: str, style: dict) -> tuple[str, list[str], list[str]]:
    x = R(dialect, style)
    script, outputs = BY_NAME[template].render(params, x)
    return script, outputs, sorted(x.features)
