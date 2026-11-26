"""Record the public demo's suggested questions and attack prompts into the cassette, so the demo can run in recorded-answer mode.

    python -m benchmark.record_demo --cassette /tmp/cassette.jsonl        # live model on a miss (spends a few cents), replay on a hit

Each prompt is run through the full pipeline against the real database; the model responses are appended to the cassette. Afterwards
set ASKBASE_LLM_MODE=replay and the demo answers exactly these questions with no API key and no spending.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys

from app.agent import Askbase
from app.api import ATTACK_PROMPTS, EXAMPLE_QUESTIONS, _kb_url
from app.config import Settings
from app.llm import AnthropicLLM
from app.retrieval import Retriever


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cassette", default="benchmark/cassette.jsonl")
    args = ap.parse_args()
    settings = dataclasses.replace(Settings(), llm_mode="auto", cassette=args.cassette, daily_spend_cap_usd=0)
    if not settings.anthropic_key:
        print("ANTHROPIC_API_KEY is not set; only already-recorded prompts will be replayed.", file=sys.stderr)
    agent = Askbase(settings, AnthropicLLM(settings), Retriever(_kb_url(settings)))
    total_cost = 0.0
    failures = 0
    for kind, prompts in (("question", EXAMPLE_QUESTIONS), ("attack", ATTACK_PROMPTS)):
        for q in prompts:
            ans = agent.ask(q)
            total_cost += ans.usage.get("cost_usd", 0)
            ok = ans.status in (("ok",) if kind == "question" else ("blocked", "refused"))
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} [{kind}] {ans.status:<9} ${ans.usage.get('cost_usd', 0):.4f}  {q[:70]}"
                  + ("" if ok else f"  -> {ans.reason[:80]}"), flush=True)
    print(f"\nrecorded {len(EXAMPLE_QUESTIONS) + len(ATTACK_PROMPTS)} prompts; cost this run ${total_cost:.4f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
