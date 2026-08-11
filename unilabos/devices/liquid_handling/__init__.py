"""Liquid-handling driver package.

Some Uni-Lab PyLabRobot builds expose an RViz backend from the package
``__init__`` and therefore import ``rclpy`` even when RViz is not selected.
Keep that optional backend lazy so Chatterbox and hardware backends can run in
Basic/HostLink processes without ROS installed.
"""

from __future__ import annotations

import importlib.util
import sys
import types

from unilabos.config.config import BasicConfig


def _install_optional_plr_rviz_stub() -> None:
    if BasicConfig.backend == "ros2" and importlib.util.find_spec("rclpy") is not None:
        return
    module_name = "pylabrobot.liquid_handling.backends.rviz_backend"
    if module_name in sys.modules:
        return
    module = types.ModuleType(module_name)

    class LiquidHandlerRvizBackend:  # pragma: no cover - instantiated on misuse
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError(
                "LiquidHandlerRvizBackend 需要 ROS2；"
                "Basic/HostLink 请使用硬件 backend 或 Chatterbox backend"
            )

    module.LiquidHandlerRvizBackend = LiquidHandlerRvizBackend
    module.__all__ = ["LiquidHandlerRvizBackend"]
    sys.modules[module_name] = module


_install_optional_plr_rviz_stub()
