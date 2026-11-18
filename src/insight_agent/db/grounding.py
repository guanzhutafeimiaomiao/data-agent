"""实体存在性探测。

"华为在华北的销售额"这类问题，SQL 能跑通，只是过滤不到任何行。问题不在 SQL，
在于库里根本没有"华为"。在执行层做一次廉价的反查，就能把"查不到"和"算错了"
区分开——前者不该进修复循环。
"""

from __future__ import annotations

import re

from .executor import Executor

# 日期、纯数字、百分比这类字面量不参与探测
_SKIP = re.compile(r"^[\d\s%.\-:/]+$")
_DATE = re.compile(r"^\d{4}(-\d{1,2}(-\d{1,2})?)?$")
_LITERAL = re.compile(r"'([^']*)'")


def candidate_tokens(sql: str, limit: int = 3) -> list[str]:
    """从 SQL 里挑出可能是业务实体的字面量。"""
    tokens: list[str] = []
    for raw in _LITERAL.findall(sql or ""):
        token = raw.strip().strip("%").strip()
        if len(token) < 2 or _SKIP.match(token) or _DATE.match(token):
            continue
        if token.lower() in {"paid", "refunded", "pending", "asc", "desc"}:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:limit]


def _exists(executor: Executor, token: str, tables: list[dict]) -> bool:
    safe = token.replace("'", "''")
    for table in tables:
        for column in table["columns"]:
            sql = (
                f'SELECT 1 FROM "{table["table"]}" '
                f"WHERE CAST(\"{column}\" AS TEXT) LIKE '%{safe}%' LIMIT 1"
            )
            try:
                result = executor.run(sql)
            except Exception:  # 探测失败不应影响主流程
                continue
            if result.error is None and result.rows:
                return True
    return False


def find_missing_literal(executor: Executor, sql: str, tables: list[dict]) -> str | None:
    """返回 SQL 里第一个在库中完全找不到的字面量；全都存在则返回 None。"""
    for token in candidate_tokens(sql):
        if not _exists(executor, token, tables):
            return token
    return None
