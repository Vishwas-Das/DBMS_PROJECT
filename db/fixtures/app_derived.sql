-- Derived artifacts of the demo `app` schema (docs/database_schema.md §4). FIXTURE.
-- Run AFTER the base tables are loaded (the generator does this), so WITH DATA is meaningful.
-- The SELECT of app.customer_segment must stay byte-identical to SEGMENT_DEFINITION_SQL in
-- db/fixtures/generator.py (registered as its RECOMPUTE_SQL definition; checked by a test).

SET client_min_messages = warning;

DROP TABLE IF EXISTS app.customer_segment CASCADE;
DROP MATERIALIZED VIEW IF EXISTS app.daily_revenue CASCADE;
DROP MATERIALIZED VIEW IF EXISTS app.customer_ltv CASCADE;

-- KEY_PRESERVING matview: customer_id survives GROUP BY; total_spent is AGGREGATE of payments
CREATE MATERIALIZED VIEW app.customer_ltv AS
SELECT o.customer_id,
       count(DISTINCT o.id) AS order_count,
       sum(p.amount_cents)::bigint AS total_spent
FROM app.purchase_order o
JOIN app.payment p ON p.order_id = o.id
WHERE o.customer_id IS NOT NULL
GROUP BY o.customer_id
WITH DATA;
CREATE UNIQUE INDEX ux_customer_ltv_customer_id ON app.customer_ltv (customer_id);

-- AGGREGATE matview: no subject key survives (REFRESH_MATVIEW)
CREATE MATERIALIZED VIEW app.daily_revenue AS
SELECT (p.paid_at AT TIME ZONE 'UTC')::date AS day,
       sum(p.amount_cents)::bigint AS revenue_cents,
       count(*) AS payment_count
FROM app.payment p
GROUP BY (p.paid_at AT TIME ZONE 'UTC')::date
WITH DATA;
CREATE UNIQUE INDEX ux_daily_revenue_day ON app.daily_revenue (day);

-- artifact-on-artifact: derived table (CTAS) computed from the customer_ltv matview
CREATE TABLE app.customer_segment AS
SELECT l.customer_id, CASE WHEN l.total_spent >= 100000 THEN 'GOLD' WHEN l.total_spent >= 20000 THEN 'SILVER' ELSE 'BRONZE' END AS segment FROM app.customer_ltv l;
ALTER TABLE app.customer_segment ADD PRIMARY KEY (customer_id);
ALTER TABLE app.customer_segment ALTER COLUMN segment SET NOT NULL;

-- read-only discovery / verification role (created by db/init); skipped if it does not exist
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lae_reader') THEN
        GRANT USAGE ON SCHEMA app TO lae_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA app TO lae_reader;
    END IF;
END
$$;
