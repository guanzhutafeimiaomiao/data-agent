"""只读 SQL 执行器。

安全边界必须落在代码里，而不是写在 prompt 里。这里做了四件事：
单语句校验、只允许 SELECT/WITH、只读连接、超时与行数上限。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|replace|trigger)\b",
    re.IGNORECASE,
)


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    error: str | None = None
    elapsed_ms: int = 0
    truncated: bool = False


def check_sql(sql: str) -> str | None:
    """返回错误原因；None 表示通过。"""
    body = sql.strip().rstrip(";").strip()
    if not body:
        return "SQL 为空"
    if ";" in body:
        return "只允许单条语句（检测到分号分隔的多条语句）"
    if not re.match(r"^(select|with)\b", body, re.IGNORECASE):
        return "只允许 SELECT 或 WITH 查询"
    match = FORBIDDEN.search(body)
    if match:
        return f"包含被禁止的关键字：{match.group(0)}"
    return None


class Executor:
    def __init__(self, db_path: str | Path, row_limit: int = 200, timeout_ms: int = 8000):
        self.db_path = Path(db_path)
        self.row_limit = row_limit
        self.timeout_ms = timeout_ms

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.execute("PRAGMA query_only=ON")
        return con

    def run(self, sql: str) -> QueryResult:
        started = time.perf_counter()

        if reason := check_sql(sql):
            return QueryResult(error=reason, elapsed_ms=int((time.perf_counter() - started) * 1000))

        deadline = time.perf_counter() + self.timeout_ms / 1000.0
        con = self._connect()

        def _deadline_hit() -> int:
            return 1 if time.perf_counter() > deadline else 0

        con.set_progress_handler(_deadline_hit, 2000)
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(self.row_limit + 1)
            truncated = len(rows) > self.row_limit
            rows = rows[: self.row_limit]
            return QueryResult(
                columns=columns,
                rows=[list(r) for r in rows],
                row_count=len(rows),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                truncated=truncated,
            )
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupted" in message:
                message = f"查询超过 {self.timeout_ms}ms 被中断"
            return QueryResult(error=message, elapsed_ms=int((time.perf_counter() - started) * 1000))
        except sqlite3.Error as exc:  # 语法错误等
            return QueryResult(error=str(exc), elapsed_ms=int((time.perf_counter() - started) * 1000))
        finally:
            con.close()
