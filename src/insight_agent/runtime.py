"""把配置、数据库、执行器、模型、口径记忆装配到一起，再交给图。"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings
from .db import Executor, build_demo_db, table_schema
from .llm.base import Backend
from .llm.fake import FakeBackend
from .memory import SemanticStore


@dataclass
class Runtime:
    settings: Settings
    executor: Executor
    tables: list[dict]
    llm: Any
    semantics: SemanticStore

    @classmethod
    def create(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        path = build_demo_db(settings.resolved_db_path())

        con = sqlite3.connect(path)
        try:
            tables = table_schema(con)
        finally:
            con.close()

        executor = Executor(path, row_limit=settings.row_limit, timeout_ms=settings.query_timeout_ms)
        return cls(
            settings=settings,
            executor=executor,
            tables=tables,
            llm=cls._build_backend(settings, tables),
            semantics=cls._build_semantics(settings),
        )

    @staticmethod
    def _build_backend(settings: Settings, tables: list[dict]) -> Backend:
        """没有 Key 也能开发：自动退回确定性实现，而不是直接崩。"""
        if settings.fake_llm or not settings.api_key:
            if not settings.fake_llm:
                print(
                    "[insight] 未配置 INSIGHT_API_KEY，本次使用确定性实现（fake）运行。",
                    file=sys.stderr,
                )
            return FakeBackend(tables)

        from .llm.chat import ChatBackend

        return ChatBackend(settings)

    @staticmethod
    def _build_semantics(settings: Settings) -> SemanticStore:
        if settings.enable_semantics:
            from .checkpoint import make_store

            store = SemanticStore(make_store(settings))
        else:
            from langgraph.store.memory import InMemoryStore

            store = SemanticStore(InMemoryStore())
        store.seed()
        return store