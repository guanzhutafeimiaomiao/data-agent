"""命令行入口：把图跑一遍，并把每个节点发生了什么打出来。

同一个 --thread 连续调用就是多轮会话（history 存在 SQLite 里，换进程也能接上）：
    python -m insight_agent.cli "2024 年华东区销售额是多少？"
    python -m insight_agent.cli "那华北呢？"
    python -m insight_agent.cli "以后客单价按客户数算"
    python -m insight_agent.cli --reset            # 清空该会话重新开始
    python -m insight_agent.cli --no-approval      # 高风险查询不问，直接跑
"""

from __future__ import annotations

import argparse
import sys

from langgraph.types import Command

from .checkpoint import make_checkpointer
from .graph.build import build_graph
from .llm.base import LLMError
from .runtime import Runtime
from .state import initial_state


def _print_delta(node: str, delta: dict) -> None:
    shown = []
    for key in (
        "intent",
        "verdict",
        "repair_attempts",
        "row_count",
        "exec_error",
        "changed",
        "skipped",
        "approval_action",
        "learned_metric",
    ):
        if key in delta and delta[key] not in (None, "", 0, False):
            shown.append(f"{key}={delta[key]}")
    suffix = ("  " + " ".join(shown)) if shown else ""
    print(f"  → {node}{suffix}")


def _pending(graph, config) -> dict | None:
    snapshot = graph.get_state(config)
    for task in snapshot.tasks or ():
        for item in task.interrupts or ():
            value = getattr(item, "value", item)
            return value if isinstance(value, dict) else {"type": "approval"}
    return None


def _ask_approval(waiting: dict) -> dict:
    """终端里的审批：和网页上是同一个 decision 结构。"""
    print("\n⚠ 这条查询需要确认：")
    for reason in waiting.get("reasons") or []:
        print(f"   - {reason}")
    print(f"\nSQL:\n{waiting.get('sql', '')}\n")
    choice = input("执行吗？[y]执行 / [n]拒绝 / [e]改 SQL: ").strip().lower()

    if choice == "n":
        return {"action": "reject"}
    if choice == "e":
        print("（直接粘贴新 SQL，空行结束）")
        lines = []
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
        return {"action": "edit", "sql": "\n".join(lines)}
    return {"action": "approve"}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="insight-agent")
    parser.add_argument("question", nargs="?", default="2024 年华东区销售额是多少？")
    parser.add_argument("--thread", default="demo", help="会话 id，多轮追问沿用同一个")
    parser.add_argument("--reset", action="store_true", help="清空该会话的历史后重新开始")
    parser.add_argument("--no-approval", action="store_true", help="关闭人工审批")
    args = parser.parse_args(argv)

    rt = Runtime.create()
    if args.no_approval:
        rt.settings = rt.settings.model_copy(update={"approval_enabled": False})
    saver = make_checkpointer(rt.settings)
    graph = build_graph(rt, checkpointer=saver)
    config = {"configurable": {"thread_id": args.thread}}

    if args.reset:
        saver.delete_thread(args.thread)
        print(f"已清空会话「{args.thread}」")

    try:
        snapshot = graph.get_state(config)
        turns = len((snapshot.values or {}).get("history") or []) if snapshot else 0
        suffix = f"（同一会话已有 {turns} 轮）" if turns else ""
        print(f"\n问题：{args.question}{suffix}\n")

        graph_input = initial_state(args.question)
        try:
            while True:
                for chunk in graph.stream(graph_input, config, stream_mode="updates"):
                    for node, delta in chunk.items():
                        if node.startswith("__"):
                            continue
                        _print_delta(node, delta)

                waiting = _pending(graph, config)
                if not waiting:
                    break
                graph_input = Command(resume=_ask_approval(waiting))
        except LLMError as exc:
            print(f"\n运行中断：\n{exc}")
            return 2

        final = graph.get_state(config).values

        if final.get("standalone_question") and final["standalone_question"] != final.get("raw_question"):
            print(f"\n理解为：{final['standalone_question']}")

        print("\n" + "=" * 60)
        print(final.get("answer", "（无输出）"))
        print("=" * 60)

        print("\nSQL 尝试记录：")
        for attempt in final.get("attempts", []):
            status = attempt["verdict"]
            print(
                f"  #{attempt['n']}  {status:<16} rows={attempt['row_count']:<4} "
                f"{attempt['sql'].splitlines()[0][:70]}"
            )

        print(f"\ntokens={final.get('usage', {})}  修复次数={final.get('repair_attempts', 0)}")
        print(f"chart_spec={final.get('chart_spec')}")
        print(f"会话历史={len(final.get('history') or [])} 轮")
        print(f"口径约定={len(rt.semantics.all())} 条")
        return 0
    finally:
        saver.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())