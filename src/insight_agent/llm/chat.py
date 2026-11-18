"""千问（以及任何 OpenAI 兼容端点）的实现。

用 langchain_openai 的 ChatOpenAI，把 base_url 指向 DashScope 的兼容接口即可。
模型分工：强模型写 SQL，便宜的小模型做规划和校验——这是省 token 的关键。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings
from . import prompts
from .base import LLMError, LLMReply

VALID_VERDICTS = {
    "ok",
    "syntax_error",
    "runtime_error",
    "empty_result",
    "magnitude_anomaly",
    "semantic_mismatch",
}


def _friendly(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "401" in lowered or "invalid_api_key" in lowered or "invalid api-key" in lowered:
        return (
            "模型鉴权失败（401）。检查三件事：\n"
            "  1. .env 里的 INSIGHT_API_KEY 是不是这个 base_url 所属工作区的 Key；\n"
            "  2. Key 是否已过期/被禁用；\n"
            "  3. 复制时有没有多带空格或引号。\n"
            f"服务端原文：{text[:200]}"
        )
    if "404" in lowered or "model" in lowered and "not found" in lowered:
        return f"模型名或地址不对（INSIGHT_MODEL_NAME / INSIGHT_MODEL_BASE_URL）。服务端原文：{text[:200]}"
    if "429" in lowered or "rate" in lowered:
        return f"触发限流或额度用尽，稍后重试或换更便宜的模型。服务端原文：{text[:200]}"
    return f"调用模型失败：{text[:300]}"


def extract_json(text: str) -> dict:
    """模型很爱在 JSON 外面裹 ```json 或者加一句解释，这里做容错。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class ChatBackend:
    name = "chat"

    def __init__(self, settings: Settings):
        from langchain.chat_models import init_chat_model

        # init_chat_model 只认它内置的 provider 名；私有化部署统一走 openai 协议
        provider = "openai" if settings.model_provider in {"qwen", "openai", ""} else settings.model_provider
        options = {
            "model_provider": provider,
            "base_url": settings.model_base_url,
            "api_key": settings.api_key,
            "temperature": 0,
            "timeout": settings.llm_timeout_s,
            "max_retries": 2,
        }
        self.strong = init_chat_model(model=settings.model_name, **options)
        self.fast = init_chat_model(model=settings.fast_model_name or settings.model_name, **options)

    def _invoke(self, model, system: str, user: str) -> LLMReply:
        try:
            message = model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        except Exception as exc:  # 网络、鉴权、限流都从这里过一遍
            raise LLMError(_friendly(exc)) from exc
        usage = getattr(message, "usage_metadata", None) or {}
        content = message.content if isinstance(message.content, str) else str(message.content)
        return LLMReply(
            content,
            {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
        )

    def rewrite_question(self, question: str, history: list[dict[str, Any]]) -> LLMReply:
        context = "\n".join(
            f"用户：{h.get('question', '')}\n助手：{str(h.get('answer', ''))[:120]}" for h in history
        )
        system = prompts.REWRITE_SYSTEM.substitute(history=context or "（无）")
        reply = self._invoke(self.fast, system, f"新问题：{question}")
        data = extract_json(reply.value)
        rewritten = str(data.get("question") or "").strip() or question
        return LLMReply(rewritten, reply.usage)

    def extract_semantics(self, question: str) -> LLMReply:
        system = prompts.EXTRACT_SEMANTICS_SYSTEM.substitute(question=question)
        reply = self._invoke(self.fast, system, "请判断并抽取。")
        data = extract_json(reply.value)
        if not data.get("remember") or not str(data.get("metric") or "").strip():
            return LLMReply({}, reply.usage)
        return LLMReply(
            {
                "metric": str(data.get("metric", "")).strip(),
                "definition": str(data.get("definition", "")).strip(),
                "note": str(data.get("note", "")).strip() or "用户约定",
            },
            reply.usage,
        )

    def plan(self, question: str, schema_context: str) -> LLMReply:
        system = prompts.PLANNER_SYSTEM.substitute(schema=schema_context)
        reply = self._invoke(self.fast, system, f"问题：{question}")
        steps = extract_json(reply.value).get("steps")
        if not isinstance(steps, list) or not steps:
            steps = [line for line in str(reply.value).splitlines() if line.strip()][:5]
        return LLMReply([str(s) for s in steps], reply.usage)

    def generate_sql(
        self,
        question: str,
        schema_context: str,
        plan: list[str],
        semantics: str = "",
    ) -> LLMReply:
        system = prompts.SQL_GEN_SYSTEM.substitute(schema=schema_context, semantics=semantics or "")
        user = f"问题：{question}\n执行计划：{json.dumps(plan, ensure_ascii=False)}"
        reply = self._invoke(self.strong, system, user)
        sql = extract_json(reply.value).get("sql") or str(reply.value).strip()
        return LLMReply(sql, reply.usage)

    def repair_sql(self, sql: str, error: str, schema_context: str) -> LLMReply:
        system = prompts.REPAIR_SYSTEM.substitute(schema=schema_context, error=error, sql=sql)
        reply = self._invoke(self.strong, system, "请输出修正后的 SQL。")
        fixed = extract_json(reply.value).get("sql") or str(reply.value).strip()
        return LLMReply(fixed, reply.usage)

    def judge(
        self,
        question: str,
        sql: str,
        error: str | None,
        rows: list[list[Any]],
        semantics: str = "",
    ) -> LLMReply:
        system = prompts.CRITIC_SYSTEM.substitute(
            question=question,
            sql=sql,
            rows=json.dumps(rows[:3], ensure_ascii=False, default=str),
            semantics=semantics or "",
        )
        reply = self._invoke(self.fast, system, "请判断。")
        data = extract_json(reply.value)
        verdict = str(data.get("verdict", "")).strip()
        if verdict not in VALID_VERDICTS:
            verdict = "ok"
        return LLMReply({"verdict": verdict, "reason": str(data.get("reason", ""))}, reply.usage)
