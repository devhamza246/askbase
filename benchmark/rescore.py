"""Re-score a saved benchmark run WITHOUT calling the model: re-execute each executed SQL against the database and compare
with the gold result under the strict metric (headline) and the lenient one (extra columns forgiven).

    python -m benchmark.rescore benchmark/run1-results.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.executor import ExecutionError, execute

from .run import gold_result
from .scoring import load_questions, results_match, results_match_lenient


def main(path: str, questions: str | None = None) -> int:
    settings = Settings()
    run = json.loads(Path(path).read_text(encoding="utf-8"))
    gold_by_id = {q["id"]: q for q in load_questions(questions)}
    strict = lenient = answered = 0
    by_diff: dict[str, list[int]] = {}
    for r in run["rows"]:
        q = gold_by_id[r["id"]]
        d = by_diff.setdefault(r["difficulty"], [0, 0, 0])
        d[2] += 1
        if r["status"] != "ok" or not r["executed_sql"]:
            print(f"{r['id']} {r['difficulty']:<6} not answered ({r['status']})")
            continue
        answered += 1
        try:
            pred = execute(r["executed_sql"], settings)
        except ExecutionError as exc:
            print(f"{r['id']} error re-executing: {exc}")
            continue
        gold = gold_result(q, settings)
        ordered = bool(q.get("ordered"))
        s = results_match(pred.rows, gold.rows, ordered)
        l = results_match_lenient(pred.rows, gold.rows, ordered)
        strict += s
        lenient += l
        d[0] += s
        d[1] += l
        if not s:
            print(f"{r['id']} {r['difficulty']:<6} strict=FAIL lenient={'PASS' if l else 'FAIL'}  rows pred/gold = {pred.row_count}/{gold.row_count}  {r['question'][:60]}")
    n = len(run["rows"])
    print(f"\nquestions: {n}, answered: {answered}")
    print(f"strict  (headline): {strict}/{n} = {strict / n:.1%}")
    print(f"lenient (extra columns forgiven): {lenient}/{n} = {lenient / n:.1%}")
    for k, (s, l, t) in by_diff.items():
        print(f"  {k:<6} strict {s}/{t}   lenient {l}/{t}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "benchmark/run1-results.json", sys.argv[2] if len(sys.argv) > 2 else None))
