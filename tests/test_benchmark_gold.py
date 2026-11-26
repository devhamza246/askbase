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


def test_lenient_scoring_forgives_extra_columns_only():
    from benchmark.scoring import results_match_lenient as lenient
    assert lenient([["UPS", 730], ["DHL", 649]], [["DHL"], ["UPS"]], False)           # an extra count column
    assert lenient([[8, "Studio 16", 58444.2]], [["Studio 16", 58444.2]], False)      # an extra id column
    assert not lenient([["UPS", 730]], [["DHL"]], False)                              # wrong value
    assert not lenient([["UPS", 730], ["DHL", 649]], [["UPS"]], False)                # truncated or extra rows
    assert not lenient([["x"]], [["x", 1]], False)                                    # a missing column is never forgiven
    assert lenient([["a", 1]], [["a", 1]], True)                                      # exact still passes


def test_numeric_tolerance_is_absolute_005_and_integers_are_exact():
    assert results_match([[11.65]], [[11.6]], False)       # a rounded value against a coarser gold
    assert results_match([[1.83]], [[1.8]], False)
    assert not results_match([[11.7]], [[11.6]], False)    # 0.1 apart is a different answer
    assert not results_match([[451961]], [[451960]], False)  # integers must be exact
    assert results_match([["a", 1.83], ["b", 2.0]], [["b", 2], ["a", 1.8]], False)
    assert not results_match([["a", 1.83]], [["b", 1.83]], False)


# --- held-out set: gold validity and independence from everything the model was tuned on --------------------------
HOLDOUT = load_questions("benchmark/holdout.yaml")


def test_holdout_has_twenty_new_questions_and_no_overlap():
    from app.retrieval import load_yaml
    assert len(HOLDOUT) == 20 and len({q["id"] for q in HOLDOUT}) == 20
    norm = lambda t: " ".join(t.lower().split())  # noqa: E731
    seen = {norm(q["question"]) for q in QUESTIONS} | {norm(e["question"]) for e in load_yaml("examples.yaml")}
    assert not seen & {norm(q["question"]) for q in HOLDOUT}
    assert {q["difficulty"] for q in HOLDOUT} == {"easy", "medium", "hard"}


@pytest.mark.parametrize("q", HOLDOUT, ids=[q["id"] for q in HOLDOUT])
def test_holdout_gold_is_valid_deterministic_and_unambiguous(q, settings):
    g = validate(q["sql"], settings.max_rows)
    assert g.ok, g.message
    first, second = execute(g.sql, settings), execute(g.sql, settings)
    assert first.rows == second.rows and first.row_count >= 1 and not first.truncated
    if first.row_count == 1 and len(first.rows[0]) == 1:
        assert first.rows[0][0] not in (0, None), "degenerate gold answer"
    if q.get("ordered") and "LIMIT 1" in q["sql"].upper():
        # a top-1 question must have a unique winner, or the gold depends on an arbitrary tie-break
        key = "ORDER BY"
        base = q["sql"].split("LIMIT 1")[0]
        top2 = execute(validate(base.replace(key, "ORDER BY", 1) + " LIMIT 2").sql, settings)
        if top2.row_count == 2:
            order_col = top2.columns[-1]
            assert top2.rows[0][-1] != top2.rows[1][-1], f"{q['id']}: the top-1 answer is tied on {order_col}"
