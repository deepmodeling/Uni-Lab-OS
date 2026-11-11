"""受管设备进程：把设备驱动跑在本机 Slave 子进程里，由 Host 拉起、看护、重启。

「装驱动包」解决的是代码进得来；「设备进程」解决的是设备怎么跑起来：
每个受管进程 = 一份 slave 图（一个或多个设备节点）+ 要挂载的驱动包目录 +
重启策略。Host 用 ``python -m unilabos --is_slave --host_node_ip <本机> …`` 拉起子进程，
子进程经 HostLink 接回本 Host；驱动崩了只影响这个子进程，按策略自动拉起，
Host 本体、物料权威和调度都不受影响；改驱动、改配置也只需重启这个子进程。

持久化：``<working_dir>/device_processes.json``（规格）与 ``<working_dir>/device_processes/<id>/``
（图文件、日志）；working_dir 即 BasicConfig.working_dir（unilabos_data 目录）。
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from unilabos.utils import logger
from unilabos.utils.log_notices import log_notices
from unilabos.server.services.driver_source_changes import get_source_changes, source_roots

SPEC_FILENAME = "device_processes.json"
PROCESS_DIRNAME = "device_processes"
STOP_GRACE_S = 8.0
RESTART_BACKOFF_S = (2.0, 4.0, 8.0, 15.0, 30.0)
RESTART_POLICIES = ("never", "on-failure", "always")


class DeviceProcessError(RuntimeError):
    """受管进程操作的可预期错误。"""


@dataclass
class DeviceProcessSpec:
    """一条受管进程的静态配置（可编辑、持久化）。"""

    id: str
    name: str
    # slave 图的 nodes[]（node-link 形状），服务把它写成 graph.json 传给 -g
    graph_nodes: List[Dict[str, Any]] = field(default_factory=list)
    # 额外 --devices 目录；驱动包目录按 package_names 从驱动包台账解析
    devices_dirs: List[str] = field(default_factory=list)
    package_names: List[str] = field(default_factory=list)
    external_only: bool = False
    auto_start: bool = True
    restart_policy: str = "on-failure"
    max_restarts: int = 5
    extra_args: List[str] = field(default_factory=list)
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass
class DeviceProcessRuntime:
    """一条受管进程的运行态（内存）。"""

    status: str = "stopped"  # stopped | starting | running | crashed | restarting
    pid: Optional[int] = None
    started_at_ms: Optional[int] = None
    stopped_at_ms: Optional[int] = None
    last_exit_code: Optional[int] = None
    restart_count: int = 0
    last_error: Optional[str] = None
    command: List[str] = field(default_factory=list)
    log_path: str = ""


def _now_ms() -> int:
    return int(time.time() * 1000)


def spec_path(working_dir: str | Path) -> Path:
    return Path(working_dir) / SPEC_FILENAME


def process_dir(working_dir: str | Path, process_id: str) -> Path:
    return Path(working_dir) / PROCESS_DIRNAME / process_id


def load_specs(working_dir: str | Path) -> Dict[str, DeviceProcessSpec]:
    path = spec_path(working_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[DeviceProcesses] 规格文件读取失败: {exc}")
        return {}
    specs: Dict[str, DeviceProcessSpec] = {}
    for item in data.get("processes", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        known = {key: item[key] for key in DeviceProcessSpec.__dataclass_fields__ if key in item}
        specs[str(item["id"])] = DeviceProcessSpec(**known)
    return specs


def save_specs(working_dir: str | Path, specs: Dict[str, DeviceProcessSpec]) -> None:
    path = spec_path(working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "processes": [asdict(item) for item in specs.values()]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _existing_device_uuid(device_id: str) -> Optional[str]:
    """物料权威里若已有同 id 的设备根物料，沿用其 uuid（Graph Authority 按 uuid 校验身份，
    换 uuid 会被 identity_conflict 拒绝）。"""
    try:
        from unilabos.server.backend.composition import get_materials_service

        service = get_materials_service()
        if service is None:
            return None
        return str(service.get_material_by_resource_id(device_id).material.material_uuid)
    except Exception:  # noqa: BLE001 - MaterialNotFoundError 或权威不可用都按新设备处理
        return None


def build_device_node(
    device_id: str,
    device_class: str,
    *,
    name: str = "",
    config: Optional[Dict[str, Any]] = None,
    pose: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按 graph.json 的 node-link 约定生成一个设备节点。

    uuid 是设备身份：权威里已有同 id 设备就沿用，否则新发号并固定在规格里。
    """
    device_id = device_id.strip()
    if not device_id:
        raise DeviceProcessError("device id 不能为空")
    return {
        "id": device_id,
        "uuid": _existing_device_uuid(device_id) or str(uuid.uuid4()),
        "name": name.strip() or device_id,
        "parent": None,
        "type": "device",
        "class": device_class.strip(),
        "template_name": device_class.strip(),
        "sites_initialized": True,
        "sites": [],
        "pose": pose or {"position": {"x": 0, "y": 0, "z": 0}, "size": {"width": 300, "height": 240, "depth": 0}},
        "config": dict(config or {}),
        "data": {},
        "extra": {},
    }


