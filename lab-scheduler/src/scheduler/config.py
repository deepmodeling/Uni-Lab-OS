"""Application settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8090
    default_algorithm: str = "WeightedCriticalPath"
    default_transfer_time: int = 5  # 默认转运时间 (分钟)
    cors_origins: list[str] = ["*"]

    model_config = {"env_prefix": "SCHEDULER_"}


settings = Settings()
