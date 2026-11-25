"""ROS 2 Host：Host 进程里的一台设备装配或构造失败只让它进入维护模式，与 HostLink 同一份不可用记录。

以前只有 ``DeviceClassInvalid`` 被接住；驱动构造器抛错、权威里的设备状态装配不起来都会从
``HostNode.__init__`` 抛出去，Host 退出。自己有 Slave 的设备（``--root_devices_as_slaves``）不经
``HostNode.initialize_device``，Slave 里的 ``initialize_device_from_dict`` 照旧把异常抛出去，由 Slave
退出与受管进程的重启策略处理。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from unilabos.client.host.ros2 import initialize_device as initialize_module
from unilabos.client.host.ros2.presets.host_node import HostNode
from unilabos.client.host.runtime.exception import DeviceClassInvalid


class _Host:
    initialize_device = HostNode.initialize_device

    def __init__(self) -> None:
        self.registered: list[str] = []

    def lab_logger(self):
        return logging.getLogger(__name__)

    def _register_local_device(self, device_id, _node) -> None:
        self.registered.append(device_id)


def _config(device_id: str):
    return SimpleNamespace(res_content=SimpleNamespace(id=device_id, template_name=f"{device_id}_driver"))


def _broken(device_id: str, failure):
    def step(*args, **kwargs):
        if args[0] == device_id:
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return object()

    return step


@pytest.mark.parametrize(
    "failing_step, outcome, stage, error_type",
    [
        ("initialize_device_from_definition", RuntimeError("串口打不开"), "construct", "RuntimeError"),
        ("initialize_device_from_definition", None, "construct", "RuntimeError"),  # DeviceInitError：实例创建返回空
        (
            "resolve_device_definition",
            ValueError("物料权威中设备 pump 存在注册表 available_sites 未声明的 Site: ['held_material']"),
            "definition",
            "ValueError",
        ),
    ],
)
def test_a_failing_device_enters_maintenance_and_the_host_keeps_going(
    monkeypatch, failing_step, outcome, stage, error_type
) -> None:
    monkeypatch.setattr(initialize_module, "resolve_device_definition", lambda *args, **kwargs: object())
    monkeypatch.setattr(initialize_module, "initialize_device_from_definition", lambda *args: object())
    monkeypatch.setattr(initialize_module, failing_step, _broken("pump", outcome))
    host = _Host()

    host.initialize_device("pump", _config("pump"))
    host.initialize_device("balance", _config("balance"))

    assert host.registered == ["balance"]
    record = host._maintenance_devices["pump"]
    assert record["maintenance"] is True and record["backend"] == "ros2"
    assert record["stage"] == stage and record["error_type"] == error_type
    assert record["registry_name"] == "pump_driver" and "pump" in record["reason"]


def test_graph_configuration_errors_are_not_maintenance(monkeypatch) -> None:
    def missing(device_id, *_args, **_kwargs):
        raise DeviceClassInvalid(f"Device [{device_id}] registry 'ghost' not found.")

    monkeypatch.setattr(initialize_module, "resolve_device_definition", missing)
    host = _Host()

    host.initialize_device("ghost", _config("ghost"))

    assert host.registered == []
    assert "ghost" not in getattr(host, "_maintenance_devices", {})


def test_the_slave_path_still_raises(monkeypatch) -> None:
    """Slave 进程（含 ``--root_devices_as_slaves``）用 ``initialize_device_from_dict``：构造失败照旧抛出。"""

    monkeypatch.setattr(initialize_module, "resolve_device_definition", lambda *args, **kwargs: object())

    def boom(*_args):
        raise RuntimeError("构造时故意失败")

    monkeypatch.setattr(initialize_module, "initialize_device_from_definition", boom)

    with pytest.raises(RuntimeError, match="构造时故意失败"):
        initialize_module.initialize_device_from_dict("fault_boom", _config("fault_boom"))
