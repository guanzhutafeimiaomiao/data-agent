"""FastAPI + SSE：把图执行过程实时推给前端。

为什么用 SSE 而不是 WebSocket：这里是单向的"服务器推事件"，SSE 是纯文本、
自带断线重连语义、用 curl 就能调试；WebSocket 在这个场景是过度设计。
前端用 fetch + ReadableStream 解析，所以不需要 EventSource 的 GET 限制。

人工审批也复用这条流：图暂停时推一个 approval 事件，前端通过 /api/approve
把决定送回去，服务端用 Command(resume=...) 继续跑同一张图。
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel

from ..checkpoint import make_checkpointer
from ..config import Settings, get_settings
from ..graph.build import build_graph
from ..llm.base import LLMError
from ..runtime import Runtime
from ..state import initial_state

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

NODE_LABELS = {
    "prepare": "准备工作区",
    "rewrite": "理解追问",
    "router": "判断意图",
    "schema_retrieval": "读取表结构",
    "recall_semantics": "读取口径约定",
    "learn": "记住口径",
    "plan": "制定计划",
    "sql_gen": "生成 SQL",
    "approval": "等待审批",
    "execute": "执行查询",
    "critic": "校验结果",
    "repair": "修复 SQL",
    "visualize": "准备图表",
    "report": "组织结论",
    "remember": "记录上下文",
    "chat": "闲聊",
    "clarify": "请求澄清",
    "escalate": "转人工",
}


def _clip(text: Any, limit: int = 90) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _describe(node: str, delta: dict[str, Any]) -> dict[str, Any]:
    """把节点返回值压成前端能直接渲染的一行。"""
    label = NODE_LABELS.get(node, node)
    detail = ""
    tone = "info"

    if node == "prepare":
        # 注意：流式事件里拿到的是节点原始返回值，trace 是 Overwrite 对象，不能下标取
        detail = _clip(delta.get("raw_question"))
    elif node == "rewrite":
        if delta.get("rewritten"):
            detail = f"→ {_clip(delta.get('standalone_question'))}"
        else:
            detail = "无需改写"
    elif node == "router":
        detail = {
            "analyze": "数据分析",
            "chat": "闲聊",
            "clarify": "需要补充信息",
            "remember": "记住口径",
        }.get(delta.get("intent", ""), "")
    elif node == "learn":
        if delta.get("learned_metric"):
            detail = f"{delta['learned_metric']} = {_clip(delta.get('learned_definition'), 40)}"
            tone = "ok"
        else:
            detail, tone = "没听出口径", "warn"
    elif node == "recall_semantics":
        context = (delta.get("semantics_context") or "").strip()
        detail = f"{len(context.splitlines())} 条约定" if context else "无约定"
    elif node == "plan":
        steps = delta.get("plan") or []
        detail = "已跳过" if not steps else f"{len(steps)} 步"
    elif node == "sql_gen":
        detail = _clip(delta.get("sql"))
    elif node == "approval":
        if delta.get("approval_required"):
            detail = "已批准" if delta.get("approval_action") != "reject" else "已拒绝"
            tone = "warn" if delta.get("approval_action") == "reject" else "ok"
        else:
            detail = "无需审批"
    elif node == "execute":
        error = delta.get("exec_error")
        if error:
            detail, tone = _clip(error), "error"
        else:
            detail = f"{delta.get('row_count', 0)} 行 · {delta.get('elapsed_ms', 0)}ms"
            tone = "ok"
    elif node == "critic":
        verdict = delta.get("verdict", "")
        detail = verdict
        tone = "ok" if verdict == "ok" else ("warn" if verdict == "no_data" else "error")
    elif node == "repair":
        detail = "已重写" if delta.get("changed") else "未改动"
    elif node == "visualize":
        detail = (delta.get("chart_spec") or {}).get("type", "")
    elif node == "schema_retrieval":
        detail = f"{len(delta.get('candidate_tables') or [])} 张表"
    elif node == "remember":
        detail = f"history {len(delta.get('history') or [])} 轮"

    return {"node": node, "label": label, "detail": detail, "tone": tone}


def _final_payload(values: dict[str, Any]) -> dict[str, Any]:
    usage = values.get("usage") or {}
    return {
        "type": "final",
        "ok": values.get("verdict") == "ok",
        "question": values.get("standalone_question") or values.get("question"),
        "raw_question": values.get("raw_question"),
        "answer": values.get("answer") or "",
        "sql": values.get("sql") or "",
        "columns": values.get("columns") or [],
        "rows": (values.get("rows") or [])[:50],
        "chart": values.get("chart_spec") or {},
        "attempts": values.get("attempts") or [],
        "verdict": values.get("verdict"),
        "missing_entity": values.get("missing_entity") or "",
        "learned_metric": values.get("learned_metric") or "",
        "learned_definition": values.get("learned_definition") or "",
        "approval_action": values.get("approval_action") or "",
        "usage": usage,
        "tokens": sum(int(v) for v in usage.values() if isinstance(v, (int, float))),
        "history": len(values.get("history") or []),
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def pending_interrupt(graph, config: dict) -> dict[str, Any] | None:
    """图是否停在某个 interrupt 上；是的话把等待的内容取出来。"""
    snapshot = graph.get_state(config)
    for task in snapshot.tasks or ():
        for item in task.interrupts or ():
            value = getattr(item, "value", item)
            return value if isinstance(value, dict) else {"type": "approval", "detail": str(value)}
    return None


class AskBody(BaseModel):
    question: str
    thread_id: str = "web"


class ApproveBody(BaseModel):
    thread_id: str = "web"
    action: str = "approve"  # approve | reject | edit
    sql: str | None = None


class SemanticBody(BaseModel):
    metric: str
    definition: str
    note: str = "用户约定"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    runtime = Runtime.create(settings)
    saver = make_checkpointer(settings)
    graph = build_graph(runtime, checkpointer=saver)
    lock = threading.Lock()  # 单机演示：同一时刻只跑一轮，避免 SQLite 写冲突

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        saver.conn.close()

    app = FastAPI(title="InsightAgent", lifespan=lifespan)
    app.state.runtime = runtime

    def stream(graph_input: Any, config: dict, thread_id: str) -> Iterator[str]:
        """跑一段图并把事件推出去；停在 interrupt 上就发 approval，否则发 final。"""
        yield _sse({"type": "start", "thread_id": thread_id})
        try:
            with lock:
                for chunk in graph.stream(graph_input, config, stream_mode="updates"):
                    for node, delta in chunk.items():
                        if node.startswith("__"):
                            # __interrupt__ 内部事件：暂停统一由下面的 approval 事件表达
                            continue
                        yield _sse({"type": "node", **_describe(node, delta)})
                waiting = pending_interrupt(graph, config)
                values = graph.get_state(config).values or {}
        except LLMError as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return
        except Exception as exc:  # 兜底：任何异常都要让前端知道
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return

        if waiting:
            yield _sse({"type": "approval", "thread_id": thread_id, **waiting})
        else:
            yield _sse(_final_payload(values))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "model": settings.model_name,
            "backend": runtime.llm.name,
            "tables": [t["table"] for t in runtime.tables],
            "approval": settings.approval_enabled,
            "semantics": len(runtime.semantics.all()) if settings.enable_semantics else 0,
        }

    @app.post("/api/ask")
    def ask(body: AskBody) -> StreamingResponse:
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")

        config = {"configurable": {"thread_id": body.thread_id}}
        if pending_interrupt(graph, config):
            raise HTTPException(
                status_code=409,
                detail="这个会话还有一条查询等着审批，请先批准或拒绝它。",
            )
        return StreamingResponse(
            stream(initial_state(question), config, body.thread_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/approve")
    def approve(body: ApproveBody) -> StreamingResponse:
        config = {"configurable": {"thread_id": body.thread_id}}
        if not pending_interrupt(graph, config):
            raise HTTPException(status_code=409, detail="该会话当前没有待审批的查询")

        decision: dict[str, Any] = {"action": body.action}
        if body.sql is not None:
            decision["sql"] = body.sql

        return StreamingResponse(
            stream(Command(resume=decision), config, body.thread_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/threads/{thread_id}")
    def thread(thread_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = graph.get_state(config)
        history = (snapshot.values or {}).get("history") or []
        return {
            "thread_id": thread_id,
            "turns": len(history),
            "history": history,
            # 有值说明这个会话停在一个待审批的查询上，刷新页面还能接着批
            "pending": pending_interrupt(graph, config),
        }

    @app.delete("/api/threads/{thread_id}")
    def reset(thread_id: str) -> dict[str, Any]:
        saver.delete_thread(thread_id)
        return {"ok": True, "thread_id": thread_id}

    @app.get("/api/semantics")
    def list_semantics() -> dict[str, Any]:
        return {"items": runtime.semantics.all()}

    @app.post("/api/semantics")
    def add_semantic(body: SemanticBody) -> dict[str, Any]:
        saved = runtime.semantics.remember(body.metric, body.definition, body.note)
        return {"ok": True, "item": saved}

    @app.delete("/api/semantics/{metric}")
    def drop_semantic(metric: str) -> dict[str, Any]:
        runtime.semantics.forget(metric)
        runtime.semantics.seed()
        return {"ok": True, "metric": metric}

    return app