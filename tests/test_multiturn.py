from __future__ import annotations

from insight_agent.checkpoint import make_checkpointer
from insight_agent.config import Settings
from insight_agent.graph.build import build_graph
from insight_agent.runtime import Runtime
from insight_agent.state import initial_state


def _settings(workdir, **overrides) -> Settings:
    return Settings(db_path=workdir / "demo.sqlite", fake_llm=True, _env_file=None, **overrides)


def test_second_turn_is_rewritten_with_context(workdir):
    rt = Runtime.create(_settings(workdir))
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "mt-1"}}

    graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)
    final = graph.invoke(initial_state("那华北呢？"), config)

    assert final["raw_question"] == "那华北呢？"
    assert "华东" in final["question"]  # 确定性改写会接上上一轮
    assert final["standalone_question"] == final["question"]
    assert len(final["history"]) == 2
    assert final["history"][0]["question"] == "2024 年华东区销售额是多少？"


def test_counters_reset_each_turn(workdir):
    rt = Runtime.create(_settings(workdir))
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "mt-2"}}

    first = graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)
    second = graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)

    assert len(first["attempts"]) == 2
    assert len(second["attempts"]) == 2  # 不是 4：上一轮的尝试被 prepare 清掉了
    assert second["repair_attempts"] == 1


def test_sqlite_checkpointer_survives_restart(workdir):
    settings = _settings(workdir, checkpoint_path=workdir / "cp.db")
    config = {"configurable": {"thread_id": "persist-1"}}

    rt = Runtime.create(settings)
    saver = make_checkpointer(settings)
    graph = build_graph(rt, checkpointer=saver)
    graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)
    saver.conn.close()

    # 模拟"新进程"：重开连接、重建图，状态应该还在
    saver2 = make_checkpointer(settings)
    try:
        snapshot = build_graph(rt, checkpointer=saver2).get_state(config)
        history = snapshot.values["history"]
        assert len(history) == 1
        assert history[0]["question"] == "2024 年华东区销售额是多少？"
    finally:
        saver2.conn.close()


def test_delete_thread_clears_history(workdir):
    settings = _settings(workdir, checkpoint_path=workdir / "cp2.db")
    config = {"configurable": {"thread_id": "reset-1"}}
    rt = Runtime.create(settings)
    saver = make_checkpointer(settings)
    graph = build_graph(rt, checkpointer=saver)
    try:
        graph.invoke(initial_state("2024 年华东区销售额是多少？"), config)
        saver.delete_thread("reset-1")
        snapshot = graph.get_state(config)
        assert not (snapshot.values or {}).get("history")
    finally:
        saver.conn.close()
