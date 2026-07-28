from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from opcua import ua

import unilabos.devices.workstation.szlab_poly_studio.plc as plc_module
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    SENSOR_ARRAY_COUNT,
    SENSOR_BITS_PER_ARRAY,
    SensorBase,
)
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    SZLabS08CapStationDevice,
)


class _FakeStatus:
    def is_good(self) -> bool:
        return True


class _ReentryDetectingTransport:
    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.reentered = threading.Event()
        self._block_first = True

    def call(self, result: Any) -> Any:
        should_block = False
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active > 1:
                self.reentered.set()
            elif self._block_first:
                self._block_first = False
                should_block = True
                self.first_entered.set()
        try:
            if self.reentered.is_set():
                raise RuntimeError("fake transport detected concurrent request")
            if should_block and not self.release_first.wait(timeout=2):
                raise TimeoutError("test did not release first OPC request")
            return result
        finally:
            with self._state_lock:
                self.active -= 1


class _FakeOpcNode:
    def __init__(self, transport: _ReentryDetectingTransport, node_id: str) -> None:
        self._transport = transport
        self.nodeid = node_id

    def get_data_type_as_variant_type(self) -> ua.VariantType:
        return self._transport.call(ua.VariantType.Boolean)


class _FakeVariable:
    def __init__(
        self,
        transport: _ReentryDetectingTransport,
        name: str,
        value: Any = True,
        error: Exception | None = None,
    ) -> None:
        self._transport = transport
        self._value = value
        self._error = error
        self.node_id = f"ns=2;s={name}"
        self._opc_node = _FakeOpcNode(transport, self.node_id)

    def read(self) -> tuple[Any, Exception | None]:
        return self._transport.call((self._value, self._error))

    def _get_node(self) -> _FakeOpcNode:
        return self._opc_node


class _FakeClient:
    def __init__(self, transport: _ReentryDetectingTransport) -> None:
        self._transport = transport
        self.uaclient = self

    def write(self, _params: Any) -> list[_FakeStatus]:
        return self._transport.call([_FakeStatus()])


class _TrackingRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depths: dict[int, int] = {}
        self.disconnect_waiting = threading.Event()
        self.disconnect_acquired = threading.Event()

    def __enter__(self) -> "_TrackingRLock":
        thread_id = threading.get_ident()
        if threading.current_thread().name == "disconnect":
            self.disconnect_waiting.set()
        self._lock.acquire()
        self._depths[thread_id] = self._depths.get(thread_id, 0) + 1
        if threading.current_thread().name == "disconnect":
            self.disconnect_acquired.set()
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        thread_id = threading.get_ident()
        depth = self._depths[thread_id] - 1
        if depth:
            self._depths[thread_id] = depth
        else:
            del self._depths[thread_id]
        self._lock.release()
        if (
            threading.current_thread().name == "subscribe"
            and depth == 0
            and self.disconnect_waiting.is_set()
        ):
            assert self.disconnect_acquired.wait(timeout=2)


class _FakeSubscription:
    def __init__(self, client: "_FakeSubscriptionClient") -> None:
        self._client = client
        self.subscribe_count = 0

    def subscribe_data_change(self, _node: Any) -> int:
        self.subscribe_count += 1
        if self.subscribe_count == 1:
            self._client.first_subscribe_entered.set()
            assert self._client.io_lock.disconnect_waiting.wait(timeout=2)
        if self.subscribe_count == SENSOR_ARRAY_COUNT:
            self._client.initializing = False
        return self.subscribe_count

    def delete(self) -> None:
        if self._client.initializing:
            self._client.lifecycle_interrupted.set()


class _FakeSubscriptionClient:
    def __init__(self, io_lock: _TrackingRLock) -> None:
        self.io_lock = io_lock
        self.initializing = False
        self.first_subscribe_entered = threading.Event()
        self.lifecycle_interrupted = threading.Event()
        self.disconnected = threading.Event()
        self.subscription = _FakeSubscription(self)

    def create_subscription(self, _interval_ms: int, _handler: Any) -> _FakeSubscription:
        self.initializing = True
        return self.subscription

    def get_node(self, node_id: str) -> Any:
        return type("OpcNode", (), {"nodeid": node_id})()

    def disconnect(self) -> None:
        if self.initializing:
            self.lifecycle_interrupted.set()
        self.disconnected.set()


def _fake_plc(
    transport: _ReentryDetectingTransport,
    variables: dict[str, _FakeVariable],
) -> SZLabPolyPLCDevice:
    device = SZLabPolyPLCDevice.__new__(SZLabPolyPLCDevice)
    device._standalone_opcua_client = True
    device._fallback_node_id_prefix = None
    device._direct_node_id_map = {}
    device._node_registry = variables
    device._variables_to_find = {name: {} for name in variables}
    device._sensor_read_warning_names = set()
    device._opc_io_lock = threading.RLock()
    device.client = _FakeClient(transport)
    return device


