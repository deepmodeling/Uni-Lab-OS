"""Job 生命周期回调按 origin 路由到 owner bridge 的契约。

执行面不再把每个 job 的 started / status / 决策挂起回调广播给所有 bridge：
声明了 ``job_origins`` 的 bridge 只收自己派发的 job，未声明的 bridge 是观察者。
"""

from __future__ import annotations

from typing import Any

from unilabos.server.backend.execution import JobExecutionBackend
from unilabos.server.backend.execution_queue import (
    JOB_ORIGIN_BACKEND_CONTROL,
    JOB_ORIGIN_LOCAL_SCHEDULER,
    QueueItem,
)


class _Bridge:
    def __init__(self, origins: frozenset[str] | None = None) -> None:
        if origins is not None:
            self.job_origins = origins
        self.started: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.decisions: list[dict[str, Any]] = []

    def publish_job_started(self, item: QueueItem) -> None:
        self.started.append(item.job_id)

    def publish_job_status(self, _data: dict, item: QueueItem, status: str, _info=None) -> None:
        self.statuses.append((item.job_id, status))

    def publish_job_error_decision_required(self, report: dict[str, Any]) -> bool:
        self.decisions.append(report)
        return True


def _item(job_id: str, origin: str) -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id="device-1",
        action_name="run",
        task_id="task-1",
        job_id=job_id,
        notebook_id="",
        device_action_key="/devices/device-1/run",
        node_id="node-1",
        origin=origin,
    )


def _backend(*bridges: Any) -> JobExecutionBackend:
    return JobExecutionBackend(host_node_getter=lambda: None, result_bridges=list(bridges))


def test_lifecycle_callbacks_reach_only_the_owning_bridge_and_observers() -> None:
    scheduler = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}))
    coordinator = _Bridge(frozenset({JOB_ORIGIN_BACKEND_CONTROL}))
    mirror = _Bridge()  # 未声明 job_origins：观察者，收到全部
    backend = _backend(scheduler, coordinator, mirror)

    local = _item("job-local", JOB_ORIGIN_LOCAL_SCHEDULER)
    remote = _item("job-remote", JOB_ORIGIN_BACKEND_CONTROL)
    backend.publish_job_started(local)
    backend.publish_job_started(remote)
    backend._publish_to_result_bridges({}, local, "success")  # noqa: SLF001
    backend._publish_to_result_bridges({}, remote, "failed")  # noqa: SLF001

    assert scheduler.started == ["job-local"]
    assert coordinator.started == ["job-remote"]
    assert mirror.started == ["job-local", "job-remote"]
    assert scheduler.statuses == [("job-local", "success")]
    assert coordinator.statuses == [("job-remote", "failed")]
    assert mirror.statuses == [("job-local", "success"), ("job-remote", "failed")]


def test_error_decision_is_held_only_when_an_owner_or_observer_can_decide() -> None:
    coordinator = _Bridge(frozenset({JOB_ORIGIN_BACKEND_CONTROL}))
    backend = _backend(coordinator)
    failure = {
        "suc": False,
        "error": "boom",
        "error_info": {"exception_type": "RuntimeError", "error_message": "boom"},
    }

    # 本机 job 没有 owner bridge：不挂起，直接放行 failed
    assert backend._begin_action_error_decision(  # noqa: SLF001
        _item("job-local", JOB_ORIGIN_LOCAL_SCHEDULER), dict(failure), {}
    ) is False
    assert backend.list_error_decisions() == []
    assert coordinator.decisions == []

    # 加入本机调度权威作为 owner：挂起并只通知它
    scheduler = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}))
    backend.result_bridges.append(scheduler)
    assert backend._begin_action_error_decision(  # noqa: SLF001
        _item("job-local-2", JOB_ORIGIN_LOCAL_SCHEDULER), dict(failure), {}
    ) is True
    (pending,) = backend.list_error_decisions()
    assert pending["job_id"] == "job-local-2"
    assert [report["job_id"] for report in scheduler.decisions] == ["job-local-2"]
    assert coordinator.decisions == []
