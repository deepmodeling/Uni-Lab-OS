"""加载原生授予策略库（native/grant-policy，Rust 编译出的 C ABI）并按配置选择策略。

函数原型与结构体来自生成的 :mod:`._abi`；这里只负责找库、校验 ABI 主版本、把等待请求放进 C 数组。
选择：``BasicConfig.scheduler_grant_policy`` = ``auto``（默认：有原生库用原生，否则 Python）/ ``python`` /
``native``（找不到或加载失败报错）。库位置：``BasicConfig.scheduler_grant_library``、环境变量
``UNILABOS_SCHEDULER_GRANT_LIBRARY``，缺省 ``<working_dir>/native/``。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from unilabos.server.backend.scheduler.grant import _abi
from unilabos.server.backend.scheduler.grant.policy import GrantPolicy, PythonFifoPolicy

logger = logging.getLogger(__name__)


def library_filename() -> str:
    if sys.platform == "win32":
        return "unilab_grant_policy.dll"
    if sys.platform == "darwin":
        return "libunilab_grant_policy.dylib"
    return "libunilab_grant_policy.so"


class NativeGrantPolicy:
    """进程内调用原生库；库不分配内存，scratch 与输出缓冲区都由这里给。"""

    name = "native-fifo"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        library = _abi.bind(ctypes.CDLL(str(self.path)))
        self.abi_version = int(library.uni_grant_abi_version())
        if self.abi_version >> 16 != _abi.UNI_GRANT_ABI_VERSION >> 16:
            raise OSError(f"ABI {self.abi_version:#x} 与宿主 {_abi.UNI_GRANT_ABI_VERSION:#x} 不兼容")
        self._decide = library.uni_grant_decide

    def decide(self, waiting, held, reserved) -> list[int]:
        held, reserved = list(held), list(reserved)
        key_space = 1 + max([*held, *reserved, *(key for item in waiting for key in item.keys)], default=-1)
        key_arrays = [(ctypes.c_uint32 * len(item.keys))(*item.keys) for item in waiting]
        requests = (_abi.UniGrantWaiting * len(waiting))(*(
            _abi.UniGrantWaiting(
                ctypes.cast(keys, ctypes.POINTER(ctypes.c_uint32)), len(item.keys), 1 if item.handoff_target else 0
            )
            for keys, item in zip(key_arrays, waiting)
        ))
        grants = (ctypes.c_uint32 * len(waiting))()
        count = ctypes.c_size_t(0)
        status = self._decide(
            requests, len(waiting),
            (ctypes.c_uint32 * len(held))(*held), len(held),
            (ctypes.c_uint32 * len(reserved))(*reserved), len(reserved),
            (ctypes.c_uint8 * key_space)(), key_space,
            grants, len(waiting), ctypes.byref(count),
        )
        if status != _abi.UNI_GRANT_OK:
            raise RuntimeError(f"grant policy library returned {status}")
        return list(grants[: count.value])


def candidate_library_paths(explicit: str = "") -> list[Path]:
    values = [explicit, os.environ.get("UNILABOS_SCHEDULER_GRANT_LIBRARY", "")]
    try:
        from unilabos.config.config import BasicConfig

        values.append(str(getattr(BasicConfig, "scheduler_grant_library", "") or ""))
        working_dir = str(getattr(BasicConfig, "working_dir", "") or "")
        if working_dir:
            values.append(str(Path(working_dir) / "native" / library_filename()))
    except Exception:  # noqa: BLE001 - 配置不可用时只用显式路径
        pass
    paths: list[Path] = []
    for value in values:
        if value and Path(value).expanduser() not in paths:
            paths.append(Path(value).expanduser())
    return paths


def load_grant_policy(mode: Optional[str] = None, library: str = "") -> GrantPolicy:
    """按配置选择策略：auto / python / native。"""

    if mode is None:
        try:
            from unilabos.config.config import BasicConfig

            mode = str(getattr(BasicConfig, "scheduler_grant_policy", "auto") or "auto")
        except Exception:  # noqa: BLE001
            mode = "auto"
    mode = mode.strip().lower()
    if mode not in {"auto", "python", "native"}:
        raise ValueError(f"scheduler_grant_policy 只能是 auto / python / native，收到 {mode!r}")
    if mode == "python":
        return PythonFifoPolicy()
    errors: list[str] = []
    for path in candidate_library_paths(library):
        if not path.is_file():
            continue
        try:
            policy = NativeGrantPolicy(path)
        except (OSError, AttributeError) as error:
            errors.append(f"{path}: {error}")
            continue
        logger.info("调度锁授予策略使用原生库 %s（ABI %#x）", path, policy.abi_version)
        return policy
    if mode == "native":
        raise RuntimeError(f"scheduler_grant_policy=native 但原生库不可用：{'；'.join(errors) or '未找到原生库'}")
    if errors:
        logger.warning("原生锁授予策略加载失败，退回 Python：%s", "；".join(errors))
    return PythonFifoPolicy()
