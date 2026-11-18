"""评测入口。

    python -m insight_agent.eval build                 # 生成 100 条并校验标准答案
    python -m insight_agent.eval run --limit 5         # 先跑 5 条试水
    python -m insight_agent.eval run --resume          # 断点续跑
    python -m insight_agent.eval run --fake            # 确定性基线（做对照）
    python -m insight_agent.eval run --no-plan         # 省一次调用，提速
    python -m insight_agent.eval run --fail-under 0.7  # CI 门禁
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..db import Executor, build_demo_db
from .cases import build_cases, load_cases, write_cases
from .runner import print_summary, run_cases

DEFAULT_CASES = Path("eval/cases.jsonl")


def _cmd_build(args: argparse.Namespace) -> int:
    settings = get_settings()
    executor = Executor(build_demo_db(settings.resolved_db_path()))

    cases = build_cases()
    broken = []
    for case in cases:
        result = executor.run(case["sql"])
        if result.error:
            broken.append((case["id"], case["question"], result.error))

    if broken:
        print(f"❌ {len(broken)} 条标准答案跑不通，评测集不可用：")
        for case_id, question, error in broken[:10]:
            print(f"  {case_id} {question}\n     {error}")
        return 1

    path = write_cases(Path(args.out), cases)
    kinds: dict[str, int] = {}
    for case in cases:
        kinds[case["difficulty"]] = kinds.get(case["difficulty"], 0) + 1
    print(f"✅ 生成 {len(cases)} 条，标准答案全部可执行 → {path}")
    print(f"   难度分布：{kinds}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    updates: dict = {}
    if args.fake:
        updates["fake_llm"] = True
    if args.max_repair is not None:
        updates["max_repair"] = args.max_repair
    if args.no_plan:
        updates["enable_plan"] = False
    # 评测是无人值守跑批，不能停在人工审批上
    updates["approval_enabled"] = False
    if updates:
        settings = settings.model_copy(update=updates)

    cases = load_cases(Path(args.cases))
    out_dir = Path(args.out) if args.out else Path("eval/runs") / datetime.now().strftime("%Y%m%d-%H%M%S")

    summary, results_path = run_cases(
        cases, settings, out_dir, resume=args.resume, limit=args.limit
    )
    print_summary(summary)
    print(f"\n明细：{results_path}")
    print(f"汇总：{out_dir / 'summary.json'}")

    if args.fail_under is not None and summary.get("ex_rate", 0) < args.fail_under:
        print(f"\n❌ EX {summary['ex_rate']:.1%} 低于门禁 {args.fail_under:.0%}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="insight-agent-eval")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="生成评测集并校验标准答案")
    p_build.add_argument("--out", default=str(DEFAULT_CASES))
    p_build.set_defaults(func=_cmd_build)

    p_run = sub.add_parser("run", help="跑评测")
    p_run.add_argument("--cases", default=str(DEFAULT_CASES))
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--resume", action="store_true")
    p_run.add_argument("--out", default=None)
    p_run.add_argument("--max-repair", type=int, default=None)
    p_run.add_argument("--fake", action="store_true")
    p_run.add_argument("--no-plan", action="store_true")
    p_run.add_argument("--fail-under", type=float, default=None)
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
