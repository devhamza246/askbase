"""LLM access: one tiny interface, an Anthropic implementation, record/replay cassettes and a scripted fake.

Roles map to models (Settings): "sql" (generation), "repair", "fast" (routing and summaries). Every call returns
tokens, latency and cost so the API can show a per-answer trace and enforce a daily spend cap.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

import httpx

from .config import Settings

PRICING = {  # USD per million tokens (input, output); overridable by editing this table
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class AllowanceUsedUp(LLMError):
    """The demo's daily live-answer allowance (global or per visitor) is used up. Recorded answers still work."""


class ReplayMiss(LLMError):
    """Replay mode and this exact request was never recorded: no network call is made, nothing is spent."""


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    for key, (pin, pout) in PRICING.items():
        if model.startswith(key):
            return (tokens_in * pin + tokens_out * pout) / 1_000_000
    return 0.0


class LLM(Protocol):
    def complete(self, role: str, system: str, user: str, max_tokens: int = 900) -> Reply: ...


# Set by the API for the duration of one request so live calls can be counted per visitor.
visitor: contextvars.ContextVar[str] = contextvars.ContextVar("askbase_visitor", default="?")
# A visitor's own Anthropic key ("bring your own key"). Lives only in this request's context: never stored, logged,
# cached or recorded. Live calls made with it bypass the demo's allowance because the visitor pays for them.
user_key: contextvars.ContextVar[str] = contextvars.ContextVar("askbase_user_key", default="")


class Budget:
    """Daily live-answer allowance (UTC day): a global spend cap plus a per-visitor limit on live calls.
    Thread-safe; in-memory, so a restart resets it. Recorded (cached) answers never touch it."""

    def __init__(self, cap_usd: float, per_visitor_calls: int = 0):
        self.cap = cap_usd
        self.per_visitor_calls = per_visitor_calls
        self._day = date.today()
        self._spent = 0.0
        self._calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def _roll(self) -> None:
        if date.today() != self._day:
            self._day, self._spent, self._calls = date.today(), 0.0, {}

    def add(self, usd: float) -> None:
        with self._lock:
            self._roll()
            self._spent += usd

    @property
    def spent(self) -> float:
        with self._lock:
            self._roll()
            return self._spent

    def exhausted(self) -> bool:
        return self.cap > 0 and self.spent >= self.cap

    def reserve_live_call(self, who: str) -> None:
        """Called just before a live (uncached) model call. Raises AllowanceUsedUp if the demo or this visitor is out."""
        with self._lock:
            self._roll()
            if self.cap > 0 and self._spent >= self.cap:
                raise AllowanceUsedUp("today's live-answer allowance for this public demo is used up")
            used = self._calls.get(who, 0)
            if self.per_visitor_calls and used >= self.per_visitor_calls:
                raise AllowanceUsedUp("you have used your live answers for today")
            self._calls[who] = used + 1


class AnthropicLLM:
    def __init__(self, settings: Settings, budget: Budget | None = None, base_url: str = "https://api.anthropic.com"):
        self.s = settings
        self.budget = budget
        self.base_url = base_url
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._cassette = Path(settings.cassette) if settings.cassette else None
        if self._cassette and self._cassette.is_file():
            for line in self._cassette.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._cache[entry["key"]] = entry

    def model_for(self, role: str) -> str:
        if role == "fast":
            return self.s.fast_model
        if role == "repair":
            return self.s.repair_model or self.s.sql_model
        return self.s.sql_model

    @staticmethod
    def _key(model: str, system: str, user: str) -> str:
        return hashlib.sha256(json.dumps([model, system, user]).encode()).hexdigest()

    def complete(self, role: str, system: str, user: str, max_tokens: int = 900) -> Reply:
        model = self.model_for(role)
        key = self._key(model, system, user)
        hit = self._cache.get(key)
        if hit and self.s.llm_mode != "live":
            return Reply(hit["text"], model, hit["input_tokens"], hit["output_tokens"], 0.0, 0.0, cached=True)
        byok = user_key.get()
        if self.s.llm_mode == "replay" and not byok:
            raise ReplayMiss("no recorded LLM response for this request (replay mode)")
        if not (byok or self.s.anthropic_key):
            raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")
        if self.budget and not byok:
            self.budget.reserve_live_call(visitor.get())
        payload = {"model": model, "max_tokens": max_tokens, "system": system, "temperature": 0,
                   "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": byok or self.s.anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        start = time.perf_counter()
        resp = httpx.post(f"{self.base_url}/v1/messages", headers=headers, json=payload, timeout=60)
        if resp.status_code == 400 and "temperature" in resp.text.lower():
            payload.pop("temperature")
            resp = httpx.post(f"{self.base_url}/v1/messages", headers=headers, json=payload, timeout=60)
        if resp.status_code == 401:
            raise LLMError("the Anthropic API rejected " + ("your key (401)" if byok else "the key (401)"))
        if resp.status_code == 429:
            raise LLMError("the Anthropic API is rate limiting requests (429); try again shortly")
        if resp.status_code >= 400:
            raise LLMError(f"the Anthropic API returned {resp.status_code}: {resp.text[:160]}")
        body = resp.json()
        text = "".join(b.get("text", "") for b in body.get("content", []))
        usage = body.get("usage", {})
        tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        reply = Reply(text, model, tin, tout, (time.perf_counter() - start) * 1000, cost_of(model, tin, tout))
        if byok:
            return reply  # the visitor's own spend: not counted against the demo, not cached, not recorded
        if self.budget:
            self.budget.add(reply.cost_usd)
        rec = {"key": key, "model": model, "text": text, "input_tokens": tin, "output_tokens": tout}
        with self._lock:
            self._cache[key] = rec  # in memory: a repeated question in this process is free
            if self._cassette and self.s.record_live:  # persisted only by the recording scripts, never by the public API
                try:
                    self._cassette.parent.mkdir(parents=True, exist_ok=True)
                    with self._cassette.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")
                except OSError:
                    pass  # an unwritable cassette must never break an answer
        return reply


@dataclass
class ScriptedLLM:
    """Deterministic stand-in for tests: ``handler(role, system, user) -> text``. Records every call."""
    handler: Callable[[str, str, str], str]
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def complete(self, role: str, system: str, user: str, max_tokens: int = 900) -> Reply:
        self.calls.append((role, system, user))
        text = self.handler(role, system, user)
        return Reply(text, f"scripted-{role}", len(system + user) // 4, len(text) // 4, 1.0, 0.0)


def parse_json_object(text: str) -> dict:
    """First JSON object in a model reply (tolerates code fences or prose around it)."""
    start = text.find("{")
    if start < 0:
        raise LLMError(f"the model reply contained no JSON object: {text[:100]!r}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"the model reply was not valid JSON: {exc.msg}") from exc
    raise LLMError("the model reply had an unterminated JSON object")
