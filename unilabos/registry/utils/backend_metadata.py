"""Registry-facing backend capability metadata.

The local Python driver runtime belongs to HostLink and is not a third public
transport. Device metadata therefore exposes only the two transport backends
understood by the scheduler and frontend: HostLink and ROS 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


PUBLIC_DEVICE_BACKENDS: tuple[str, ...] = ("hostlink", "ros2")


def normalize_supported_backends(
    value: Any = None,
    *,
    device_type: str | None = None,
) -> list[str]:
    """Return canonical public backend capability metadata.

    Ordinary Python devices default to both public transports.  A native ROS 2
    node (``device_type == "ros2"``) is direct evidence that the implementation
    is ROS-only, unless it supplies an explicit capability list.
    """

    if value is None:
        return ["ros2"] if device_type == "ros2" else list(PUBLIC_DEVICE_BACKENDS)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("supported_backends 必须是 backend 名称数组")

    requested = list(value)
    if not requested:
        return ["ros2"] if device_type == "ros2" else list(PUBLIC_DEVICE_BACKENDS)
    invalid = [name for name in requested if name not in PUBLIC_DEVICE_BACKENDS]
    if invalid:
        raise ValueError(
            "supported_backends 只允许 hostlink/ros2，包含未知 backend："
            + ", ".join(str(name) for name in invalid)
        )

    # 去重并固定输出顺序，避免 Registry completion 因声明顺序产生无意义差异。
    normalized = [name for name in PUBLIC_DEVICE_BACKENDS if name in requested]
    if not normalized:
        raise ValueError("supported_backends 不能为空")
    return normalized


__all__ = ["PUBLIC_DEVICE_BACKENDS", "normalize_supported_backends"]
