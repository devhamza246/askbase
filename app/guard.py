"""SQL guardrails. The model's SQL is untrusted input: it is parsed with sqlglot, validated on the syntax tree,
rewritten (forced LIMIT) and then REGENERATED from the tree. Only the regenerated SQL is ever executed, so a
string that parses one way for the guard and another way for Postgres cannot slip through.

Rules, in order:
  1. exactly one statement, parseable as Postgres, bounded size
  2. a read-only query: SELECT / set operation (UNION...) / WITH ... SELECT
  3. no DML, DDL, COPY, SET, transaction control, SELECT INTO, row locks, or bind parameters anywhere in the tree
  4. only allow-listed tables of the public schema (CTE names allowed); no table functions, no system catalogs
  5. no blocklisted or unknown functions (system administration, file access, sleeping, dblink, setting access)
  6. a LIMIT is always present and never above the row cap
The validated SQL is re-parsed and re-checked after regeneration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .sampledb import ALLOWED_TABLES

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # 'falling back to Command' is expected for rejected input

MAX_SQL_CHARS = 6000
MAX_NODES = 2500

FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "AlterTable", "AlterColumn", "Command", "Copy", "Merge",
    "TruncateTable", "Truncate", "Set", "SetItem", "Use", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Into",
    "Lock", "Pragma", "Comment", "Describe", "Analyze", "Execute", "Kill", "LoadData", "Attach", "Detach", "Refresh",
    "Parameter", "Placeholder", "SessionParameter", "Declare", "DeclareItem", "Show", "Cache", "Uncache",
)
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in FORBIDDEN_NODE_NAMES if hasattr(exp, n))
SET_OPERATIONS = tuple(getattr(exp, n) for n in ("SetOperation", "Union") if hasattr(exp, n))

# Known sqlglot function classes that expose server or session information.
FORBIDDEN_KNOWN_FUNCS = {"current_user", "session_user", "user", "current_schema", "current_schemas", "current_database",
                         "current_catalog", "current_role"}
# Function-name blocklist (exact) and prefixes; applies to every function regardless of how it parses.
FORBIDDEN_FUNC_NAMES = {"set_config", "current_setting", "version", "txid_current", "inet_server_addr", "inet_client_addr",
                        "inet_server_port", "inet_client_port", "lastval", "nextval", "setval", "currval", "query_to_xml",
                        "table_to_xml", "database_to_xml", "xpath", "lo_import", "lo_export", "lo_open", "lo_unlink",
                        "dblink", "dblink_exec", "dblink_connect", "copy", "generate_series", "unnest_all", "has_table_privilege",
                        "has_schema_privilege", "has_database_privilege", "obj_description", "col_description", "to_regclass"}
FORBIDDEN_FUNC_PREFIXES = ("pg_", "lo_", "dblink", "ts_debug", "txid_", "xmlforest", "brin_", "gin_", "bt_", "hash_")
# Functions sqlglot does not model (they parse as Anonymous) that are safe and commonly generated.
ANON_ALLOW = {"date_part", "age", "to_char", "to_date", "to_number", "greatest", "least", "initcap", "split_part",
              "string_agg", "array_agg", "bool_and", "bool_or", "every", "mode", "percentile_cont", "percentile_disc",
              "regexp_replace", "regexp_match", "left", "right", "lpad", "rpad", "trim", "ltrim", "rtrim", "replace",
              "translate", "div", "mod", "power", "sqrt", "ln", "log", "exp", "sign", "trunc",
              "date_bin", "make_date", "isfinite", "justify_days", "ntile", "cume_dist", "percent_rank", "first_value",
              "last_value", "nth_value", "width_bucket", "corr", "covar_pop", "covar_samp", "stddev", "stddev_pop",
              "stddev_samp", "variance", "var_pop", "var_samp", "regr_slope", "concat_ws",
              "starts_with", "strpos", "position", "reverse", "repeat", "abs", "ceil", "ceiling", "floor", "round",
              "extract", "date_trunc", "coalesce", "nullif", "lag", "lead", "rank", "dense_rank", "row_number"}
_BLOCK_NAMES = {n.replace("_", "") for n in FORBIDDEN_FUNC_NAMES | FORBIDDEN_KNOWN_FUNCS} | FORBIDDEN_FUNC_NAMES | FORBIDDEN_KNOWN_FUNCS | {"currentversion", "current_version"}
FORBIDDEN_TYPES = {"regclass", "regproc", "regprocedure", "regoper", "regoperator", "regtype", "regconfig", "regdictionary",
                   "regnamespace", "regrole", "xml"}


@dataclass
class GuardResult:
    ok: bool
    sql: str | None = None                 # the validated, regenerated SQL (only when ok)
    reasons: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    limit_applied: bool = False

    @property
    def message(self) -> str:
        return "; ".join(self.reasons)


def _reject(code: str, reason: str) -> GuardResult:
    return GuardResult(ok=False, reasons=[reason], codes=[code])


def _func_names(node: exp.Func) -> set[str]:
    """Every name a function can go by: how it was written, sqlglot's class name and key, without underscores."""
    names = {str(node.name).lower()} if isinstance(node, exp.Anonymous) else {
        str(node.sql_name()).lower(), type(node).__name__.lower(), str(node.key).lower()}
    return {n for n in names if n} | {n.replace("_", "") for n in names if n}


