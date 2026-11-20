"""受管设备进程：把设备驱动跑在本机 Slave 子进程里，由 Host 拉起、看护、重启。

「装驱动包」解决的是代码进得来；「设备进程」解决的是设备怎么跑起来：
每个受管进程 = 一份 slave 图（一个或多个设备节点）+ 要挂载的驱动包目录 +
重启策略。Host 用 ``python -m unilabos --is_slave --host_node_ip <本机> …`` 拉起子进程，
子进程经 HostLink 接回本 Host；驱动崩了只影响这个子进程，按策略自动拉起，
Host 本体、物料权威和调度都不受影响；改驱动、改配置也只需重启这个子进程。

受管 Slave 跟随拉起它的 Host：拉起时写入 Host 的 pid（``app/lifetime.py``），Host 无论怎么没了
（含 ``kill -9``、崩溃），Slave 都察觉并退出，不留下来占着各自的数据库根目录与 HostLink 连接；
Windows 上另放进 KILL_ON_JOB_CLOSE 的 Job Object 兜底。Host 有序退出时由 :meth:`DeviceProcessService.shutdown`
（atexit）先一起请全部 Slave 退出，再限时等待。

持久化：``<working_dir>/device_processes.json``（规格）与 ``<working_dir>/device_processes/<id>/``
（图文件、日志，以及该 Slave 自己的数据库根目录 ``database/``）；working_dir 即
BasicConfig.working_dir（unilabos_data 目录）。一个数据库根目录只归一个进程，受管 Slave 不能与
Host / 权威共用 ``<working_dir>/database``，所以每个受管进程各有一个。

生成的规格（``generated_by`` 非空）不是用户手建的：``unilab --root_devices_as_slaves`` 每次 Host
启动时按设备图重建（:meth:`DeviceProcessService.sync_generated`），API 里照常可见、可启停 / 重启，
但不能编辑或删除——改设备图后重启 Host 即可。手建的规格从不被生成流程改动。

受管进程没在运行（崩溃、退避重启中、被手动停止）时，:meth:`DeviceProcessService.unavailable_devices`
给出其图中每台设备的不可用原因（退出码、重启次数、日志路径与日志末尾），Host 的能力快照据此登记
停用 route，权威的 ``/devices`` 显示设备不可用及原因，而不是让它无声无息地消失。
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from unilabos.app.lifetime import KillOnCloseJob, follow_parent_env
from unilabos.utils import logger
from unilabos.utils.log_notices import log_notices
from unilabos.server.services.driver_source_changes import get_source_changes, source_roots
from unilabos.server.services.host_mutations import HostMutationBusy, get_host_mutation_gate

SPEC_FILENAME = "device_processes.json"
PROCESS_DIRNAME = "device_processes"
#: 受管 Slave 的数据库根目录（在各自的进程目录下）：Slave 把启动图登记进本机 Graph Authority
PROCESS_DATABASE_DIRNAME = "database"
STOP_GRACE_S = 8.0
RESTART_BACKOFF_S = (2.0, 4.0, 8.0, 15.0, 30.0)
RESTART_POLICIES = ("never", "on-failure", "always")
#: 进程退出后保留的日志末尾行数（写进设备不可用原因，不必先去翻日志文件）
LOG_TAIL_LINES = 8
_LOG_TAIL_LINE_CHARS = 300
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class DeviceProcessError(RuntimeError):
    """受管进程操作的可预期错误。"""


class DeviceProcessBusyError(DeviceProcessError):
    """包变更或 Host 重启尚未完成。"""


class DeviceProcessGeneratedError(DeviceProcessError):
    """生成的规格不接受编辑 / 删除：它随 Host 启动按设备图重建。"""


@dataclass
class DeviceProcessSpec:
    """一条受管进程的静态配置（可编辑、持久化）。"""

    id: str
    name: str
    # slave 图的 nodes[]（node-link 形状），服务把它写成 graph.json 传给 -g
    graph_nodes: List[Dict[str, Any]] = field(default_factory=list)
    # slave 图的 links[]：两端都在本进程图里的连线（协议编译按物理连线找路径）
    graph_links: List[Dict[str, Any]] = field(default_factory=list)
    # 额外 --devices 目录；驱动包目录按 package_names 从驱动包台账解析
    devices_dirs: List[str] = field(default_factory=list)
    package_names: List[str] = field(default_factory=list)
    external_only: bool = False
    auto_start: bool = True
    restart_policy: str = "on-failure"
    max_restarts: int = 5
    extra_args: List[str] = field(default_factory=list)
    # 生成来源（如 ``root_devices_as_slaves``）；空串为用户手建
    generated_by: str = ""
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


def graph_filename(spec: DeviceProcessSpec) -> str:
    """图文件名即 Graph Authority 里的图名：带进程名与 id 前缀，多个受管进程不互相覆盖。"""
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in spec.name.strip()) or "process"
    return f"managed_{slug}_{spec.id[:8]}.json"


def write_graph(working_dir: str | Path, spec: DeviceProcessSpec) -> Path:
    """把规格的 slave 图（nodes + 内部 links）写到进程目录，返回传给 ``-g`` 的文件。"""

    if not spec.graph_nodes:
        raise DeviceProcessError("受管进程至少要有一个设备节点")
    directory = process_dir(working_dir, spec.id)
    directory.mkdir(parents=True, exist_ok=True)
    graph_file = directory / graph_filename(spec)
    graph_file.write_text(
        json.dumps({"nodes": spec.graph_nodes, "links": spec.graph_links}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return graph_file


def sync_generated_specs(
    working_dir: str | Path, generated_by: str, specs: List[DeviceProcessSpec]
) -> Dict[str, List[str]]:
    """把 ``generated_by`` 来源的规格整体换成 ``specs``（Host 启动时、拉起受管进程之前调用）。

    - 同 id 的生成规格沿用 ``created_at_ms``，内容不变时连 ``updated_at_ms`` 也不变，重启 Host 不会堆出新规格；
    - 本来源里不再出现的规格移除（进程目录里的日志保留，与 :meth:`DeviceProcessService.delete` 一致）；
    - 手建规格（``generated_by`` 为空）与其它来源的规格一概不动；与手建规格同 id 的条目跳过，
      由调用方按 ``conflicts`` 处理。

    只改规格文件与图文件；有正在运行的进程时用 :meth:`DeviceProcessService.sync_generated`。
    返回 ``{"created", "updated", "unchanged", "removed", "conflicts"}`` 各自的规格 id。
    """

    if not generated_by:
        raise DeviceProcessError("generated_by 不能为空：手建规格不经此入口")
    summary: Dict[str, List[str]] = {key: [] for key in ("created", "updated", "unchanged", "removed", "conflicts")}
    wanted = {item.id: item for item in specs}
    current = load_specs(working_dir)
    for process_id, existing in list(current.items()):
        if existing.generated_by == generated_by and process_id not in wanted:
            current.pop(process_id)
            summary["removed"].append(process_id)
    now = _now_ms()
    written: List[DeviceProcessSpec] = []
    for process_id, item in wanted.items():
        existing = current.get(process_id)
        if existing is not None and existing.generated_by != generated_by:
            summary["conflicts"].append(process_id)
            continue
        spec = DeviceProcessSpec(**{**asdict(item), "generated_by": generated_by})
        if existing is None:
            spec.created_at_ms = spec.updated_at_ms = now
            summary["created"].append(process_id)
        else:
            unstamped = {"created_at_ms": 0, "updated_at_ms": 0}
            same = {**asdict(existing), **unstamped} == {**asdict(spec), **unstamped}
            spec.created_at_ms = existing.created_at_ms or now
            spec.updated_at_ms = existing.updated_at_ms if same else now
            summary["unchanged" if same else "updated"].append(process_id)
        current[process_id] = spec
        written.append(spec)
    changed = any(summary[key] for key in ("created", "updated", "removed"))
    if changed:
        save_specs(working_dir, current)
    # 图文件总是按规格重写：进程目录可能被清理过，或上次写入中断
    for spec in written:
        write_graph(working_dir, spec)
    if changed:
        log_notices.changed(sources_changed=True)
    return summary


def _existing_device_uuid(device_id: str) -> Optional[str]:
    """物料权威里若已有同 id 的设备根物料，沿用其 uuid（Graph Authority 按 uuid 校验身份，
    换 uuid 会被 identity_conflict 拒绝）。"""
    try:
        from unilabos.server.execution_composition import get_materials_service

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


def _device_ids(spec: DeviceProcessSpec) -> List[str]:
    return [
        str(node.get("id"))
        for node in spec.graph_nodes
        if node.get("id") and str(node.get("type") or "device") == "device"
    ]


def _device_parents(spec: DeviceProcessSpec) -> Dict[str, str]:
    """设备 id → 图中最近的祖先设备 id（父节点用 ``parent`` id 或 ``parent_uuid`` 表达）。"""

    by_id = {str(node.get("id")): node for node in spec.graph_nodes if node.get("id")}
    by_uuid = {str(node.get("uuid")): node for node in spec.graph_nodes if node.get("uuid")}

    def parent_of(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        parent_uuid = str(node.get("parent_uuid") or "")
        if parent_uuid and parent_uuid in by_uuid:
            return by_uuid[parent_uuid]
        return by_id.get(str(node.get("parent") or ""))

    parents: Dict[str, str] = {}
    for device_id in _device_ids(spec):
        current = parent_of(by_id[device_id])
        seen = {device_id}
        while current is not None and str(current.get("id")) not in seen:
            seen.add(str(current.get("id")))
            if str(current.get("type") or "") == "device":
                parents[device_id] = str(current.get("id"))
                break
            current = parent_of(current)
    return parents


_ERROR_MARKERS = ("Error", "Exception", "Traceback", "错误", "失败", "异常")


def _last_error_line(tail: List[str]) -> str:
    for line in reversed(tail):
        if any(marker in line for marker in _ERROR_MARKERS):
            return line
    return ""


def _unavailable_reason(spec: DeviceProcessSpec, runtime: DeviceProcessRuntime, log_path: str, tail: List[str]) -> str:
    """受管进程没在运行时给设备目录看的原因：发生了什么、看护到哪一步、去哪看日志、怎么恢复。"""

    name = spec.name
    code = runtime.last_exit_code
    exited = f"退出码 {code}" if code is not None else "进程已结束"
    error_line = _last_error_line(tail)
    error = f"；日志最后的错误：{error_line}" if error_line else ""
    if runtime.status == "stopped":
        return f"受管设备进程 {name} 已停止；在设备进程页启动它后设备恢复（日志 {log_path}）"
    if runtime.status == "restarting":
        return (
            f"受管设备进程 {name} 意外退出（{exited}），按重启策略第 {runtime.restart_count}/{spec.max_restarts} 次"
            f"重启中{error}；日志 {log_path}"
        )
    should_restart = spec.restart_policy == "always" or (spec.restart_policy == "on-failure" and code != 0)
    if not should_restart:
        return (
            f"受管设备进程 {name} 已退出（{exited}），重启策略 {spec.restart_policy} 不自动重启{error}；"
            f"查看日志 {log_path} 后在设备进程页启动它"
        )
    if runtime.restart_count >= spec.max_restarts:
        return (
            f"受管设备进程 {name} 意外退出（{exited}），已自动重启 {runtime.restart_count} 次仍退出，停止看护{error}；"
            f"查看日志 {log_path}，修复后在设备进程页重启它"
        )
    return f"受管设备进程 {name} 重启失败：{runtime.last_error or exited}{error}；日志 {log_path}"


class DeviceProcessService:
    """受管 Slave 子进程的规格、生命周期与看护。"""

    def __init__(self, working_dir: str | Path) -> None:
        self.working_dir = Path(working_dir)
        self._lock = threading.RLock()
        self._runtime: Dict[str, DeviceProcessRuntime] = {}
        self._procs: Dict[str, subprocess.Popen[bytes]] = {}
        self._stop_requested: set[str] = set()
        # 本 Host 生命周期内拉起过的规格（不可用原因要用进程名与图中设备，不必每次读盘）
        self._launched: Dict[str, DeviceProcessSpec] = {}
        # 进程输出的最后几行（输出读完即定稿），写进设备不可用原因
        self._log_tails: Dict[str, List[str]] = {}
        self._monitor: Optional[threading.Thread] = None
        self._closing = False
        # Windows：受管 Slave 随本 Host 消亡由系统回收（拿不到 job 时靠 Slave 自己跟随父进程）
        self._job = KillOnCloseJob.create()
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
            # 不落在 Host / 权威独占的 <working_dir>/database：每个受管 Slave 自己一个根目录
            "--server_database_root",
            str(process_dir(self.working_dir, spec.id) / PROCESS_DATABASE_DIRNAME),
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
        roots, packages = source_roots(dirs, records)
        self.source_changes.register(spec.id, kind="slave", name=spec.name, pid=None, roots=roots,
                                     packages=packages, auto_start=spec.auto_start)

    @staticmethod
    def graph_filename(spec: DeviceProcessSpec) -> str:
        """图文件名即 Graph Authority 里的图名：带进程名与 id 前缀，多个受管进程不互相覆盖。"""
        return graph_filename(spec)

    def _write_graph(self, spec: DeviceProcessSpec) -> Path:
        return write_graph(self.working_dir, spec)

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
        self._refuse_generated(current, "编辑")
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
        self._refuse_generated(specs[process_id], "删除")
        self.stop(process_id, wait=True)
        specs.pop(process_id)
        save_specs(self.working_dir, specs)
        self._forget(process_id)
        log_notices.changed(sources_changed=True)

    @staticmethod
    def _refuse_generated(spec: DeviceProcessSpec, operation: str) -> None:
        if spec.generated_by:
            raise DeviceProcessGeneratedError(
                f"{spec.name} 由 Host 启动参数 --{spec.generated_by} 按设备图生成，每次 Host 启动时重建，"
                f"不能{operation}；可以启动 / 停止 / 重启。改设备图后重启 Host，或去掉该参数重启 Host 恢复为 Host 内运行"
            )

    def _forget(self, process_id: str) -> None:
        with self._lock:
            self._runtime.pop(process_id, None)
            self._launched.pop(process_id, None)
            self._log_tails.pop(process_id, None)

    def sync_generated(self, generated_by: str, specs: List[DeviceProcessSpec]) -> Dict[str, List[str]]:
        """:func:`sync_generated_specs` 的服务版：先停掉要移除且正在运行的生成规格进程。"""

        wanted = {item.id for item in specs}
        stale = [
            process_id
            for process_id, existing in load_specs(self.working_dir).items()
            if existing.generated_by == generated_by and process_id not in wanted
        ]
        for process_id in stale:
            # 停进程要等它退出（最多 STOP_GRACE_S）：不在持锁时等，看护线程与设备快照刷新照常
            with self._lock:
                proc = self._procs.get(process_id)
            if proc is not None and proc.poll() is None:
                self.stop(process_id, wait=True)
        with self._lock:
            summary = sync_generated_specs(self.working_dir, generated_by, specs)
            for process_id in summary["removed"]:
                self._forget(process_id)
        return summary

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
            graph_links=list(payload.get("graph_links", existing.graph_links if existing else [])),
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
        spec = load_specs(self.working_dir).get(process_id)
        if spec is None:
            raise DeviceProcessError(f"device process not found: {process_id}")
        try:
            # 在停止旧 slave 前取得变更锁，拒绝时保持正在运行的进程。
            with get_host_mutation_gate(self.working_dir).hold('slave_restart'):
                self.stop(process_id, wait=True)
                with self._lock:
                    self._stop_requested.discard(process_id)
                    self._launch_with_sources(spec, reset_restart_count=True)
                self._ensure_monitor()
                return self._view(spec)
        except HostMutationBusy as exc:
            raise DeviceProcessBusyError(str(exc)) from exc

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
        """Host 退出 / 重启时结束全部子进程（不留孤儿）。

        先一起请全部子进程退出，再共用一个 ``STOP_GRACE_S`` 等待，超时强制结束；逐个
        「请求 + 等待」在受管进程多时会把 Host 的退出拖得很长。
        """
        self._closing = True
        with self._lock:
            procs = list(self._procs.items())
            self._stop_requested.update(process_id for process_id, _ in procs)
        running = [proc for _, proc in procs if proc.poll() is None]
        for proc in running:
            self._terminate(proc, wait=False)
        deadline = time.monotonic() + STOP_GRACE_S
        for proc in running:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for process_id, _ in procs:
            self.source_changes.remove(process_id)

    # ── 内部：启动 / 结束 / 看护 ─────────────────────────────────

    def _launch(self, spec: DeviceProcessSpec, *, reset_restart_count: bool) -> None:
        try:
            with get_host_mutation_gate(self.working_dir).hold('slave_start'):
                self._launch_with_sources(spec, reset_restart_count=reset_restart_count)
        except HostMutationBusy as exc:
            raise DeviceProcessBusyError(str(exc)) from exc

    def _launch_with_sources(self, spec: DeviceProcessSpec, *, reset_restart_count: bool) -> None:
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
        # Slave 跟随本 Host 进程：Host 消失（含强制结束）时 Slave 自行退出。这里可能在 API 工作线程、
        # 重启定时器线程里执行，拉起线程不常驻，所以不用 Linux 死亡信号（它跟随的是线程）。
        follow_parent_env(env)
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
        if self._job is not None:
            self._job.assign(proc)
        runtime.pid = proc.pid
        runtime.started_at_ms = _now_ms()
        runtime.stopped_at_ms = None
        runtime.status = "running"
        self._procs[spec.id] = proc
        self._launched[spec.id] = spec
        self._log_tails.pop(spec.id, None)
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
        tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        with log_path.open("ab") as handle:
            assert proc.stdout is not None
            for raw in proc.stdout:
                handle.write(raw)
                handle.flush()
                line = _ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace")).strip()
                if line:
                    tail.append(line[:_LOG_TAIL_LINE_CHARS])
                log_notices.changed(f"managed:{process_id}")
        with self._lock:
            # 输出读完 = 进程已退出；同一 id 已被重新拉起时不覆盖新实例
            if self._procs.get(process_id) in (None, proc):
                self._log_tails[process_id] = list(tail)

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
                timer = threading.Timer(delay, self._restart_later, args=(process_id,))
                # 守护线程：退避中的重启不能拖住 Host 退出
                timer.daemon = True
                timer.start()

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
        # 生成规格的图带着设备子树里的物料节点：这里只列设备
        view["device_ids"] = _device_ids(spec)
        view["graph_path"] = str(process_dir(self.working_dir, spec.id) / self.graph_filename(spec))
        notices = [item for item in self.source_changes.status()["notices"] if item["process_id"] == spec.id]
        view["restart_required"] = bool(notices)
        view["restart_notices"] = notices
        return view

    # ── 设备不可用原因 ────────────────────────────────────────────

    def unavailable_devices(self) -> Dict[str, Dict[str, Any]]:
        """本 Host 拉起过、此刻没在运行的受管进程里每台设备的不可用记录（device_id → 记录）。

        只看内存态（Host 每 0.5 s 刷新一次设备快照，不读盘）：从未拉起过的规格不出现；进程在跑
        （含刚拉起、设备还没接入）也不出现。记录形状与 HostLink runtime 跳过设备时一致
        （``device_id`` / ``registry_name`` / ``backend`` / ``reason``），另带 ``managed_process``。
        """

        from unilabos.config.config import BasicConfig

        backend = str(getattr(BasicConfig, "backend", "") or "")
        records: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            items = [
                (process_id, spec, self._runtime.get(process_id), self._procs.get(process_id))
                for process_id, spec in self._launched.items()
            ]
            tails = {process_id: list(self._log_tails.get(process_id) or []) for process_id, *_ in items}
        for process_id, spec, runtime, proc in items:
            if runtime is None or runtime.status not in ("crashed", "restarting", "stopped"):
                continue
            if proc is not None and proc.poll() is None:
                continue
            log_path = runtime.log_path or str(process_dir(self.working_dir, process_id) / "process.log")
            reason = _unavailable_reason(spec, runtime, log_path, tails.get(process_id) or [])
            parents = _device_parents(spec)
            for node in spec.graph_nodes:
                if str(node.get("type") or "device") != "device":
                    continue
                device_id = str(node.get("id") or "")
                if not device_id:
                    continue
                record: Dict[str, Any] = {
                    "device_id": device_id,
                    "registry_name": str(node.get("template_name") or node.get("class") or ""),
                    "backend": backend,
                    "reason": reason,
                    "managed_process": {
                        "id": process_id,
                        "name": spec.name,
                        "status": runtime.status,
                        "last_exit_code": runtime.last_exit_code,
                        "restart_count": runtime.restart_count,
                        "max_restarts": spec.max_restarts,
                        "restart_policy": spec.restart_policy,
                        "log_path": log_path,
                        "log_tail": tails.get(process_id) or [],
                    },
                }
                if parents.get(device_id):
                    record["parent_device_id"] = parents[device_id]
                records[device_id] = record
        return records


_service: Optional[DeviceProcessService] = None
_service_lock = threading.Lock()


def get_device_process_service() -> DeviceProcessService:
    global _service
    with _service_lock:
        if _service is None:
            from unilabos.config.config import BasicConfig

            _service = DeviceProcessService(getattr(BasicConfig, "working_dir", None) or Path.cwd())
        return _service


def peek_device_process_service() -> Optional[DeviceProcessService]:
    """已创建的服务单例；还没有时为 None（不创建：创建会登记 atexit 收尾、Windows Job 等）。"""

    with _service_lock:
        return _service


def managed_device_unavailability() -> Dict[str, Dict[str, Any]]:
    """Host 设备快照用：受管进程没在运行的设备及原因；本进程还没有受管进程服务时为空。

    不创建服务单例，所以执行适配器可以无条件调用。
    """

    service = peek_device_process_service()
    if service is None:
        return {}
    try:
        return service.unavailable_devices()
    except Exception:  # noqa: BLE001 - 原因记录只是诊断信息，不能拖垮设备快照刷新
        logger.exception("[DeviceProcesses] 受管进程设备不可用原因计算失败")
        return {}


__all__ = [
    "DeviceProcessBusyError",
    "DeviceProcessError",
    "DeviceProcessGeneratedError",
    "DeviceProcessService",
    "DeviceProcessSpec",
    "build_device_node",
    "get_device_process_service",
    "graph_filename",
    "load_specs",
    "managed_device_unavailability",
    "peek_device_process_service",
    "save_specs",
    "sync_generated_specs",
    "write_graph",
]
