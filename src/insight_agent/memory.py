"""口径记忆（Store）。

数据助手最烦的一件事：同一个指标，每个公司口径不同。默认口径写在 prompt 里能解决
80% 的情况，但"我们公司客单价按客户数算"这种个性化约定，必须能记住、并且下一轮
自动生效——否则用户每问一次都要重复解释一遍。

存储用 LangGraph 的 Store（带命名空间和 key），和 checkpointer 分开：
- checkpointer 存**对话状态**，按 thread 隔离；
- store 存**跨会话的知识**，全局共享。

这个划分很重要：换成另一个会话问同样的问题，口径约定不该丢。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

NAMESPACE = ("semantics", "metric")

# 默认口径：没有用户约定时用它，也是 learn 覆盖的基线
DEFAULT_SEMANTICS: list[dict[str, str]] = [
    {
        "metric": "销售额",
        "definition": "SUM(quantity * unit_price * (1 - discount))",
        "note": "只统计已支付订单，金额含折扣",
    },
    {
        "metric": "订单数",
        "definition": "COUNT(DISTINCT orders.id)",
        "note": "按订单去重",
    },
    {
        "metric": "客单价",
        "definition": "销售额 / 订单数",
        "note": "公司默认按订单数摊",
    },
]

# 用户说这些话时，认为是在交代口径，而不是在提问
REMEMBER_HINTS = ("记住", "以后", "我们公司", "我们这边", "统一按", "默认按", "改成", "口径是")


def looks_like_remembering(question: str) -> bool:
    return any(hint in question for hint in REMEMBER_HINTS)


def parse_semantics_rule(question: str) -> dict[str, str]:
    """从一句话里抽出「指标 + 定义」。

    确定性版本，作为无 Key / 评测时的基线：找已知指标名，再看定义落在哪一边。
    真模型走 llm.extract_semantics，两者结果结构一致。
    """
    text = (question or "").strip()
    known = [item["metric"] for item in DEFAULT_SEMANTICS]
    metric = next((name for name in known if name in text), "")

    if "客单价" in text or "平均单价" in text:
        metric = "客单价"
        if "客户数" in text or "用户数" in text:
            return {"metric": metric, "definition": "销售额 / 客户数", "note": "用户约定：按客户数摊"}
        if "订单数" in text or "单量" in text:
            return {"metric": metric, "definition": "销售额 / 订单数", "note": "用户约定：按订单数摊"}
        return {"metric": metric, "definition": "", "note": "只提到了指标，没说口径"}

    match = re.search(r"(?:按|用|改成|统一按)\s*([^\s，。,]+)", text)
    definition = match.group(1) if match else ""
    if metric and definition:
        return {"metric": metric, "definition": definition, "note": "用户约定"}
    return {"metric": metric, "definition": "", "note": ""}


class SemanticStore:
    """口径约定的读写门面。内部是 LangGraph Store，外部只看到 dict。"""

    def __init__(self, store: Any):
        self.store = store

    # ---- 读 ----
    def all(self) -> list[dict[str, str]]:
        items = []
        for item in self.store.search(NAMESPACE, limit=100):
            value = dict(item.value or {})
            value["key"] = item.key
            items.append(value)
        return sorted(items, key=lambda item: item.get("metric", ""))

    def get(self, metric: str) -> dict[str, str] | None:
        item = self.store.get(NAMESPACE, metric)
        return dict(item.value) if item else None

    def render(self) -> str:
        """给提示词用的文本块。空的时候返回空串，避免 prompt 里出现无意义的标题。"""
        items = self.all()
        if not items:
            return ""
        lines = [f"- {item['metric']} = {item['definition']}" for item in items if item.get("definition")]
        return "\n".join(lines)

    # ---- 写 ----
    def remember(self, metric: str, definition: str, note: str = "") -> dict[str, str]:
        metric = (metric or "").strip()
        if not metric:
            raise ValueError("metric 不能为空")
        value = {
            "metric": metric,
            "definition": (definition or "").strip(),
            "note": note or "用户约定",
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.store.put(NAMESPACE, metric, value)
        return value

    def forget(self, metric: str) -> bool:
        self.store.delete(NAMESPACE, metric)
        return True

    def seed(self, items: list[dict[str, str]] | None = None) -> int:
        """写入默认口径。已有同名条目就跳过——用户约定不该被默认值覆盖。"""
        written = 0
        for item in items if items is not None else DEFAULT_SEMANTICS:
            if self.get(item["metric"]):
                continue
            self.remember(item["metric"], item["definition"], item.get("note", "默认口径"))
            written += 1
        return written