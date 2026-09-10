"""验证进程内重启会按所有权顺序处理可重置运行态数据库。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import unilabos.app as app_package
from unilabos.app import runtime_storage, utils


@pytest.fixture(autouse=True)
def _release_runtime_storage_session() -> Any:
    """保证重启测试前后释放工作目录排他锁。

    参数：无。返回：供 pytest 执行测试主体的生成器。异常：锁释放失败时原样
    传播，避免后续测试误判为并发进程占用。
    """

    runtime_storage.close_runtime_storage_session()
    yield
    runtime_storage.close_runtime_storage_session()


def _module(name: str, **attributes: Any) -> ModuleType:
    """创建带指定生命周期接缝的轻量模块替身。

    参数：``name`` 是完整模块名，``attributes`` 是要公开的函数或状态。返回：
    可放入 ``sys.modules`` 的模块。异常：无。
    """

    module = ModuleType(name)
    for attribute_name, value in attributes.items():
        setattr(module, attribute_name, value)
    return module


def _patch_restart_environment(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    edge_failure: bool = False,
) -> None:
    """隔离 ROS2、网络与产品生命周期，只保留真实数据库会话。

    参数：``monkeypatch`` 管理测试替换；``events`` 收集所有权关闭顺序；
    ``edge_failure`` 控制 Edge Store 关闭是否失败。返回：无。异常：无。
    """

    class _CommunicationClient:
        """记录通信客户端停止事件的最小替身。"""

        def stop(self) -> None:
            """记录停止调用。

            参数：无。返回：无。异常：无。
            """

            events.append("communication")

    class _HostNode:
        """表示本测试没有启动真实 ROS2 Host 节点。"""

        @classmethod
        def get_instance(cls, *, timeout: float) -> None:
            """返回不存在的 Host 运行实例。

            参数：``timeout`` 是生产代码传入的有界等待秒数。返回：固定为
            ``None``。异常：无。
            """

            assert timeout == 5

    def shutdown_edge_services() -> None:
        """记录 Edge Store 关闭并按测试策略选择失败。

        参数：无。返回：成功时无。异常：``edge_failure`` 为真时抛出
        ``RuntimeError``，验证文件不会在连接关闭失败后被删除。
        """

        events.append("edge")
        if edge_failure:
            raise RuntimeError("Edge Store 关闭失败")

    communication = _module(
        "unilabos.app.communication",
        _communication_client=_CommunicationClient(),
        get_communication_client=lambda: communication._communication_client,
    )
    monkeypatch.setitem(sys.modules, communication.__name__, communication)
    monkeypatch.setattr(app_package, "communication", communication, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "unilabos.package_manager",
        _module(
            "unilabos.package_manager",
            close_workspace_product_lifecycle=lambda: events.append("workspace"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "unilabos.workflow.composition",
        _module(
            "unilabos.workflow.composition",
            shutdown_workflow_runtime=lambda: events.append("workflow"),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "unilabos.app.scheduler.integration",
        _module(
            "unilabos.app.scheduler.integration",
            shutdown_edge_services=shutdown_edge_services,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "unilabos.ros.nodes.presets.host_node",
        _module("unilabos.ros.nodes.presets.host_node", HostNode=_HostNode),
    )
    timer_module = _module("rclpy.timer", Timer=type("Timer", (), {}))
    monkeypatch.setitem(sys.modules, "rclpy.timer", timer_module)
    monkeypatch.setitem(
        sys.modules,
        "rclpy",
        _module("rclpy", ok=lambda: False, timer=timer_module),
    )
    monkeypatch.setitem(
        sys.modules,
        "unilabos.utils.tracing",
        _module(
            "unilabos.utils.tracing",
            shutdown_tracing=lambda: events.append("tracing"),
        ),
    )
    monkeypatch.setattr(utils, "print_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(utils.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(utils.threading, "enumerate", list)
    monkeypatch.setattr(utils.gc, "collect", lambda: 0)

    original_discard = runtime_storage.discard_runtime_storage_session
    original_close = runtime_storage.close_runtime_storage_session

    def discard_runtime_storage_session() -> None:
        """记录并执行真实运行态数据库清空。

        参数：无。返回：无。异常：真实删除失败时原样传播。
        """

        events.append("discard")
        original_discard()

    def close_runtime_storage_session() -> None:
        """记录并执行真实工作目录锁释放。

        参数：无。返回：无。异常：真实解锁失败时原样传播。
        """

        events.append("close")
        original_close()

    monkeypatch.setattr(
        runtime_storage,
        "discard_runtime_storage_session",
        discard_runtime_storage_session,
    )
    monkeypatch.setattr(
        runtime_storage,
        "close_runtime_storage_session",
        close_runtime_storage_session,
    )


def test_restart_discards_databases_after_all_store_owners_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明默认重启先关数据库所有者，再删除文件并释放目录锁。

    参数：``tmp_path`` 是隔离工作目录；``monkeypatch`` 隔离外部服务。返回：无；
    断言本代运行态被清空且同目录可被下一代重新取得。异常：顺序漂移时测试失败。
    """

    first_paths = runtime_storage.prepare_runtime_storage_session(
        {}, working_dir=str(tmp_path)
    )
    first_runtime_root = Path(first_paths.inventory_db).parent
    database = Path(first_paths.inventory_db)
    database.write_text("current-generation", encoding="utf-8")
    stable_database = tmp_path / "inventory.db"
    stable_database.write_text("legacy-stable", encoding="utf-8")
    events: list[str] = []
    _patch_restart_environment(monkeypatch, events)

    assert utils.cleanup_for_restart()

    assert not database.exists()
    assert not first_runtime_root.exists()
    assert stable_database.read_text(encoding="utf-8") == "legacy-stable"
    assert events == [
        "workspace",
        "workflow",
        "communication",
        "edge",
        "discard",
        "close",
        "tracing",
    ]
    second_paths = runtime_storage.prepare_runtime_storage_session(
        {},
        working_dir=str(tmp_path),
    )
    assert Path(second_paths.inventory_db).parent != first_runtime_root


