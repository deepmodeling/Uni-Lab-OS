from __future__ import annotations

import threading

import pytest

from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice


def test_plc_configures_long_lived_opcua_timeouts(monkeypatch):
    class FakeClient:
        def __init__(self, url):
            self.url = url

    monkeypatch.setattr("unilabos.devices.workstation.szlab_poly_studio.plc.Client", FakeClient)

    device = SZLabPolyPLCDevice(
        url="opc.tcp://127.0.0.1:4840/",
        csv_path=False,
        auto_connect=False,
    )

    assert device.client.session_timeout == 8 * 60 * 60 * 1000
    assert device.client.secure_channel_timeout == 60 * 60 * 1000


def test_plc_read_reconnects_once_and_retries(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)
    device._session_generation = 4
    reads = []
    reconnects = []

    def read_once(name):
        reads.append(name)
        if len(reads) == 1:
            raise RuntimeError('"The session id is not valid."(BadSessionIdInvalid)')
        return True

    def reconnect(observed_generation, reason, force=False):
        reconnects.append((observed_generation, str(reason), force))
        device._session_generation += 1

    monkeypatch.setattr(device, "_read_variable_once", read_once)
    monkeypatch.setattr(device, "_reconnect_after_failure", reconnect)

    assert device.read_variable("S06加工完成", use_cache=False) is True
    assert reads == ["S06加工完成", "S06加工完成"]
    assert reconnects == [
        (4, '"The session id is not valid."(BadSessionIdInvalid)', False),
    ]


def test_plc_treats_missing_socket_write_as_recoverable():
    device = object.__new__(SZLabPolyPLCDevice)

    assert device._is_recoverable_connection_error(
        AttributeError("'NoneType' object has no attribute 'write'")
    )


@pytest.mark.parametrize(
    "detail",
    [
        "[Errno 9] Bad file descriptor",
        "CancelledError(CancelledError())",
    ],
)
def test_plc_treats_cancelled_or_closed_read_as_recoverable(detail):
    device = object.__new__(SZLabPolyPLCDevice)

    assert device._is_recoverable_connection_error(
        RuntimeError(
            "读取 PLC 变量失败: Robot_任务完成: "
            f"NodeId=ns=4;s=上位机通讯|Robot_任务完成: {detail}"
        )
    )


def test_plc_reconnect_restores_sensor_subscription(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.disconnect_calls = 0
            self.connect_calls = 0

        def disconnect(self):
            self.disconnect_calls += 1

        def connect(self):
            self.connect_calls += 1

    def callback(*_args):
        return None

    device = object.__new__(SZLabPolyPLCDevice)
    device.client = FakeClient()
    device._auto_reconnect = True
    device._reconnect_attempts = 2
    device._reconnect_interval = 0.0
    device._reconnect_lock = threading.RLock()
    device._sensor_subscription_lock = threading.RLock()
    device._session_generation = 2
    device._sensor_change_callbacks = [callback]
    device._sensor_array_subscription = object()
    device._sensor_array_subscription_handles = [object()]
    device._sensor_array_node_indexes = {"node": 0}
    device._sensor_array_subscription_values = {0: [False] * 16}
    device._sensor_array_subscription_interval_ms = 350
    restored = []

    def restore(restored_callback, interval_ms=200):
        restored.append((restored_callback, interval_ms))
        device._sensor_array_subscription = object()

    monkeypatch.setattr(device, "start_sensor_array_subscription", restore)

    device._reconnect_after_failure(
        2,
        RuntimeError('"The session id is not valid."(BadSessionIdInvalid)'),
    )

    assert device.client.disconnect_calls == 1
    assert device.client.connect_calls == 1
    assert device._session_generation == 3
    assert restored == [(callback, 350)]
    assert device._sensor_change_callbacks == [callback]


def test_plc_write_reconnects_without_retrying_ambiguous_write(monkeypatch):
    device = object.__new__(SZLabPolyPLCDevice)
    device._session_generation = 1
    node = object()
    writes = []
    reconnects = []
    device.use_node = lambda _name: node

    def fail_write(actual_node, value):
        writes.append((actual_node, value))
        raise RuntimeError('"The session id is not valid."(BadSessionIdInvalid)')

    def reconnect(observed_generation, reason, force=False):
        reconnects.append((observed_generation, str(reason), force))
        device._session_generation += 1

    monkeypatch.setattr(device, "_write_value_only", fail_write)
    monkeypatch.setattr(device, "_reconnect_after_failure", reconnect)

    with pytest.raises(RuntimeError, match="为避免重复写入，本次写操作未自动重试"):
        device.write_variable("任务号", 17)

    assert writes == [(node, 17)]
    assert reconnects == [
        (1, '"The session id is not valid."(BadSessionIdInvalid)', False),
    ]
