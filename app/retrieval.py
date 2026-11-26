"""Schema-aware retrieval: tables, glossary terms and similar solved questions, stored in pgvector.

The default embedder is a deterministic hashing embedder (no model download, no API call). It is lexical, not
semantic: fine for an 8-table schema whose glossary carries the synonyms, and replaceable through the same
``embed(text) -> list[float]`` interface.
"""
from __future__ import annotations

import json
import math
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

DIM = 256
KNOWLEDGE = Path(__file__).parent / "knowledge"
_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was", "were", "by", "with", "me",
         "show", "what", "which", "how", "many", "much", "give", "list", "please", "do", "does", "did", "we", "our",
         "my", "i", "all", "each", "per", "from", "that", "this", "have", "has", "had", "there", "get", "tell"}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")) if w not in _STOP]


class HashingEmbedder:
    dim = DIM

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = tokens(text)
        grams = [(t, 1.0) for t in toks] + [(f"{a}_{b}", 0.6) for a, b in zip(toks, toks[1:])]
        for gram, weight in grams:
            h = zlib.crc32(gram.encode())
            vec[h % self.dim] += weight * (1.0 if (h >> 16) & 1 else -1.0)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def load_yaml(name: str):
    return yaml.safe_load((KNOWLEDGE / name).read_text(encoding="utf-8"))


def build_table_doc(table: str, meta: dict, columns: list[tuple[str, str, bool]]) -> str:
    lines = [f"TABLE {table}: {meta['description']}"]
    for name, dtype, nullable in columns:
        desc = meta["columns"].get(name, "")
        lines.append(f"  {name} {dtype}{'' if nullable else ' NOT NULL'} - {desc}")
    if meta.get("relationships"):
        lines.append("  Joins: " + "; ".join(meta["relationships"]))
    return "\n".join(lines)


def index_knowledge(admin: psycopg.Connection, embedder: HashingEmbedder | None = None) -> int:
    """(Re)build askbase_meta.docs from the live column list plus the knowledge files. Returns row count."""
    from .sampledb import ALLOWED_TABLES
    embedder = embedder or HashingEmbedder()
    docs: list[tuple[str, str, str, str]] = []  # kind, ref, content, embed_text
    schema = load_yaml("schema_docs.yaml")
    for table in ALLOWED_TABLES:
        cols = admin.execute(
            "SELECT column_name, data_type, is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", (table,)).fetchall()
        content = build_table_doc(table, schema[table], cols)
        embed_text = f"{table} {schema[table]['description']} " + " ".join(
            f"{c[0]} {schema[table]['columns'].get(c[0], '')}" for c in cols)
        docs.append(("table", table, content, embed_text))
    for g in load_yaml("glossary.yaml"):
        content = f"{g['term']}: {g['definition']} (SQL: {g['sql_hint']})"
        docs.append(("glossary", g["term"], content, f"{g['term']} {' '.join(str(a) for a in g.get('aliases', []))} {g['definition']}"))
    for ex in load_yaml("examples.yaml"):
        docs.append(("example", ex["question"], json.dumps({"question": ex["question"], "sql": " ".join(ex["sql"].split())}),
                     ex["question"]))
    with admin.transaction():
        admin.execute("TRUNCATE askbase_meta.docs RESTART IDENTITY")
        for kind, ref, content, embed_text in docs:
            admin.execute("INSERT INTO askbase_meta.docs (kind, ref, content, embedding) VALUES (%s, %s, %s, %s::askbase_meta.vector)",
                          (kind, ref, content, _vec(embedder.embed(embed_text))))
    return len(docs)


@dataclass
class Context:
    tables: list[str] = field(default_factory=list)          # rendered table docs
    table_names: list[str] = field(default_factory=list)
    glossary: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)

    def render(self) -> str:
        parts = ["## Tables\n" + "\n\n".join(self.tables)]
        if self.glossary:
            parts.append("## Business definitions (apply these)\n" + "\n".join(f"- {g}" for g in self.glossary))
        if self.examples:
            parts.append("## Similar solved questions\n" + "\n\n".join(
                f"Q: {e['question']}\nSQL: {e['sql']}" for e in self.examples))
        return "\n\n".join(parts)


class Retriever:
    """Reads the knowledge base through its own role (askbase_kb), which cannot see the business tables."""

    def __init__(self, kb_url: str, embedder: HashingEmbedder | None = None):
        self.kb_url = kb_url
        self.embedder = embedder or HashingEmbedder()
        self._adjacency: dict[str, set[str]] | None = None

    def _graph(self) -> dict[str, set[str]]:
        if self._adjacency is None:
            graph: dict[str, set[str]] = {}
            for table, meta in load_yaml("schema_docs.yaml").items():
                for rel in meta.get("relationships", []):
                    left, right = (side.strip().split(".")[0] for side in rel.split("(")[0].split("->"))
                    graph.setdefault(left, set()).add(right)
                    graph.setdefault(right, set()).add(left)
            self._adjacency = graph
        return self._adjacency

    def search(self, question: str, k_tables: int = 4, k_glossary: int = 4, k_examples: int = 3) -> Context:
        vec = _vec(self.embedder.embed(question))
        ctx = Context()
        with psycopg.connect(self.kb_url, autocommit=True) as conn:
            def top(kind: str, k: int):
                return conn.execute(
                    "SELECT ref, content, 1 - (embedding OPERATOR(askbase_meta.<=>) %s::askbase_meta.vector) AS score "
                    "FROM askbase_meta.docs WHERE kind = %s "
                    "ORDER BY embedding OPERATOR(askbase_meta.<=>) %s::askbase_meta.vector LIMIT %s", (vec, kind, vec, k)).fetchall()

            tables = top("table", k_tables)
            names = [t[0] for t in tables]
            # join-path expansion: a "revenue by category" question needs the bridge tables too
            for t in names[:3]:
                for neighbour in sorted(self._graph().get(t, ())):
                    if neighbour not in names and len(names) < 7:
                        names.append(neighbour)
            by_name = {r[0]: r[1] for r in conn.execute("SELECT ref, content FROM askbase_meta.docs WHERE kind = 'table'")}
            ctx.table_names = names
            ctx.tables = [by_name[n] for n in names if n in by_name]
            ctx.glossary = [g[1] for g in top("glossary", k_glossary) if g[2] > 0.05]
            ctx.examples = [json.loads(e[1]) for e in top("example", k_examples) if e[2] > 0.1]
        return ctx