def test_restart_keeps_files_and_lock_when_a_store_owner_fails_to_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明数据库连接关闭失败时不删除运行态，也不主动释放目录锁。

    参数：``tmp_path`` 是隔离工作目录；``monkeypatch`` 注入 Edge Store 失败。
    返回：无；断言清理报告失败且跳过删除/解锁。异常：安全边界漂移时测试失败。
    """

    paths = runtime_storage.prepare_runtime_storage_session(
        {}, working_dir=str(tmp_path)
    )
    runtime_root = Path(paths.workflow_history_db).parent
    database = Path(paths.workflow_history_db)
    database.write_text("diagnostic-state", encoding="utf-8")
    events: list[str] = []
    _patch_restart_environment(monkeypatch, events, edge_failure=True)

    assert not utils.cleanup_for_restart()

    assert database.read_text(encoding="utf-8") == "diagnostic-state"
    assert runtime_root.exists()
    assert "discard" not in events
    assert "close" not in events


def test_preserve_option_keeps_stable_databases_during_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明“不换库”选项在有序重启时仍保留稳定目录中的数据库。

    参数：``tmp_path`` 是稳定工作目录；``monkeypatch`` 隔离外部服务。返回：无；
    断言 Store 关闭和目录锁释放不删除原库。异常：保留合同漂移时测试失败。
    """

    paths = runtime_storage.prepare_runtime_storage_session(
        {"preserve_runtime_databases": True},
        working_dir=str(tmp_path),
    )
    database = Path(paths.inventory_db)
    database.write_text("preserved", encoding="utf-8")
    events: list[str] = []
    _patch_restart_environment(monkeypatch, events)

    assert utils.cleanup_for_restart()

    assert database.read_text(encoding="utf-8") == "preserved"
    assert "discard" in events
    assert "close" in events