def _check(tree: exp.Expression, allowed: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (codes, reasons, tables) for a parsed tree."""
    codes: list[str] = []
    reasons: list[str] = []

    def bad(code: str, reason: str) -> None:
        if reason not in reasons:
            codes.append(code)
            reasons.append(reason)

    nodes = list(tree.walk())
    if len(nodes) > MAX_NODES:
        bad("too_complex", f"query is too large or complex ({len(nodes)} nodes; limit {MAX_NODES})")
        return codes, reasons, []
    root = tree
    while isinstance(root, (exp.Subquery, exp.Paren)):
        root = root.this
    if not isinstance(root, (exp.Select, *SET_OPERATIONS)):
        bad("not_select", f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")
    for node in nodes:
        if isinstance(node, FORBIDDEN_NODES):
            bad("forbidden_statement", f"{type(node).__name__.upper()} is not allowed: Askbase is read-only")
    cte_names = {str(c.alias).lower() for c in tree.find_all(exp.CTE)}
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            bad("table_function", "table functions and non-table sources are not allowed")
            continue
        name = table.name if table.this.quoted else table.name.lower()
        schema = table.db if (table.args.get("db") is not None and table.args["db"].quoted) else table.db.lower()
        if table.catalog:
            bad("forbidden_table", "cross-database references are not allowed")
        elif schema and schema != "public":
            bad("forbidden_table", f"schema '{table.db}' is not accessible")
        elif not schema and name.lower() in cte_names and not table.this.quoted:
            continue
        elif name not in allowed:
            bad("forbidden_table", f"table '{table.name}' is not available (allowed: {', '.join(sorted(allowed))})")
        else:
            tables.append(name)
    for fn in tree.find_all(exp.Func):
        names = _func_names(fn)
        shown = str(fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if (names & _BLOCK_NAMES or any(n.startswith(p) for n in names for p in FORBIDDEN_FUNC_PREFIXES)
                or any("generateseries" in n for n in names)):
            bad("forbidden_function", f"function '{shown}' is not allowed")
        elif isinstance(fn, exp.Anonymous) and shown not in ANON_ALLOW:
            bad("forbidden_function", f"function '{shown}' is not on the allowed list")
    for node in nodes:  # set-returning and other function-like nodes that are not exp.Func subclasses
        if not isinstance(node, exp.Func) and (type(node).__name__.lower() in _BLOCK_NAMES
                                               or "generateseries" in type(node).__name__.lower()):
            bad("forbidden_function", f"'{type(node).__name__.lower()}' is not allowed")
    for dtype in tree.find_all(exp.DataType):
        if dtype.sql("postgres").lower().split("(")[0] in FORBIDDEN_TYPES or str(dtype.args.get("kind", "")).lower() in FORBIDDEN_TYPES:
            bad("forbidden_type", "casting to system object types is not allowed")
    return codes, reasons, sorted(set(tables))


def _parse_one(sql: str) -> tuple[exp.Expression | None, GuardResult | None]:
    if not sql or not sql.strip():
        return None, _reject("empty", "no SQL was produced")
    if len(sql) > MAX_SQL_CHARS:
        return None, _reject("too_long", f"SQL is longer than {MAX_SQL_CHARS} characters")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as exc:
        return None, _reject("parse_error", f"the SQL could not be parsed as PostgreSQL: {str(exc).splitlines()[0][:160]}")
    except RecursionError:
        return None, _reject("too_complex", "the SQL is too deeply nested")
    if len(statements) != 1:
        return None, _reject("not_single_statement", f"exactly one statement is allowed (found {len(statements)})")
    return statements[0], None


def _apply_limit(tree: exp.Expression, max_rows: int) -> tuple[exp.Expression, bool]:
    root = tree
    while isinstance(root, exp.Subquery) and not root.args.get("limit"):
        root = root.this
    if isinstance(root, exp.Select):
        limit = root.args.get("limit")
        fetch = root.args.get("fetch")
        if fetch is not None:
            root = root.copy()
            root.set("fetch", None)
            limit = None
        value = None
        if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
            value = int(limit.expression.this)
        if value is not None and 0 <= value <= max_rows:
            return root, False
        return root.limit(max_rows, copy=True), True
    # UNION / INTERSECT / EXCEPT: wrap so the cap applies to the whole result
    wrapped = exp.select("*").from_(root.subquery("_askbase_q")).limit(max_rows)
    return wrapped, True


def validate(sql: str, max_rows: int = 500, allowed_tables: tuple[str, ...] = ALLOWED_TABLES) -> GuardResult:
    allowed = set(allowed_tables)
    tree, failure = _parse_one(sql)
    if failure:
        return failure
    codes, reasons, tables = _check(tree, allowed)
    if reasons:
        return GuardResult(ok=False, reasons=reasons, codes=codes, tables=tables)
    try:
        limited, applied = _apply_limit(tree, max_rows)
        out = limited.sql(dialect="postgres")
    except SqlglotError as exc:
        return _reject("rewrite_error", f"could not rewrite the query safely: {str(exc)[:120]}")
    # Re-validate what will actually run.
    again, failure = _parse_one(out)
    if failure:
        return failure
    codes2, reasons2, tables2 = _check(again, allowed)
    if reasons2:
        return GuardResult(ok=False, reasons=reasons2, codes=codes2, tables=tables2)
    return GuardResult(ok=True, sql=out, tables=tables2, limit_applied=applied)
