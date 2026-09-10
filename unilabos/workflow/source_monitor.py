"""创作草稿（Authoring Draft）文件的进程级源码监视器（Source Monitor）。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from unilabos.workflow.service import WorkflowError


@dataclass(frozen=True)
class SourceMonitorHealth:
    """源码监视器（Source Monitor）的最小稳定健康投影。"""

    state: str
    fatal_error_code: str | None


class SourceChangeService(Protocol):
    """声明源码监视器（Source Monitor）唯一依赖的命令接口。"""

    def list_registered_sources(self) -> list[dict[str, Any]]:
        """返回全部规范来源注册。

        参数：无。返回：每项含稳定工作流 UUID 的来源注册列表。
        """

        ...

    def source_signature(
        self,
        workflow_uuid: str,
    ) -> tuple[Any, ...]:
        """读取一个当前授权工作流来源的轻量文件签名。

        参数：``workflow_uuid`` 是服务当前授权的稳定工作流身份。返回：仅用于
        去抖和文件世代比较的稳定元组。异常：撤权或路径失效由服务稳定拒绝。
        """

        ...

    def submit_source_change(
        self,
        workflow_uuid: str,
        *,
        observed_signature: tuple[Any, ...],
    ) -> bool:
        """提交一个已经稳定观测的源码变化命令。

        参数：``workflow_uuid`` 是规范工作流身份；``observed_signature`` 是
        本次观测的文件世代。返回：``True`` 表示已结算，``False`` 表示服务要求
        保留并重试；异常表示本次提交未被确认。
        """

        ...


class WorkflowSourceMonitor:
    """轮询全部已注册源码，并让 Service 负责哈希去重与编译。"""

    def __init__(
        self,
        service: SourceChangeService,
        *,
        interval_seconds: float = 0.25,
        settle_seconds: float = 0.1,
    ) -> None:
        """建立进程级轮询器，但不自动启动后台线程。

        参数：``service`` 是唯一源码变化命令权威接口；``interval_seconds`` 是
        轮询间隔；``settle_seconds`` 是文件世代保持不变后才可提交的去抖时间。
        返回：无；待处理、已结算和退避记录都只用于进程内轮询恢复。
        """

        self._service = service
        self._interval_seconds = interval_seconds
        self._settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)
        self._stopping = False
        self._stop_join_active = False
        # ``_fatal_error`` 只保留最近一次未分类工作线程异常供诊断；公共健康投影
        # 仅公开稳定错误码，不泄漏异常消息、路径或复杂内部状态。
        self._fatal_error: BaseException | None = None
        self._processed: dict[str, tuple[Any, ...]] = {}
        self._pending: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._retries: dict[
            str,
            tuple[tuple[Any, ...], float, float],
        ] = {}

    def start(self) -> None:
        """幂等启动唯一源码监视后台线程。

        参数：无。返回：无；已经启动时不创建第二个线程。
        """

        with self._lifecycle_condition:
            while self._stopping:
                self._lifecycle_condition.wait()
            current_thread = self._thread
            if current_thread is not None and current_thread.is_alive():
                return
            # 每次启动都有独立停止世代；已停止事件不能泄漏到重新启动的线程。
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(stop_event,),
                name="workflow-source-monitor",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = worker
            self._fatal_error = None
            try:
                worker.start()
            except BaseException as start_error:
                if self._thread is worker:
                    self._thread = None
                self._fatal_error = start_error
                self._lifecycle_condition.notify_all()
                raise

    def stop(self) -> None:
        """停止源码监视线程并等待其有界退出。

        参数：无。返回：无；线程未在五秒内退出时抛出 ``RuntimeError``，避免
        组合根误认为监视权威已经停止。
        """

        with self._lifecycle_condition:
            while self._stopping:
                self._lifecycle_condition.wait()
            stop_event = self._stop_event
            thread = self._thread
            if thread is None:
                return
            self._stopping = True
            self._stop_join_active = True
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lifecycle_condition:
            self._stop_join_active = False
            if thread is not threading.current_thread() and thread.is_alive():
                # 保持 ``stopping``，直到迟到退出的工作线程在 ``finally`` 中确认
                # 终止；等待中的 ``start`` 不得复用已收到停止信号的旧线程。
                self._fatal_error = RuntimeError("Workflow Draft 监视器未能停止")
                raise RuntimeError("Workflow Draft 监视器未能停止")
            if self._thread is thread:
                self._thread = None
            self._stopping = False
            self._lifecycle_condition.notify_all()

    def health(self) -> SourceMonitorHealth:
        """返回不泄漏内部队列的稳定生命周期和致命错误投影。

        参数：无。返回：``running``、``stopping``、``fatal`` 或 ``stopped`` 之一，
        以及仅在致命状态存在的稳定错误码；读取本身不启动、停止或恢复线程。
        """

        with self._lifecycle_condition:
            thread = self._thread
            if self._stopping:
                state = "stopping"
            elif thread is not None and thread.is_alive():
                state = "running"
            elif self._fatal_error is not None:
                state = "fatal"
            else:
                state = "stopped"
            return SourceMonitorHealth(
                state=state,
                fatal_error_code=(
                    "unclassified_monitor_failure"
                    if self._fatal_error is not None
                    else None
                ),
            )

    def _run(self, stop_event: threading.Event) -> None:
        """轮询注册来源并只向服务提交稳定源码变化命令。

        参数：``stop_event`` 是本次工作线程独占的停止世代。返回：无；确定性
        输入错误等待新文件世代，瞬态失败和未结算结果保留同一命令并退避重试；
        无论正常或异常退出都会释放可重新启动的线程身份。
        """

        current_thread = threading.current_thread()
        fatal_error: BaseException | None = None
        try:
            while not stop_event.is_set():
                try:
                    registrations = self._service.list_registered_sources()
                except (OSError, RuntimeError, WorkflowError):
                    stop_event.wait(self._interval_seconds)
                    continue
                # ``active`` 是本轮仍由服务确认存在的规范工作流来源身份集合。
                active = {
                    registration["workflow_uuid"]
                    for registration in registrations
                }
                known = set(self._processed) | set(self._pending) | set(self._retries)
                for workflow_uuid in known - active:
                    self._processed.pop(workflow_uuid, None)
                    self._pending.pop(workflow_uuid, None)
                    self._retries.pop(workflow_uuid, None)
                for registration in registrations:
                    if stop_event.is_set():
                        return
                    workflow_uuid = registration["workflow_uuid"]
                    signature: tuple[Any, ...] | None = None
                    try:
                        # ``signature`` 只标识文件观测世代；服务按 UUID 重新校验
                        # 当前授权，监视器不得用本轮枚举得到的旧路径 DTO 读取文件。
                        signature = self._service.source_signature(workflow_uuid)
                        if self._processed.get(workflow_uuid) == signature:
                            self._pending.pop(workflow_uuid, None)
                            self._retries.pop(workflow_uuid, None)
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
                        if (
                            retry is not None
                            and retry[0] == signature
                            and now < retry[1]
                        ):
                            continue
                        settled = self._service.submit_source_change(
                            workflow_uuid,
                            observed_signature=signature,
                        )
                        latest_signature = self._service.source_signature(
                            workflow_uuid
                        )
                        if not settled:
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
                stop_event.wait(self._interval_seconds)
        except BaseException as unclassified_error:  # noqa: BLE001
            # 未分类异常不得只作为 daemon thread stderr 消失；保留异常对象用于本地
            # 诊断，并通过 ``health`` 公开不泄漏内容的稳定错误码。
            if not stop_event.is_set():
                fatal_error = unclassified_error
        finally:
            with self._lifecycle_condition:
                if fatal_error is not None:
                    self._fatal_error = fatal_error
                if self._thread is current_thread:
                    self._thread = None
                if self._stopping and not self._stop_join_active:
                    self._stopping = False
                self._lifecycle_condition.notify_all()

    def _schedule_retry(
        self,
        workflow_uuid: str,
        signature: tuple[Any, ...],
    ) -> None:
        """为未确认命令安排有上限的指数退避。

        参数：``workflow_uuid`` 是规范工作流身份；``signature`` 是必须保留的
        文件观测世代。返回：无；相同世代逐次退避，变化后的新世代从最小值开始。
        """

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


__all__ = [
    "SourceChangeService",
    "SourceMonitorHealth",
    "WorkflowSourceMonitor",
]
