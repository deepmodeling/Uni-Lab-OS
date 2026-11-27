"""数据库配置的校验与启动选择。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DatabaseSettings:
    database_type: str = "sqlite"
    database_url: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.database_type not in {"sqlite", "postgresql"}:
            raise ValueError("database_type 只允许 sqlite 或 postgresql")
        if self.database_type == "postgresql":
            try:
                url = urlsplit(self.database_url)
                valid = (
                    url.scheme == "postgresql"
                    and bool(url.hostname)
                    and bool(url.path.strip("/"))
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "PostgreSQL 模式需要有效的 database_url：postgresql://用户:密码@主机:端口/数据库"
                )


_settings_override: ContextVar[DatabaseSettings | None] = ContextVar(
    "database_settings", default=None
)


@contextmanager
def use_database_settings(settings: DatabaseSettings):
    token = _settings_override.set(settings)
    try:
        yield
    finally:
        _settings_override.reset(token)


def configured_settings() -> DatabaseSettings:
    from unilabos.config.config import BasicConfig

    return _settings_override.get() or DatabaseSettings(
        BasicConfig.database_type, BasicConfig.database_url
    )


def resolve_database_settings(arguments: Mapping[str, object]) -> DatabaseSettings:
    """配置文件及环境变量已加载后，只应用显式传入的命令行参数。"""
    from unilabos.config.config import BasicConfig

    settings = DatabaseSettings(
        str(arguments.get("database_type") or BasicConfig.database_type),
        str(
            arguments.get("database_url")
            if arguments.get("database_url") is not None
            else BasicConfig.database_url
        ),
    )
    BasicConfig.database_type = settings.database_type
    BasicConfig.database_url = settings.database_url
    return settings


def verify_database_connection(settings: DatabaseSettings) -> None:
    """启动业务前检查 PG 连接，失败时不回退到 SQLite。"""
    if settings.database_type == "postgresql":
        from unilabos.persistence.database import PostgreSQLDatabase

        database = PostgreSQLDatabase(settings, "preflight")
        database.close()
