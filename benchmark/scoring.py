"""Execution accuracy: a prediction is correct when it returns the same result set as the gold query.

Rules (deliberately lenient about presentation, strict about content):
  * column NAMES are ignored; column ORDER is ignored (rows are compared as multisets of value-sets per row
    when the exact column order does not match)
  * row order matters only when the gold question has `ordered: true` (top-N / "most first" questions)
  * non-integer numbers match within an absolute 0.05, so a rounded 11.65 equals a gold 11.6 (a question rarely specifies rounding);
    integers must match exactly
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

QUESTIONS = Path(__file__).parent / "questions.yaml"


def load_questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def _norm_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str):
        stripped = value.strip()
        try:  # numbers serialised as text by the JSON layer
            return _norm_cell(float(stripped)) if stripped.replace(".", "", 1).replace("-", "", 1).isdigit() else stripped.lower()
        except ValueError:
            return stripped.lower()
    return str(value)


def _key(value: Any) -> tuple:
    return (value is None, str(type(value).__name__ if not isinstance(value, (int, float)) else "num"), str(value))


def _row_key(row: tuple) -> tuple:
    return tuple(_key(v) for v in row)


def _cell_equal(a, b) -> bool:
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if numeric(a) and numeric(b):
        return abs(a - b) <= (0.0 if (isinstance(a, int) and isinstance(b, int)) else 0.0501)
    return a == b


def _rows_equal(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all(_cell_equal(x, y) for x, y in zip(a, b))


def _multiset_equal(pred: list[tuple], gold: list[tuple]) -> bool:
    """Order-insensitive row matching with numeric tolerance (greedy over rows sorted by a stable key)."""
    remaining = list(pred)
    for g in gold:
        hit = next((i for i, p in enumerate(remaining) if _rows_equal(p, g)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return not remaining


def results_match(pred_rows: list[list], gold_rows: list[list], ordered: bool) -> bool:
    pred = [tuple(_norm_cell(v) for v in r) for r in pred_rows]
    gold = [tuple(_norm_cell(v) for v in r) for r in gold_rows]
    if len(pred) != len(gold):
        return False
    if pred and gold and len(pred[0]) != len(gold[0]):
        return False
    if ordered:
        return all(_rows_equal(p, g) for p, g in zip(pred, gold))
    if _multiset_equal(pred, gold):
        return True
    # same values, different column order
    key = lambda v: (v is None, type(v).__name__ if not isinstance(v, (int, float)) else "num", str(v))  # noqa: E731
    return _multiset_equal([tuple(sorted(r, key=key)) for r in pred], [tuple(sorted(r, key=key)) for r in gold])


def results_match_lenient(pred_rows: list[list], gold_rows: list[list], ordered: bool) -> bool:
    """Looser second metric: also correct if the prediction has MORE columns than the gold and some selection of them,
    in some order, equals the gold result. It forgives helpful extra context (a count next to a name) but nothing else:
    wrong rows, wrong values, truncated lists and missing columns all still fail."""
    from itertools import permutations
    if results_match(pred_rows, gold_rows, ordered):
        return True
    if not pred_rows or not gold_rows or len(pred_rows) != len(gold_rows):
        return False
    pc, gc = len(pred_rows[0]), len(gold_rows[0])
    if pc <= gc or pc > 8:
        return False
    for cols in permutations(range(pc), gc):
        if results_match([[r[i] for i in cols] for r in pred_rows], gold_rows, ordered):
            return True
    return False
