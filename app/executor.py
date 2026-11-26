"""Run validated SQL as the read-only role. Defence in depth: even if the guard had a hole,
the role has SELECT on 8 tables only, the transaction is READ ONLY, and a statement timeout kills slow queries."""
from __future__ import annotations

import datetime as dt
import decimal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .config import Settings


class ExecutionError(Exception):
    """A database-side failure with a message safe to show to the user and to feed to the repair step."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    types: list[str] = field(default_factory=list)  # "number" | "text" | "date" | "bool" per column


def to_json(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds() / 86400.0  # days, the unit a business question expects
    if isinstance(value, (uuid.UUID, bytes, memoryview)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    return value


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, decimal.Decimal, dt.timedelta)):
        return "number"
    if isinstance(value, (dt.date, dt.datetime)):
        return "date"
    return "text"


def execute(sql: str, settings: Settings, max_rows: int | None = None) -> QueryResult:
    cap = max_rows or settings.max_rows
    start = time.perf_counter()
    try:
        with psycopg.connect(settings.ro_url, autocommit=False, connect_timeout=5) as conn:
            conn.read_only = True  # BEGIN READ ONLY
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                raw = cur.fetchmany(cap + 1)
            conn.rollback()
    except psycopg.errors.QueryCanceled:
        raise ExecutionError(f"the query ran longer than {settings.statement_timeout_ms / 1000:.0f}s and was cancelled") from None
    except psycopg.errors.InsufficientPrivilege as exc:
        raise ExecutionError(f"permission denied: {str(exc).splitlines()[0]}") from None
    except psycopg.errors.ReadOnlySqlTransaction:
        raise ExecutionError("the database is read-only: write statements are not permitted") from None
    except psycopg.Error as exc:
        raise ExecutionError(str(exc).strip().splitlines()[0][:300]) from None
    truncated = len(raw) >= cap  # the row cap was reached: there may be more rows than shown
    raw = raw[:cap]
    rows = [[to_json(v) for v in r] for r in raw]
    kinds = []
    for i in range(len(columns)):
        sample = next((r[i] for r in raw if r[i] is not None), None)
        kinds.append(_kind(sample))
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
                       elapsed_ms=(time.perf_counter() - start) * 1000, types=kinds)
