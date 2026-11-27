"""Host 进程正常退出必须释放 ROS2 网络所有权。"""

from __future__ import annotations

import signal
from typing import Any

import pytest

from unilabos.app import process_shutdown
from unilabos.app.scheduler import integration


class _CommunicationClient:
    """记录正常退出处理器是否停止通信客户端的测试替身。"""

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        """保存事件列表和可选失败策略。

        参数说明：``events`` 收集清理顺序，``fail`` 为真时停止操作抛出错误。
        返回：无；仅构造不执行清理。异常：无。
        """

        self._events = events
        self._fail = fail

    def stop(self) -> None:
        """记录通信停止并按夹具策略选择失败。

        参数：无。返回：成功时无；``fail`` 为真时抛出 ``RuntimeError``，用于
        证明单个通信错误不能阻止 ROS2 定向发现服务所属 Edge 服务继续关闭。
        """

        self._events.append("communication")
        if self._fail:
            raise RuntimeError("通信客户端停止失败")


def test_sigterm_explicitly_shuts_down_edge_network_after_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM 必须显式关闭通信客户端和 Edge 网络服务。

    参数说明：``monkeypatch`` 隔离进程信号表与实际服务单例；局部 ``handlers``
    记录信号注册，``events`` 记录清理顺序。返回：无；断言 SIGINT/SIGTERM 共用
    同一处理器、通信失败后仍关闭工作流（Workflow）、Edge 服务和运行态锁，并以
    正常状态退出。
    异常：只捕获并验证处理器约定的 ``SystemExit(0)``。
    """

    handlers: dict[int, Any] = {}
    events: list[str] = []

    def record_signal(signum: int, handler: Any) -> Any:
        """记录待安装或清理期间更新的信号处理器。

        参数说明：``signum`` 是信号编号，``handler`` 是新处理器。返回：先前处理
        器在本测试中没有消费者，固定返回 ``None``。异常：无。
        """

        handlers[signum] = handler
        return None

    def shutdown_edge_services() -> None:
        """记录 Edge 服务及 ROS2 定向发现清理接缝被调用。

        参数：无。返回：无。异常：无；真实发现服务停止由既有生命周期测试覆盖。
        """

        events.append("edge")

    def close_workspace_product_lifecycle() -> None:
        """记录统一工作区文件监视生命周期先于 Edge 服务关闭。

        参数：无。返回：无。异常：无。
        """

        events.append("workspace")

    def shutdown_workflow_runtime() -> None:
        """记录工作流运行态在数据库所有者关闭前停止。

        参数：无。返回：无。异常：无。
        """

        events.append("workflow")

    def close_runtime_storage_session() -> None:
        """记录三类运行态数据库均关闭后释放目录锁。

        参数：无。返回：无。异常：无。
        """

        events.append("storage")

    monkeypatch.setattr(process_shutdown.signal, "signal", record_signal)
    monkeypatch.setattr(integration, "shutdown_edge_services", shutdown_edge_services)
    package_manager = __import__(
        "unilabos.package_manager", fromlist=["package_manager"]
    )
    monkeypatch.setattr(
        package_manager,
        "close_workspace_product_lifecycle",
        close_workspace_product_lifecycle,
    )
    workflow_composition = __import__(
        "unilabos.workflow.composition", fromlist=["composition"]
    )
    monkeypatch.setattr(
        workflow_composition,
        "shutdown_workflow_runtime",
        shutdown_workflow_runtime,
    )
    runtime_storage = __import__(
        "unilabos.app.runtime_storage", fromlist=["runtime_storage"]
    )
    monkeypatch.setattr(
        runtime_storage,
        "close_runtime_storage_session",
        close_runtime_storage_session,
    )
    returned_handler = process_shutdown.install_host_shutdown_handlers(
        [_CommunicationClient(events, fail=True)]
    )

    assert handlers[signal.SIGINT] is returned_handler
    assert handlers[signal.SIGTERM] is returned_handler
    with pytest.raises(SystemExit) as caught:
        returned_handler(signal.SIGTERM, None)
    assert caught.value.code == 0
    assert events == ["communication", "workspace", "workflow", "edge", "storage"]
    assert handlers[signal.SIGINT] is signal.SIG_IGN
    assert handlers[signal.SIGTERM] is signal.SIG_IGN
