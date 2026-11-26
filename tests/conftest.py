import os

import psycopg
import pytest

from app.bootstrap import bootstrap
from app.config import Settings

ADMIN = os.environ.get("ASKBASE_TEST_ADMIN_URL", "postgresql://postgres:testadmin@127.0.0.1:55432/postgres")
PASSWORD = "ro-test-password"


def _swap_db(url: str, db: str, user: str | None = None) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    netloc = p.netloc if user is None else f"{user}:{PASSWORD}@{p.netloc.rsplit('@', 1)[-1]}"
    return urlunsplit((p.scheme, netloc, "/" + db, p.query, p.fragment))


@pytest.fixture(scope="session")
def settings():
    """A freshly bootstrapped database (schema, deterministic seed, locked-down roles, pgvector index)."""
    try:
        with psycopg.connect(ADMIN, autocommit=True, connect_timeout=3) as conn:
            conn.execute("DROP DATABASE IF EXISTS askbase_test WITH (FORCE)")
            conn.execute("CREATE DATABASE askbase_test")
    except psycopg.OperationalError as exc:
        pytest.skip(f"no test Postgres at ASKBASE_TEST_ADMIN_URL: {exc}")
    admin = _swap_db(ADMIN, "askbase_test")
    s = Settings(admin_url=admin, ro_url=_swap_db(ADMIN, "askbase_test", "askbase_ro"), ro_password=PASSWORD,
                 anthropic_key="", max_rows=500, statement_timeout_ms=2000, rate_limit_per_min=0, daily_spend_cap_usd=0)
    bootstrap(s, verbose=False)
    return s


@pytest.fixture(scope="session")
def kb_url(settings):
    return _swap_db(ADMIN, "askbase_test", "askbase_kb")