def _run_thread(
    target: Callable[[], Any],
    errors: list[BaseException],
    *,
    name: str | None = None,
) -> threading.Thread:
    def run() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


def test_station_opcua_client_compatibility_modules_are_removed() -> None:
    assert not hasattr(plc_module, "SzlabOpcUaVariableClient")
    root = Path("unilabos/devices/workstation/szlab_poly_studio")
    assert not (root / "s06_pump/opcua_client.py").exists()
    assert not (root / "s07_solid_addition/opcua_client.py").exists()
    assert not (root / "s08_decap/decap_s08_opcua_client.py").exists()


def test_s06_device_uses_szlab_poly_plc_device_directly() -> None:
    with patch.object(SZLabPolyPLCDevice, "__init__", autospec=True, return_value=None) as init:
        SzlabMixerPumpDevice(
            url="opc.tcp://example:4840",
            username="user",
            password="pass",
            opcua_browse_depth=3,
            opcua_browse_limit=99,
            opcua_node_id_map={"S06允许加工": "ns=4;s=S06允许加工"},
            opcua_allow_recursive_browse=True,
        )

    assert init.call_args.kwargs["csv_path"] is False
    assert init.call_args.kwargs["opcua_object_name"] == "VirtualMixer"
    assert init.call_args.kwargs["opcua_browse_depth"] == 3
    assert init.call_args.kwargs["opcua_browse_limit"] == 99
    assert init.call_args.kwargs["node_id_map"] == {"S06允许加工": "ns=4;s=S06允许加工"}
    assert init.call_args.kwargs["opcua_allow_recursive_browse"] is True


def test_s08_device_uses_szlab_poly_plc_device_directly() -> None:
    with patch.object(SZLabPolyPLCDevice, "__init__", autospec=True, return_value=None) as init:
        with patch.object(SZLabPolyPLCDevice, "write", return_value=None):
            SZLabS08CapStationDevice(
                url="opc.tcp://127.0.0.1:50102/",
                username="user",
                password="pass",
                opcua_browse_depth=4,
                opcua_browse_limit=88,
                opcua_node_id_map={"S08允许加工": "ns=4;s=S08允许加工"},
                opcua_allow_recursive_browse=True,
                opcua_object_name="CustomS08",
            )

    assert init.call_args.kwargs["csv_path"] is False
    assert init.call_args.kwargs["opcua_object_name"] == "CustomS08"
    assert init.call_args.kwargs["opcua_browse_depth"] == 4
    assert init.call_args.kwargs["opcua_browse_limit"] == 88
    assert init.call_args.kwargs["node_id_map"] == {"S08允许加工": "ns=4;s=S08允许加工"}
    assert init.call_args.kwargs["opcua_allow_recursive_browse"] is True


def test_shared_client_serializes_read_write_and_get_variables_io() -> None:
    transport = _ReentryDetectingTransport()
    device = _fake_plc(
        transport,
        {
            "read": _FakeVariable(transport, "read"),
            "write": _FakeVariable(transport, "write"),
            "batch": _FakeVariable(transport, "batch"),
        },
    )
    errors: list[BaseException] = []

    threads = [_run_thread(lambda: device.read_variable("read"), errors)]
    assert transport.first_entered.wait(timeout=1)
    threads.extend(
        [
            _run_thread(lambda: device.write_variable("write", True), errors),
            _run_thread(lambda: device.get_variables(["batch"]), errors),
        ]
    )
    reentered_before_release = transport.reentered.wait(timeout=0.1)
    transport.release_first.set()
    for thread in threads:
        thread.join(timeout=2)

    assert reentered_before_release is False
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert transport.max_active == 1


def test_dynamic_use_node_lookup_does_not_reenter_shared_client_io() -> None:
    transport = _ReentryDetectingTransport()
    dynamic_node = _FakeVariable(transport, "dynamic")
    write_node = _FakeVariable(transport, "write")

    def dynamic_base_use_node(
        _device: SZLabPolyPLCDevice,
        _name: str,
    ) -> _FakeVariable:
        return transport.call(dynamic_node)

    device = SZLabPolyPLCDevice.__new__(SZLabPolyPLCDevice)
    device.__dict__.update(_fake_plc(transport, {"write": write_node}).__dict__)
    device._standalone_opcua_client = False
    errors: list[BaseException] = []

    with patch.object(
        plc_module.BaseClient,
        "use_node",
        autospec=True,
        side_effect=dynamic_base_use_node,
    ):
        threads = [_run_thread(lambda: device.use_node("dynamic"), errors)]
        assert transport.first_entered.wait(timeout=1)
        threads.append(
            _run_thread(lambda: device._write_value_only(write_node, True), errors)
        )
        reentered_before_release = transport.reentered.wait(timeout=0.1)
        transport.release_first.set()
        for thread in threads:
            thread.join(timeout=2)

    assert reentered_before_release is False
    assert errors == []
    assert transport.max_active == 1


