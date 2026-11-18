"""图的状态定义。

这是整个项目的"接口"，比节点代码更重要：状态里放什么，决定了你能不能做
断点续跑、多轮追问、失败归因和成本核算。所有节点只读写这里声明的字段。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Verdict = Literal[
    "ok",
    "no_data",
    "rejected",
    "syntax_error",
    "runtime_error",
    "empty_result",
    "magnitude_anomaly",
    "semantic_mismatch",
    "needs_clarification",
]


class SQLAttempt(TypedDict, total=False):
    """一次 SQL 尝试的完整记录。没有它，critic 就只能看到最后一次。"""

    n: int
    sql: str
    error: str | None
    row_count: int
    verdict: str


class Evidence(TypedDict, total=False):
    """报告里每个结论都要能回溯到这里，这是"可解释"的落地方式。"""

    sql: str
    tables: list[str]
    row_count: int
    sample_rows: list[list[Any]]


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """token / 成本按节点累加，用于回答"这次问题花了多少钱"。"""
    out = dict(left or {})
    for key, value in (right or {}).items():
        out[key] = out.get(key, 0) + value
    return out


class Turn(TypedDict, total=False):
    """一轮对话。history 里存的就是它，追问改写靠它还原上下文。"""

    question: str  # 用户原话
    effective_question: str  # 改写后的完整问题
    answer: str
    sql: str


class AnalysisState(TypedDict, total=False):
    # ---- 输入 ----
    question: str  # 当前生效的问题（可能是改写后的）
    raw_question: str  # 用户原话
    standalone_question: str  # 改写结果，便于前端展示"我问的 vs 它理解的"
    rewritten: bool  # 这一轮是否真的发生了改写
    db_id: str
    history: list[Turn]  # 由 checkpointer 按 thread 持久化

    # ---- 路由 ----
    intent: Literal["analyze", "chat", "clarify", "reject", "remember"]

    # ---- 口径记忆 ----
    semantics_context: str  # 已生效的口径约定，拼进生成 / 校验的提示词
    learned_metric: str  # 本轮刚记住的指标
    learned_definition: str

    # ---- 人工审批 ----
    approval_required: bool
    approval_reasons: list[str]
    approval_action: Literal["approve", "edit", "reject", ""]

    # ---- schema 检索 ----
    candidate_tables: list[dict[str, Any]]
    schema_context: str

    # ---- 规划 ----
    plan: list[str]

    # ---- 生成 / 执行 ----
    sql: str
    attempts: Annotated[list[SQLAttempt], operator.add]
    repair_attempts: Annotated[int, operator.add]
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    exec_error: str | None
    elapsed_ms: int

    # ---- 校验 ----
    verdict: Verdict
    critique: str
    missing_entity: str  # 库里找不到的业务实体（"华为"）
    repair_noop: bool  # 修复没有改动 SQL，再试也没意义

    # ---- 输出 ----
    chart_spec: dict[str, Any]
    answer: str
    evidence: Evidence

    # ---- 运行信息 ----
    trace: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[dict[str, int], add_usage]


def initial_state(question: str, db_id: str = "demo") -> AnalysisState:
    """一轮对话的输入。

    只带"这一轮"的输入：上一轮的 attempts / usage 由 prepare 节点清空，
    history 由 checkpointer 按 thread 恢复。所以同一个 thread 上连续调用，
    天然就是一问一答的会话，而不是每次从零开始。
    """
    return AnalysisState(question=question, db_id=db_id)
