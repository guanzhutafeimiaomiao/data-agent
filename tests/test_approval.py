from __future__ import annotations

from langgraph.types import Command

from insight_agent.config import Settings
from insight_agent.graph.build import build_graph
from insight_agent.graph.policy import approval_reasons, needs_approval
from insight_agent.runtime import Runtime
from insight_agent.state import initial_state


def _runtime(workdir, **overrides) -> Runtime:
    settings = Settings(
        db_path=workdir / "demo.sqlite",
        store_path=workdir / "store.db",
        checkpoint_path=workdir / "cp.db",
        fake_llm=True,
        approval_enabled=True,
        _env_file=None,
        **overrides,
    )
    return Runtime.create(settings)


def _pending(graph, config) -> dict | None:
    snapshot = graph.get_state(config)
    for task in snapshot.tasks or ():
        for item in task.interrupts or ():
            value = getattr(item, "value", item)
            return value if isinstance(value, dict) else None
    return None


def test_policy_flags_full_scan_and_unbounded_time():
    reasons = approval_reasons("SELECT COUNT(*) FROM orders")
    assert len(reasons) == 2
    assert any("WHERE" in reason for reason in reasons)

    assert approval_reasons("SELECT 1 FROM orders WHERE status = 'paid'") != []
    assert approval_reasons("SELECT 1 FROM orders WHERE order_date > '2024-01-01'") == []
    assert needs_approval("SELECT COUNT(*) FROM orders", enabled=False) is False


def test_risky_query_pauses_before_executing(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "ap-1"}}

    # fake 后端对这个问题给出无 WHERE 的 SQL
    final = graph.invoke(initial_state("统计一下总订单数"), config)

    waiting = _pending(graph, config)
    assert waiting is not None
    assert waiting["type"] == "approval"
    assert waiting["reasons"]
    assert final["approval_required"] is False  # 节点还没返回，停在 interrupt 里
    assert not final.get("attempts")  # 关键：没批准就不该执行


def test_approve_resumes_and_runs(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "ap-2"}}

    graph.invoke(initial_state("统计一下总订单数"), config)
    final = graph.invoke(Command(resume={"action": "approve"}), config)

    assert final["approval_action"] == "approve"
    assert final["attempts"]
    assert final["row_count"] >= 1


def test_reject_stops_without_touching_the_database(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "ap-3"}}

    graph.invoke(initial_state("统计一下总订单数"), config)
    final = graph.invoke(Command(resume={"action": "reject"}), config)

    assert final["approval_action"] == "reject"
    assert "已取消执行" in final["answer"]
    assert not final.get("attempts")


def test_edit_runs_the_reviewed_sql(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "ap-4"}}

    graph.invoke(initial_state("统计一下总订单数"), config)
    safe_sql = "SELECT COUNT(*) AS n FROM orders WHERE order_date >= '2024-01-01'"
    final = graph.invoke(Command(resume={"action": "edit", "sql": safe_sql}), config)

    assert final["approval_action"] == "edit"
    assert final["sql"] == safe_sql
    assert final["attempts"][0]["sql"] == safe_sql


def test_safe_query_is_not_interrupted(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "ap-5"}}

    final = graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)

    assert _pending(graph, config) is None
    assert final["approval_required"] is False
    assert final["attempts"]