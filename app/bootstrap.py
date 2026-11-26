"""One-time setup, run with the ADMIN connection only: schema, seed data, locked-down roles, knowledge index.

    python -m app.bootstrap            # idempotent; add --reseed to rebuild the sample data

Two runtime roles are created, neither can write anything:
  askbase_ro  SELECT on the 8 allow-listed tables only. Runs the generated SQL. Read-only by default,
              5 s statement timeout, no access to schema `private` or `askbase_meta`.
  askbase_kb  SELECT on askbase_meta (schema docs, glossary, examples) only. Used for retrieval.
"""
from __future__ import annotations

import sys

import psycopg
from psycopg import sql

from .config import Settings
from .retrieval import index_knowledge
from .sampledb import ALLOWED_TABLES, DDL, INSERTS, SEQUENCED, build_rows


def _seed(conn: psycopg.Connection) -> dict[str, int]:
    rows = build_rows()
    order = ["categories", "products", "customers", "orders", "order_items", "payments", "shipments", "reviews",
             "api_keys", "staff"]
    for table in order:
        with conn.cursor() as cur:
            cur.executemany(INSERTS[table], rows[table])
    for table in SEQUENCED:
        conn.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({}, 'id'), (SELECT MAX(id) FROM {}))").format(
            sql.Literal(f"public.{table}"), sql.Identifier(table)))
    return {t: len(rows[t]) for t in order}


def _roles(conn: psycopg.Connection, password: str, timeout_ms: int, db: str) -> None:
    for role in ("askbase_ro", "askbase_kb"):
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(f"{verb} ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                             "CONNECTION LIMIT 12 PASSWORD {}").format(sql.Literal(password)))
        conn.execute(sql.SQL(f"REVOKE ALL ON DATABASE {{}} FROM {role}").format(sql.Identifier(db)))
        conn.execute(sql.SQL(f"GRANT CONNECT ON DATABASE {{}} TO {role}").format(sql.Identifier(db)))
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM askbase_ro, askbase_kb")
    conn.execute("GRANT USAGE ON SCHEMA public TO askbase_ro")
    for table in ALLOWED_TABLES:
        conn.execute(sql.SQL("GRANT SELECT ON {} TO askbase_ro").format(sql.Identifier(table)))
    conn.execute("REVOKE ALL ON SCHEMA private FROM PUBLIC, askbase_ro, askbase_kb")
    conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA private FROM PUBLIC, askbase_ro, askbase_kb")
    conn.execute("GRANT USAGE ON SCHEMA askbase_meta TO askbase_kb")
    conn.execute("GRANT SELECT ON askbase_meta.docs TO askbase_kb")
    conn.execute("REVOKE ALL ON SCHEMA askbase_meta FROM askbase_ro")
    for role, extra in (("askbase_ro", ""), ("askbase_kb", "")):
        conn.execute(sql.SQL(f"ALTER ROLE {role} SET default_transaction_read_only = on"))
        conn.execute(sql.SQL(f"ALTER ROLE {role} SET statement_timeout = {{}}").format(sql.Literal(f"{timeout_ms}ms")))
        conn.execute(sql.SQL(f"ALTER ROLE {role} SET lock_timeout = '1s'"))
        conn.execute(sql.SQL(f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '10s'"))
    conn.execute("ALTER ROLE askbase_ro SET search_path = public")
    conn.execute("ALTER ROLE askbase_kb SET search_path = askbase_meta")
    # Defence in depth: the obvious "make the database hang" functions are not callable by these roles.
    for fn in ("pg_sleep(double precision)", "pg_sleep_for(interval)", "pg_sleep_until(timestamp with time zone)"):
        conn.execute(sql.SQL(f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC"))


def bootstrap(settings: Settings | None = None, reseed: bool = False, verbose: bool = True) -> dict:
    s = settings or Settings()
    if not s.admin_url or not s.ro_password:
        raise SystemExit("ASKBASE_ADMIN_URL and ASKBASE_RO_PASSWORD must be set")
    report: dict = {}
    with psycopg.connect(s.admin_url, autocommit=True) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]
        exists = conn.execute("SELECT to_regclass('public.orders')").fetchone()[0] is not None
        if exists and reseed:
            conn.execute("DROP SCHEMA public CASCADE; DROP SCHEMA IF EXISTS private CASCADE; CREATE SCHEMA public")
            exists = False
        if not exists:
            with conn.transaction():
                conn.execute(DDL)
                report["seeded"] = _seed(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS askbase_meta")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA askbase_meta")  # the kb role cannot see `public`
        conn.execute("CREATE TABLE IF NOT EXISTS askbase_meta.docs (id SERIAL PRIMARY KEY, kind TEXT NOT NULL, "
                     "ref TEXT NOT NULL, content TEXT NOT NULL, embedding askbase_meta.vector(256) NOT NULL)")
        _roles(conn, s.ro_password, s.statement_timeout_ms, db)
        report["indexed_docs"] = index_knowledge(conn)
    if verbose:
        print("bootstrap ok:", report)
    return report


if __name__ == "__main__":
    bootstrap(reseed="--reseed" in sys.argv)
