"""图的节点实现。

每个节点都很小，只做一件事并返回状态增量。真正复杂的是节点之间的边：
什么时候回头重试、什么时候放弃、什么时候交给人。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Overwrite, interrupt

from ..db import find_missing_literal
from ..memory import looks_like_remembering, parse_semantics_rule
from ..runtime import Runtime
from ..state import AnalysisState
from .policy import approval_reasons

CHAT_KEYWORDS = ("你好", "hi", "hello", "你是谁", "help", "怎么用")


def _trace(node: str, **detail: Any) -> dict[str, Any]:
    return {"node": node, **detail}


def prepare(state: AnalysisState, rt: Runtime) -> dict:
    """每轮开始先清空上一轮的中间结果。

    attempts / usage / trace 是累加器（reducer），直接传空值不会清空，必须用
    Overwrite 覆盖。history 不动——它是跨轮次的记忆，由 checkpointer 保管。
    """
    # 注意：必须先取 question。上一轮的 raw_question 还留在 checkpoint 里，
    # 如果先读它，这轮的新问题就会被旧问题顶掉。
    raw = (state.get("question") or state.get("raw_question") or "").strip()
    turn_no = len(state.get("history") or []) + 1
    return {
        "raw_question": raw,
        "question": raw,
        "standalone_question": raw,
        "rewritten": False,
        "intent": "",
        "plan": [],
        "sql": "",
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "exec_error": None,
        "elapsed_ms": 0,
        "verdict": "",
        "critique": "",
        "missing_entity": "",
        "repair_noop": False,
        "approval_required": False,
        "approval_reasons": [],
        "approval_action": "",
        "learned_metric": "",
        "learned_definition": "",
        "chart_spec": {},
        "answer": "",
        "evidence": {},
        "attempts": Overwrite([]),
        "repair_attempts": Overwrite(0),
        "usage": Overwrite({}),
        "trace": Overwrite([_trace("prepare", turn=turn_no)]),
    }


def rewrite(state: AnalysisState, rt: Runtime) -> dict:
    """把"那华北呢"补成完整问题。

    没有历史就跳过；改写失败（模型没按格式回）也退回原问题，
    宁可问得笨一点，也不能因为改写失败而答非所问。
    """
    raw = state.get("raw_question") or state.get("question", "")
    history = state.get("history") or []

    if not rt.settings.enable_rewrite or not history:
        return {
            "question": raw,
            "standalone_question": raw,
            "rewritten": False,
            "trace": [_trace("rewrite", skipped=True)],
        }

    reply = rt.llm.rewrite_question(raw, history[-rt.settings.history_turns :])
    rewritten_question = (reply.value or "").strip() or raw
    return {
        "question": rewritten_question,
        "standalone_question": rewritten_question,
        "rewritten": rewritten_question != raw,
        "usage": reply.usage,
        "trace": [_trace("rewrite", changed=rewritten_question != raw)],
    }


def route(state: AnalysisState, rt: Runtime) -> dict:
    question = (state.get("question") or "").strip()
    if looks_like_remembering(question):
        # 口径约定不是提问，别去生成 SQL
        intent = "remember"
    elif any(k in question.lower() for k in CHAT_KEYWORDS):
        intent = "chat"
    elif len(question) < 4:
        intent = "clarify"
    else:
        intent = "analyze"
    return {"intent": intent, "trace": [_trace("router", intent=intent)]}


def chat(state: AnalysisState, rt: Runtime) -> dict:
    answer = "我是数据助手：可以按时间、地区、品类等维度取数并给出结论。直接问，比如「2024 年华东区销售额是多少」。"
    return {"answer": answer, "trace": [_trace("chat")]}


def clarify(state: AnalysisState, rt: Runtime) -> dict:
    answer = "能再具体一点吗？至少告诉我：看什么指标、什么时间范围、哪个维度（地区/品类/渠道）。"
    return {"answer": answer, "verdict": "needs_clarification", "trace": [_trace("clarify")]}


def schema_retrieval(state: AnalysisState, rt: Runtime) -> dict:
    """MVP 全量读 schema；库大了这里换 BM25 + 向量召回 + LLM 列裁剪。"""
    context = "\n".join(f"- {t['table']}({', '.join(t['columns'])})" for t in rt.tables)
    return {
        "candidate_tables": rt.tables,
        "schema_context": context,
        "trace": [_trace("schema_retrieval", tables=len(rt.tables))],
    }


def recall_semantics(state: AnalysisState, rt: Runtime) -> dict:
    """把用户约定过的口径取出来，拼进后面生成和校验的提示词。

    每轮都重新读一次：用户可能刚刚才教会它一个新口径，
    下一问就该生效，不需要重启进程。
    """
    context = rt.semantics.render() if rt.settings.enable_semantics else ""
    learned = len(rt.semantics.all()) if rt.settings.enable_semantics else 0
    return {
        "semantics_context": context,
        "trace": [_trace("recall_semantics", learned=learned)],
    }


def learn(state: AnalysisState, rt: Runtime) -> dict:
    """把"我们公司客单价按客户数算"这类约定写进 Store。

    模型抽不出东西时退回确定性解析，两边都失败就如实说不明白，
    绝不瞎存一条错口径——错的口径比没有口径危险得多。
    """
    question = state.get("question") or state.get("raw_question") or ""
    usage: dict[str, int] = {}

    reply = rt.llm.extract_semantics(question)
    usage = reply.usage
    data = dict(reply.value or {})
    if not data.get("metric") or not data.get("definition"):
        data = parse_semantics_rule(question)

    metric = (data.get("metric") or "").strip()
    definition = (data.get("definition") or "").strip()

    if not metric or not definition:
        answer = (
            "没听出要记住什么口径。可以这样说：\n"
            "「以后客单价按客户数算」「记住：销售额不含折扣」。"
        )
        return {
            "answer": answer,
            "usage": usage,
            "trace": [_trace("learn", saved=False)],
        }

    saved = rt.semantics.remember(metric, definition, data.get("note", "用户约定"))
    answer = f"已记住：{metric} = {definition}。之后的分析都会按这个口径算。"
    return {
        "answer": answer,
        "learned_metric": saved["metric"],
        "learned_definition": saved["definition"],
        "semantics_context": rt.semantics.render(),
        "usage": usage,
        "trace": [_trace("learn", saved=True, metric=saved["metric"])],
    }


def plan(state: AnalysisState, rt: Runtime) -> dict:
    if not rt.settings.enable_plan:
        return {
            "plan": [f"直接回答：{state['question']}"],
            "trace": [_trace("plan", skipped=True)],
        }

    reply = rt.llm.plan(state["question"], state.get("schema_context", ""))
    return {
        "plan": reply.value,
        "usage": reply.usage,
        "trace": [_trace("plan", steps=len(reply.value or []), model=rt.llm.name)],
    }


def sql_gen(state: AnalysisState, rt: Runtime) -> dict:
    reply = rt.llm.generate_sql(
        state["question"],
        state.get("schema_context", ""),
        state.get("plan", []),
        state.get("semantics_context", ""),
    )
    sql = (reply.value or "").strip()
    return {
        "sql": sql,
        "exec_error": None,
        "usage": reply.usage,
        "trace": [_trace("sql_gen", chars=len(sql))],
    }


def approval(state: AnalysisState, rt: Runtime) -> dict:
    """高风险查询执行前暂停，等人点头。

    interrupt 的语义：第一次执行到这里会抛出中断、整个图挂起；恢复时这个节点
    **从头重跑**，interrupt() 换成调用方传进来的值。所以 interrupt 之前不能有副作用。
    """
    sql = state.get("sql", "")
    reasons = approval_reasons(sql) if rt.settings.approval_enabled else []

    if not reasons:
        return {
            "approval_required": False,
            "approval_reasons": [],
            "approval_action": "",
            "trace": [_trace("approval", skipped=True)],
        }

    decision = interrupt(
        {
            "type": "approval",
            "question": state.get("question", ""),
            "sql": sql,
            "reasons": reasons,
        }
    )

    if isinstance(decision, dict):
        action = str(decision.get("action") or "approve")
        if action == "edit":
            edited = str(decision.get("sql") or "").strip()
            return {
                "approval_required": True,
                "approval_reasons": reasons,
                "approval_action": "edit",
                "sql": edited or sql,
                "trace": [_trace("approval", action="edit")],
            }
        if action == "reject":
            return {
                "approval_required": True,
                "approval_reasons": reasons,
                "approval_action": "reject",
                "verdict": "rejected",
                "trace": [_trace("approval", action="reject")],
            }
        action = "approve"
    else:
        action = "approve"

    return {
        "approval_required": True,
        "approval_reasons": reasons,
        "approval_action": action,
        "trace": [_trace("approval", action=action)],
    }


def execute(state: AnalysisState, rt: Runtime) -> dict:
    result = rt.executor.run(state["sql"])
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "exec_error": result.error,
        "elapsed_ms": result.elapsed_ms,
        "trace": [_trace("execute", error=result.error, rows=result.row_count)],
    }


def _is_empty(rows: list[list[Any]]) -> bool:
    """没有行，或者没有任何"有意义的"值，都算没有数据。

    注意不能只看"整行都是 NULL"：SQL 常把过滤字面量一起 SELECT 出来，
    例如 ['华为', '华北', None]——前两列是回显，真正的指标是那个 NULL。
    判据是：这一行里既没有数值，也不是全部非空，那就等于没查到东西。
    """
    if not rows:
        return True

    def meaningful(row: list[Any]) -> bool:
        has_number = any(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in row
        )
        return has_number or all(value is not None for value in row)

    return not any(meaningful(row) for row in rows)


def _is_zero(rows: list[list[Any]]) -> bool:
    """单值 0：可能是真的 0，也可能是 COALESCE 把 NULL 抹平了，值得再查一下。"""
    return (
        len(rows) == 1
        and len(rows[0]) == 1
        and isinstance(rows[0][0], (int, float))
        and not isinstance(rows[0][0], bool)
        and rows[0][0] == 0
    )


def critic(state: AnalysisState, rt: Runtime) -> dict:
    """失败归因：确定性事实优先，模型只判断"语义对不对"。

    空结果的判断根本不需要模型——有没有行、是不是 NULL 是客观事实。
    只有非空结果才值得花一次调用，让模型看口径和量级。
    """
    attempt_no = state.get("repair_attempts", 0) + 1
    error = state.get("exec_error")
    rows = state.get("rows") or []
    sql = state.get("sql", "")
    usage: dict[str, int] = {}
    missing = ""

    if error:
        verdict = "syntax_error" if _is_schema_error(error) else "runtime_error"
        reason = f"执行失败：{error}"
    elif _is_empty(rows) or _is_zero(rows):
        empty = _is_empty(rows)
        verdict = "empty_result" if empty else "ok"
        reason = "结果为空：过滤条件下没有匹配的数据。" if empty else ""
        if rt.settings.enable_grounding:
            missing = find_missing_literal(rt.executor, sql, rt.tables) or ""
            if missing:
                verdict = "no_data"
                reason = f"数据库中没有与「{missing}」相关的记录。"
    else:
        reply = rt.llm.judge(
            state.get("question", ""),
            sql,
            error,
            rows,
            state.get("semantics_context", ""),
        )
        usage = reply.usage
        data = reply.value if isinstance(reply.value, dict) else {}
        verdict = str(data.get("verdict") or "ok")
        reason = str(data.get("reason") or "")
        if verdict in {"syntax_error", "runtime_error"}:
            verdict, reason = "ok", ""
        if verdict != "ok" and not reason:
            reason = f"校验未通过：{verdict}"

    attempt = {
        "n": attempt_no,
        "sql": sql,
        "error": error,
        "row_count": state.get("row_count", 0),
        "verdict": verdict,
    }
    return {
        "verdict": verdict,
        "critique": reason,
        "missing_entity": missing,
        "attempts": [attempt],
        "usage": usage,
        "trace": [_trace("critic", verdict=verdict)],
    }


def _is_schema_error(error: str) -> bool:
    lowered = error.lower()
    return "no such column" in lowered or "no such table" in lowered or "syntax" in lowered


def repair(state: AnalysisState, rt: Runtime) -> dict:
    reply = rt.llm.repair_sql(
        state.get("sql", ""),
        state.get("critique", ""),
        state.get("schema_context", ""),
    )
    fixed = (reply.value or state.get("sql", "")).strip()
    changed = fixed != (state.get("sql") or "").strip()
    return {
        "sql": fixed,
        "repair_attempts": 1,
        "repair_noop": not changed,
        "exec_error": None,
        "usage": reply.usage,
        "trace": [_trace("repair", changed=changed)],
    }


def visualize(state: AnalysisState, rt: Runtime) -> dict:
    columns, rows = state.get("columns", []), state.get("rows", [])
    if len(columns) == 1 and len(rows) == 1:
        spec = {"type": "metric", "label": columns[0], "value": rows[0][0]}
    elif len(columns) >= 2 and len(rows) > 1:
        spec = {"type": "bar", "x": columns[0], "y": columns[1]}
    else:
        spec = {"type": "table", "columns": columns}
    return {"chart_spec": spec, "trace": [_trace("visualize", chart=spec["type"])]}


def report(state: AnalysisState, rt: Runtime) -> dict:
    if state.get("verdict") == "no_data":
        missing = state.get("missing_entity") or "该条件"
        answer = (
            f"{state['question']}\n\n"
            f"未查询到数据：数据库中没有与「{missing}」相关的记录。\n"
            f"建议：确认名称是否正确，或换成品类 / 地区 / 渠道这类库里已有的维度再试。"
        )
        evidence = {
            "sql": state.get("sql", ""),
            "tables": [t["table"] for t in state.get("candidate_tables", [])],
            "row_count": 0,
            "sample_rows": [],
        }
        return {"answer": answer, "evidence": evidence, "trace": [_trace("report", no_data=True)]}

    if state.get("verdict") != "ok":
        return escalate(state, rt)

    columns, rows = state["columns"], state["rows"]
    if len(columns) == 1 and len(rows) == 1 and isinstance(rows[0][0], (int, float)):
        headline = f"{columns[0]} = {rows[0][0]:,.2f}"
    else:
        preview = "; ".join(" | ".join(str(c) for c in row) for row in rows[:3])
        headline = f"共 {state['row_count']} 行：{preview}"

    answer = (
        f"{state['question']}\n\n"
        f"结论：{headline}\n"
        f"口径：已支付订单（status='paid'），金额含折扣，按订单日期区间过滤。\n"
        f"来源：{state.get('sql', '').strip().splitlines()[0] if state.get('sql') else ''} …（完整 SQL 见证据）"
    )
    evidence = {
        "sql": state.get("sql", ""),
        "tables": [t["table"] for t in state.get("candidate_tables", [])],
        "row_count": state.get("row_count", 0),
        "sample_rows": rows[:3],
    }
    return {"answer": answer, "evidence": evidence, "trace": [_trace("report")]}


def escalate(state: AnalysisState, rt: Runtime) -> dict:
    """超过重试预算或无法自动修复时，交给人的不是"失败"，而是定位信息。

    但"确实没查到数据"不是故障，别用"修复没有收敛"吓人。
    """
    if state.get("verdict") == "rejected":
        answer = "已取消执行：这条查询没有经过批准，因此没有产生任何数据。"
        return {"answer": answer, "trace": [_trace("escalate", reason="rejected")]}

    if state.get("verdict") == "empty_result":
        answer = (
            f"{state.get('question', '')}\n\n"
            "未查询到符合条件的数据。\n"
            "建议：放宽时间范围、减少过滤条件，或确认筛选值是否正确。\n"
            f"最后一次 SQL：\n{state.get('sql', '')}"
        )
        return {"answer": answer, "trace": [_trace("escalate", reason="empty_result")]}

    answer = (
        "自动修复没有收敛，已停止重试。\n"
        f"最后一次错误：{state.get('exec_error') or state.get('critique')}\n"
        f"最后一次 SQL：\n{state.get('sql', '')}"
    )
    return {"answer": answer, "trace": [_trace("escalate")]}


def remember(state: AnalysisState, rt: Runtime) -> dict:
    """一轮结束，把这一问一答写进 history。

    只保留最近 N 轮：history 会随对话线性增长，不截断的话，
    改写和后续节点迟早会把上下文塞爆。
    """
    question = state.get("raw_question") or state.get("question", "")
    if not question:
        return {"trace": [_trace("remember", skipped=True)]}

    turn = {
        "question": question,
        "effective_question": state.get("question", ""),
        "answer": state.get("answer", ""),
        "sql": state.get("sql", ""),
    }
    history = list(state.get("history") or [])
    history.append(turn)

    keep = max(1, rt.settings.history_turns)
    return {"history": history[-keep:], "trace": [_trace("remember", turns=len(history[-keep:]))]}
