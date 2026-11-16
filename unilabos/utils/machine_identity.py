"""本机身份：判断两个 unilab 进程（调度权威、Host、Slave）是否在同一台机器上。

``machine_name`` 是 HostLink 节点名，可以随意指定（受管 Slave 还会带 ``_managed_`` 后缀），
不能用来判断是不是同一台机器。这里取操作系统自己的机器标识：

- Linux：``/etc/machine-id``（或 ``/var/lib/dbus/machine-id``）；
- macOS：``IOPlatformUUID``；
- Windows：``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``；
- 都取不到时退回主机名 + 网卡地址。

原始标识不外传：对外只给加了固定前缀的 sha256 摘要前 32 位。同一台机器上的进程（同一个
容器内的进程）得到相同的值；``BasicConfig.machine_id``（或环境变量
``UNILABOS_BASICCONFIG_MACHINE_ID``）可以显式覆盖，例如让同一宿主机上的几个容器算作同一台。
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

_PREFIX = "unilabos-machine:"
_lock = threading.Lock()
_cached: Optional[str] = None


def _linux_machine_id() -> str:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return ""


def _macos_machine_id() -> str:
    try:
        output = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in output.splitlines():
        if "IOPlatformUUID" in line:
            return line.rpartition("=")[2].strip().strip('"')
    return ""


def _windows_machine_id() -> str:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            return str(value or "").strip()
    except OSError:
        return ""


def _raw_machine_id() -> str:
    if sys.platform.startswith("linux"):
        value = _linux_machine_id()
    elif sys.platform == "darwin":
        value = _macos_machine_id()
    elif sys.platform == "win32":
        value = _windows_machine_id()
    else:
        value = ""
    if value:
        return value
    # 取不到操作系统标识：主机名 + 网卡地址（getnode 找不到网卡时每个进程随机，
    # 那样只会让本机进程互判为跨机器，不会把两台机器误判成一台）
    return f"{platform.node()}:{uuid.getnode():012x}"


def machine_id() -> str:
    """本机身份摘要（进程内缓存）；``BasicConfig.machine_id`` 非空时直接用它。"""

    from unilabos.config.config import BasicConfig

    override = str(getattr(BasicConfig, "machine_id", "") or "").strip()
    if override:
        return override
    global _cached
    with _lock:
        if _cached is None:
            digest = hashlib.sha256((_PREFIX + _raw_machine_id()).encode("utf-8")).hexdigest()
            _cached = digest[:32]
        return _cached


__all__ = ["machine_id"]
