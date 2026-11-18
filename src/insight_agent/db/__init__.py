"""数据层：演示库生成 + 只读执行器 + 实体探测。"""

from .demo_db import build_demo_db, table_schema
from .executor import Executor, QueryResult, check_sql
from .grounding import candidate_tokens, find_missing_literal

__all__ = [
    "build_demo_db",
    "table_schema",
    "Executor",
    "QueryResult",
    "check_sql",
    "candidate_tokens",
    "find_missing_literal",
]
