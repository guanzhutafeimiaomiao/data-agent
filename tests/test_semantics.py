from __future__ import annotations

from insight_agent.config import Settings
from insight_agent.graph.build import build_graph
from insight_agent.memory import SemanticStore, parse_semantics_rule
from insight_agent.runtime import Runtime
from insight_agent.state import initial_state


def _runtime(workdir, **overrides) -> Runtime:
    settings = Settings(
        db_path=workdir / "demo.sqlite",
        store_path=workdir / "store.db",
        checkpoint_path=workdir / "cp.db",
        fake_llm=True,
        approval_enabled=False,
        _env_file=None,
        **overrides,
    )
    return Runtime.create(settings)


def test_default_conventions_are_seeded(workdir):
    rt = _runtime(workdir)
    items = {item["metric"]: item for item in rt.semantics.all()}

    assert "客单价" in items
    assert "订单数" in items["客单价"]["definition"]
    assert "客单价 = " in rt.semantics.render()


def test_parse_recognises_the_aov_convention():
    rule = parse_semantics_rule("以后我们公司客单价按客户数算")
    assert rule["metric"] == "客单价"
    assert "客户数" in rule["definition"]

    rule2 = parse_semantics_rule("以后客单价按订单数算")
    assert "订单数" in rule2["definition"]

    assert parse_semantics_rule("2024 年华东的客单价是多少").get("definition") == ""


def test_learn_writes_to_store(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "sm-1"}}

    final = graph.invoke(initial_state("以后客单价按客户数算"), config)

    assert final["intent"] == "remember"
    assert final["learned_metric"] == "客单价"
    assert "客户数" in rt.semantics.get("客单价")["definition"]
    assert "已记住" in final["answer"]
    assert not final.get("attempts")  # 记住口径不该去查数据库


def test_memory_changes_the_next_sql(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "sm-2"}}

    before = graph.invoke(initial_state("2024 年华东的客单价是多少？"), config)
    assert "COUNT(DISTINCT o.id)" in before["sql"]

    graph.invoke(initial_state("以后客单价按客户数算"), config)

    after = graph.invoke(initial_state("2024 年华东的客单价是多少？"), config)
    assert "COUNT(DISTINCT o.customer_id)" in after["sql"]
    assert after["semantics_context"].count("客单价") == 1


def test_unknown_statement_does_not_overwrite(workdir):
    rt = _runtime(workdir)
    graph = build_graph(rt)
    config = {"configurable": {"thread_id": "sm-3"}}

    original = rt.semantics.get("客单价")["definition"]
    final = graph.invoke(initial_state("记住这个表很重要"), config)

    assert "没听出" in final["answer"]
    assert rt.semantics.get("客单价")["definition"] == original


def test_store_survives_a_new_runtime(workdir):
    first = _runtime(workdir)
    first.semantics.remember("客单价", "销售额 / 客户数", "测试")

    second = _runtime(workdir)
    assert "客户数" in second.semantics.get("客单价")["definition"]


def test_manual_edit_and_forget(workdir):
    rt = _runtime(workdir)
    store: SemanticStore = rt.semantics

    store.remember("销售额", "SUM(amount)", "手工维护")
    assert store.get("销售额")["definition"] == "SUM(amount)"

    store.forget("销售额")
    store.seed()
    assert store.get("销售额")["definition"].startswith("SUM(quantity")