"""Conda 包的 ROS 2 运行时冒烟测试。

在子进程中初始化 rclpy、加载 ``unilabos_msgs`` 类型支持并创建一个带 ``Resource``
发布者的节点，验证 cp314 原生扩展与 RMW 可用。

只允许本机发现并设置硬超时：macOS CI 上 Cyclone DDS 向组播地址写入失败后会一直重试，
曾让 conda 构建卡到 6 小时上限。超时按失败处理，不再无限等待。

用法：``python -m unilabos.backend.ros2.conda_smoke``，返回码 0 表示通过。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Mapping, Optional

TIMEOUT_SECONDS = 180
# Windows 首次修补 rclpy DLL 搜索路径后以 75 退出，需在新进程中再验证一次。
RESTART_EXIT_CODE = 75
TIMEOUT_EXIT_CODE = 124

CHILD_CODE = """
from unilabos.app.utils import patch_rclpy_dll_windows
patch_rclpy_dll_windows()
import rclpy
from rosidl_generator_py import import_type_support_impl
from unilabos_msgs.msg import Resource
import_type_support_impl.import_type_support('unilabos_msgs')
rclpy.init()
node = rclpy.create_node('conda_cp314_test')
node.create_publisher(Resource, 'conda_cp314_resource', 1)
node.destroy_node()
rclpy.shutdown()
"""

# 关闭 Cyclone DDS 组播，只与本机对端发现；其他 RMW 会忽略该变量。
CYCLONEDDS_LOCAL_ONLY = (
    "<CycloneDDS><Domain>"
    "<General><AllowMulticast>false</AllowMulticast></General>"
    '<Discovery><Peers><Peer Address="localhost"/></Peers></Discovery>'
    "</Domain></CycloneDDS>"
)


def smoke_environment(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """返回限定本机发现的环境；已显式设置的变量保持不变。"""

    env = dict(os.environ if base is None else base)
    env.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")  # Jazzy 及以后
    env.setdefault("ROS_LOCALHOST_ONLY", "1")  # Humble
    env.setdefault("CYCLONEDDS_URI", CYCLONEDDS_LOCAL_ONLY)
    return env


def run_once(code: str = CHILD_CODE, timeout: float = TIMEOUT_SECONDS) -> int:
    try:
        return subprocess.run(
            [sys.executable, "-c", code], env=smoke_environment(), timeout=timeout
        ).returncode
    except subprocess.TimeoutExpired:
        print(
            f"ROS 2 冒烟测试 {timeout:g} 秒内未结束，判定失败（DDS 初始化或发现被阻塞）",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT_CODE


def main(code: str = CHILD_CODE, timeout: float = TIMEOUT_SECONDS) -> int:
    result = run_once(code, timeout)
    if result == RESTART_EXIT_CODE:
        result = run_once(code, timeout)
    return result


if __name__ == "__main__":
    sys.exit(main())
