"""模型接入自检：不跑图，只验证「配置 + Key + 端点 + 模型名」这一层通不通。

用法：
    python -m insight_agent.doctor
"""

from __future__ import annotations

import sys

import httpx

from .config import get_settings

HINT_401 = (
    "Key 无效，或者不属于这个端点所在的工作区。\n"
    "  1. 去控制台重新复制一次（注意别少字符、别带引号）；\n"
    "  2. 确认 Key 与 base_url 是同一个区域 / 同一个工作区；\n"
    "  3. 确认没有过期或被禁用。"
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    key = settings.api_key
    print(f"模型        : {settings.model_name}")
    print(f"端点        : {settings.model_base_url}")
    print(f"快速模型    : {settings.fast_model_name}")
    print(f"Key         : 长度 {len(key)}，{'以 sk- 开头' if key.startswith('sk-') else '不以 sk- 开头'}")

    if not key:
        print("\n未配置 Key：请在 .env 里设置 INSIGHT_API_KEY。")
        return 2

    print("\n发送一次最小请求…")
    try:
        response = httpx.post(
            settings.model_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
            timeout=30,
        )
    except Exception as exc:  # 网络层
        print(f"网络错误：{type(exc).__name__} {exc}")
        return 3

    print(f"HTTP {response.status_code}")
    if response.status_code == 200:
        print("✅ 接通了，可以跑 python -m insight_agent.cli 了。")
        return 0

    print(f"❌ 未通过：{response.text[:300]}")
    if response.status_code == 401:
        print(HINT_401)
    if response.status_code == 404:
        print("模型名可能不对：确认 INSIGHT_MODEL_NAME 与端点支持的模型列表一致。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
