from __future__ import annotations

from insight_agent.config import Settings
from insight_agent.db.executor import check_sql
from insight_agent.graph.build import build_graph
from insight_agent.runtime import Runtime
from insight_agent.state import initial_state


def _runtime(workdir) -> Runtime:
    # _env_file=None：测试不读仓库里的 .env，避免用到真实 Key 和真实网络
    settings = Settings(db_path=workdir / "demo.sqlite", _env_file=None)
    return Runtime.create(settings)


def test_repairs_once_then_answers(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "t-repair"}}

    final = graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)

    assert final["verdict"] == "ok"
    assert final["repair_attempts"] == 1
    assert len(final["attempts"]) == 2
    assert final["attempts"][0]["verdict"] == "syntax_error"
    assert final["attempts"][1]["verdict"] == "ok"
    assert final["row_count"] == 1
    assert final["rows"][0][0] > 0
    assert final["chart_spec"]["type"] == "metric"
    assert "结论" in final["answer"]


def test_chat_short_circuits(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    final = graph.invoke(initial_state("你好"), {"configurable": {"thread_id": "t-chat"}})

    assert final["intent"] == "chat"
    assert "数据助手" in final["answer"]
    assert not final.get("attempts")


def test_clarify_when_too_vague(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    final = graph.invoke(initial_state("看下"), {"configurable": {"thread_id": "t-clarify"}})

    assert final["intent"] == "clarify"
    assert final["verdict"] == "needs_clarification"


def test_executor_rejects_writes():
    assert check_sql("DELETE FROM orders") is not None
    assert check_sql("SELECT 1; DROP TABLE orders") is not None
    assert check_sql("PRAGMA table_info(orders)") is not None
    assert check_sql("SELECT 1") is None
    assert check_sql("WITH x AS (SELECT 1 AS a) SELECT * FROM x") is None
