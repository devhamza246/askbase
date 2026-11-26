"""Execution accuracy: a prediction is correct when it returns the same result set as the gold query.

Rules (deliberately lenient about presentation, strict about content):
  * column NAMES are ignored; column ORDER is ignored (rows are compared as multisets of value-sets per row
    when the exact column order does not match)
  * row order matters only when the gold question has `ordered: true` (top-N / "most first" questions)
  * non-integer numbers are compared after rounding to 1 decimal, so 23.37 equals 23.4 (a question rarely specifies rounding)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

QUESTIONS = Path(__file__).parent / "questions.yaml"


def load_questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def _norm(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else round(value, 1)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str):
        stripped = value.strip()
        try:  # numbers serialised as text by the JSON layer
            return _norm(float(stripped)) if stripped.replace(".", "", 1).replace("-", "", 1).isdigit() else stripped.lower()
        except ValueError:
            return stripped.lower()
    return str(value)


def _key(value: Any) -> tuple:
    return (value is None, str(type(value).__name__ if not isinstance(value, (int, float)) else "num"), str(value))


def _row_key(row: tuple) -> tuple:
    return tuple(_key(v) for v in row)


def results_match(pred_rows: list[list], gold_rows: list[list], ordered: bool) -> bool:
    pred = [tuple(_norm(v) for v in r) for r in pred_rows]
    gold = [tuple(_norm(v) for v in r) for r in gold_rows]
    if len(pred) != len(gold):
        return False
    if pred and gold and len(pred[0]) != len(gold[0]):
        return False
    if ordered:
        return pred == gold
    if sorted(map(_row_key, pred)) == sorted(map(_row_key, gold)):
        return True
    # same values, different column order
    return sorted(_row_key(tuple(sorted(r, key=_key))) for r in pred) == sorted(_row_key(tuple(sorted(r, key=_key))) for r in gold)


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
