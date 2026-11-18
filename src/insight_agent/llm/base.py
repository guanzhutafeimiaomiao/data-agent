"""模型层的公共契约。

图只认这几个方法，不认具体是哪个厂商。fake 和真模型实现同一套接口，
所以接千问不需要动图谱一行代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """模型调用失败（鉴权、限流、网络）。消息是给人看的，不是堆栈。"""


@dataclass
class LLMReply:
    """一次模型调用的结果 + 这次花了多少 token。"""

    value: Any
    usage: dict[str, int] = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def rewrite_question(self, question: str, history: list[dict[str, Any]]) -> LLMReply: ...

    def extract_semantics(self, question: str) -> LLMReply: ...

    def plan(self, question: str, schema_context: str) -> LLMReply: ...

    def generate_sql(
        self,
        question: str,
        schema_context: str,
        plan: list[str],
        semantics: str = "",
    ) -> LLMReply: ...

    def repair_sql(self, sql: str, error: str, schema_context: str) -> LLMReply: ...

    def judge(
        self,
        question: str,
        sql: str,
        error: str | None,
        rows: list[list[Any]],
        semantics: str = "",
    ) -> LLMReply: ...