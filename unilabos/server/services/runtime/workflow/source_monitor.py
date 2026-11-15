"""Authoring Draft 文件的进程级轮询监视器。

由本机 Workflow Authority 拥有（``server/backend/composition.py:setup_local_scheduler``
启动、``shutdown_backend_services`` 停止）：外部编辑器改动已注册源码后重新编译出候选，
Apply 写回失败留下的 ``writeback_status=pending`` 记录持续重试直到写回落盘。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from unilabos.server.services.runtime.workflow.service import WorkflowError, WorkflowService

logger = logging.getLogger(__name__)


class WorkflowSourceMonitor:
    """轮询全部已注册源码，并让 Service 负责哈希去重与编译。"""

    def __init__(
        self,
        service: WorkflowService,
        *,
        interval_seconds: float = 0.25,
        settle_seconds: float = 0.1,
    ):
        self._service = service
        self._interval_seconds = interval_seconds
        self._settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._processed: Dict[str, Tuple[Any, ...]] = {}
        self._pending: Dict[str, Tuple[Tuple[Any, ...], float]] = {}
        self._retries: Dict[
            str,
            Tuple[Tuple[Any, ...], float, float],
        ] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._guarded_run,
            name="workflow-source-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("Workflow Draft 监视器未能停止")
        self._thread = None

    def _guarded_run(self) -> None:
        """意外错误不能让监视线程永久停摆：记日志后下一轮重新扫描。"""

        while not self._stop_event.is_set():
            try:
                self._run()
            except Exception:  # noqa: BLE001
                logger.exception("工作流源码监视本轮失败，稍后重试")
                self._stop_event.wait(self._interval_seconds)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            registrations = self._service.list_registered_sources()
            active = {registration["workflow_uuid"] for registration in registrations}
            known = set(self._processed) | set(self._pending) | set(self._retries)
            for workflow_uuid in known - active:
                self._processed.pop(workflow_uuid, None)
                self._pending.pop(workflow_uuid, None)
                self._retries.pop(workflow_uuid, None)
            for registration in registrations:
                if self._stop_event.is_set():
                    return
                workflow_uuid = registration["workflow_uuid"]
                signature: Optional[Tuple[Any, ...]] = None
                try:
                    signature = self._service.source_signature(registration)
                    if self._processed.get(workflow_uuid) == signature:
                        if not self._service.source_reconciliation_pending(
                            workflow_uuid
                        ):
                            self._pending.pop(workflow_uuid, None)
                            self._retries.pop(workflow_uuid, None)
                            continue
                        self._processed.pop(workflow_uuid, None)
                        self._pending[workflow_uuid] = (
                            signature,
                            time.monotonic(),
                        )
                        self._schedule_retry(workflow_uuid, signature)
                        continue
                    now = time.monotonic()
                    pending = self._pending.get(workflow_uuid)
                    if pending is None or pending[0] != signature:
                        self._pending[workflow_uuid] = (signature, now)
                        self._retries.pop(workflow_uuid, None)
                        continue
                    if now - pending[1] < self._settle_seconds:
                        continue
                    retry = self._retries.get(workflow_uuid)
                    if retry is not None and retry[0] == signature and now < retry[1]:
                        continue
                    self._service.reconcile_registered_source(workflow_uuid)
                    latest_signature = self._service.source_signature(registration)
                    if self._service.source_reconciliation_pending(workflow_uuid):
                        self._processed.pop(workflow_uuid, None)
                        self._pending[workflow_uuid] = (
                            latest_signature,
                            time.monotonic(),
                        )
                        self._schedule_retry(
                            workflow_uuid,
                            latest_signature,
                        )
                    elif latest_signature == signature:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                        self._retries.pop(workflow_uuid, None)
                    else:
                        self._pending[workflow_uuid] = (
                            latest_signature,
                            time.monotonic(),
                        )
                        self._retries.pop(workflow_uuid, None)
                except WorkflowError as exc:
                    # 文件内容错误只在签名变化后重试；编译器和目录等
                    # 暂态故障则使用有上限的指数退避。
                    if signature is not None and exc.code in {
                        "invalid_input",
                        "workflow_not_found",
                    }:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                        self._retries.pop(workflow_uuid, None)
                    elif signature is not None:
                        self._schedule_retry(workflow_uuid, signature)
                    continue
                except (OSError, RuntimeError):
                    if signature is not None:
                        self._schedule_retry(workflow_uuid, signature)
                    continue
                except Exception:  # noqa: BLE001 - 一个源码出错不能挡住其余源码
                    logger.exception("工作流 %s 的源码同步失败，稍后重试", workflow_uuid)
                    if signature is not None:
                        self._schedule_retry(workflow_uuid, signature)
                    continue
            self._stop_event.wait(self._interval_seconds)

    def _schedule_retry(
        self,
        workflow_uuid: str,
        signature: Tuple[Any, ...],
    ) -> None:
        previous = self._retries.get(workflow_uuid)
        minimum = max(0.02, self._interval_seconds * 4)
        delay = minimum
        if previous is not None and previous[0] == signature:
            delay = min(previous[2] * 2, 1.0)
        self._retries[workflow_uuid] = (
            signature,
            time.monotonic() + delay,
            delay,
        )


__all__ = ["WorkflowSourceMonitor"]
