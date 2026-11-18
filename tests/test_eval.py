from __future__ import annotations

from insight_agent.db import Executor, build_demo_db
from insight_agent.eval.cases import build_cases
from insight_agent.eval.metrics import result_equal, summarize


def test_result_equal_is_order_and_column_insensitive():
    assert result_equal([["华东", 1.0], ["华北", 2.0]], [["华北", 2.0], ["华东", 1.0]])
    assert result_equal([["华东", 1.0]], [[1.0, "华东"]])
    assert result_equal([[2219926.1065]], [[2219926.11]])


def test_result_equal_rejects_wrong_values_or_counts():
    assert not result_equal([["华东", 1.0]], [["华东", 9.0]])
    assert not result_equal([["华东", 1.0]], [["华东", 1.0], ["华北", 2.0]])
    # 列数不同时不能崩，直接判不等
    assert not result_equal([["华东", 1.0]], [[1.0]])


def test_result_equal_accepts_wide_and_long_shapes():
    gold = [["2023", 100.0], ["2024", 200.0]]
    assert result_equal(gold, [[100.0, 200.0]])
    assert not result_equal(gold, [[100.0, 999.0]])


def test_result_equal_tolerates_sqlite_type_affinity():
    # strftime 返回文本、CAST 返回整数，是同一个年份
    assert result_equal([["2023", 5]], [[2023, 5.0]])


def test_summary_counts_repaired_cases():
    records = [
        {"ex": True, "attempts": [{"verdict": "ok"}], "tokens": 10, "seconds": 1.0},
        {"ex": True, "attempts": [{"verdict": "syntax_error"}, {"verdict": "ok"}], "tokens": 20, "seconds": 2.0},
        {"ex": False, "attempts": [{"verdict": "empty_result"}], "tokens": 30, "seconds": 3.0},
    ]
    summary = summarize(records)
    assert summary["ex_rate"] == round(2 / 3, 3)
    assert summary["repaired_ok"] == 1
    assert summary["first_try_rate"] == round(1 / 3, 3)


def test_cases_are_unique_and_executable(workdir):
    executor = Executor(build_demo_db(workdir / "demo.sqlite"))
    cases = build_cases()

    assert len(cases) == 100
    assert len({case["id"] for case in cases}) == 100
    for case in cases:
        assert executor.run(case["sql"]).error is None, case["id"]
