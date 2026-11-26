"""The question-to-answer pipeline, an explicit state machine:

    question -> [route || retrieve] -> generate SQL -> guard -> execute -> (one repair on failure) -> summarize + chart

Every decision that matters for safety is deterministic code (the guard, the read-only role). The model proposes;
it never executes anything.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import prompts
from .charts import pick_chart
from .config import Settings
from .executor import ExecutionError, QueryResult, execute as run_query
from .guard import GuardResult, validate
from .llm import LLM, AllowanceUsedUp, LLMError, LLMNotConfigured, Reply, ReplayMiss, parse_json_object
from .retrieval import Context, Retriever

# Phrases that make a question worth a routing call even with no history. They only *flag*; the router decides.
SUSPICIOUS = re.compile(
    r"\b(drop\w*|delet\w*|truncat\w*|updat\w*|insert\w*|alter\w*|grant\w*|revok\w*|creat\w* (table|role|user|schema)|"
    r"ignore (all |the |your )?(previous|prior|above|earlier)?\s?(instructions|rules)|system (prompt|override)|jailbreak|pretend|"
    r"you are now|admin mode|password\w*|api[_ ]?key\w*|secret\w*|credential\w*|hash(es)?|pg_\w+|information_schema|"
    r"sudo|rm -rf|shutdown|disable|superuser|copy .* to|set role|wipe\w*|erase\w*|remove all)\b", re.I)


@dataclass
class Step:
    name: str
    ms: float
    detail: str = ""


@dataclass
class Answer:
    status: str                       # ok | refused | blocked | error | no_llm | rate_limited
    question: str
    answer: str = ""
    sql: str | None = None            # the SQL the model wrote (always shown when there is one)
    executed_sql: str | None = None   # the guard-regenerated SQL that actually ran (None if nothing ran)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict | None = None
    assumptions: str = ""
    repaired: bool = False
    reason: str = ""                  # why it was refused/blocked/failed
    guard_codes: list[str] = field(default_factory=list)
    standalone_question: str = ""
    steps: list[Step] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def is_attack(g: GuardResult) -> bool:
    """Guard rejections that mean 'this is not something to retry': writes, other schemas, system access."""
    hard = {"forbidden_statement", "not_select", "not_single_statement", "forbidden_type", "table_function"}
    text = g.message.lower()
    return bool(set(g.codes) & hard) or "schema '" in text or "cross-database" in text or "pg_" in text \
        or ("forbidden_function" in g.codes and "allowed list" not in text)


class Askbase:
    def __init__(self, settings: Settings, llm: LLM | None, retriever: Retriever,
                 executor: Callable[[str, Settings], QueryResult] = run_query,
                 guard: Callable[..., GuardResult] = validate):
        self.s = settings
        self.llm = llm
        self.retriever = retriever
        self.executor = executor
        self.guard = guard

    # -- helpers -----------------------------------------------------------------------------------------
    def _timed(self, steps: list[Step], name: str, fn: Callable, detail: Callable[[Any], str] | None = None):
        t0 = time.perf_counter()
        out = fn()
        steps.append(Step(name, round((time.perf_counter() - t0) * 1000, 1), detail(out) if detail else ""))
        return out

    @staticmethod
    def _usage(replies: list[Reply]) -> dict:
        return {"llm_calls": len(replies), "input_tokens": sum(r.input_tokens for r in replies),
                "output_tokens": sum(r.output_tokens for r in replies),
                "cost_usd": round(sum(r.cost_usd for r in replies), 6),
                "llm_ms": round(sum(r.latency_ms for r in replies), 1),
                "models": sorted({r.model for r in replies})}

    def _call(self, replies: list[Reply], role: str, system: str, user: str, max_tokens: int = 900) -> str:
        reply = self.llm.complete(role, system, user, max_tokens)
        replies.append(reply)
        return reply.text

    # -- pipeline ----------------------------------------------------------------------------------------
    def ask(self, question: str, history: list[dict] | None = None) -> Answer:
        started = time.perf_counter()
        history = [h for h in (history or []) if h.get("role") in ("user", "assistant") and h.get("content")][-6:]
        question = (question or "").strip()
        steps: list[Step] = []
        replies: list[Reply] = []

        def finish(ans: Answer) -> Answer:
            ans.steps = steps
            ans.usage = self._usage(replies) | {"total_ms": round((time.perf_counter() - started) * 1000, 1)}
            return ans

        if not question:
            return finish(Answer("refused", question, reason="Ask a question about the data."))
        if len(question) > self.s.max_question_chars:
            return finish(Answer("refused", question, reason=f"Questions are limited to {self.s.max_question_chars} characters."))
        if self.llm is None:
            return finish(Answer("no_llm", question, reason="No language model is configured on this server (set ANTHROPIC_API_KEY)."))

        try:
            flagged = bool(SUSPICIOUS.search(question))
            needs_router = bool(history) or flagged
            standalone, intent = question, "data_question"

            def route():
                if not needs_router:
                    return {"intent": "data_question", "standalone_question": question, "reason": ""}
                out = self._call(replies, "fast", prompts.ROUTER_SYSTEM, prompts.question_block(question, history), 300)
                return parse_json_object(out)

            def retrieve():
                return self.retriever.search(question)

            with ThreadPoolExecutor(max_workers=2) as pool:
                t0 = time.perf_counter()
                f_route, f_ret = pool.submit(route), pool.submit(retrieve)
                routed, ctx = f_route.result(), f_ret.result()
            steps.append(Step("route+retrieve", round((time.perf_counter() - t0) * 1000, 1),
                              f"intent={routed.get('intent')}; tables={','.join(ctx.table_names)}"))
            intent = routed.get("intent", "data_question")
            standalone = (routed.get("standalone_question") or question).strip() or question
            if standalone != question:
                ctx = self._timed(steps, "retrieve (rewritten question)", lambda: self.retriever.search(standalone))

            if intent == "unsafe":
                return finish(Answer("refused", question, standalone_question=standalone,
                                     reason=routed.get("reason") or "Askbase is read-only and only answers analytics questions about the sample data."))
            if intent == "off_topic":
                return finish(Answer("refused", question, standalone_question=standalone,
                                     reason="I can only answer questions about the data in this database."))
            if intent == "schema_question":
                text = "Here is what the database contains for that:\n\n" + "\n\n".join(ctx.tables[:4])
                return finish(Answer("ok", question, answer=text, standalone_question=standalone))

            user = f"{ctx.render()}\n\n{prompts.question_block(standalone, history)}"
            raw = self._timed(steps, "generate sql", lambda: self._call(replies, "sql", prompts.SQL_SYSTEM, user, 900),
                              lambda t: f"{len(t)} chars")
            gen = parse_json_object(raw)
            if gen.get("refusal"):
                return finish(Answer("refused", question, standalone_question=standalone, reason=str(gen["refusal"])[:300]))
            sql = str(gen.get("sql", "")).strip()
            assumptions = str(gen.get("assumptions", ""))[:300]
            ans = Answer("ok", question, sql=sql, assumptions=assumptions, standalone_question=standalone)

            result: QueryResult | None = None
            error: str | None = None
            g = self._timed(steps, "guard", lambda: self.guard(sql, self.s.max_rows),
                            lambda r: "ok" if r.ok else f"rejected: {','.join(r.codes)}")
            if g.ok:
                try:
                    result = self._timed(steps, "execute", lambda: self.executor(g.sql, self.s),
                                         lambda r: f"{r.row_count} rows")
                    ans.executed_sql = g.sql
                except ExecutionError as exc:
                    error = str(exc)
            elif is_attack(g):
                ans.status, ans.reason, ans.guard_codes = "blocked", g.message, g.codes
                return finish(ans)
            else:
                error = g.message
                ans.guard_codes = g.codes

            if result is None:  # one self-repair attempt
                repair_user = (f"{ctx.render()}\n\n{prompts.question_block(standalone, history)}\n\n"
                               f"<failed_sql>\n{sql}\n</failed_sql>\n<error>\n{error}\n</error>")
                raw2 = self._timed(steps, "repair sql", lambda: self._call(replies, "repair", prompts.REPAIR_SYSTEM, repair_user, 900))
                gen2 = parse_json_object(raw2)
                if gen2.get("refusal"):
                    ans.status, ans.reason = "error", f"The query failed ({error}) and could not be repaired."
                    return finish(ans)
                sql2 = str(gen2.get("sql", "")).strip()
                ans.sql, ans.repaired = sql2, True
                g2 = self.guard(sql2, self.s.max_rows)
                if not g2.ok:
                    ans.status = "blocked" if is_attack(g2) else "error"
                    ans.reason, ans.guard_codes = g2.message, g2.codes
                    return finish(ans)
                try:
                    result = self._timed(steps, "execute (repaired)", lambda: self.executor(g2.sql, self.s),
                                         lambda r: f"{r.row_count} rows")
                    ans.executed_sql = g2.sql
                except ExecutionError as exc:
                    ans.status, ans.reason = "error", f"The query failed after one repair: {exc}"
                    return finish(ans)

            ans.columns, ans.rows, ans.row_count, ans.truncated = result.columns, result.rows, result.row_count, result.truncated
            ans.chart = pick_chart(result, standalone)
            sample = result.rows[:15]
            summary_user = (f"Question: {standalone}\nSQL: {ans.executed_sql}\nColumns: {result.columns}\n"
                            f"Rows (first {len(sample)} of {result.row_count}{'+' if result.truncated else ''}): {sample}")
            try:
                ans.answer = self._timed(steps, "summarize", lambda: self._call(replies, "fast", prompts.SUMMARY_SYSTEM, summary_user, 250)).strip()
            except LLMError:
                ans.answer = f"The query returned {result.row_count} row(s)."
            return finish(ans)
        except LLMNotConfigured as exc:
            return finish(Answer("no_llm", question, reason=str(exc)))
        except AllowanceUsedUp as exc:
            return finish(Answer("cap_reached", question, reason=(
                f"{str(exc)[0].upper() + str(exc)[1:]}. The recorded questions in the sidebar still work and cost nothing; "
                "live answers reset at midnight UTC. To ask anything you like, run Askbase yourself with your own API key (see the README).")))
        except ReplayMiss:
            return finish(Answer("not_recorded", question, reason=(
                "This public demo answers from recorded runs, so it can never spend anyone's API credits. "
                "That question was not recorded: pick one of the suggested questions in the sidebar, or run Askbase "
                "yourself with your own API key (see the README).")))
        except LLMError as exc:
            return finish(Answer("error", question, reason=str(exc)))
