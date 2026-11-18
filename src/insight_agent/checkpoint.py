"""持久化：会话状态（checkpointer）+ 跨会话知识（store）。

两者刻意分开：
- checkpointer 按 thread_id 存对话，一个会话一份；
- store 存全局知识（口径约定），换会话照样生效。
"""

from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from .config import Settings


def make_checkpointer(settings: Settings) -> SqliteSaver:
    path = settings.resolved_checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def make_store(settings: Settings) -> SqliteStore:
    path = settings.resolved_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：交给 SqliteStore 自己管事务。默认的隐式事务会和它
    # 内部的 BEGIN 打架，报 "cannot start a transaction within a transaction"。
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    store.setup()
    return store