"""Run the 40-question benchmark through the real pipeline and score execution accuracy.

    python -m benchmark.run --mode auto            # live calls on a cassette miss, replay on a hit
    python -m benchmark.run --mode replay          # free and deterministic; needs benchmark/cassette.jsonl
    python -m benchmark.run --gauge-out results/gauge.json   # also write a file `gauge import` understands

Needs ASKBASE_ADMIN_URL / ASKBASE_RO_URL / ASKBASE_RO_PASSWORD (a bootstrapped database) and, for live calls,
ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.agent import Askbase
from app.api import _kb_url
from app.config import Settings
from app.executor import ExecutionError, execute
from app.guard import validate
from app.llm import AnthropicLLM
from app.retrieval import Retriever

from .scoring import load_questions, results_match

HERE = Path(__file__).parent


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10, cwd=HERE.parent)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def gold_result(question: dict, settings: Settings):
    g = validate(question["sql"], settings.max_rows)
    if not g.ok:
        raise SystemExit(f"gold SQL for {question['id']} is rejected by the guard: {g.message}")
    return execute(g.sql, settings, max_rows=settings.max_rows)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.4999) - 1))]


def run(settings: Settings, limit: int | None = None, only: list[str] | None = None) -> dict:
    llm = AnthropicLLM(settings) if settings.llm_configured else None
    if llm is None:
        raise SystemExit("no LLM available: set ANTHROPIC_API_KEY, or use --mode replay with a cassette")
    agent = Askbase(settings, llm, Retriever(_kb_url(settings)))
    questions = load_questions()
    if only:
        questions = [q for q in questions if q["id"] in only]
    if limit:
        questions = questions[:limit]
    rows = []
    for q in questions:
        gold = gold_result(q, settings)
        ans = agent.ask(q["question"])
        correct = ans.status == "ok" and ans.executed_sql is not None and results_match(ans.rows, gold.rows, bool(q.get("ordered")))
        rows.append({
            "id": q["id"], "difficulty": q["difficulty"], "question": q["question"], "status": ans.status,
            "correct": correct, "repaired": ans.repaired, "predicted_sql": ans.sql, "executed_sql": ans.executed_sql,
            "gold_sql": " ".join(q["sql"].split()), "reason": ans.reason, "latency_ms": ans.usage.get("total_ms", 0),
            "cost_usd": ans.usage.get("cost_usd", 0), "input_tokens": ans.usage.get("input_tokens", 0),
            "output_tokens": ans.usage.get("output_tokens", 0), "pred_rows": ans.rows[:5], "gold_rows": gold.rows[:5]})
        print(f"{'PASS' if correct else 'FAIL'} {q['id']} [{q['difficulty']}] {q['question'][:70]}"
              + ("" if correct else f"  -> {ans.status} {ans.reason[:80]}"), flush=True)
    by_diff = {}
    for d in ("easy", "medium", "hard"):
        sub = [r for r in rows if r["difficulty"] == d]
        if sub:
            by_diff[d] = {"correct": sum(r["correct"] for r in sub), "total": len(sub)}
    lat = [r["latency_ms"] for r in rows if r["latency_ms"] > 0]
    summary = {
        "total": len(rows), "correct": sum(r["correct"] for r in rows),
        "execution_accuracy": (sum(r["correct"] for r in rows) / len(rows)) if rows else 0.0, "by_difficulty": by_diff,
        "repaired": sum(r["repaired"] for r in rows), "latency_p50_ms": percentile(lat, 50) if lat else 0,
        "latency_p95_ms": percentile(lat, 95) if lat else 0, "latency_mean_ms": statistics.mean(lat) if lat else 0,
        "under_8s": sum(1 for x in lat if x < 8000), "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
        "models": {"sql": settings.sql_model, "fast": settings.fast_model},
    }
    return {"summary": summary, "rows": rows, "commit": _git("rev-parse", "HEAD"),
            "timestamp": datetime.now(timezone.utc).isoformat()}


def to_gauge(result: dict, suite: str = "askbase-benchmark") -> dict:
    """Generic `gauge import` format: one case per benchmark question."""
    cases = [{"id": r["id"], "tags": [r["difficulty"]], "input": r["question"], "expected": r["gold_sql"],
              "output": r["executed_sql"] or r["reason"], "passed": r["correct"],
              "reason": "result set matches gold" if r["correct"] else (r["reason"] or "result set differs from gold"),
              "latency_ms": r["latency_ms"], "cost_usd": r["cost_usd"], "input_tokens": r["input_tokens"],
              "output_tokens": r["output_tokens"], "scorer": "execution_accuracy"} for r in result["rows"]]
    return {"suite": suite, "commit": result["commit"], "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "main",
            "message": _git("log", "-1", "--pretty=%s"), "started_at": result["timestamp"], "source": "askbase", "cases": cases}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "live", "replay"], default="auto")
    ap.add_argument("--cassette", default=str(HERE / "cassette.jsonl"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma separated question ids")
    ap.add_argument("--out", default=str(HERE / "results" / "latest.json"))
    ap.add_argument("--gauge-out")
    args = ap.parse_args()
    settings = dataclasses.replace(Settings(), llm_mode=args.mode, cassette=args.cassette)
    result = run(settings, args.limit, args.only.split(",") if args.only else None)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.gauge_out:
        Path(args.gauge_out).write_text(json.dumps(to_gauge(result)), encoding="utf-8")
    s = result["summary"]
    print(f"\nexecution accuracy: {s['correct']}/{s['total']} = {s['execution_accuracy']:.1%} | "
          f"p50 {s['latency_p50_ms']:.0f} ms, p95 {s['latency_p95_ms']:.0f} ms | cost ${s['cost_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
