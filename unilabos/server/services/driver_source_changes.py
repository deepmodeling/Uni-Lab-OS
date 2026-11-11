"""按进程启动时的源码快照提示重启；只观察文件，不执行重启。"""

from __future__ import annotations

import atexit
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from weakref import WeakValueDictionary

from unilabos.utils import logger

_SUFFIXES = {".py", ".pyi", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".xml", ".xdl", ".pyd", ".so", ".dll"}
_IGNORED = {".git", ".hg", ".svn", ".staging", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "build", "dist", "logs"}


def source_snapshot(roots: list[str]) -> dict[str, str]:
    """内容摘要识别同时间戳改写；不跟随目录符号链接，避免循环扫描。"""
    files: set[Path] = set()

    def failed(error: OSError) -> None:
        raise error

    for raw in roots:
        root = Path(raw)
        if root.is_file():
            files.add(root)
        elif root.is_dir():
            for directory, dirs, names in os.walk(root, onerror=failed):
                dirs[:] = sorted(name for name in dirs if name not in _IGNORED and not name.endswith(".egg-info"))
                files.update(Path(directory) / name for name in names if Path(name).suffix.lower() in _SUFFIXES)
    snapshot = {}
    for path in sorted(files):
        with path.open("rb") as handle:
            snapshot[str(path)] = hashlib.file_digest(handle, "sha256").hexdigest()
    return snapshot


@dataclass
class _Target:
    kind: str
    id: str
    name: str
    pid: int | None
    roots: list[str]
    packages: list[str]
    auto_start: bool
    baseline: dict[str, str]
    candidate: dict[str, str] | None = None
    changes: dict[str, str] = field(default_factory=dict)
    reasons: set[str] = field(default_factory=set)
    changed_packages: set[str] = field(default_factory=set)
    detected_at_ms: int | None = None
    scan_error: str | None = None
    announced: str = ""