class DeviceProcessService:
    """受管 Slave 子进程的规格、生命周期与看护。"""

    def __init__(self, working_dir: str | Path) -> None:
        self.working_dir = Path(working_dir)
        self._lock = threading.RLock()
        self._runtime: Dict[str, DeviceProcessRuntime] = {}
        self._procs: Dict[str, subprocess.Popen[bytes]] = {}
        self._stop_requested: set[str] = set()
        self._monitor: Optional[threading.Thread] = None
        self._closing = False
        self.source_changes = get_source_changes(self.working_dir)
        atexit.register(self.shutdown)

    # ── 环境 ──────────────────────────────────────────────────────

    @staticmethod
    def host_link_target() -> Dict[str, Any]:
        """本机 Slave 应连接的 HostLink 地址：绑定 0.0.0.0 时用回环。"""
        from unilabos.config.config import HostLinkConfig

        bind = str(getattr(HostLinkConfig, "bind", "") or "").strip()
        host = "127.0.0.1" if bind in ("", "0.0.0.0", "::") else bind
        return {"host": host, "port": int(getattr(HostLinkConfig, "port", 7302) or 7302)}

    def _package_dirs(self, names: List[str]) -> List[str]:
        from unilabos.server.services.driver_packages import load_ledger

        ledger = load_ledger(self.working_dir)
        dirs: List[str] = []
        missing: List[str] = []
        for name in names:
            record = ledger.get(name.strip().lower().replace("-", "_"))
            if record is None:
                missing.append(name)
                continue
            dirs.extend(item for item in record.package_dirs if item not in dirs)
        if missing:
            raise DeviceProcessError(f"驱动包不在台账中：{', '.join(missing)}")
        return dirs

    def build_command(self, spec: DeviceProcessSpec) -> List[str]:
        from unilabos.config.config import BasicConfig

        target = self.host_link_target()
        graph_file = self._write_graph(spec)
        backend = str(getattr(BasicConfig, "backend", "hostlink") or "hostlink")
        command = [
            sys.executable,
            "-m",
            "unilabos",
            "--backend",
            backend,
            "--is_slave",
            "--machine_name",
            f"{BasicConfig.machine_name}_managed_{spec.id.replace('-', '_')}",
            "--skip_env_check",
            "--disable_browser",
            "--visual",
            "disable",
            "--host_node_ip",
            target["host"],
            "--hostlink_port",
            str(target["port"]),
            "-g",
            str(graph_file),
        ]
        for item in [*self._package_dirs(spec.package_names), *spec.devices_dirs]:
            command.extend(["--devices", item])
        if spec.external_only:
            command.append("--external_devices_only")
        command.extend(str(item) for item in spec.extra_args if str(item).strip())
        return command

    def _register_source_watch(self, spec: DeviceProcessSpec, command: List[str]) -> None:
        """观察子进程实际命令和工作目录会挂载的目录，包含额外参数及启用台账。"""
        from unilabos.app.cli.parser import build_parser
        from unilabos.config.working_dir import resolve_working_dir
        from unilabos.server.services.driver_packages import enabled_package_dirs, load_ledger

        try:
            args, _ = build_parser().parse_known_args(command[3:])
        except (SystemExit, argparse.ArgumentError) as exc:
            raise DeviceProcessError("设备进程启动参数无效，无法确定源码挂载目录") from exc

        def absolute(value: str | None) -> str | None:
            return str((self.working_dir / Path(value).expanduser()).resolve()) if value else None

        child_dir = resolve_working_dir(absolute(args.working_dir), absolute(args.config), cwd=str(self.working_dir))
        records = {**load_ledger(self.working_dir), **load_ledger(child_dir)}
        dirs = [absolute(item) for item in args.devices or []] + enabled_package_dirs(child_dir)
        roots, packages = source_roots(dirs, records, external_only=args.external_devices_only)
        self.source_changes.register(spec.id, kind="slave", name=spec.name, pid=None, roots=roots,
                                     packages=packages, auto_start=spec.auto_start)

    @staticmethod
    def graph_filename(spec: DeviceProcessSpec) -> str:
        """图文件名即 Graph Authority 里的图名：带进程名与 id 前缀，多个受管进程不互相覆盖。"""
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in spec.name.strip()) or "process"
        return f"managed_{slug}_{spec.id[:8]}.json"

    def _write_graph(self, spec: DeviceProcessSpec) -> Path:
        if not spec.graph_nodes:
            raise DeviceProcessError("受管进程至少要有一个设备节点")
        directory = process_dir(self.working_dir, spec.id)
        directory.mkdir(parents=True, exist_ok=True)
        graph_file = directory / self.graph_filename(spec)
        graph_file.write_text(
            json.dumps({"nodes": spec.graph_nodes, "links": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return graph_file

    # ── 规格 CRUD ─────────────────────────────────────────────────

    def list(self) -> List[Dict[str, Any]]:
        specs = load_specs(self.working_dir)
        return [self._view(spec) for spec in specs.values()]

    def get(self, process_id: str) -> Dict[str, Any]:
        spec = load_specs(self.working_dir).get(process_id)
        if spec is None:
            raise DeviceProcessError(f"device process not found: {process_id}")
        return self._view(spec)

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        spec = self._spec_from_payload(payload, existing=None)
        specs = load_specs(self.working_dir)
        specs[spec.id] = spec
        save_specs(self.working_dir, specs)
        self._write_graph(spec)
        log_notices.changed(sources_changed=True)
        return self._view(spec)

    def update(self, process_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        specs = load_specs(self.working_dir)
        current = specs.get(process_id)
        if current is None:
            raise DeviceProcessError(f"device process not found: {process_id}")
        spec = self._spec_from_payload(payload, existing=current)
        specs[process_id] = spec
        save_specs(self.working_dir, specs)
        self.source_changes.describe(process_id, name=spec.name, auto_start=spec.auto_start)
        self._write_graph(spec)
        log_notices.changed(sources_changed=True)
        return self._view(spec)

    def delete(self, process_id: str) -> None:
        specs = load_specs(self.working_dir)
        if process_id not in specs:
            raise DeviceProcessError(f"device process not found: {process_id}")
        self.stop(process_id, wait=True)
        specs.pop(process_id)
        save_specs(self.working_dir, specs)
        with self._lock:
            self._runtime.pop(process_id, None)
        log_notices.changed(sources_changed=True)

    def _spec_from_payload(self, payload: Dict[str, Any], existing: Optional[DeviceProcessSpec]) -> DeviceProcessSpec:
        name = str(payload.get("name") or (existing.name if existing else "")).strip()
        if not name:
            raise DeviceProcessError("name 不能为空")
        nodes = payload.get("graph_nodes", existing.graph_nodes if existing else None)
        if not isinstance(nodes, list) or not nodes or not all(isinstance(item, dict) and item.get("id") and item.get("class") for item in nodes):
            raise DeviceProcessError("graph_nodes 必须是非空列表，每个节点至少含 id 与 class")
        policy = str(payload.get("restart_policy", existing.restart_policy if existing else "on-failure"))
        if policy not in RESTART_POLICIES:
            raise DeviceProcessError(f"restart_policy 只能是 {', '.join(RESTART_POLICIES)}")
        package_names = [str(item) for item in payload.get("package_names", existing.package_names if existing else [])]
        self._package_dirs(package_names)  # 校验都在台账里
        now = _now_ms()
        return DeviceProcessSpec(
            id=existing.id if existing else str(payload.get("id") or uuid.uuid4()),
            name=name,
            graph_nodes=nodes,
            devices_dirs=[str(item) for item in payload.get("devices_dirs", existing.devices_dirs if existing else [])],
            package_names=package_names,
            external_only=bool(payload.get("external_only", existing.external_only if existing else False)),
            auto_start=bool(payload.get("auto_start", existing.auto_start if existing else True)),
            restart_policy=policy,
            max_restarts=max(0, int(payload.get("max_restarts", existing.max_restarts if existing else 5))),
            extra_args=[str(item) for item in payload.get("extra_args", existing.extra_args if existing else [])],
            created_at_ms=existing.created_at_ms if existing else now,
            updated_at_ms=now,
        )

    # ── 生命周期 ──────────────────────────────────────────────────

    def start(self, process_id: str) -> Dict[str, Any]:
        spec = load_specs(self.working_dir).get(process_id)
        if spec is None:
            raise DeviceProcessError(f"device process not found: {process_id}")
        with self._lock:
            proc = self._procs.get(process_id)
            if proc is not None and proc.poll() is None:
                raise DeviceProcessError(f"{spec.name} 已在运行（pid {proc.pid}）")
            self._stop_requested.discard(process_id)
            self._launch(spec, reset_restart_count=True)
        self._ensure_monitor()
        return self._view(spec)

    def stop(self, process_id: str, *, wait: bool = True) -> Dict[str, Any]:
        spec = load_specs(self.working_dir).get(process_id)
        if spec is None:
            raise DeviceProcessError(f"device process not found: {process_id}")
        with self._lock:
            self._stop_requested.add(process_id)
            proc = self._procs.get(process_id)
            runtime = self._runtime.setdefault(process_id, DeviceProcessRuntime())
        if proc is not None and proc.poll() is None:
            self._terminate(proc, wait=wait)
        with self._lock:
            runtime.status = "stopped"
            runtime.pid = None
            runtime.stopped_at_ms = _now_ms()
            if proc is not None:
                runtime.last_exit_code = proc.poll()
            self._procs.pop(process_id, None)
            self.source_changes.remove(process_id)
        log_notices.changed(f"managed:{process_id}", sources_changed=True)
        return self._view(spec)

    def restart(self, process_id: str) -> Dict[str, Any]:
        self.stop(process_id, wait=True)
        return self.start(process_id)

    def start_auto(self) -> List[str]:
        """Host 启动完成后拉起 auto_start 的进程；返回启动的 id。"""
        started: List[str] = []
        for spec in load_specs(self.working_dir).values():
            if not spec.auto_start:
                continue
            try:
                self.start(spec.id)
                started.append(spec.id)
            except DeviceProcessError as exc:
                logger.warning(f"[DeviceProcesses] 自动启动 {spec.name} 失败: {exc}")
        return started

    def shutdown(self) -> None:
        """Host 退出 / 重启时结束全部子进程（不留孤儿）。"""
        self._closing = True
        with self._lock:
            procs = list(self._procs.items())
        for process_id, proc in procs:
            self._stop_requested.add(process_id)
            if proc.poll() is None:
                self._terminate(proc, wait=True)
            self.source_changes.remove(process_id)

    # ── 内部：启动 / 结束 / 看护 ─────────────────────────────────

    def _launch(self, spec: DeviceProcessSpec, *, reset_restart_count: bool) -> None:
        runtime = self._runtime.setdefault(spec.id, DeviceProcessRuntime())
        command = self.build_command(spec)
        directory = process_dir(self.working_dir, spec.id)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "process.log"
        runtime.command = command
        runtime.log_path = str(log_path)
        runtime.status = "starting"
        runtime.last_error = None
        if reset_restart_count:
            runtime.restart_count = 0
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        # 输出进的是文件 / API，不是终端：关掉着色，日志页才不会满屏 ESC 序列
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        # 在 Popen 前取基线，子进程异步导入期间发生的编辑也必须提示。
        self._register_source_watch(spec, command)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.source_changes.remove(spec.id)
            runtime.status = "crashed"
            runtime.last_error = str(exc)
            raise DeviceProcessError(f"无法启动子进程：{exc}") from exc
        runtime.pid = proc.pid
        runtime.started_at_ms = _now_ms()
        runtime.stopped_at_ms = None
        runtime.status = "running"
        self._procs[spec.id] = proc
        self.source_changes.set_pid(spec.id, proc.pid)
        self.source_changes.start()
        log_notices.changed(f"managed:{spec.id}", sources_changed=True)
        threading.Thread(
            target=self._pump_output,
            args=(spec.id, proc, log_path),
            name=f"DeviceProcessLog-{spec.id[:8]}",
            daemon=True,
        ).start()
        logger.info(f"[DeviceProcesses] 已启动 {spec.name} (pid {proc.pid})")

    def _pump_output(self, process_id: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
        with log_path.open("ab") as handle:
            assert proc.stdout is not None
            for raw in proc.stdout:
                handle.write(raw)
                handle.flush()
                log_notices.changed(f"managed:{process_id}")

    def _terminate(self, proc: subprocess.Popen[bytes], *, wait: bool) -> None:
        try:
            proc.terminate()
        except OSError:
            return
        if not wait:
            return
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._monitor = threading.Thread(target=self._watch, name="DeviceProcessMonitor", daemon=True)
            self._monitor.start()

    def _watch(self) -> None:
        """看护线程：发现意外退出按策略退避重启。"""
        while not self._closing:
            time.sleep(1.0)
            with self._lock:
                items = list(self._procs.items())
            for process_id, proc in items:
                code = proc.poll()
                if code is None:
                    continue
                with self._lock:
                    # 采样后可能已由用户重启，旧进程退出不能覆盖新实例及其源码基线。
                    if self._procs.get(process_id) is not proc:
                        continue
                    runtime = self._runtime.setdefault(process_id, DeviceProcessRuntime())
                    runtime.last_exit_code = code
                    runtime.pid = None
                    runtime.stopped_at_ms = _now_ms()
                    self._procs.pop(process_id, None)
                    self.source_changes.remove(process_id)
                    requested = process_id in self._stop_requested
                log_notices.changed(f"managed:{process_id}", sources_changed=True)
                if requested:
                    runtime.status = "stopped"
                    continue
                spec = load_specs(self.working_dir).get(process_id)
                runtime.status = "crashed"
                runtime.last_error = f"子进程退出，退出码 {code}"
                logger.warning(f"[DeviceProcesses] {process_id} 意外退出 code={code}")
                if spec is None:
                    continue
                should_restart = spec.restart_policy == "always" or (spec.restart_policy == "on-failure" and code != 0)
                if not should_restart or runtime.restart_count >= spec.max_restarts:
                    if should_restart:
                        runtime.last_error = f"已连续重启 {runtime.restart_count} 次仍退出（code {code}），停止看护"
                    continue
                delay = RESTART_BACKOFF_S[min(runtime.restart_count, len(RESTART_BACKOFF_S) - 1)]
                runtime.status = "restarting"
                runtime.restart_count += 1
                threading.Timer(delay, self._restart_later, args=(process_id,)).start()

    def _restart_later(self, process_id: str) -> None:
        if self._closing or process_id in self._stop_requested:
            return
        spec = load_specs(self.working_dir).get(process_id)
        if spec is None:
            return
        with self._lock:
            try:
                self._launch(spec, reset_restart_count=False)
            except DeviceProcessError as exc:
                runtime = self._runtime.setdefault(process_id, DeviceProcessRuntime())
                runtime.status = "crashed"
                runtime.last_error = str(exc)

    # ── 视图 ──────────────────────────────────────────────────────

    def _view(self, spec: DeviceProcessSpec) -> Dict[str, Any]:
        with self._lock:
            runtime = self._runtime.get(spec.id) or DeviceProcessRuntime()
            proc = self._procs.get(spec.id)
            if proc is not None and proc.poll() is None and runtime.status in ("stopped", "crashed"):
                runtime.status = "running"
            view = asdict(runtime)
        view.update(asdict(spec))
        view["device_ids"] = [str(node.get("id")) for node in spec.graph_nodes]
        view["graph_path"] = str(process_dir(self.working_dir, spec.id) / self.graph_filename(spec))
        notices = [item for item in self.source_changes.status()["notices"] if item["process_id"] == spec.id]
        view["restart_required"] = bool(notices)
        view["restart_notices"] = notices
        return view


_service: Optional[DeviceProcessService] = None
_service_lock = threading.Lock()


def get_device_process_service() -> DeviceProcessService:
    global _service
    with _service_lock:
        if _service is None:
            from unilabos.config.config import BasicConfig

            _service = DeviceProcessService(getattr(BasicConfig, "working_dir", None) or Path.cwd())
        return _service


__all__ = [
    "DeviceProcessError",
    "DeviceProcessService",
    "DeviceProcessSpec",
    "build_device_node",
    "get_device_process_service",
    "load_specs",
    "save_specs",
]
