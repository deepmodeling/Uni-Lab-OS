"""调度器锁授予策略（可插拔）：谁在这一轮拿到完整资源集合。

宿主（:class:`SchedulerResourceManager`）只把决策所需的事实交给策略——按到达顺序的等待请求、
已被持有的键、被 pending handoff 预留的键、哪些请求是 handoff 目标——策略返回本轮授予的请求
下标（按授予顺序）。宿主随后逐条复核（未越界、未重复、仍在等待、与已持有及彼此不相交），
不合格的结果整轮退回 Python 参考实现，坏的策略库不会造成重复占用。

默认策略是严格按键 FIFO：不相交的集合并行授予，被挡住的请求阻塞与它重叠的后来者。原生实现
（``native/grant-policy``，Rust 编译出的 C ABI 动态库）与本文件的 :func:`fifo_grants` 逐条等价，
存在时进程内经 ctypes 调用，缺失或加载失败时退回 Python。

选择方式：``BasicConfig.scheduler_grant_policy`` = ``auto``（默认：有原生库用原生，否则 Python）/
``python`` / ``native``（找不到原生库则报错）；原生库位置由 ``UNILABOS_SCHEDULER_GRANT_LIBRARY``
或 ``BasicConfig.scheduler_grant_library`` 指定，缺省在 ``<working_dir>/native/`` 下查找。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Hashable, Iterable, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

ABI_VERSION = 0x0001_0000
LIBRARY_BASENAME = "unilab_grant_policy"


@dataclass(frozen=True)
class WaitingRequest:
    keys: tuple[int, ...]
    handoff_target: bool = False


class GrantPolicy(Protocol):
    name: str

    def decide(
        self,
        waiting: Sequence[WaitingRequest],
        held: Iterable[int],
        reserved: Iterable[int],
    ) -> list[int]: ...


def fifo_grants(
    waiting: Sequence[WaitingRequest],
    held: Iterable[int],
    reserved: Iterable[int],
) -> list[int]:
    """参考实现：单次扫描，严格按键 FIFO（与 native/grant-policy 的 ``fifo_grants`` 等价）。"""

    reserved_set = set(reserved)
    owned = set(held)
    blocked: set[int] = set()
    grants: list[int] = []
    for index, request in enumerate(waiting):
        keys = set(request.keys)
        if request.handoff_target:
            blocked |= keys
            continue
        if keys & reserved_set or keys & blocked or keys & owned:
            blocked |= keys
            continue
        owned |= keys
        grants.append(index)
    return grants


class PythonFifoPolicy:
    name = "python-fifo"

    def decide(self, waiting, held, reserved) -> list[int]:
        return fifo_grants(waiting, held, reserved)


def encode_request(
    waiting: Sequence[WaitingRequest],
    held: Iterable[int],
    reserved: Iterable[int],
) -> bytes:
    words: list[int] = [ABI_VERSION, len(waiting)]
    for request in waiting:
        words.append(1 if request.handoff_target else 0)
        words.append(len(request.keys))
        words.extend(request.keys)
    held_list = list(held)
    reserved_list = list(reserved)
    words.append(len(held_list))
    words.extend(held_list)
    words.append(len(reserved_list))
    words.extend(reserved_list)
    return struct.pack(f"<{len(words)}I", *words)


def decode_grants(payload: bytes) -> list[int]:
    if len(payload) < 4 or len(payload) % 4:
        raise ValueError("grant policy returned a malformed buffer")
    count = struct.unpack_from("<I", payload)[0]
    if len(payload) != 4 * (count + 1):
        raise ValueError("grant policy returned a malformed buffer")
    return list(struct.unpack_from(f"<{count}I", payload, 4))


class NativeGrantPolicy:
    """经 ctypes 进程内调用 Rust 编译出的 C ABI 库。"""

    name = "native-fifo"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        library = ctypes.CDLL(str(self.path))
        library.uni_grant_abi_version.restype = ctypes.c_uint32
        library.uni_grant_abi_version.argtypes = []
        version = int(library.uni_grant_abi_version())
        if version >> 16 != ABI_VERSION >> 16:
            raise OSError(f"grant policy ABI {version:#x} 与宿主 {ABI_VERSION:#x} 不兼容")
        library.uni_grant_decide.restype = ctypes.c_int32
        library.uni_grant_decide.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        library.uni_grant_free.restype = None
        library.uni_grant_free.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        self._library = library
        self.abi_version = version

    def decide(self, waiting, held, reserved) -> list[int]:
        request = encode_request(waiting, held, reserved)
        out_ptr = ctypes.POINTER(ctypes.c_uint8)()
        out_len = ctypes.c_size_t(0)
        status = self._library.uni_grant_decide(
            request, len(request), ctypes.byref(out_ptr), ctypes.byref(out_len)
        )
        if status != 0:
            raise RuntimeError(f"grant policy library returned {status}")
        try:
            payload = ctypes.string_at(out_ptr, out_len.value)
        finally:
            self._library.uni_grant_free(out_ptr, out_len.value)
        return decode_grants(payload)


def library_filename() -> str:
    if sys.platform == "win32":
        return f"{LIBRARY_BASENAME}.dll"
    if sys.platform == "darwin":
        return f"lib{LIBRARY_BASENAME}.dylib"
    return f"lib{LIBRARY_BASENAME}.so"


def candidate_library_paths(explicit: str = "") -> list[Path]:
    paths: list[Path] = []
    for value in (explicit, os.environ.get("UNILABOS_SCHEDULER_GRANT_LIBRARY", "")):
        if value:
            paths.append(Path(value).expanduser())
    try:
        from unilabos.config.config import BasicConfig

        configured = str(getattr(BasicConfig, "scheduler_grant_library", "") or "")
        if configured:
            paths.append(Path(configured).expanduser())
        working_dir = str(getattr(BasicConfig, "working_dir", "") or "")
        if working_dir:
            paths.append(Path(working_dir).expanduser() / "native" / library_filename())
    except Exception:  # noqa: BLE001 - 配置不可用时只用显式路径
        pass
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


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
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        logger.info("调度锁授予策略使用原生库 %s（ABI %#x）", path, policy.abi_version)
        return policy
    if mode == "native":
        detail = "；".join(errors) or "未找到原生库"
        raise RuntimeError(f"scheduler_grant_policy=native 但原生库不可用：{detail}")
    if errors:
        logger.warning("原生锁授予策略加载失败，退回 Python：%s", "；".join(errors))
    return PythonFifoPolicy()


class KeyIndex:
    """把锁键 / owner 映射为本轮决策用的整数编号。"""

    def __init__(self) -> None:
        self._ids: dict[Hashable, int] = {}

    def __call__(self, key: Hashable) -> int:
        identity = self._ids.get(key)
        if identity is None:
            identity = len(self._ids)
            self._ids[key] = identity
        return identity


def validate_grants(
    grants: Sequence[int],
    waiting: Sequence[WaitingRequest],
    held: Iterable[int],
    reserved: Iterable[int],
) -> bool:
    """宿主复核：下标有效且不重复、不授予 handoff 目标、与已持有 / 预留 / 彼此不相交。"""

    taken = set(held) | set(reserved)
    seen: set[int] = set()
    for index in grants:
        if not isinstance(index, int) or index in seen or not 0 <= index < len(waiting):
            return False
        seen.add(index)
        request = waiting[index]
        keys = set(request.keys)
        if request.handoff_target or keys & taken:
            return False
        taken |= keys
    return True


__all__ = [
    "ABI_VERSION",
    "GrantPolicy",
    "KeyIndex",
    "NativeGrantPolicy",
    "PythonFifoPolicy",
    "WaitingRequest",
    "candidate_library_paths",
    "decode_grants",
    "encode_request",
    "fifo_grants",
    "library_filename",
    "load_grant_policy",
    "validate_grants",
]