def test_subscription_initialization_is_atomic_with_disconnect() -> None:
    io_lock = _TrackingRLock()
    client = _FakeSubscriptionClient(io_lock)
    device = SZLabPolyPLCDevice.__new__(SZLabPolyPLCDevice)
    device._opc_io_lock = io_lock
    device._sensor_subscription_lock = threading.RLock()
    device._sensor_array_subscription = None
    device._sensor_array_subscription_handles = []
    device._sensor_array_node_indexes = {}
    device._sensor_array_subscription_values = {}
    device._sensor_change_callbacks = []
    device._direct_node_id_map = {
        SensorBase.array(index): f"ns=2;s=array-{index}"
        for index in range(SENSOR_ARRAY_COUNT)
    }
    device.client = client
    device.heartbeat_on = False
    device._heartbeat_timer = None
    errors: list[BaseException] = []

    subscribe_thread = _run_thread(
        lambda: device.start_sensor_array_subscription(lambda _index, _values: None),
        errors,
        name="subscribe",
    )
    assert client.first_subscribe_entered.wait(timeout=1)
    disconnect_thread = _run_thread(device.disconnect, errors, name="disconnect")
    subscribe_thread.join(timeout=2)
    disconnect_thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in (subscribe_thread, disconnect_thread))
    assert errors == []
    assert client.lifecycle_interrupted.is_set() is False
    assert client.subscription.subscribe_count == SENSOR_ARRAY_COUNT
    assert client.disconnected.is_set()


def test_opc_io_lock_exists_before_base_client_initialization(monkeypatch) -> None:
    lock_was_ready: list[bool] = []

    def observing_base_init(device: SZLabPolyPLCDevice) -> None:
        lock_was_ready.append(
            isinstance(device._opc_io_lock, type(threading.RLock()))
        )
        device._name_mapping = {}
        device._reverse_mapping = {}

    monkeypatch.setattr(plc_module, "Client", lambda _url: object())
    monkeypatch.setattr(plc_module.BaseClient, "__init__", observing_base_init)

    SZLabPolyPLCDevice(
        url="opc.tcp://example:4840",
        csv_path=False,
        auto_connect=False,
    )

    assert lock_was_ready == [True]


def test_sensor_scalar_fallback_warns_once_with_original_error(monkeypatch) -> None:
    transport = _ReentryDetectingTransport()
    transport.release_first.set()
    sensor_name = SensorBase.bit(2, 3)
    array_name = SensorBase.array(2)
    original_error = RuntimeError("BadRequestIdInvalid")
    warning_messages = []
    monkeypatch.setattr(
        plc_module.logger,
        "warning",
        lambda message: warning_messages.append(message),
    )
    array_values = [False] * SENSOR_BITS_PER_ARRAY
    array_values[3] = True
    device = _fake_plc(
        transport,
        {
            sensor_name: _FakeVariable(transport, sensor_name, error=original_error),
            array_name: _FakeVariable(transport, array_name, value=array_values),
        },
    )

    assert device.read_variable(sensor_name) is True
    assert device.read_variable(sensor_name) is True

    assert len(warning_messages) == 1
    assert sensor_name in warning_messages[0]
    assert "BadRequestIdInvalid" in warning_messages[0]


def test_opc_lock_is_released_between_action_io_calls(monkeypatch) -> None:
    transport = _ReentryDetectingTransport()
    transport.release_first.set()
    device = _fake_plc(
        transport,
        {
            "pulse": _FakeVariable(transport, "pulse"),
            "robot": _FakeVariable(transport, "robot"),
        },
    )
    between_writes = threading.Event()
    allow_reset = threading.Event()
    read_finished = threading.Event()
    errors: list[BaseException] = []

    def pause_between_writes(_delay: float) -> None:
        between_writes.set()
        if not allow_reset.wait(timeout=2):
            raise TimeoutError("test did not allow pulse reset")

    monkeypatch.setattr(plc_module.time, "sleep", pause_between_writes)
    pulse_thread = _run_thread(lambda: device.pulse("pulse"), errors)
    assert between_writes.wait(timeout=1)
    read_thread = _run_thread(
        lambda: (device.read_variable("robot"), read_finished.set()),
        errors,
    )

    assert read_finished.wait(timeout=1)
    allow_reset.set()
    pulse_thread.join(timeout=2)
    read_thread.join(timeout=2)

    assert errors == []
    assert transport.max_active == 1