class DriverSourceChanges:
    """每次实际启动重建基线；读提示不清除，停止或重新启动才清除。"""

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self._lock = threading.RLock()
        self._targets: dict[str, _Target] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutation = False

    def register(
        self, target_id: str, *, kind: str, name: str, pid: int | None,
        roots: list[str], packages: list[str], auto_start: bool = True,
    ) -> None:
        roots = sorted({str(Path(root).resolve()) for root in roots})
        error = None
        try:
            baseline = source_snapshot(roots)
        except OSError as exc:
            baseline, error = {}, str(exc)
        target = _Target(kind, target_id, name, pid, roots, packages, auto_start, baseline, scan_error=error)
        if error:
            target.reasons.add("source_scan_failed")
        with self._lock:
            self._targets[target_id] = target

    def set_pid(self, target_id: str, pid: int) -> None:
        with self._lock:
            self._targets[target_id].pid = pid

    def remove(self, target_id: str) -> None:
        with self._lock:
            self._targets.pop(target_id, None)

    def describe(self, target_id: str, *, name: str, auto_start: bool) -> None:
        """运行中编辑进程名称或自动启动策略，不改变它已经加载的源码基线。"""
        with self._lock:
            target = self._targets.get(target_id)
            if target is not None:
                target.name = name
                target.auto_start = auto_start

    def mutation(self, active: bool) -> None:
        """源码事务中的中间文件状态不作为稳定变更通知。"""
        with self._lock:
            self._mutation = active
            if active:
                for target in self._targets.values():
                    target.candidate = None

    def package_changed(self, name: str, *, reason: str, host_mount: bool = False, shared_dependencies: bool = False) -> None:
        normalized = name.lower().replace("-", "_")
        with self._lock:
            for target in self._targets.values():
                selected = normalized in {item.lower().replace("-", "_") for item in target.packages}
                if shared_dependencies or selected or (target.kind == "host" and host_mount):
                    target.reasons.add("shared_dependencies_changed" if shared_dependencies else reason)
                    target.changed_packages.add(name)
                    target.detected_at_ms = target.detected_at_ms or int(time.time() * 1000)

    def poll(self) -> None:
        """连续两次内容相同才发布，合并编辑器的临时删除和原子保存。"""
        with self._lock:
            if self._mutation:
                return
            targets = list(self._targets.values())
        snapshots: dict[tuple[str, ...], dict[str, str]] = {}
        for target in targets:
            try:
                key = tuple(target.roots)
                if key not in snapshots:
                    snapshots[key] = source_snapshot(target.roots)
                current = snapshots[key]
            except OSError as exc:
                with self._lock:
                    target.scan_error = str(exc)
                continue
            with self._lock:
                if self._mutation or self._targets.get(target.id) is not target:
                    continue
                target.scan_error = None
                if current == target.candidate:
                    for path in sorted(target.baseline.keys() | current.keys()):
                        if target.baseline.get(path) != current.get(path):
                            target.changes[path] = "added" if path not in target.baseline else "deleted" if path not in current else "modified"
                    if target.changes:
                        target.reasons.add("source_files_changed")
                        target.detected_at_ms = target.detected_at_ms or int(time.time() * 1000)
                target.candidate = current
                if target.reasons:
                    signature = repr((sorted(target.reasons), sorted(target.changed_packages), sorted(target.changes.items())))
                    if signature != target.announced:
                        target.announced = signature
                        affected = "；同时停止本机托管 slave，按 auto_start 恢复" if target.kind == "host" else "；只重启该 slave"
                        logger.warning(
                            f"[DriverSourceChanges] {target.kind} {target.name} (pid={target.pid}) 需要重启{affected}；"
                            f"变更文件：{', '.join(sorted(target.changes)[:8]) or '包或依赖配置变更'}；"
                            "请查询 unilab package changes 查看完整范围，确认设备任务结束后操作"
                        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            slaves = [{"id": t.id, "name": t.name, "pid": t.pid, "auto_start": t.auto_start}
                      for t in self._targets.values() if t.kind == "slave"]
            notices = []
            for target in self._targets.values():
                if not target.reasons:
                    continue
                host = target.kind == "host"
                notices.append({
                    "target": target.kind, "process_id": target.id, "name": target.name, "pid": target.pid,
                    "reasons": sorted(target.reasons), "detected_at_ms": target.detected_at_ms,
                    "changed_files": [{"path": path, "change": change} for path, change in sorted(target.changes.items())],
                    "packages": list(target.packages),
                    "changed_packages": sorted(target.changed_packages),
                    "message": ("需重启 Host；将停止本机托管 slave，仅 auto_start=true 的进程自动恢复。"
                                if host else f"需重启 slave「{target.name}」；Host 与其他 slave 不因此重启。"
                                "现有 slave 重启接口立即停止进程，请先确认该进程的设备任务已结束。"),
                    "affected_slaves": slaves if host else [],
                    "restart_request": {"method": "POST", "path": "/api/v1/restart" if host else
                                        f"/api/v1/device-processes/{quote(target.id, safe='')}/restart",
                                        "body": {"mode": "quiescent", "scope": "auto"} if host else None},
                    "requires_confirmation": True,
                    "waits_for_idle": host,
                })
            return {
                "policy": "notify", "automatic_restart": False, "poll_interval_seconds": self.interval,
                "restart_required": bool(notices), "notices": notices,
                "watching": [{"target": t.kind, "process_id": t.id, "name": t.name, "pid": t.pid,
                              "roots": list(t.roots), "scan_error": t.scan_error} for t in self._targets.values()],
                "coverage": "当前 Host 和本机托管 slave；远程及手动启动的 slave 不在此进程清单内",
            }

    def start(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="DriverSourceChanges", daemon=True)
                self._thread.start()
                atexit.register(self.close)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception as exc:  # 文件监测失败不影响设备执行，下一轮继续。
                logger.warning(f"[DriverSourceChanges] 源码检测失败：{exc}")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


_monitors: WeakValueDictionary[str, DriverSourceChanges] = WeakValueDictionary()
_monitors_lock = threading.Lock()


def get_source_changes(working_dir: str | Path) -> DriverSourceChanges:
    """Host 包服务与进程服务共享观察器；不引入独立守护进程。"""
    key = str(Path(working_dir).resolve())
    with _monitors_lock:
        monitor = _monitors.get(key)
        if monitor is None:
            monitor = DriverSourceChanges()
            _monitors[key] = monitor
        return monitor


def source_roots(scan_dirs: list[str], records: Any, *, external_only: bool) -> tuple[list[str], list[str]]:
    """按实际挂载目录追踪源码及项目定义，覆盖 data 缓存和外部开发目录。"""
    roots = {str(Path(item).resolve()) for item in scan_dirs}
    packages = []
    for record in records.values():
        if any(str(Path(item).resolve()) in roots for item in record.package_dirs):
            packages.append(record.name)
            if record.package_root:
                roots.add(str(Path(record.package_root).resolve()))
    if not external_only:
        roots.add(str(Path(__file__).resolve().parents[2] / "devices"))
    return sorted(roots), packages
