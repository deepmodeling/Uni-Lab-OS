"""执行端使用的后端接口；不导入数据库、调度器或领域驱动。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol
from urllib.request import Request, urlopen


class ExecutionPort(Protocol):
    def call(self, operation: str, payload: dict[str, Any]) -> Any: ...


class BackendExecutionClient:
    """每个请求只发送一次；失败保留未知结果，不重试物理动作。"""

    def __init__(self, url: str, api_key: str, *, timeout: float = 30.0):
        if not url or not api_key:
            raise ValueError("执行端必须配置后端地址与工作区凭据")
        self.url, self.api_key, self.timeout = url.rstrip("/"), api_key, timeout

    def call(self, operation: str, payload: dict[str, Any]) -> Any:
        request = Request(
            self.url + "/runtime/execution/" + operation,
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)["data"]


class RuntimeMaterials:
    """公开的库存只读查询；进程外消费者不持有 Store 或数据库路径。"""

    def __init__(self, port: ExecutionPort):
        self.port = port
        self.by_id = port.call("materials.index", {})

    def snapshot(self, roles, *, quantity_unit="mL"):
        return self.port.call(
            "materials.snapshot", {"roles": roles, "quantity_unit": quantity_unit}
        )

    def relations(self, identifiers, *, scene_parent=None):
        result = self.port.call(
            "materials.relations", {"identifiers": list(identifiers)}
        )
        if scene_parent is not None:
            for identifier, parent in result.items():
                if parent is None:
                    result[identifier] = scene_parent(identifier)
        return result


@dataclass(frozen=True)
class _ExecutionCommand:
    command_id: str
    source_boot_id: str
    monotonic_sequence: int
    body: dict
    source: str = "runtime-execution"

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.body, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def to_dict(self):
        return {
            "command_id": self.command_id,
            "source": self.source,
            "source_boot_id": self.source_boot_id,
            "monotonic_sequence": self.monotonic_sequence,
            "body": self.body,
        }


class JournaledExecution:
    """复用 OS 命令日志实现进程重启后的幂等和 UNKNOWN，执行端不自建作业状态。"""

    def __init__(self, path, *, namespace):
        from unilabos.runtime.commands import open_command_journal

        self.journal = open_command_journal(path, namespace=namespace)
        self.boot = str(uuid.uuid4())
        self.sequence = 0
        self.lock = Lock()
        self.journal.mark_previous_boot_inflight_unknown(self.boot)

    def completed(self, request):
        """只读已完成回执；OS 重试另有 Job UUID，不要求历史作业仍在 running。"""
        from unilabos.runtime.commands import CommandConflictError, CommandState

        previous = self.journal.get(request["identity"]["node_job_uuid"])
        if previous is None:
            return None
        recorded = previous.request["body"]["request"]
        if json.dumps(recorded, sort_keys=True, allow_nan=False) != json.dumps(
            request, sort_keys=True, allow_nan=False
        ):
            raise CommandConflictError("同一 Job 的执行请求已变化")
        if previous.state == CommandState.SUCCEEDED:
            return dict(previous.output)
        return None

    def run(self, request, grant, execute):
        from unilabos.runtime.commands import CommandState

        with self.lock:
            if self.journal.has_unknown():
                raise RuntimeError("execution_reconciliation_required")
            self.sequence += 1
            command = _ExecutionCommand(
                request["identity"]["node_job_uuid"],
                self.boot,
                self.sequence,
                {"request": request, "grant": grant},
            )
            accepted, previous = self.journal.accept(command, self.boot)
            if not accepted:
                if previous.state == CommandState.SUCCEEDED:
                    return dict(previous.output)
                raise RuntimeError("execution_result_unknown")
            self.journal.update(
                command.command_id, CommandState.RUNNING, self.boot, "执行已开始"
            )
            try:
                result = execute()
                self.journal.update(
                    command.command_id,
                    CommandState.SUCCEEDED,
                    self.boot,
                    "执行与结算已确认",
                    result,
                )
                return result
            except BaseException:
                self.journal.update(
                    command.command_id,
                    CommandState.UNKNOWN,
                    self.boot,
                    "执行结果待对账",
                )
                raise
