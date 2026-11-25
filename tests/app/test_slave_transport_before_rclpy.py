"""ROS 2 Slave 的 Fast DDS 传输必须在进程第一次加载 ROS 库之前选好。

Windows 上导入 rclpy（加载 Fast DDS）之后再设 ``FASTRTPS_DEFAULT_PROFILES_FILE``，参与者不一定按这份配置建，
Slave 会发现不了 Host。这里在子进程里拦下第一次导入 ROS 模块，记录那一刻的环境后直接退出，不需要装 ROS。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from unilabos.client.host.hostlink.ros_assist import FASTDDS_USER_TRANSPORT_ENV, NETWORK_ONLY_PROFILE_FILENAME

_PROBE = textwrap.dedent(
    """
    import importlib.abc, importlib.machinery, json, os, sys

    class _Stop(importlib.abc.Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            from unilabos.config.config import BasicConfig
            print("PROBE " + json.dumps({
                "profile": os.environ.get("FASTRTPS_DEFAULT_PROFILES_FILE", ""),
                "transport": getattr(BasicConfig, "ros_transport", ""),
            }), flush=True)
            os._exit(0)

    # 第一次碰到 ROS 一侧就停：rclpy、任何 *_msgs 消息包（加载 typesupport 库），或框架的消息转换入口
    _ROS_ENTRY = "unilabos.client.host.ros2.msgs.message_converter"

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            top = name.split(".", 1)[0]
            if top == "rclpy" or top.endswith("_msgs") or name == _ROS_ENTRY:
                return importlib.machinery.ModuleSpec(name, _Stop())
            return None

    sys.meta_path.insert(0, _Finder())
    sys.argv = ["unilab", "--is_slave", "--backend", "ros2", "--skip_env_check"]
    from unilabos.app.main import main
    main()
    """
)


def test_slave_selects_the_network_profile_before_the_first_ros_import(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in FASTDDS_USER_TRANSPORT_ENV
        and key not in {"RMW_IMPLEMENTATION", "ROS_LOCALHOST_ONLY", "ROS_AUTOMATIC_DISCOVERY_RANGE", "RMW_FASTRTPS_USE_QOS_FROM_XML"}
        and not key.startswith("UNILABOS_")
    }
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[2]), env.get("PYTHONPATH", "")])
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("PROBE ")]
    assert lines, f"子进程没有导入 ROS 模块就退出了：\nstdout={completed.stdout}\nstderr={completed.stderr}"
    probe = json.loads(lines[0][len("PROBE "):])
    # 回环传不了大 UDP 报文的环境保留默认传输（fallback）；其余都必须在导入 rclpy 之前设好网络配置
    assert probe["transport"] in {"network", "fallback"}, probe
    if probe["transport"] == "network":
        assert Path(probe["profile"]).name == NETWORK_ONLY_PROFILE_FILENAME
