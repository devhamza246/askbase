# Askbase

**Ask your database in plain English. Askbase answers from Postgres, read-only, with the SQL always shown next to the answer.**

A text-to-SQL agent built around the places naive demos fail: it never trusts the model's SQL. Every query is parsed,
validated on its syntax tree, rewritten with a forced `LIMIT`, regenerated, and executed as a database role that
physically cannot write. Ask a business question and you get an explained answer, the SQL, a table and an automatic chart.
Ask it to `DROP TABLE orders` and you get a refusal that says exactly why.

**Live:** https://askbase.hamzacodes.dev (the SQL-guardrail playground and attack buttons work without any model;
answering questions needs the server's API key to be configured) · sample database: an 8-table e-commerce shop.

## How it works

```mermaid
flowchart LR
  Q[Question + history] --> R{Route<br/>Haiku, only if needed}
  Q --> K[(pgvector<br/>schema, glossary,<br/>solved examples)]
  R --> G[Generate SQL<br/>Sonnet]
  K --> G
  G --> V[Guard<br/>sqlglot AST]
  V -- rejected: attack --> X[Blocked + explained<br/>nothing executed]
  V -- rejected: mistake --> P[One self-repair<br/>with the error]
  P --> V
  V -- allowed, LIMIT forced,<br/>SQL regenerated --> E[Execute as askbase_ro<br/>READ ONLY txn, 5 s timeout]
  E -- DB error --> P
  E --> S[Summarize + pick chart<br/>Haiku + rules]
  S --> A[Answer + SQL + table + chart]
```

1. **Retrieve** the relevant tables (with join paths), business-glossary definitions ("revenue excludes cancelled and refunded
   orders") and similar solved questions from pgvector. The default embedder is a deterministic hashing embedder
   (lexical, no model download); the glossary carries the synonyms. Swap in any embedding model through the same interface.
2. **Generate** one `SELECT` with the schema, definitions and examples as context. The model may also refuse.
3. **Guard** ([`app/guard.py`](app/guard.py)): parse as PostgreSQL, exactly one statement, a read-only query, allow-listed
   tables only, no table functions or system catalogs, no blocklisted or unknown functions, no parameters, forced
   `LIMIT`. The SQL that runs is **regenerated from the validated tree**, then re-validated, so a string that parses one way
   for the guard and another for Postgres cannot slip through.
4. **Execute** as `askbase_ro`: `SELECT` on 8 tables only, `BEGIN READ ONLY`, 5 s statement timeout, row cap.
5. **Repair once** if Postgres or the guard reports a *mistake* (wrong column, hallucinated table). Attacks are never repaired.
6. **Summarize** with a cheap model, choose a chart deterministically from the result's shape.

## Defence in depth

| Layer | What it stops | Verified by |
|---|---|---|
| Router + model refusal | Obvious abuse, prompt injection | Live-model adversarial run (not yet run: needs an API key) |
| **AST guard** | Writes, DDL, `COPY`, `SET`, stacked statements, CTE writes, `SELECT INTO`, row locks, other schemas, system catalogs, `pg_*`/`dblink`/`lo_*`/`set_config`, table functions, regclass casts, bind parameters | 127 unit tests: 26 legitimate queries accepted, 91 attack vectors rejected |
| Regenerated SQL | Parser-differential tricks, smuggled comments | Round-trip tests |
| **Read-only role** | Anything the guard missed: no write privilege anywhere, no access to `private` or the knowledge schema, `pg_sleep` revoked | 18 statements fired directly at the role with the guard bypassed, all refused |
| Read-only transaction + timeout + row cap | Writes through functions, runaway queries, huge results | Cross-join timeout test |

Two roles, neither can write: `askbase_ro` runs generated SQL; `askbase_kb` can only read the knowledge base. The admin
connection is used by the one-off bootstrap step and nothing else.

## Adversarial results (measured)

[`benchmark/adversarial.yaml`](benchmark/adversarial.yaml) has 20 attack prompts across DDL, DML, stacked statements, CTE
writes, private data, system access, denial of service, file access, privilege escalation and prompt injection.
The run feeds each prompt through the full pipeline with a **fully compromised model that emits the matching attack SQL
every time** (the strongest case for the guard: no help from the model at all), snapshotting every table, hash and
privilege before and after:

```
python -m benchmark.adversarial
compromised-model: 20 prompts, write/DDL statements executed = 0, blocked by guard = 19, errors = 1 (statement timeout),
database unchanged = True
```

The one non-blocked case is a self cross-join: a legitimate `SELECT` that the 5-second statement timeout cancels.
This measures the guardrails, not Claude's own refusals; the live-model variant is `python -m benchmark.adversarial --live`.

## Benchmark: 40 questions, execution accuracy

[`benchmark/questions.yaml`](benchmark/questions.yaml): 12 easy, 16 medium, 12 hard questions, each with a gold query written
against the documented business definitions. A prediction is correct when its **result set** equals the gold result set
(column names and, unless the question is a top-N, row order are ignored; non-integers compare at one decimal). The
questions are deliberately different from the solved examples the model sees.

**Status: the pipeline has not been scored yet** because it needs an Anthropic API key. What is verified without one:
all 40 gold queries pass the guard, run, are deterministic and have non-trivial answers; the scoring rules are
unit-tested; the harness runs against the real database. To produce the headline number:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m benchmark.run --gauge-out results/gauge.json   # records benchmark/cassette.jsonl, prints accuracy and latency
git add benchmark/cassette.jsonl                         # makes the result replayable offline (and runnable in CI)
```

Targets, to be confirmed or refuted by that run: at least 85% execution accuracy, under 8 s per answer, 0 writes.
Latency, cost per answer and repair rate are reported by the same run. Results can be tracked per commit with
[Gauge](https://github.com/devhamza246/gauge) (`gauge import`).

## Run it

```bash
cp .env.example .env            # set both passwords; add ANTHROPIC_API_KEY to enable answering
docker compose --env-file .env up -d --build
# UI http://localhost:8813   API http://localhost:8812/docs
```

The container runs `python -m app.bootstrap` first (idempotent): schema, a deterministic seed (400 customers, 60 products,
2,500 orders, about 5,800 order lines), the locked-down roles and the pgvector knowledge index.

Point it at your own database by replacing the schema docs, glossary and examples in [`app/knowledge/`](app/knowledge/),
the table allow-list in `app/sampledb.py`, and the read-only role grants in `app/bootstrap.py`.

API: `POST /api/ask {question, history}` · `POST /api/validate {sql}` (verdict only, never executes) · `GET /api/status`.

## Known limits

- **Answering needs a model.** Without `ANTHROPIC_API_KEY` the app runs and the guardrails can be demonstrated, but
  questions return a clear "no model configured" message.
- **Single database, single schema, no per-user permissions.** One read-only role for everyone (out of scope for v1).
- **The embedder is lexical.** It is enough for an 8-table schema with a glossary; a larger schema would want a real
  embedding model.
- **The timeout is a backstop, not a planner.** A legitimately expensive query is cancelled after 5 s.
- **The guard is an allow-list of tables and a block/allow list of functions.** A new Postgres function is rejected until
  it is added to `ANON_ALLOW`; that is deliberate (fail closed) but can reject valid queries.
- **Public demo cost controls:** per-IP rate limit and a daily spend cap (both in-memory).

## Development

```bash
pip install -e ".[ui,dev]"
docker run -d --name askbase-test-pg -e POSTGRES_PASSWORD=testadmin -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
pytest                      # 235 tests: guard, database roles, retrieval, agent, API, benchmark validity, adversarial
```

MIT licensed.
