from __future__ import annotations

import sqlite3

from insight_agent.config import Settings
from insight_agent.db import Executor, build_demo_db, find_missing_literal, table_schema
from insight_agent.graph import nodes
from insight_agent.graph.build import after_critic
from insight_agent.runtime import Runtime


def _setup(workdir):
    db_path = build_demo_db(workdir / "demo.sqlite")
    connection = sqlite3.connect(db_path)
    try:
        tables = table_schema(connection)
    finally:
        connection.close()
    settings = Settings(db_path=db_path, fake_llm=True, _env_file=None)
    return Executor(db_path), tables, Runtime.create(settings)


def test_finds_entity_that_does_not_exist(workdir):
    executor, tables, _ = _setup(workdir)
    sql = (
        "SELECT SUM(oi.quantity * oi.unit_price) FROM orders o "
        "JOIN customers c ON c.id = o.customer_id "
        "WHERE c.name LIKE '%华为%' AND c.region = '华北'"
    )
    assert find_missing_literal(executor, sql, tables) == "华为"


def test_known_values_are_not_flagged(workdir):
    executor, tables, _ = _setup(workdir)
    sql = "SELECT 1 FROM customers WHERE region = '华北' AND name LIKE '%客户%'"
    assert find_missing_literal(executor, sql, tables) is None


def test_critic_marks_no_data_without_calling_the_model(workdir):
    _, _, runtime = _setup(workdir)
    state = {
        "question": "华为在华北地区销售额是多少？",
        "sql": "SELECT SUM(oi.quantity) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE c.name LIKE '%华为%' AND c.region = '华北'",
        "rows": [["华为", "华北", None]],  # 维度字面量被回显，指标是 NULL
        "row_count": 1,
        "exec_error": None,
        "repair_attempts": 0,
    }
    result = nodes.critic(state, runtime)

    assert result["verdict"] == "no_data"
    assert result["missing_entity"] == "华为"
    assert result["usage"] == {}  # 空结果是确定性的，不该花一次模型调用


def test_no_data_goes_straight_to_report(workdir):
    _, _, runtime = _setup(workdir)
    assert after_critic({"verdict": "no_data"}, runtime) == "report"


def test_noop_repair_stops_the_loop(workdir):
    _, _, runtime = _setup(workdir)
    state = {"verdict": "empty_result", "repair_noop": True, "repair_attempts": 0}
    assert after_critic(state, runtime) == "escalate"


def test_zero_is_flagged_only_when_the_entity_is_missing(workdir):
    _, _, runtime = _setup(workdir)
    base = {"question": "华为在华北的销售额", "exec_error": None, "repair_attempts": 0}

    # COALESCE 把 NULL 抹成 0，但实体不存在 → 仍然判 no_data
    sql_missing = "SELECT SUM(1) FROM customers WHERE name LIKE '%华为%'"
    out = nodes.critic({**base, "sql": sql_missing, "rows": [[0]], "row_count": 1}, runtime)
    assert out["verdict"] == "no_data"

    # 真实的 0（实体都在），不该误判
    sql_ok = (
        "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE c.region = '华北'"
    )
    out2 = nodes.critic({**base, "sql": sql_ok, "rows": [[0]], "row_count": 1}, runtime)
    assert out2["verdict"] == "ok"


def test_report_explains_missing_entity(workdir):
    _, _, runtime = _setup(workdir)
    state = {
        "question": "华为在华北地区销售额是多少？",
        "verdict": "no_data",
        "missing_entity": "华为",
        "sql": "SELECT 1",
    }
    result = nodes.report(state, runtime)

    assert "未查询到数据" in result["answer"]
    assert "华为" in result["answer"]


def test_empty_result_is_not_reported_as_failure(workdir):
    _, _, runtime = _setup(workdir)
    state = {"question": "2024 年拉萨的销售额", "verdict": "empty_result", "sql": "SELECT 1"}
    result = nodes.escalate(state, runtime)

    assert "未查询到符合条件的数据" in result["answer"]
    assert "修复没有收敛" not in result["answer"]
