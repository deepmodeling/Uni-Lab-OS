"""HostNode 本地设备动作端点就绪门禁的回归测试。"""

from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.ros.action_transport import build_runtime_action_mappings
from unilabos.ros.nodes.presets import host_node as host_node_module
from unilabos.ros.nodes.presets.host_node import HostNode


class _Logger:
    """收集本地设备初始化期间的可诊断日志。"""

    def __init__(self) -> None:
        """初始化空消息集合。

        参数：无。
        返回：无。
        异常：无。
        """

        self.messages: list[str] = []

    def info(self, message: str) -> None:
        """记录信息日志。

        参数：``message`` 是待记录的日志正文。
        返回：无。
        异常：无。
        """

        self.messages.append(message)

    def error(self, message: str) -> None:
        """记录错误日志。

        参数：``message`` 是待记录的日志正文。
        返回：无。
        异常：无。
        """

        self.messages.append(message)

    def warning(self, message: str) -> None:
        """记录警告日志。

        参数：``message`` 是待记录的日志正文。
        返回：无。
        异常：无。
        """

        self.messages.append(message)

    def trace(self, message: str) -> None:
        """记录跟踪日志。

        参数：``message`` 是待记录的日志正文。
        返回：无。
        异常：无。
        """

        self.messages.append(message)


class _ActionClient:
    """代表 HostNode 创建的最小 ROS 动作客户端。"""

    def __init__(
        self,
        _node: Any,
        action_type: Any,
        action_id: str,
        callback_group: Any = None,
    ) -> None:
        """保存端点类型、身份和回调组。

        参数：``_node`` 是未使用的 HostNode；``action_type`` 是 ROS 动作类型；
        ``action_id`` 是绝对端点身份；``callback_group`` 是可选回调组。
        返回：无。
        异常：无。
        """

        self.action_type = action_type
        self.action_id = action_id
        self.callback_group = callback_group

    def server_is_ready(self) -> bool:
        """返回测试客户端的固定就绪状态。

        参数：无。
        返回：始终返回 ``True``；失败路径由门禁替身独立控制。
        异常：无。
        """

        return True


def _fake_host() -> tuple[SimpleNamespace, list[list[tuple[str, str]]], _Logger]:
    """构造只包含本地设备初始化所需状态的 HostNode 替身。

    参数：无。
    返回：HostNode 替身、动作锁上报记录和日志记录器。
    异常：无。
    """

    # ``reported_locks`` 保存设备通过就绪门禁后准备上报的业务动作集合。
    reported_locks: list[list[tuple[str, str]]] = []
    # ``logger`` 保存门禁失败时供操作员诊断的稳定错误信息。
    logger = _Logger()

    def report_action_locks_free(pairs: list[tuple[str, str]]) -> None:
        """记录设备通过就绪门禁后上报的业务动作集合。

        参数：``pairs`` 是设备身份与业务动作名称组成的二元组列表。
        返回：无；输入列表会追加到当前测试的上报记录。
        异常：无。
        """

        reported_locks.append(pairs)

    # ``host`` 只提供 HostNode.initialize_device 直接访问的字段和协作者。
    host = SimpleNamespace(
        devices_names={},
        device_machine_names={},
        devices_instances={},
        _action_value_mappings={},
        _action_clients={},
        _online_devices=set(),
        lab_logger=lambda: logger,
        _report_action_locks_free=report_action_locks_free,
    )
    return host, reported_locks, logger


