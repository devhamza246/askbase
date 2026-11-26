"""The sample database and the read-only role, against a real Postgres."""
import dataclasses

import psycopg
import pytest

from app.executor import ExecutionError, execute
from app.sampledb import ALLOWED_TABLES, build_rows


def test_seed_is_deterministic_and_sized():
    a, b = build_rows(), build_rows()
    assert a == b
    assert len(a["customers"]) == 400 and len(a["orders"]) == 2500 and len(a["products"]) == 60
    assert len(a["order_items"]) > 5000 and len(a["reviews"]) > 500


def test_bootstrap_created_everything(settings):
    with psycopg.connect(settings.admin_url) as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ALLOWED_TABLES}
        assert counts["orders"] == 2500 and counts["categories"] == 16
        assert conn.execute("SELECT COUNT(*) FROM private.staff").fetchone()[0] == 2
        # referential integrity: every order item points at a real order and product
        assert conn.execute("SELECT COUNT(*) FROM order_items oi LEFT JOIN orders o ON o.id = oi.order_id WHERE o.id IS NULL").fetchone()[0] == 0
        # order totals are consistent with their lines
        bad = conn.execute("SELECT COUNT(*) FROM orders o JOIN (SELECT order_id, SUM(quantity*unit_price) s FROM order_items GROUP BY order_id) i "
                           "ON i.order_id = o.id WHERE ABS(o.total_amount - (i.s - o.discount_amount + o.shipping_fee)) > 0.02").fetchone()[0]
        assert bad == 0


def test_bootstrap_is_idempotent(settings):
    from app.bootstrap import bootstrap
    report = bootstrap(settings, verbose=False)
    assert "seeded" not in report and report["indexed_docs"] > 20


def test_executor_runs_a_select(settings):
    r = execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC", settings)
    assert r.columns == ["status", "n"] and r.row_count == 5 and r.types == ["text", "number"]
    assert sum(row[1] for row in r.rows) == 2500 and not r.truncated


def test_executor_serialises_dates_and_decimals(settings):
    r = execute("SELECT order_date, total_amount, delivered_at - shipped_at AS days FROM orders o JOIN shipments s ON s.order_id = o.id "
                "WHERE delivered_at IS NOT NULL LIMIT 3", settings)
    assert isinstance(r.rows[0][0], str) and isinstance(r.rows[0][1], float) and isinstance(r.rows[0][2], (int, float))
    assert r.types[0] == "date"


def test_row_cap_truncates(settings):
    r = execute("SELECT id FROM order_items", settings, max_rows=50)
    assert r.row_count == 50 and r.truncated


def test_statement_timeout_kills_runaway_queries(settings):
    with pytest.raises(ExecutionError, match="cancelled"):
        execute("SELECT COUNT(*) FROM orders a, orders b, orders c, orders d", settings)


def _as_ro(settings):
    return psycopg.connect(settings.ro_url, autocommit=True)


@pytest.mark.parametrize("statement", [
    "INSERT INTO customers (full_name, email, country, city, segment, signup_date, marketing_opt_in) VALUES ('x','x@x','a','b','consumer','2026-01-01',true)",
    "UPDATE products SET price = 0",
    "DELETE FROM orders",
    "TRUNCATE orders",
    "DROP TABLE orders",
    "ALTER TABLE orders ADD COLUMN x int",
    "CREATE TABLE evil (x int)",
    "CREATE SCHEMA evil",
    "GRANT ALL ON orders TO PUBLIC",
    "SELECT * FROM private.staff",
    "SELECT * FROM private.api_keys",
    "SELECT * FROM askbase_meta.docs",
    "SELECT rolpassword FROM pg_authid",
    "SELECT pg_sleep(1)",
    "SELECT pg_read_file('/etc/passwd')",
    "COPY orders TO PROGRAM 'id'",
    "SET ROLE postgres",
    "CREATE ROLE evil",
])
def test_the_database_role_itself_refuses_everything_dangerous(settings, statement):
    """Defence in depth: bypass the guard entirely and talk to Postgres as the runtime role."""
    with _as_ro(settings) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(statement)


def test_role_cannot_turn_off_read_only_and_write(settings):
    with _as_ro(settings) as conn:
        conn.execute("SET default_transaction_read_only = off")  # a user may flip the setting...
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # ...but still has no write privilege at all
            conn.execute("UPDATE products SET price = 0")


def test_role_can_only_read_the_allowlisted_tables(settings):
    with _as_ro(settings) as conn:
        for t in ALLOWED_TABLES:
            conn.execute(f"SELECT 1 FROM {t} LIMIT 1")


def test_data_survived_all_of_that(settings):
    with psycopg.connect(settings.admin_url) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2500
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 400
        assert conn.execute("SELECT COUNT(*) FROM private.staff").fetchone()[0] == 2


def test_kb_role_sees_only_the_knowledge_schema(kb_url):
    with psycopg.connect(kb_url, autocommit=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM askbase_meta.docs").fetchone()[0] > 20
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.orders")
        with pytest.raises(psycopg.Error):
            conn.execute("INSERT INTO askbase_meta.docs (kind, ref, content, embedding) VALUES ('x','x','x', array_fill(0, ARRAY[256])::vector)")
