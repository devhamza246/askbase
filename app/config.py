"""Settings, read from the environment once. The runtime only ever holds the read-only DSN;
the admin DSN is used by the bootstrap step alone (create roles, schema, seed data)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    admin_url: str = field(default_factory=lambda: os.environ.get("ASKBASE_ADMIN_URL", ""))
    ro_url: str = field(default_factory=lambda: os.environ.get("ASKBASE_RO_URL", ""))
    ro_password: str = field(default_factory=lambda: os.environ.get("ASKBASE_RO_PASSWORD", ""))
    anthropic_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    sql_model: str = field(default_factory=lambda: os.environ.get("ASKBASE_SQL_MODEL", "claude-sonnet-5-5"))
    fast_model: str = field(default_factory=lambda: os.environ.get("ASKBASE_FAST_MODEL", "claude-haiku-5-5"))
    repair_model: str = field(default_factory=lambda: os.environ.get("ASKBASE_REPAIR_MODEL", ""))  # "" = same as sql_model
    llm_mode: str = field(default_factory=lambda: os.environ.get("ASKBASE_LLM_MODE", "auto"))  # auto|live|replay
    cassette: str = field(default_factory=lambda: os.environ.get("ASKBASE_CASSETTE", ""))
    max_rows: int = field(default_factory=lambda: _int("ASKBASE_MAX_ROWS", 500))
    statement_timeout_ms: int = field(default_factory=lambda: _int("ASKBASE_STATEMENT_TIMEOUT_MS", 5000))
    daily_spend_cap_usd: float = field(default_factory=lambda: _float("ASKBASE_DAILY_SPEND_CAP_USD", 2.0))
    rate_limit_per_min: int = field(default_factory=lambda: _int("ASKBASE_RATE_LIMIT_PER_MIN", 12))
    max_question_chars: int = field(default_factory=lambda: _int("ASKBASE_MAX_QUESTION_CHARS", 500))

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_key) or (self.llm_mode == "replay" and bool(self.cassette))
