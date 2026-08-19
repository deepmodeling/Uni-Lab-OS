"""旧 Backend 兼容能力的唯一开关。

正常运行路径不得读取配置文件来启用这里的能力；只能由进程启动参数
--legacy 显式开启。
"""

from __future__ import annotations

_legacy_enabled = False


class LegacySupportDisabled(RuntimeError):
    """调用了只允许在 --legacy 模式使用的旧接口。"""


def configure_legacy_support(enabled: bool) -> None:
    global _legacy_enabled
    _legacy_enabled = bool(enabled)


def legacy_support_enabled() -> bool:
    return _legacy_enabled


def require_legacy_support(feature: str) -> None:
    if not _legacy_enabled:
        raise LegacySupportDisabled(
            f"{feature} belongs to the old Backend contract; restart with --legacy"
        )


__all__ = [
    "LegacySupportDisabled",
    "configure_legacy_support",
    "legacy_support_enabled",
    "require_legacy_support",
]
