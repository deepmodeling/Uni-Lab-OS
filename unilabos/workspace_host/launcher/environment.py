"""桌面解释器环境准备，不改变主线启动计划。"""
import ipaddress
import os
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ...app.center_client.console_address import LAUNCHER_CONSOLE_URL_ENV, detect_console_origin


def prepare_environment(checkout: Path | None) -> None:
    # -B 不会自动传给 ROS 工具等二级 Python 命令，所有子进程须继承只读策略。
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    prefix = Path(sys.prefix)
    if os.name == "nt":
        entries = [prefix, prefix / "Library" / "bin", prefix / "Scripts", prefix / "bin"]
        fallback = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    else:
        entries = [prefix / "bin"]
        fallback = "/usr/bin:/bin"
    head = os.pathsep.join(str(path) for path in entries if path.exists())
    current = os.environ.get("PATH", fallback)
    os.environ["PATH"] = head + (os.pathsep + current if current else "")
    if os.name == "nt":
        os.environ["PYTHONUTF8"] = "1"
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["CONDA_DLL_SEARCH_MODIFICATION_ENABLE"] = "1"
    os.environ["CONDA_PREFIX"] = str(prefix)
    os.environ.setdefault("AMENT_PREFIX_PATH", str(prefix / "Library" if os.name == "nt" else prefix))
    os.environ.setdefault("COLCON_PREFIX_PATH", str(prefix))
    if checkout is not None:
        os.environ["PYTHONPATH"] = str(checkout) + os.pathsep + os.environ.get("PYTHONPATH", "")
        if str(checkout) not in sys.path:
            sys.path.insert(0, str(checkout))


@contextmanager
def desktop_web_environment(host: str, port: int, dist: Path) -> Iterator[int]:
    """为本次桌面会话固定 Backend 地址和页面目录，重启子进程时沿用。"""
    family = socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET
    with socket.socket(family) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        probe.listen()
        port = probe.getsockname()[1]
    updates = {
        "UNILABOS_DESKTOP_WEB_HOST": host,
        "UNILABOS_DESKTOP_CONSOLE_DIR": str(dist.resolve()),
        LAUNCHER_CONSOLE_URL_ENV: detect_console_origin(host, port, os.environ.get("CENTER_ENDPOINT")),
    }
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield port
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
