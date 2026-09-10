"""工作区统一监视器与遗留工作流源码监视器的互斥所有权合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.source_discovery import EditableSourceDiscoveryPlan


@pytest.fixture(autouse=True)
def reset_composed_runtime() -> Any:
    """在每个用例前后关闭进程级工作流权威（Workflow Authority）。

    参数：无。
    返回：pytest 生命周期值。
    异常：清理失败时传播异常，避免跨用例遗留监视线程。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def test_workspace_composition_does_not_create_legacy_source_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区模式只能由统一文件世代监视器拥有刷新事件。

    参数：``tmp_path`` 提供工作流数据库；``monkeypatch`` 把遗留监视器替换为
    一旦构造就失败的测试哨兵。
    返回：无；断言预编译源码计划仍完成注册恢复，但不启动第二个监视权威。
    异常：组合根仍构造 ``WorkflowSourceMonitor`` 时测试失败。
    """

    class ForbiddenLegacyMonitor:
        """拒绝工作区产品路径构造遗留工作流源码监视器。"""

        def __init__(self, _service: Any) -> None:
            """立即暴露不允许的第二监视权威。

            参数：``_service`` 是意外传入的工作流服务（WorkflowService）。
            返回：永不返回。
            异常：固定抛出 ``AssertionError``。
            """

            raise AssertionError("workspace must not create legacy source monitor")

    monkeypatch.setattr(composition, "WorkflowSourceMonitor", ForbiddenLegacyMonitor)

    service = composition.compose_workflow_runtime(
        tmp_path,
        editable_source_discovery_plan=EditableSourceDiscoveryPlan(
            registrations=(),
            root_identities=(),
        ),
        start_source_monitor=False,
    )

    assert service is composition.get_workflow_service()


def test_legacy_composition_still_owns_and_closes_source_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非工作区遗留入口必须继续保留原源码监视生命周期。

    参数：``tmp_path`` 提供工作流数据库；``monkeypatch`` 记录监视器启动和停止。
    返回：无；断言默认组合启动一次，并由统一重置关闭一次。
    异常：遗留入口意外失去监视能力或生命周期泄漏时测试失败。
    """

    lifecycle_calls: list[str] = []

    class RecordingLegacyMonitor:
        """记录遗留工作流源码监视器的产品生命周期。"""

        def __init__(self, _service: Any) -> None:
            """记录构造但不创建后台线程。

            参数：``_service`` 是组合出的工作流服务。
            返回：无。
            异常：无。
            """

            lifecycle_calls.append("created")

        def start(self) -> None:
            """记录启动。

            参数：无。返回：无。异常：无。
            """

            lifecycle_calls.append("started")

        def stop(self) -> None:
            """记录停止。

            参数：无。返回：无。异常：无。
            """

            lifecycle_calls.append("stopped")

    monkeypatch.setattr(composition, "WorkflowSourceMonitor", RecordingLegacyMonitor)

    composition.compose_workflow_runtime(tmp_path)
    composition.reset_workflow_service_for_test()

    assert lifecycle_calls == ["created", "started", "stopped"]
