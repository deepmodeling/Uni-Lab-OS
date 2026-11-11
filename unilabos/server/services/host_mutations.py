"""包变更、slave 启动和 Host 重启共享的安静点；文件锁跨进程生效。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from unilabos.utils.file_lock import acquire_exclusive_file_lock, release_file_lock


class HostMutationBusy(RuntimeError):
    """Host 正在修改运行环境，或有需要人工核实的中断操作。"""


class HostMutationGate:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._active: tuple[str, Any] | None = None
        self._restart: object | None = None

    def _unresolved(self) -> list[str]:
        path = self.root / 'driver_package_operations.json'
        if not path.exists():
            return []
        try:
            operations = json.loads(path.read_text(encoding='utf-8'))['operations']
            return [item['operation_id'] for item in operations
                    if item.get('requires_confirmation') or item['status'] == 'running']
        except (ValueError, KeyError, TypeError) as exc:
            raise HostMutationBusy('包操作台账无法读取，请先修复后再启动设备') from exc

    def acquire(self, kind: str, *, recovery: bool = False):
        with self._lock:
            if self._active is not None or self._restart is not None and not (recovery and kind == 'recovery_confirmation'):
                raise HostMutationBusy('Host 正在等待重启或执行包变更/设备启动，请稍后重试')
            self.root.mkdir(parents=True, exist_ok=True)
            stream = (self.root / '.host-mutation.lock').open('a+b')
            try:
                acquire_exclusive_file_lock(stream, blocking=False)
                if not recovery and self._unresolved():
                    raise HostMutationBusy('存在未完成或待人工确认的包操作，请先查看并处置 operations')
            except BaseException as exc:
                stream.close()
                if isinstance(exc, BlockingIOError):
                    raise HostMutationBusy('其他进程正在修改驱动包或启动设备') from exc
                raise
            self._active = (kind, stream)
            return stream

    def release(self, stream) -> None:
        with self._lock:
            if not self._active or self._active[1] is not stream:
                raise RuntimeError('Host 变更锁身份不匹配')
            release_file_lock(stream)
            stream.close()
            self._active = None

    @contextmanager
    def hold(self, kind: str, *, recovery: bool = False) -> Iterator[None]:
        token = self.acquire(kind, recovery=recovery)
        try:
            yield
        finally:
            self.release(token)

    def request_restart(self, owner: object) -> None:
        with self._lock:
            if self._restart is not None and self._restart is not owner:
                raise HostMutationBusy('已有另一个重启请求')
            self._restart = owner

    def claim_restart(self, owner: object) -> bool:
        with self._lock:
            if self._restart is not owner:
                return False
            if self._active:
                return self._active[0] == 'restart'
            # 原子取得 OS 锁后保持到进程退出，防止检查为空后又开始安装。
            self._restart = None
            try:
                self.acquire('restart')
            except HostMutationBusy:
                return False
            finally:
                self._restart = owner
            return True

    def cancel_restart(self, owner: object) -> None:
        with self._lock:
            if self._restart is not owner:
                return
            self._restart = None
            if self._active and self._active[0] == 'restart':
                self.release(self._active[1])

    def status(self) -> dict[str, Any]:
        with self._lock:
            try:
                unresolved, error = self._unresolved(), None
            except HostMutationBusy as exc:
                unresolved, error = [], str(exc)
            return {'active': self._active[0] if self._active else None,
                    'restart_pending': self._restart is not None,
                    'unresolved_operations': unresolved, 'error': error}


_gates: dict[Path, HostMutationGate] = {}
_gates_lock = threading.Lock()


def get_host_mutation_gate(working_dir: str | Path) -> HostMutationGate:
    root = Path(working_dir).resolve()
    with _gates_lock:
        return _gates.setdefault(root, HostMutationGate(root))
