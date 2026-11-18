"""审批策略：什么样的查询值得停下来问人。

原则是**规则可解释、可测试**，不要"看模型心情"。这里只用两条确定性规则，
它们对应真实的数据风险：
1. 没有 WHERE：全表扫描，大库上可能几十秒 + 锁表；
2. 没有时间过滤：跨全历史的聚合，结果没法核对，也最容易被误用。

两条都不命中就直接执行——大多数正常提问不该被打断，否则人很快就学会闭眼点"同意"。
"""

from __future__ import annotations

import re

_WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)
_TIME = re.compile(r"order_date|order_time|created_at|date\(", re.IGNORECASE)


def approval_reasons(sql: str) -> list[str]:
    body = (sql or "").strip()
    if not body:
        return ["SQL 为空"]

    reasons: list[str] = []
    if not _WHERE.search(body):
        reasons.append("没有 WHERE 条件，会扫描整张表")
    if not _TIME.search(body):
        reasons.append("没有限定时间范围，会统计全部历史数据")
    return reasons


def needs_approval(sql: str, enabled: bool = True) -> bool:
    return enabled and bool(approval_reasons(sql))