"""旧 Backend 兼容能力的唯一开关。

正常运行路径不得读取配置文件来启用这里的能力；只能由进程启动参数
--legacy 显式开启。
"""

from __future__ import annotations

import warnings

LEGACY_REMOVAL_DATE = "2026-12-01"
LEGACY_DEPRECATION_MESSAGE = (
    "--legacy is deprecated and will be removed on "
    f"{LEGACY_REMOVAL_DATE}; migrate to control.v1 WebSocket notices and "
    "the microbackend HTTP APIs"
)

_legacy_enabled = False


class LegacySupportDisabled(RuntimeError):
    """调用了只允许在 --legacy 模式使用的旧接口。"""


class LegacySupportDeprecationWarning(FutureWarning):
    """旧 Backend 兼容层将在约定日期移除。"""


def configure_legacy_support(enabled: bool) -> None:
    global _legacy_enabled
    _legacy_enabled = bool(enabled)
    if _legacy_enabled:
        warnings.warn(
            LEGACY_DEPRECATION_MESSAGE,
            LegacySupportDeprecationWarning,
            stacklevel=2,
        )


def legacy_support_enabled() -> bool:
    return _legacy_enabled


def require_legacy_support(feature: str) -> None:
    if not _legacy_enabled:
        raise LegacySupportDisabled(
            f"{feature} belongs to the old Backend contract; restart with --legacy"
        )


__all__ = [
    "LEGACY_DEPRECATION_MESSAGE",
    "LEGACY_REMOVAL_DATE",
    "LegacySupportDisabled",
    "LegacySupportDeprecationWarning",
    "configure_legacy_support",
    "legacy_support_enabled",
    "require_legacy_support",
]
