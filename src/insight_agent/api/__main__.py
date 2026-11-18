"""python -m insight_agent.api —— 起一个带界面的服务。"""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="insight-agent-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--reload", action="store_true", help="开发时自动重载")
    args = parser.parse_args(argv)

    uvicorn.run(
        "insight_agent.api.app:create_app" if args.reload else create_app(),
        host=args.host,
        port=args.port,
        factory=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
