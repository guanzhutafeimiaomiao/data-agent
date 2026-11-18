"""跑评测：逐条执行图，落盘 JSONL，支持断点续跑。

设计取舍：
- 每条用例独立 thread_id，互不污染；
- 每跑完一条就写一行，跑一半中断也不丢数据；
- 记录 attempts、tokens、耗时，这些是简历上要用的数字。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..config import Settings
from ..graph.build import build_graph
from ..llm.base import LLMError
from ..runtime import Runtime
from ..state import initial_state
from .metrics import result_equal, summarize


def _execute_gold(rt: Runtime, sql: str) -> tuple[list[list], str | None]:
    result = rt.executor.run(sql)
    return result.rows, result.error


def run_cases(
    cases: list[dict],
    settings: Settings,
    out_dir: Path,
    resume: bool = False,
    limit: int | None = None,
) -> tuple[dict, Path]:
    rt = Runtime.create(settings)
    graph = build_graph(rt)

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"

    done: set[str] = set()
    if resume and results_path.exists():
        with results_path.open(encoding="utf-8") as fh:
            done = {json.loads(line)["id"] for line in fh if line.strip()}

    selected = [c for c in cases if c["id"] not in done]
    if limit:
        selected = selected[:limit]

    print(f"用例 {len(selected)} 条（已完成 {len(done)} 条，总 {len(cases)} 条）", flush=True)
    with results_path.open("a", encoding="utf-8") as fh:
        for index, case in enumerate(selected, start=1):
            started = time.perf_counter()
            record: dict = {
                "id": case["id"],
                "question": case["question"],
                "difficulty": case["difficulty"],
                "kind": case["kind"],
            }
            try:
                final = graph.invoke(
                    initial_state(case["question"]),
                    {"configurable": {"thread_id": f"eval-{case['id']}"}},
                )
                gold_rows, gold_error = _execute_gold(rt, case["sql"])
                attempts = final.get("attempts", [])
                record.update(
                    ex=bool(not gold_error and result_equal(gold_rows, final.get("rows", []))),
                    gold_error=gold_error,
                    verdict=final.get("verdict"),
                    attempts=attempts,
                    repair_attempts=final.get("repair_attempts", 0),
                    sql=final.get("sql", ""),
                    rows=final.get("rows", [])[:5],
                    gold_rows=gold_rows[:5],
                    usage=final.get("usage", {}),
                    tokens=sum(final.get("usage", {}).values()),
                    seconds=round(time.perf_counter() - started, 1),
                )
            except LLMError as exc:
                record.update(ex=False, error=str(exc), seconds=round(time.perf_counter() - started, 1))
            except Exception as exc:  # 单条用例炸掉不能拖垮整轮
                import traceback

                record.update(
                    ex=False,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc()[-1500:],
                    seconds=round(time.perf_counter() - started, 1),
                )

            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            fh.flush()

            mark = "✅" if record.get("ex") else "❌"
            print(
                f"[{index}/{len(selected)}] {case['id']} {mark} "
                f"{record.get('seconds')}s {record.get('tokens')}tok {case['question'][:28]}",
                flush=True,
            )

    records = []
    with results_path.open(encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    summary = summarize(records)
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["model"] = settings.model_name
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary, results_path


def print_summary(summary: dict) -> None:
    if not summary.get("total"):
        print("没有可汇总的结果")
        return
    print("\n" + "=" * 56)
    print(f"执行准确率 EX      : {summary['ex_rate']:.1%}  ({summary['graded']} 条有效)")
    print(f"一次成型率         : {summary['first_try_rate']:.1%}")
    print(f"修复后通过         : {summary['repaired_ok']} 条")
    print(f"平均尝试次数       : {summary['avg_attempts']}")
    print(f"平均 token / 耗时  : {summary['avg_tokens']} / {summary['avg_seconds']}s")
    print(f"调用异常           : {summary['errored']} 条")
    for difficulty, bucket in sorted(summary.get("by_difficulty", {}).items()):
        print(f"  {difficulty:<8} {bucket['ok']}/{bucket['total']} = {bucket['ex_rate']:.1%}")
    print("=" * 56)
