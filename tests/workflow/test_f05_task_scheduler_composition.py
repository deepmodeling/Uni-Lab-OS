"""F05.3-C 工作流服务与任务调度桥（TaskSchedulerBridge）组合合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "12000000-0000-4000-8000-000000000001"
TASK_UUID = "22000000-0000-4000-8000-000000000001"


class _RecordingStore:
    """记录工作流服务持久化顺序的最小写权威替身。"""

    def __init__(self, events: list[str]) -> None:
        """绑定事件序列；参数 ``events`` 记录持久化与刷新顺序，返回无。"""

        self.events = events
        self.closed = False
        self.persisted_task: dict[str, Any] | None = None

    def get_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        """读取测试工作流；参数是稳定身份，返回对应定义投影。"""

        return {"uuid": workflow_uuid}

    def create_task_with_jobs(self, **values: Any) -> dict[str, Any]:
        """模拟原子创建任务/作业。

        参数：``values`` 是工作流服务提交的标准创建字段。返回：初始待处理任务；
        同时记录本步骤已经完成持久化。
        """

        self.events.append("persist")
        # ``persisted_task`` 是桥接前必须已经可查询的工作流任务事实。
        self.persisted_task = {
            "uuid": values["task_uuid"],
            "workflow_uuid": values["workflow_uuid"],
            "status": "pending",
        }
        return dict(self.persisted_task)

    def get_task(self, task_uuid: str) -> dict[str, Any]:
        """读取当前任务；参数是任务身份，返回桥接后刷新投影。"""

        assert self.persisted_task is not None
        return {**self.persisted_task, "uuid": task_uuid, "status": "running"}

    def close(self) -> None:
        """关闭测试写权威；参数无，返回无并记录关闭状态。"""

        self.closed = True


class _RecordingTaskBridge:
    """验证服务持久化顺序并返回刷新聚合的任务调度桥替身。"""

    def __init__(self, store: _RecordingStore, events: list[str]) -> None:
        """绑定写权威与事件序列；参数均用于证明调用顺序，返回无。"""

        self.store = store
        self.events = events
        self.closed = False

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        """提交已持久化任务。

        参数：``task`` 是创建事务返回的标准投影。返回：桥接后的任务/作业聚合；
        若任务尚未持久化则断言失败。
        """

        assert self.store.persisted_task == task
        self.events.append("submit")
        return {"task": self.store.get_task(task["uuid"]), "jobs": []}

    def close(self) -> None:
        """幂等关闭桥；参数无，返回无并记录释放。"""

        self.closed = True


def test_workflow_service_persists_before_submit_and_returns_refreshed_task() -> None:
    """创建工作流任务必须先持久化，再调度并返回刷新状态。

    参数：无。返回无；断言工作流服务（WorkflowService）把普通任务交给公共桥，
    并在关闭时释放桥后关闭存储。
    """

    events: list[str] = []
    store = _RecordingStore(events)
    bridge = _RecordingTaskBridge(store, events)
    service = WorkflowService(store, task_scheduler_bridge=bridge)  # type: ignore[arg-type]

    created = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    service.close()

    assert events == ["persist", "submit"]
    assert created["status"] == "running"
    assert bridge.closed is True
    assert store.closed is True


def test_local_composition_builds_bridge_from_existing_scheduler_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地组合根只能从既有调度器复用库存权威（Inventory Authority）。

    参数：``tmp_path`` 隔离 SQLite；``monkeypatch`` 替换桥构造接缝。返回无；替身
    构造签名故意不接受 inventory 参数，证明组合层没有新建或另传第二库存服务。
    """

    captured: dict[str, Any] = {}
    # ``inventory_authority`` 是既有调度器独占的同一库存服务身份。
    inventory_authority = object()
    scheduler = SimpleNamespace(inventory_service=inventory_authority)

    class _CompositionBridge:
        """记录组合根传入的存储与既有调度器。"""

        def __init__(self, store: WorkflowStore, *, scheduler: Any) -> None:
            """记录构造参数；只接受存储和调度器，拒绝第二库存参数。"""

            captured["store"] = store
            captured["scheduler"] = scheduler

        def recover_active_tasks(self) -> None:
            """满足组合根的启动恢复合同；本测试不构造活动任务。"""

        def close(self) -> None:
            """满足服务清理合同；参数无，返回无。"""

    composition.reset_workflow_service_for_test()
    monkeypatch.setattr(
        composition, "TaskSchedulerBridge", _CompositionBridge, raising=False
    )
    try:
        service = composition.compose_workflow_runtime(tmp_path, scheduler=scheduler)

        assert service is composition.get_workflow_service()
        assert isinstance(captured["store"], WorkflowStore)
        assert captured["scheduler"] is scheduler
        assert captured["scheduler"].inventory_service is inventory_authority
    finally:
        composition.reset_workflow_service_for_test()
