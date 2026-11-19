"""执行适配器的一次开始边界：应答不确定时禁止再调用驱动。"""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Any, Callable
from uuid import uuid4

from unilabos.protocol.runtime.execution_start import ExecutionPermit, execution_identity
from unilabos.backend.runtime.action import ActionCancelled


class ExecutionStartUnknown(RuntimeError):
    """无法证明本次调用仍有唯一开始许可；保留资源等待核实。"""


class ExecutionStartGuard:
    def __init__(self, channel: Any, payload: dict[str, Any], executor_epoch: str,
                 canceled: Callable[[], bool], consume: Callable[[], None], *, stopped: Callable[[], bool] = lambda: False):
        self.channel = channel
        self.payload = deepcopy(payload)
        self.executor_epoch = executor_epoch
        self.canceled = canceled
        self.consume = consume
        self.stopped = stopped
        self.invocation_uuid = str(uuid4())
        self._lock = threading.Lock()
        self._attempted = False

    def __call__(self) -> None:
        with self._lock:
            if self._attempted:
                raise ExecutionStartUnknown("同一开始边界不能再次调用")
            try:
                if self.channel is None:
                    raise ExecutionStartUnknown("执行许可权威不可用")
                permit = ExecutionPermit.model_validate(self.payload.get("execution_permit"))
                request = {"permit": permit.model_dump(mode="json"), "invocation_uuid": self.invocation_uuid,
                           "executor_epoch": self.executor_epoch, "identity": execution_identity(self.payload)}
                while True:
                    if self.stopped():
                        raise ExecutionStartUnknown("执行进程停止，保留尝试等待恢复核实")
                    if self.canceled():
                        raise ActionCancelled("开始前已取消或执行进程正在停止")
                    self._attempted = True
                    result = self.channel.begin_execution(self.payload["job_id"], request)
                    if not isinstance(result, dict) or result.get("decision") not in {"START", "WAIT"}:
                        raise ExecutionStartUnknown("开始许可已领用或尝试已失效，禁止重复执行")
                    if result["decision"] == "START":
                        break
                    self._attempted = False
                    time.sleep(0.1)
                # 数量权威提交失败也可能已经生效；不可把它当作普通设备失败后自动重试。
                self.consume()
                receipt = self.channel.record_start_receipt(self.payload["job_id"], {
                    "permit": permit.model_dump(mode="json"), "invocation_uuid": self.invocation_uuid,
                    "executor_epoch": self.executor_epoch, "receipt_uuid": str(uuid4()),
                    "started_at_ms": int(time.time() * 1000),
                })
                if not isinstance(receipt, dict) or receipt.get("may_start") is not True:
                    raise ExecutionStartUnknown("开始边界前权威或占用已经失效，禁止继续调用")
            except (ExecutionStartUnknown, ActionCancelled):
                raise
            except Exception as exc:
                raise ExecutionStartUnknown(f"开始许可或库存提交结果不确定：{exc}") from exc


def confirm_execution_start(item: Any) -> None:
    guard = getattr(item, "start_guard", None)
    if guard is not None:
        guard()
