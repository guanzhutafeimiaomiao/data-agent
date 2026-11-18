"""Runtime configuration. Everything is env-overridable via the INSIGHT_ prefix."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INSIGHT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 模型：默认走千问（OpenAI 兼容端点），换 base_url 即可换厂商
    model_provider: str = "qwen"
    model_name: str = "qwen3.7-max"
    model_base_url: str = (
        "https://ws-zw93rxplh37pokua.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("INSIGHT_API_KEY", "DATA_API_KEY"),
    )
    fast_model_name: str = "qwen3.7-max"  # 规划 / 校验用的模型，可换更便宜的型号

    # 私有化部署经常要 50s+ 才回，60s 会频繁误杀
    llm_timeout_s: int = 180

    # 数据
    db_path: Path = Path("data/demo.sqlite")

    # 图的上限：这些数字决定了"自纠错"会不会失控烧钱
    max_repair: int = 2
    row_limit: int = 200
    query_timeout_ms: int = 8000

    # 强制使用确定性实现（评测基线、离线开发）；不设且没有 Key 时也会自动回退
    fake_llm: bool = False

    # 规划节点：关掉能省一次调用（评测提速、也用于消融）
    enable_plan: bool = True

    # 会话持久化：checkpointer 库 + 保留多少轮历史
    checkpoint_path: Path = Path("data/checkpoints.db")
    history_turns: int = 6
    enable_rewrite: bool = True

    # 空结果时反查"问题里的实体是否真的存在"，用于区分"查不到"和"查错了"
    enable_grounding: bool = True

    # 人工审批：高风险查询（全表扫描 / 不限时间）执行前暂停等人确认
    approval_enabled: bool = True

    # 口径记忆：用户约定过的指标定义持久化，后续提问自动带上
    store_path: Path = Path("data/store.db")
    enable_semantics: bool = True

    def resolved_db_path(self) -> Path:
        path = self.db_path
        return path if path.is_absolute() else (Path.cwd() / path).resolve()

    def resolved_checkpoint_path(self) -> Path:
        path = self.checkpoint_path
        return path if path.is_absolute() else (Path.cwd() / path).resolve()

    def resolved_store_path(self) -> Path:
        path = self.store_path
        return path if path.is_absolute() else (Path.cwd() / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
