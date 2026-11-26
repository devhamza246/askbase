"""The benchmark itself must be valid: every gold query passes the guard, runs, and is deterministic."""
import pytest

from app.executor import execute
from app.guard import validate
from benchmark.scoring import load_questions, results_match

QUESTIONS = load_questions()


def test_forty_questions_with_a_difficulty_mix():
    assert len(QUESTIONS) == 40
    assert len({q["id"] for q in QUESTIONS}) == 40
    mix = {d: sum(q["difficulty"] == d for q in QUESTIONS) for d in ("easy", "medium", "hard")}
    assert mix == {"easy": 12, "medium": 16, "hard": 12}


def test_benchmark_does_not_leak_into_the_few_shot_examples():
    from app.retrieval import load_yaml
    examples = {" ".join(e["question"].lower().split()) for e in load_yaml("examples.yaml")}
    assert not examples & {" ".join(q["question"].lower().split()) for q in QUESTIONS}


@pytest.mark.parametrize("q", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_gold_sql_is_valid_and_meaningful(q, settings):
    g = validate(q["sql"], settings.max_rows)
    assert g.ok, g.message
    first = execute(g.sql, settings)
    second = execute(g.sql, settings)
    assert first.rows == second.rows, "gold answer is not deterministic"
    assert first.row_count >= 1, "gold query returns nothing, so it cannot distinguish a right answer from a wrong one"
    assert not first.truncated
    if q.get("ordered"):
        assert "ORDER BY" in q["sql"].upper()


def test_no_gold_answer_is_trivially_zero(settings):
    for q in QUESTIONS:
        r = execute(validate(q["sql"]).sql, settings)
        if r.row_count == 1 and len(r.rows[0]) == 1:
            assert r.rows[0][0] not in (0, None), f"{q['id']} has a degenerate gold answer"


# --- scoring rules -----------------------------------------------------------------------------------------
def test_scoring_ignores_row_order_unless_ordered():
    a, b = [["x", 1], ["y", 2]], [["y", 2], ["x", 1]]
    assert results_match(a, b, ordered=False)
    assert not results_match(a, b, ordered=True)


def test_scoring_ignores_column_order_and_names_but_not_values():
    assert results_match([[1, "a"], [2, "b"]], [["a", 1], ["b", 2]], ordered=False)
    assert not results_match([[1, "a"]], [[2, "a"]], ordered=False)
    assert not results_match([[1]], [[1], [2]], ordered=False)


def test_scoring_rounds_floats_and_normalises_types():
    assert results_match([[23.37]], [[23.4]], False)
    assert results_match([[5.0]], [[5]], False)
    assert results_match([["2026-01-01"]], [["2026-01-01"]], False)
    assert not results_match([[23.0]], [[24.0]], False)
    assert results_match([["Phones"]], [["phones "]], False)
