"""Host / 本机受管 Slave / 远端 Slave 日志目录及有界读取；不新建数据库。"""

from __future__ import annotations

import os
from pathlib import Path

from unilabos.backend.hostlink.protocol import ActionType, LinkError
from unilabos.protocol.runtime.logs import RuntimeLogBatch, RuntimeLogQuery, RuntimeLogSource, RuntimeLogSources
from unilabos.utils.runtime_logs import current_log_path, read_log_file


class LogSourceNotFound(LookupError):
    pass


class LogSourceUnavailable(RuntimeError):
    pass


def _managed_machine(process: dict, host_name: str) -> str:
    # 启动命令才是身份真相（extra_args 可能显式覆盖），不根据设备集合推断身份。
    command = process.get("command") or []
    machine_name = f"{host_name}_managed_{process['id'].replace('-', '_')}"
    for index, token in enumerate(command):
        option, separator, value = str(token).partition("=")
        if option not in {"--machine_name", "--machine-name"}:
            continue
        if separator:
            machine_name = value
        elif index + 1 < len(command):
            machine_name = str(command[index + 1])
    return machine_name


class RuntimeLogService:
    def __init__(self, processes, link_server, *, machine_name: str):
        self.processes = processes
        self.link_server = link_server
        self.machine_name = machine_name

    def sources(self) -> RuntimeLogSources:
        peers = self.link_server.peers() if self.link_server else []
        peer_by_node = {str(peer.get("node_id")): peer for peer in peers}
        host_devices = (self.link_server.hello_payload.get("devices") or []) if self.link_server else []
        sources = [RuntimeLogSource(
            source_id="host", name="Host", role="host", machine_name=self.machine_name,
            node_id=self.machine_name, pid=os.getpid(), online=True,
            device_ids=[str(device["id"]) for device in host_devices if isinstance(device, dict) and device.get("id")],
            supported=current_log_path() is not None,
            detail="本进程主日志；独立通信日志不在此接口暴露",
        )]
        managed_nodes: set[str] = set()
        for process in self.processes.list():
            node_id = _managed_machine(process, self.machine_name)
            managed_nodes.add(node_id)
            peer = peer_by_node.get(node_id, {})
            sources.append(RuntimeLogSource(
                source_id=f"managed:{process['id']}", name=str(process["name"]), role="slave",
                machine_name=node_id, node_id=node_id, pid=process.get("pid"),
                device_ids=list(process.get("device_ids") or []), online=bool(peer.get("online")),
                managed=True, detail=f"本机受管进程 · {process.get('status', 'stopped')}；包含 stdout / stderr",
            ))
        for peer in peers:
            node_id = str(peer.get("node_id") or "")
            if not node_id or node_id in managed_nodes:
                continue
            sources.append(RuntimeLogSource(
                source_id=f"slave:{node_id}", name=str(peer.get("machine_name") or node_id), role="slave",
                machine_name=str(peer.get("machine_name") or node_id), node_id=node_id,
                online=bool(peer.get("online")), device_ids=list(peer.get("device_ids") or []),
                detail="经 HostLink 读取远端主日志",
            ))
        return RuntimeLogSources(sources=sources)

    def read(self, source_id: str, *, cursor: str = "", limit: int = 300) -> RuntimeLogBatch:
        query = RuntimeLogQuery(cursor=cursor, limit=limit)
        source = next((item for item in self.sources().sources if item.source_id == source_id), None)
        if source is None:
            raise LogSourceNotFound("日志来源不存在，请刷新进程列表")
        if not source.supported:
            raise LogSourceUnavailable(source.detail)
        try:
            if source.source_id == "host":
                path = current_log_path()
                if path is None:
                    raise LogSourceUnavailable("Host 尚未配置日志文件")
                return read_log_file(path, source_id=source_id, pid=source.pid, **query.model_dump())
            if source.managed:
                process_id = source.source_id.removeprefix("managed:")
                # 只读台账中已存在的 process.log；不能用请求参数拼任意磁盘路径。
                root = (Path(self.processes.working_dir) / "device_processes").resolve()
                path = (root / process_id / "process.log").resolve()
                if not path.is_relative_to(root) or path.parent.parent != root:
                    raise LogSourceNotFound("非法日志来源")
                return read_log_file(path, source_id=source_id, pid=source.pid, **query.model_dump())
            if not source.online or self.link_server is None:
                raise LogSourceUnavailable("Slave 已离线，暂时无法读取远端日志")
            peer = next((peer for peer in self.link_server.peers() if peer.get("node_id") == source.node_id), None)
            if peer is None:
                raise LogSourceUnavailable("Slave 已断开连接")
            result = self.link_server.request_peer(
                str(peer["addr"]), ActionType.LOG_READ, query.model_dump(), timeout=3.0,
            )
            batch = RuntimeLogBatch.model_validate(result)
            # 远端只认识 self；浏览器始终使用目录给出的来源标识。
            batch.source_id = source_id
            return batch
        except (FileNotFoundError, PermissionError) as exc:
            raise LogSourceUnavailable("日志文件尚未创建或不可读取；等待进程启动后刷新") from exc
        except LinkError as exc:
            raise LogSourceUnavailable(f"Slave 日志暂不可读：{exc}") from exc


def get_runtime_log_service() -> RuntimeLogService:
    from unilabos.backend.hostlink.server import get_hostlink_server
    from unilabos.config.config import BasicConfig
    from unilabos.server.services.device_processes import get_device_process_service

    return RuntimeLogService(
        get_device_process_service(), get_hostlink_server(), machine_name=BasicConfig.machine_name,
    )
