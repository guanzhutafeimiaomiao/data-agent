"""把节点连成图。

这张图有四个"回头路"和一个"出口"：
- prepare → rewrite：每轮开始清状态、补全追问；
- critic → repair → execute 的自纠错环，是项目的主线；
- 超过重试预算 → escalate，保证不会无限烧钱；
- 无法分析 → clarify，而不是硬编一个 SQL。

结束前统一走 remember，把这一轮写进 history——下一次调用靠 checkpointer 恢复。
"""

from __future__ import annotations

from functools import partial

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from ..runtime import Runtime
from ..state import AnalysisState
from . import nodes


def after_router(state: AnalysisState) -> str:
    return state.get("intent", "analyze")


def after_approval(state: AnalysisState) -> str:
    """拒绝就到此为止，批准（或改完 SQL）才继续执行。"""
    return "escalate" if state.get("approval_action") == "reject" else "execute"


def after_critic(state: AnalysisState, rt: Runtime) -> str:
    verdict = state.get("verdict")
    if verdict == "ok":
        return "visualize"
    if verdict == "no_data":
        # 库里就没有这个东西，修 SQL 也修不出来，直接给"未查询到"
        return "report"
    if verdict == "needs_clarification":
        return "escalate"
    if state.get("repair_noop"):
        # 修复没有改动 SQL，再执行一遍只会重复同一个错误
        return "escalate"
    if state.get("repair_attempts", 0) >= rt.settings.max_repair:
        return "escalate"
    return "repair"


def build_graph(rt: Runtime, checkpointer=None):
    builder = StateGraph(AnalysisState)

    builder.add_node("prepare", partial(nodes.prepare, rt=rt))
    builder.add_node("rewrite", partial(nodes.rewrite, rt=rt))
    builder.add_node("router", partial(nodes.route, rt=rt))
    builder.add_node("chat", partial(nodes.chat, rt=rt))
    builder.add_node("clarify", partial(nodes.clarify, rt=rt))
    builder.add_node("learn", partial(nodes.learn, rt=rt))
    builder.add_node("schema_retrieval", partial(nodes.schema_retrieval, rt=rt))
    builder.add_node("recall_semantics", partial(nodes.recall_semantics, rt=rt))
    builder.add_node("plan", partial(nodes.plan, rt=rt))
    builder.add_node("sql_gen", partial(nodes.sql_gen, rt=rt))
    builder.add_node("approval", partial(nodes.approval, rt=rt))
    builder.add_node("execute", partial(nodes.execute, rt=rt))
    builder.add_node("critic", partial(nodes.critic, rt=rt))
    builder.add_node("repair", partial(nodes.repair, rt=rt))
    builder.add_node("visualize", partial(nodes.visualize, rt=rt))
    builder.add_node("report", partial(nodes.report, rt=rt))
    builder.add_node("escalate", partial(nodes.escalate, rt=rt))
    builder.add_node("remember", partial(nodes.remember, rt=rt))

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "rewrite")
    builder.add_edge("rewrite", "router")
    builder.add_conditional_edges(
        "router",
        after_router,
        {
            "analyze": "schema_retrieval",
            "chat": "chat",
            "clarify": "clarify",
            "remember": "learn",
        },
    )
    builder.add_edge("chat", END)
    builder.add_edge("clarify", END)
    builder.add_edge("learn", "remember")
    builder.add_edge("schema_retrieval", "recall_semantics")
    builder.add_edge("recall_semantics", "plan")
    builder.add_edge("plan", "sql_gen")
    builder.add_edge("sql_gen", "approval")
    builder.add_conditional_edges(
        "approval",
        after_approval,
        {"execute": "execute", "escalate": "escalate"},
    )
    builder.add_edge("execute", "critic")
    builder.add_conditional_edges(
        "critic",
        partial(after_critic, rt=rt),
        {"visualize": "visualize", "report": "report", "repair": "repair", "escalate": "escalate"},
    )
    builder.add_edge("repair", "execute")
    builder.add_edge("visualize", "report")
    builder.add_edge("report", "remember")
    builder.add_edge("escalate", "remember")
    builder.add_edge("remember", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())
