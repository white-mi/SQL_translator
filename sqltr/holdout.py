"""Out-of-search validation set: hand-written script pairs outside the generator's 14 templates.

Purpose: check that what the search grows transfers beyond the generator. These scripts are never
given to the researcher and never used during the search; they are scored once for the baseline
before the campaign and once for the finalists after it (docs/adr/0003-dataset-split.md).

Each pair is written by hand to be semantically equal on all four data states; the Trino twin is
checked on the stand by `python -m sqltr self-check --split-dir data/holdout` (GP on PostgreSQL vs
Trino on Trino). Constructs deliberately absent from the templates: self-joins, ROLLUP/grouping(),
calendar zero-fill with window frames, DISTINCT ON, USING joins, CASE banding with JSON numbers,
DELETE ... USING, regex predicates, DESC ordering over NULLs, epoch arithmetic, FILTER vs count_if.
"""

from __future__ import annotations

from .gen.build import Instance

HOLDOUT: list[dict] = [
    {
        "name": "basket_pairs",
        "features": ["self_join", "temp_table", "having", "gp_storage", "gp_distribution"],
        "outputs": ["wrk.ho_basket_pairs"],
        "gp": """CREATE TEMP TABLE order_products AS
SELECT DISTINCT oi.order_id, oi.product_id
FROM retail.order_items oi
JOIN retail.orders o ON o.order_id = oi.order_id
WHERE o.status <> 'cancelled'
DISTRIBUTED BY (order_id);

CREATE TABLE wrk.ho_basket_pairs WITH (appendonly=true, orientation=column) AS
SELECT a.product_id AS product_a, b.product_id AS product_b, count(*)::int AS orders_together
FROM order_products a
JOIN order_products b ON a.order_id = b.order_id AND a.product_id < b.product_id
GROUP BY 1, 2
HAVING count(*) >= 2
DISTRIBUTED BY (product_a);

DROP TABLE order_products;
""",
        "trino": """CREATE TABLE iceberg.sandbox.order_products AS
SELECT DISTINCT oi.order_id, oi.product_id
FROM iceberg.retail.order_items oi
JOIN iceberg.retail.orders o ON o.order_id = oi.order_id
WHERE o.status <> 'cancelled';

CREATE TABLE iceberg.wrk.ho_basket_pairs AS
SELECT a.product_id AS product_a, b.product_id AS product_b, CAST(count(*) AS INTEGER) AS orders_together
FROM iceberg.sandbox.order_products a
JOIN iceberg.sandbox.order_products b ON a.order_id = b.order_id AND a.product_id < b.product_id
GROUP BY 1, 2
HAVING count(*) >= 2;

DROP TABLE iceberg.sandbox.order_products;
""",
    },
    {
        "name": "rolling_7d",
        "features": ["series_days", "window_frame", "left_join_zero_fill", "temp_table", "colon_cast"],
        "outputs": ["wrk.ho_rolling_7d"],
        "gp": """CREATE TEMP TABLE days AS
SELECT d::date AS sale_day
FROM generate_series('2026-01-01'::date, '2026-03-31'::date, interval '1 day') AS g(d);

CREATE TABLE wrk.ho_rolling_7d AS
WITH daily AS (
    SELECT o.order_ts::date AS sale_day, sum(oi.qty * oi.unit_price)::numeric(14,2) AS revenue
    FROM retail.orders o
    JOIN retail.order_items oi ON oi.order_id = o.order_id
    WHERE o.status IN ('paid', 'shipped')
    GROUP BY 1
)
SELECT d.sale_day,
       coalesce(daily.revenue, 0)::numeric(14,2) AS revenue,
       round(avg(coalesce(daily.revenue, 0)) OVER (ORDER BY d.sale_day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS revenue_7d_avg
FROM days d
LEFT JOIN daily ON daily.sale_day = d.sale_day
DISTRIBUTED BY (sale_day);

DROP TABLE days;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_rolling_7d AS
WITH days AS (
    SELECT sale_day
    FROM UNNEST(sequence(DATE '2026-01-01', DATE '2026-03-31', INTERVAL '1' DAY)) AS t(sale_day)
), daily AS (
    SELECT CAST(o.order_ts AS DATE) AS sale_day, CAST(sum(oi.qty * oi.unit_price) AS DECIMAL(14,2)) AS revenue
    FROM iceberg.retail.orders o
    JOIN iceberg.retail.order_items oi ON oi.order_id = o.order_id
    WHERE o.status IN ('paid', 'shipped')
    GROUP BY 1
)
SELECT d.sale_day,
       CAST(coalesce(daily.revenue, 0) AS DECIMAL(14,2)) AS revenue,
       round(avg(CAST(coalesce(daily.revenue, 0) AS DECIMAL(14,2))) OVER (ORDER BY d.sale_day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS revenue_7d_avg
FROM days d
LEFT JOIN daily ON daily.sale_day = d.sale_day;
""",
    },
    {
        "name": "region_channel_rollup",
        "features": ["rollup", "grouping_function", "count_distinct", "left_join"],
        "outputs": ["wrk.ho_region_channel"],
        "gp": """CREATE TABLE wrk.ho_region_channel AS
SELECT CASE WHEN grouping(s.region) = 1 THEN 'ALL' ELSE s.region END AS region,
       CASE WHEN grouping(o.channel) = 1 THEN 'ALL' ELSE coalesce(o.channel, 'unknown') END AS channel,
       count(DISTINCT o.order_id)::bigint AS orders,
       sum(p.amount)::numeric(14,2) AS paid
FROM retail.orders o
JOIN retail.stores s ON s.store_id = o.store_id
LEFT JOIN retail.payments p ON p.order_id = o.order_id
GROUP BY ROLLUP (s.region, o.channel)
DISTRIBUTED RANDOMLY;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_region_channel AS
SELECT CASE WHEN grouping(s.region) = 1 THEN 'ALL' ELSE s.region END AS region,
       CASE WHEN grouping(o.channel) = 1 THEN 'ALL' ELSE coalesce(o.channel, 'unknown') END AS channel,
       CAST(count(DISTINCT o.order_id) AS BIGINT) AS orders,
       CAST(sum(p.amount) AS DECIMAL(14,2)) AS paid
FROM iceberg.retail.orders o
JOIN iceberg.retail.stores s ON s.store_id = o.store_id
LEFT JOIN iceberg.retail.payments p ON p.order_id = o.order_id
GROUP BY ROLLUP (s.region, o.channel);
""",
    },
    {
        "name": "tag_revenue",
        "features": ["unnest_tags", "string_agg_distinct", "temp_table", "left_join"],
        "outputs": ["wrk.ho_tag_revenue"],
        "gp": """CREATE TEMP TABLE product_tags AS
SELECT p.product_id, p.category, trim(t.tag) AS tag
FROM retail.products p, unnest(string_to_array(p.tags, ',')) AS t(tag)
WHERE p.tags <> '';

CREATE TABLE wrk.ho_tag_revenue AS
SELECT pt.tag,
       count(DISTINCT pt.product_id)::int AS products,
       coalesce(sum(oi.qty * oi.unit_price), 0)::numeric(14,2) AS revenue,
       string_agg(DISTINCT pt.category, '|' ORDER BY pt.category) AS categories
FROM product_tags pt
LEFT JOIN retail.order_items oi ON oi.product_id = pt.product_id
GROUP BY pt.tag;

DROP TABLE product_tags;
""",
        "trino": """CREATE TABLE iceberg.sandbox.product_tags AS
SELECT p.product_id, p.category, trim(t.tag) AS tag
FROM iceberg.retail.products p
CROSS JOIN UNNEST(split(p.tags, ',')) AS t(tag)
WHERE p.tags <> '';

CREATE TABLE iceberg.wrk.ho_tag_revenue AS
SELECT pt.tag,
       CAST(count(DISTINCT pt.product_id) AS INTEGER) AS products,
       CAST(coalesce(sum(oi.qty * oi.unit_price), 0) AS DECIMAL(14,2)) AS revenue,
       array_join(array_sort(array_distinct(array_agg(pt.category) FILTER (WHERE pt.category IS NOT NULL))), '|') AS categories
FROM iceberg.sandbox.product_tags pt
LEFT JOIN iceberg.retail.order_items oi ON oi.product_id = pt.product_id
GROUP BY pt.tag;

DROP TABLE iceberg.sandbox.product_tags;
""",
    },
    {
        "name": "first_order_span",
        "features": ["distinct_on", "date_minus_date", "subquery_join"],
        "outputs": ["wrk.ho_first_order", "wrk.ho_customer_span"],
        "gp": """CREATE TABLE wrk.ho_first_order AS
SELECT DISTINCT ON (customer_id) customer_id, order_id AS first_order_id, order_ts AS first_order_ts, channel AS first_channel
FROM retail.orders
WHERE status <> 'cancelled'
ORDER BY customer_id, order_ts, order_id;

CREATE TABLE wrk.ho_customer_span AS
SELECT f.customer_id,
       f.first_order_ts::date AS first_day,
       l.last_ts::date AS last_day,
       l.last_ts::date - f.first_order_ts::date AS span_days,
       l.orders
FROM wrk.ho_first_order f
JOIN (SELECT customer_id, max(order_ts) AS last_ts, count(*)::int AS orders
      FROM retail.orders
      WHERE status <> 'cancelled'
      GROUP BY customer_id) l ON l.customer_id = f.customer_id;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_first_order AS
SELECT customer_id, order_id AS first_order_id, order_ts AS first_order_ts, channel AS first_channel
FROM (
    SELECT o.customer_id, o.order_id, o.order_ts, o.channel,
           row_number() OVER (PARTITION BY o.customer_id ORDER BY o.order_ts, o.order_id) AS rn
    FROM iceberg.retail.orders o
    WHERE o.status <> 'cancelled'
) x
WHERE rn = 1;

CREATE TABLE iceberg.wrk.ho_customer_span AS
SELECT f.customer_id,
       CAST(f.first_order_ts AS DATE) AS first_day,
       CAST(l.last_ts AS DATE) AS last_day,
       date_diff('day', CAST(f.first_order_ts AS DATE), CAST(l.last_ts AS DATE)) AS span_days,
       l.orders
FROM iceberg.wrk.ho_first_order f
JOIN (SELECT customer_id, max(order_ts) AS last_ts, CAST(count(*) AS INTEGER) AS orders
      FROM iceberg.retail.orders
      WHERE status <> 'cancelled'
      GROUP BY customer_id) l ON l.customer_id = f.customer_id;
""",
    },
    {
        "name": "weekly_cohorts",
        "features": ["date_trunc_week", "using_join", "cte_chain", "integer_division"],
        "outputs": ["wrk.ho_weekly_cohorts"],
        "gp": """CREATE TABLE wrk.ho_weekly_cohorts AS
WITH weeks AS (
    SELECT customer_id, date_trunc('week', order_ts)::date AS week_start
    FROM retail.orders
    WHERE customer_id IS NOT NULL
    GROUP BY 1, 2
), firsts AS (
    SELECT customer_id, min(week_start) AS cohort_week
    FROM weeks
    GROUP BY 1
)
SELECT f.cohort_week, ((w.week_start - f.cohort_week) / 7)::int AS week_number, count(*)::int AS customers
FROM weeks w
JOIN firsts f USING (customer_id)
GROUP BY 1, 2
DISTRIBUTED BY (cohort_week);
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_weekly_cohorts AS
WITH weeks AS (
    SELECT customer_id, CAST(date_trunc('week', order_ts) AS DATE) AS week_start
    FROM iceberg.retail.orders
    WHERE customer_id IS NOT NULL
    GROUP BY 1, 2
), firsts AS (
    SELECT customer_id, min(week_start) AS cohort_week
    FROM weeks
    GROUP BY 1
)
SELECT f.cohort_week, CAST(date_diff('day', f.cohort_week, w.week_start) / 7 AS INTEGER) AS week_number,
       CAST(count(*) AS INTEGER) AS customers
FROM weeks w
JOIN firsts f USING (customer_id)
GROUP BY 1, 2;
""",
    },
    {
        "name": "device_mix",
        "features": ["json_get", "aggregate_filter", "integer_division"],
        "outputs": ["wrk.ho_device_mix"],
        "gp": """CREATE TABLE wrk.ho_device_mix AS
SELECT channel,
       count(*) FILTER (WHERE attrs::json->>'device' = 'ios')::int AS ios,
       count(*) FILTER (WHERE attrs::json->>'device' = 'android')::int AS android,
       count(*) FILTER (WHERE attrs::json->>'device' = 'desktop')::int AS desktop,
       count(*) FILTER (WHERE attrs IS NULL OR attrs::json->>'device' IS NULL)::int AS unknown,
       (1000 * count(*) FILTER (WHERE attrs::json->>'device' IN ('ios', 'android'))) / count(*) AS mobile_permille
FROM retail.orders
GROUP BY channel;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_device_mix AS
SELECT channel,
       CAST(count_if(json_extract_scalar(attrs, '$.device') = 'ios') AS INTEGER) AS ios,
       CAST(count_if(json_extract_scalar(attrs, '$.device') = 'android') AS INTEGER) AS android,
       CAST(count_if(json_extract_scalar(attrs, '$.device') = 'desktop') AS INTEGER) AS desktop,
       CAST(count_if(attrs IS NULL OR json_extract_scalar(attrs, '$.device') IS NULL) AS INTEGER) AS unknown,
       (1000 * count_if(json_extract_scalar(attrs, '$.device') IN ('ios', 'android'))) / count(*) AS mobile_permille
FROM iceberg.retail.orders
GROUP BY channel;
""",
    },
    {
        "name": "price_bands",
        "features": ["case_bands", "json_number", "aggregate_filter", "boolean_filter"],
        "outputs": ["wrk.ho_price_bands"],
        "gp": """CREATE TABLE wrk.ho_price_bands AS
SELECT CASE WHEN p.price < 100 THEN '0-100'
            WHEN p.price < 500 THEN '100-500'
            WHEN p.price < 2000 THEN '500-2000'
            ELSE '2000+' END AS band,
       count(*)::int AS products,
       count(*) FILTER (WHERE p.is_active)::int AS active,
       min(p.price) AS min_price,
       max(p.price) AS max_price,
       round(avg((p.attrs::json->>'weight')::numeric), 2) AS avg_weight
FROM retail.products p
WHERE p.price IS NOT NULL
GROUP BY 1;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_price_bands AS
SELECT CASE WHEN p.price < 100 THEN '0-100'
            WHEN p.price < 500 THEN '100-500'
            WHEN p.price < 2000 THEN '500-2000'
            ELSE '2000+' END AS band,
       CAST(count(*) AS INTEGER) AS products,
       CAST(count_if(p.is_active) AS INTEGER) AS active,
       min(p.price) AS min_price,
       max(p.price) AS max_price,
       round(avg(CAST(json_extract_scalar(p.attrs, '$.weight') AS DECIMAL(10,2))), 2) AS avg_weight
FROM iceberg.retail.products p
WHERE p.price IS NOT NULL
GROUP BY 1;
""",
    },
    {
        "name": "clean_orders",
        "features": ["delete_using", "multi_output", "gp_distribution"],
        "outputs": ["wrk.ho_clean_orders", "wrk.ho_store_clean"],
        "gp": """CREATE TABLE wrk.ho_clean_orders AS
SELECT order_id, customer_id, store_id, order_ts, status
FROM retail.orders
DISTRIBUTED BY (order_id);

DELETE FROM wrk.ho_clean_orders c
USING retail.payments p
WHERE p.order_id = c.order_id AND p.method = 'cash' AND p.amount < 100;

DELETE FROM wrk.ho_clean_orders WHERE status IN ('cancelled', 'returned') OR status IS NULL;

ANALYZE wrk.ho_clean_orders;

CREATE TABLE wrk.ho_store_clean AS
SELECT store_id, count(*)::int AS orders, count(DISTINCT customer_id)::int AS customers
FROM wrk.ho_clean_orders
GROUP BY store_id
DISTRIBUTED BY (store_id);
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_clean_orders AS
SELECT order_id, customer_id, store_id, order_ts, status
FROM iceberg.retail.orders;

DELETE FROM iceberg.wrk.ho_clean_orders
WHERE order_id IN (SELECT p.order_id FROM iceberg.retail.payments p WHERE p.method = 'cash' AND p.amount < 100);

DELETE FROM iceberg.wrk.ho_clean_orders WHERE status IN ('cancelled', 'returned') OR status IS NULL;

CREATE TABLE iceberg.wrk.ho_store_clean AS
SELECT store_id, CAST(count(*) AS INTEGER) AS orders, CAST(count(DISTINCT customer_id) AS INTEGER) AS customers
FROM iceberg.wrk.ho_clean_orders
GROUP BY store_id;
""",
    },
    {
        "name": "contact_quality",
        "features": ["split_part", "regex_match", "aggregate_filter", "cyrillic_length"],
        "outputs": ["wrk.ho_contact_quality"],
        "gp": """CREATE TABLE wrk.ho_contact_quality AS
SELECT lower(split_part(email, '@', 2)) AS email_domain,
       count(*)::int AS customers,
       count(*) FILTER (WHERE loyalty_code ~ '^[0-9]+$')::int AS numeric_codes,
       count(*) FILTER (WHERE loyalty_code IS NULL OR loyalty_code IN ('', 'N/A'))::int AS missing_codes,
       max(length(full_name)) AS longest_name
FROM retail.customers
GROUP BY 1;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_contact_quality AS
SELECT lower(split_part(email, '@', 2)) AS email_domain,
       CAST(count(*) AS INTEGER) AS customers,
       CAST(count_if(regexp_like(loyalty_code, '^[0-9]+$')) AS INTEGER) AS numeric_codes,
       CAST(count_if(loyalty_code IS NULL OR loyalty_code IN ('', 'N/A')) AS INTEGER) AS missing_codes,
       max(length(full_name)) AS longest_name
FROM iceberg.retail.customers
GROUP BY 1;
""",
    },
    {
        "name": "latest_stock",
        "features": ["rank_desc_nulls", "temp_table", "aggregate_filter", "empty_source"],
        "outputs": ["wrk.ho_stock_by_category"],
        "gp": """CREATE TEMP TABLE latest AS
SELECT store_id, product_id, on_hand
FROM (SELECT i.store_id, i.product_id, i.on_hand,
             rank() OVER (PARTITION BY i.store_id, i.product_id ORDER BY i.snapshot_date DESC) AS rk
      FROM retail.inventory i) x
WHERE rk = 1;

CREATE TABLE wrk.ho_stock_by_category AS
SELECT p.category,
       count(*)::int AS positions,
       sum(l.on_hand)::bigint AS units,
       count(*) FILTER (WHERE l.on_hand = 0)::int AS empty_positions
FROM latest l
JOIN retail.products p ON p.product_id = l.product_id
GROUP BY p.category;

DROP TABLE latest;
""",
        "trino": """CREATE TABLE iceberg.sandbox.latest AS
SELECT store_id, product_id, on_hand
FROM (SELECT i.store_id, i.product_id, i.on_hand,
             rank() OVER (PARTITION BY i.store_id, i.product_id ORDER BY i.snapshot_date DESC NULLS FIRST) AS rk
      FROM iceberg.retail.inventory i) x
WHERE rk = 1;

CREATE TABLE iceberg.wrk.ho_stock_by_category AS
SELECT p.category,
       CAST(count(*) AS INTEGER) AS positions,
       CAST(sum(l.on_hand) AS BIGINT) AS units,
       CAST(count_if(l.on_hand = 0) AS INTEGER) AS empty_positions
FROM iceberg.sandbox.latest l
JOIN iceberg.retail.products p ON p.product_id = l.product_id
GROUP BY p.category;

DROP TABLE iceberg.sandbox.latest;
""",
    },
    {
        "name": "payment_lag",
        "features": ["epoch_diff", "aggregate_filter", "floor_division"],
        "outputs": ["wrk.ho_payment_lag"],
        "gp": """CREATE TABLE wrk.ho_payment_lag AS
SELECT p.method,
       count(*)::int AS payments,
       count(*) FILTER (WHERE extract(epoch FROM (p.paid_ts - o.order_ts)) <= 3600)::int AS within_hour,
       max(floor(extract(epoch FROM (p.paid_ts - o.order_ts)) / 60))::bigint AS max_lag_minutes
FROM retail.payments p
JOIN retail.orders o ON o.order_id = p.order_id
WHERE p.paid_ts IS NOT NULL AND o.order_ts IS NOT NULL
GROUP BY p.method;
""",
        "trino": """CREATE TABLE iceberg.wrk.ho_payment_lag AS
SELECT p.method,
       CAST(count(*) AS INTEGER) AS payments,
       CAST(count_if(date_diff('second', o.order_ts, p.paid_ts) <= 3600) AS INTEGER) AS within_hour,
       CAST(max(floor(date_diff('second', o.order_ts, p.paid_ts) / 60e0)) AS BIGINT) AS max_lag_minutes
FROM iceberg.retail.payments p
JOIN iceberg.retail.orders o ON o.order_id = p.order_id
WHERE p.paid_ts IS NOT NULL AND o.order_ts IS NOT NULL
GROUP BY p.method;
""",
    },
]


def holdout_instances() -> list[Instance]:
    instances = []
    for item in HOLDOUT:
        for direction, (src, dst) in (("gp2trino", ("gp", "trino")), ("trino2gp", ("trino", "gp"))):
            instances.append(Instance(
                id=f"ho-{direction}-{item['name']}", split="holdout", direction=direction,
                family=f"holdout-{item['name']}", template=f"holdout:{item['name']}", variant=0,
                params={}, style={}, source_dialect=src, target_dialect=dst,
                source_script=item[src], oracle_script=item[dst], outputs=list(item["outputs"]),
                features=list(item["features"]),
            ))
    return instances
