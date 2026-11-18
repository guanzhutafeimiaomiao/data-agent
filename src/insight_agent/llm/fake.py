"""无 Key 时的确定性实现。

它们和真模型实现同一套接口（见 base.py），所以从 fake 切到千问只需要换实现，
图结构一行都不用动。这也是评测里的 baseline：接模型后要能量化"比规则强多少"。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .base import LLMReply


def plan_for(question: str) -> list[str]:
    return [
        "定位与问题相关的表与关联键",
        "确定时间、地区、状态等过滤口径",
        "计算指标并按问题要求聚合",
    ]


def aov_sql(question: str, semantics: str) -> str | None:
    """客单价的确定性实现，按记忆里的口径决定除的是订单数还是客户数。

    这是"口径记忆真的生效"的最小证据：同一句问题，记忆不同 → SQL 不同。
    """
    if "客单价" not in question and "平均单价" not in question:
        return None

    divisor = "COUNT(DISTINCT o.id)"
    if "客户数" in (semantics or ""):
        divisor = "COUNT(DISTINCT o.customer_id)"

    where = ["o.status = 'paid'"]
    if "华东" in question:
        where.append("c.region = '华东'")
    if "华北" in question:
        where.append("c.region = '华北'")
    for year in ("2022", "2023", "2024"):
        if year in question:
            where.append(f"o.order_date >= '{year}-01-01' AND o.order_date < '{int(year) + 1}-01-01'")
            break

    return (
        "SELECT ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) * 1.0 "
        f"/ {divisor}, 2) AS aov\n"
        "FROM order_items oi\n"
        "JOIN orders o ON o.id = oi.order_id\n"
        "JOIN customers c ON c.id = o.customer_id\n"
        f"WHERE {' AND '.join(where)}"
    )


def naive_sql(question: str) -> str:
    """模拟"不看 schema 直接写"的模型：列名靠猜，所以会写错。

    这是真实 LLM 最常见的失败模式，用它来触发一次自纠错，正好验证回路。
    """
    if "华东" in question and "2024" in question:
        return (
            "SELECT ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount_rate)), 2) AS revenue\n"
            "FROM order_items oi\n"
            "JOIN orders o ON o.id = oi.order_id\n"
            "JOIN customers c ON c.id = o.customer_id\n"
            "WHERE c.region = '华东' AND o.status = 'paid'\n"
            "  AND o.order_date >= '2024-01-01' AND o.order_date < '2025-01-01'"
        )
    return "SELECT COUNT(*) AS n FROM orders"


def repair_sql(sql: str, error: str, tables: list[dict]) -> str:
    """按错误信息做 schema 感知的重写。

    用编辑距离找最接近的真实列名——可解释、可复现的基线策略。
    """
    match = re.search(r"no such column:\s*([\w.]+)", error or "", re.IGNORECASE)
    if not match:
        return sql

    bad = match.group(1).split(".")[-1]
    candidates = sorted({col for table in tables for col in table["columns"]})
    close = difflib.get_close_matches(bad, candidates, n=1, cutoff=0.5)
    if not close:
        return sql
    return re.sub(rf"\b{re.escape(bad)}\b", close[0], sql)


def judge_rule(error: str | None, rows: list[list[Any]]) -> dict[str, str]:
    if error:
        lowered = error.lower()
        if "no such column" in lowered or "no such table" in lowered or "syntax" in lowered:
            return {"verdict": "syntax_error", "reason": f"执行失败：{error}"}
        return {"verdict": "runtime_error", "reason": f"执行失败：{error}"}

    if not rows:
        return {"verdict": "empty_result", "reason": "结果为空：过滤条件可能过严或取值口径不对。"}

    if len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        if isinstance(value, (int, float)) and abs(value) > 1e9:
            return {"verdict": "magnitude_anomaly", "reason": "结果量级异常：检查是否重复 JOIN 或指标口径错误。"}

    return {"verdict": "ok", "reason": ""}


def rewrite_rule(question: str, history: list[dict]) -> str:
    """确定性改写：短问句且没带年份时，把上一轮的问题接上。

    "那华北呢" 这种省略句，规则就能修好大半；真正的语义改写交给模型。
    """
    if not history:
        return question
    last = (history[-1].get("question") or "").strip()
    if not last:
        return question

    has_year = bool(re.search(r"(19|20)\d{2}", question))
    looks_elliptical = len(question) <= 12 or question.startswith(("那", "呢", "再", "还有"))
    if looks_elliptical and not has_year:
        return f"{last}（补充条件：{question}）"
    return question


class FakeBackend:
    name = "fake"

    def __init__(self, tables: list[dict]):
        self.tables = tables

    def rewrite_question(self, question: str, history: list[dict]) -> LLMReply:
        return LLMReply(rewrite_rule(question, history))

    def extract_semantics(self, question: str) -> LLMReply:
        from ..memory import parse_semantics_rule

        rule = parse_semantics_rule(question)
        if not rule.get("metric") or not rule.get("definition"):
            return LLMReply({})
        return LLMReply(rule)

    def plan(self, question: str, schema_context: str) -> LLMReply:
        return LLMReply(plan_for(question))

    def generate_sql(
        self,
        question: str,
        schema_context: str,
        plan: list[str],
        semantics: str = "",
    ) -> LLMReply:
        return LLMReply(aov_sql(question, semantics) or naive_sql(question))

    def repair_sql(self, sql: str, error: str, schema_context: str) -> LLMReply:
        return LLMReply(repair_sql(sql, error, self.tables))

    def judge(
        self,
        question: str,
        sql: str,
        error: str | None,
        rows: list[list[Any]],
        semantics: str = "",
    ) -> LLMReply:
        return LLMReply(judge_rule(error, rows))
