from __future__ import annotations

import json

from fastapi.testclient import TestClient

from insight_agent.api.app import create_app
from insight_agent.config import Settings


def _client(workdir, **overrides) -> TestClient:
    settings = Settings(
        db_path=workdir / "demo.sqlite",
        checkpoint_path=workdir / "cp.db",
        store_path=workdir / "store.db",
        fake_llm=True,
        _env_file=None,
        **overrides,
    )
    return TestClient(create_app(settings))


def _events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _ask(client: TestClient, question: str, thread_id: str) -> list[dict]:
    with client.stream("POST", "/api/ask", json={"question": question, "thread_id": thread_id}) as r:
        assert r.status_code == 200
        return _events("".join(r.iter_text()))


def _approve(client: TestClient, thread_id: str, action: str, sql: str | None = None) -> list[dict]:
    body = {"thread_id": thread_id, "action": action}
    if sql is not None:
        body["sql"] = sql
    with client.stream("POST", "/api/approve", json=body) as r:
        assert r.status_code == 200
        return _events("".join(r.iter_text()))


def test_health_reports_backend_and_tables(workdir):
    with _client(workdir) as client:
        body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["backend"] == "fake"
    assert "order_items" in body["tables"]
    assert body["approval"] is False  # conftest 默认关掉
    assert body["semantics"] >= 3


def test_index_serves_the_ui(workdir):
    with _client(workdir) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "InsightAgent" in response.text


def test_ask_streams_node_events_and_final(workdir):
    with _client(workdir) as client:
        events = _ask(client, "2024 年华东区销售额是多少？", "t1")

    types = [event["type"] for event in events]
    assert types[0] == "start"
    assert "final" in types
    assert any(e["type"] == "node" and e["node"] == "execute" for e in events)

    final = next(e for e in events if e["type"] == "final")
    assert final["ok"] is True
    assert final["rows"][0][0] > 0
    assert final["attempts"]


def test_thread_history_and_reset(workdir):
    with _client(workdir) as client:
        _ask(client, "2024 年华东区销售额是多少？", "t2")

        history = client.get("/api/threads/t2").json()
        assert history["turns"] == 1
        assert history["history"][0]["question"] == "2024 年华东区销售额是多少？"

        assert client.delete("/api/threads/t2").json()["ok"] is True
        assert client.get("/api/threads/t2").json()["turns"] == 0


def test_risky_query_streams_an_approval_event(workdir):
    with _client(workdir, approval_enabled=True) as client:
        events = _ask(client, "统计一下总订单数", "t3")

        types = [event["type"] for event in events]
        assert "approval" in types
        assert "final" not in types  # 还没批准，不该有结论

        approval = next(e for e in events if e["type"] == "approval")
        assert approval["type"] == "approval"
        assert approval["reasons"]
        assert "SELECT" in approval["sql"]

        assert client.get("/api/threads/t3").json()["pending"]["type"] == "approval"


def test_approve_streams_the_rest(workdir):
    with _client(workdir, approval_enabled=True) as client:
        _ask(client, "统计一下总订单数", "t4")
        events = _approve(client, "t4", "approve")

        final = next(e for e in events if e["type"] == "final")
        assert final["ok"] is True
        assert final["approval_action"] == "approve"
        assert client.get("/api/threads/t4").json()["pending"] is None


def test_reject_returns_a_clear_answer(workdir):
    with _client(workdir, approval_enabled=True) as client:
        _ask(client, "统计一下总订单数", "t5")
        final = next(e for e in _approve(client, "t5", "reject") if e["type"] == "final")

        assert "已取消执行" in final["answer"]
        assert final["attempts"] == []


def test_approve_without_pending_returns_409(workdir):
    with _client(workdir) as client:
        response = client.post("/api/approve", json={"thread_id": "nothing", "action": "approve"})
    assert response.status_code == 409


def test_semantics_endpoints(workdir):
    with _client(workdir) as client:
        items = client.get("/api/semantics").json()["items"]
        assert any(item["metric"] == "客单价" for item in items)

        client.post("/api/semantics", json={"metric": "客单价", "definition": "销售额 / 客户数"})
        updated = {i["metric"]: i for i in client.get("/api/semantics").json()["items"]}
        assert "客户数" in updated["客单价"]["definition"]

        client.delete("/api/semantics/客单价")
        restored = {i["metric"]: i for i in client.get("/api/semantics").json()["items"]}
        assert "订单数" in restored["客单价"]["definition"]


def test_learn_end_to_end_changes_next_sql(workdir):
    with _client(workdir) as client:
        learned = next(
            e for e in _ask(client, "以后客单价按客户数算", "t6") if e["type"] == "final"
        )
        assert learned["learned_metric"] == "客单价"

        final = next(e for e in _ask(client, "2024 年华东的客单价是多少？", "t6") if e["type"] == "final")
        assert "COUNT(DISTINCT o.customer_id)" in final["sql"]