def test_local_json_action_is_online_only_after_generic_endpoint_is_ready(
    monkeypatch,
) -> None:
    """本地 JSON 业务动作通过通用端点门禁后才宣告空闲和在线。

    参数：``monkeypatch`` 替换设备构造、动作客户端和就绪探测边界。
    返回：无；断言探测了同步通用端点，并在通过后上报业务动作与在线设备。
    异常：未创建通用客户端、绕过门禁或漏报业务动作时触发断言失败。
    """

    # ``runtime_mappings`` 是本地设备包装器持有的完整运行时动作映射。
    runtime_mappings = build_runtime_action_mappings(
        {"scan": {"type": "UniLabJsonCommand"}}
    )
    # ``device`` 模拟成功初始化并暴露运行时动作映射的 ROS 设备包装器。
    device = SimpleNamespace(
        _ros_node=SimpleNamespace(
            namespace="/devices/scanner-a",
            _action_value_mappings=runtime_mappings,
        )
    )
    # ``probed_endpoints`` 记录 HostNode 实际要求全部就绪的端点身份。
    probed_endpoints: list[tuple[str, ...]] = []

    def probe(
        _clients: dict[str, Any],
        endpoint_ids: tuple[str, ...],
        *,
        wait_timeout: float,
    ) -> bool:
        """记录门禁输入并模拟全部端点已连接。

        参数：``_clients`` 是已创建客户端；``endpoint_ids`` 是必需端点；
        ``wait_timeout`` 是共享等待秒数。
        返回：始终返回 ``True``。
        异常：等待预算不是正数时触发断言失败。
        """

        assert wait_timeout > 0
        probed_endpoints.append(endpoint_ids)
        return True

    def initialize_test_device(
        _device_id: str,
        _device_config: Any,
    ) -> SimpleNamespace:
        """返回已经包装好的测试设备。

        参数：``_device_id`` 和 ``_device_config`` 是被测组合根传入的设备身份与
        配置，本替身无需读取。
        返回：暴露完整运行时动作映射的测试设备。
        异常：无。
        """

        return device

    monkeypatch.setattr(host_node_module, "ActionClient", _ActionClient)
    monkeypatch.setattr(
        host_node_module,
        "initialize_device_from_dict",
        initialize_test_device,
    )
    monkeypatch.setattr(
        host_node_module,
        "wait_for_action_endpoints",
        probe,
        raising=False,
    )
    host, reported_locks, _logger = _fake_host()

    HostNode.initialize_device(host, "scanner-a", SimpleNamespace())

    assert probed_endpoints == [
        ("/devices/scanner-a/_execute_driver_command",)
    ]
    assert "/devices/scanner-a/_execute_driver_command" in host._action_clients
    assert "/devices/scanner-a/scanner-a" in host._online_devices
    assert any(("scanner-a", "scan") in report for report in reported_locks)


def test_local_device_fails_closed_when_a_required_endpoint_is_unavailable(
    monkeypatch,
) -> None:
    """任一必需 ROS 端点未就绪时本地设备不得宣告可调度。

    参数：``monkeypatch`` 替换设备构造、动作客户端和就绪探测边界。
    返回：无；断言初始化以稳定错误失败，且没有在线标记或空闲锁上报。
    异常：门禁被绕过时由预期异常或状态断言失败暴露。
    """

    # ``runtime_mappings`` 是含同步通用端点的本地设备运行时动作映射。
    runtime_mappings = build_runtime_action_mappings(
        {"scan": {"type": "UniLabJsonCommand"}}
    )
    # ``device`` 模拟端点尚未被 ROS 图发现的本地设备包装器。
    device = SimpleNamespace(
        _ros_node=SimpleNamespace(
            namespace="/devices/scanner-a",
            _action_value_mappings=runtime_mappings,
        )
    )

    def initialize_test_device(
        _device_id: str,
        _device_config: Any,
    ) -> SimpleNamespace:
        """返回端点尚未就绪的测试设备。

        参数：``_device_id`` 和 ``_device_config`` 是被测组合根传入的设备身份与
        配置，本替身无需读取。
        返回：暴露同步通用端点的测试设备。
        异常：无。
        """

        return device

    def reject_endpoint_readiness(
        _clients: dict[str, Any],
        _endpoint_ids: tuple[str, ...],
        *,
        wait_timeout: float,
    ) -> bool:
        """模拟至少一个必需 ROS 动作端点未就绪。

        参数：``_clients`` 是动作客户端集合；``_endpoint_ids`` 是必需端点；
        ``wait_timeout`` 是共享等待预算。
        返回：始终返回 ``False``，触发关闭式失败门禁。
        异常：等待预算不是正数时触发断言失败。
        """

        assert wait_timeout > 0
        return False

    monkeypatch.setattr(host_node_module, "ActionClient", _ActionClient)
    monkeypatch.setattr(
        host_node_module,
        "initialize_device_from_dict",
        initialize_test_device,
    )
    monkeypatch.setattr(
        host_node_module,
        "wait_for_action_endpoints",
        reject_endpoint_readiness,
        raising=False,
    )
    host, reported_locks, logger = _fake_host()

    with pytest.raises(RuntimeError, match="device_action_transport_not_ready"):
        HostNode.initialize_device(host, "scanner-a", SimpleNamespace())

    assert host._online_devices == set()
    assert reported_locks == []
    assert any("device_action_transport_not_ready" in message for message in logger.messages)
