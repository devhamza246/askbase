import pytest

from app.charts import pick_chart
from app.executor import QueryResult
from app.retrieval import HashingEmbedder, Retriever, tokens


def test_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    v1, v2 = e.embed("revenue by category"), e.embed("revenue by category")
    assert v1 == v2 and len(v1) == 256 and abs(sum(x * x for x in v1) - 1) < 1e-9


def test_tokenizer_drops_stopwords_and_stems():
    assert tokens("Show me the orders per month") == ["order", "month"]


@pytest.fixture(scope="module")
def retriever(kb_url, settings):
    return Retriever(kb_url)


def test_retrieval_finds_the_join_path_for_revenue_by_category(retriever):
    ctx = retriever.search("What was our revenue by product category in 2025?")
    assert {"orders", "order_items", "products", "categories"} <= set(ctx.table_names)
    assert any(g.startswith("revenue:") for g in ctx.glossary)


def test_retrieval_surfaces_the_right_glossary_terms(retriever):
    assert any(g.startswith("refund rate") for g in retriever.search("What is our refund rate?").glossary)
    assert any(g.startswith("delivery time") for g in retriever.search("average days to deliver a package").glossary)
    assert any(g.startswith("time periods") for g in retriever.search("sales this year per month").glossary)


def test_retrieval_returns_similar_solved_examples(retriever):
    ctx = retriever.search("How many orders per month in 2026?")
    assert any("month" in e["question"].lower() for e in ctx.examples)


def test_retrieval_bridges_tables_via_foreign_keys(retriever):
    ctx = retriever.search("average rating of products")
    assert "reviews" in ctx.table_names and "products" in ctx.table_names
    assert len(ctx.table_names) <= 7


def test_rendered_context_has_columns_and_joins(retriever):
    text = retriever.search("customers by country").render()
    assert "TABLE customers" in text and "country text" in text and "Joins:" in text


def _res(cols, kinds, rows):
    return QueryResult(columns=cols, rows=rows, row_count=len(rows), truncated=False, elapsed_ms=1, types=kinds)


def test_chart_selection_rules():
    assert pick_chart(_res(["n"], ["number"], [[5]])) is None
    assert pick_chart(_res(["a", "b"], ["text", "number"], [["x", 1]])) is None  # single row: no chart
    line = pick_chart(_res(["month", "revenue"], ["date", "number"], [["2026-01-01", 1], ["2026-02-01", 2]]))
    assert line["type"] == "line" and line["x"] == "month" and line["y"] == ["revenue"]
    bar = pick_chart(_res(["country", "n"], ["text", "number"], [["US", 5], ["UK", 3]]))
    assert bar["type"] == "bar" and bar["y"] == ["n"]
    pie = pick_chart(_res(["method", "pct"], ["text", "number"], [["card", 60.0], ["paypal", 40.0]]))
    assert pie["type"] == "pie"
    many = pick_chart(_res(["name", "n"], ["text", "number"], [[str(i), i] for i in range(40)]))
    assert many is None
    assert pick_chart(_res(["a", "b"], ["text", "text"], [["x", "y"], ["z", "w"]])) is None
