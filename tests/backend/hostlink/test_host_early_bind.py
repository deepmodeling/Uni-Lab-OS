"""Host 的 HostLink 端口在进程启动之初就可连，Slave 可以先于 Host 启动。

- Host 先开端口再装配 / 启动设备：设备 post_init 还卡着时 Slave 已能握手、登记、心跳；
- 依赖 Host 状态的请求（物料权威、本机设备 / service）在对应阶段就绪前回「Host 启动中」，
  Slave 原样退避重试，Host 就绪后拿到正确结果；等待有上限，超时给出可操作的报错；
- Slave 连接按退避重试直到 ``host_wait_timeout``，不会在一次连接超时后就放弃；
- 预绑定的 server 由设备 runtime 接管；Host 重启（同端口重新拉起）时端口立刻可重新绑定。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any, Callable, Optional

import pytest

from unilabos.backend.hostlink import startup as hostlink_startup
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.hostlink.protocol import (
    HOST_NOT_READY,
    ActionType,
    HostNotReadyError,
    LinkError,
    PROTOCOL_VERSION,
    new_request,
    response_error,
)
from unilabos.backend.hostlink.server import (
    HOST_STARTUP_GATES,
    HostLinkServer,
    HostStage,
    get_hostlink_server,
    set_hostlink_server,
)
from unilabos.config.config import BasicConfig, HostLinkConfig

SLAVE_DEVICE = "slave-dev"
HOST_DEVICE = "host-blocker"


def _wait_until(predicate: Callable[[], Any], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class _Gate:
    """驱动 post_init 卡在这里，直到测试放行：模拟启动很慢的真实设备。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


#: device_id -> 该设备 post_init 要等的闸门（驱动类按路径导入，只能经模块级表传入）
_GATES: dict[str, _Gate] = {}


class BlockingDriver:
    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id
        self.gate = _GATES[str(device_id)]

    def post_init(self, node) -> None:
        self.gate.entered.set()
        if not self.gate.release.wait(30):
            raise RuntimeError("测试未放行设备启动")

    def echo(self, value: int = 0) -> int:
        return int(value)


def _blocking_driver(gate: _Gate) -> type:
    _GATES[HOST_DEVICE] = gate
    return BlockingDriver


class EchoDriver:
    def __init__(self, device_id=None, config=None) -> None:
        self.device_id = device_id

    def echo(self, value: int = 0) -> int:
        return int(value)


def _runtime(device_id: str, driver_class: type) -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            device_id=device_id,
            driver_class=driver_class,
            config={},
            registry_name="echo",
            display_name=device_id,
            action_names=("echo",),
        )
    )
    return runtime


def _start_in_thread(backend: HostLinkBackend) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            backend.start()
        except BaseException as exc:  # noqa: BLE001 - 交给测试断言
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="test-backend-start", daemon=True)
    thread.start()
    return thread, outcome


def _slave_client(port: int, **kwargs: Any) -> HostLinkClient:
    options: dict[str, Any] = {
        "machine_name": "early-slave",
        "device_descriptors": [{"id": SLAVE_DEVICE, "actions": ["echo"]}],
        "heartbeat_interval": 0.1,
        "connect_timeout": 0.5,
        "request_timeout": 2.0,
        "host_wait_timeout": 20.0,
    }
    options.update(kwargs)
    return HostLinkClient("127.0.0.1", port, **options)


def _device_call(value: int) -> dict[str, Any]:
    return {
        "caller_device_id": SLAVE_DEVICE,
        "device_id": HOST_DEVICE,
        "action": "echo",
        "arguments": {"value": value},
    }


@pytest.fixture
def hostlink_config(monkeypatch):
    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.1)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 0.5)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "host_wait_timeout", 20.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "early-bind-slave")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)
    yield HostLinkConfig
    hostlink_startup.release_prebound_host_server()
    set_hostlink_server(None)
    for gate in _GATES.values():
        gate.release.set()
    _GATES.clear()


def _record_not_ready(server: HostLinkServer) -> list[str]:
    """包一层 dispatch，记下 Host 回过的「Host 启动中」应答（按所等阶段）。"""

    seen: list[str] = []
    original = server.dispatch

    def dispatch(message, peer_key):
        response = original(message, peer_key)
        if response.get("not_ready"):
            seen.append(str(response["not_ready"]))
        return response

    server.dispatch = dispatch  # type: ignore[method-assign]
    return seen


# ---------------------------------------------------------------------------
# 门控语义（不建连接）
# ---------------------------------------------------------------------------


def _dispatch(server: HostLinkServer, action: str, data: Optional[dict] = None) -> dict:
    message = new_request(action, data=data if data is not None else {})
    return server.dispatch(message, "127.0.0.1:50000")


def test_startup_gate_answers_not_ready_before_handlers_run() -> None:
    server = HostLinkServer("127.0.0.1", 0)
    server.hold_until_ready(HOST_STARTUP_GATES)
    calls: list[str] = []
    try:
        hello = _dispatch(server, ActionType.HELLO, {"machine_name": "gate-slave"})
        assert hello["ok"] is True
        assert hello["data"]["pending_stages"] == [HostStage.MATERIALS, HostStage.DEVICES]
        assert _dispatch(server, ActionType.PING)["ok"] is True
        assert _dispatch(server, ActionType.DEVICE_STATE, {"device_id": "x"})["ok"] is True

        # 处理器还没装配：按启动中拒绝，而不是「unknown action」
        early = _dispatch(server, ActionType.TOPIC_SUBSCRIBE, {"topic": "/a"})
        assert early["ok"] is False and early["not_ready"] == HostStage.STARTUP

        for action in (ActionType.MATERIAL_SEARCH, ActionType.DEVICE_CALL, ActionType.TOPIC_SUBSCRIBE):
            server.register_handler(action, lambda data, peer, name=action: calls.append(name) or {"done": name})

        material = _dispatch(server, ActionType.MATERIAL_SEARCH, {"name": "plate"})
        device = _dispatch(server, ActionType.DEVICE_CALL, {})
        assert material["not_ready"] == HostStage.MATERIALS
        assert device["not_ready"] == HostStage.DEVICES
        assert material["error_info"]["code"] == HOST_NOT_READY
        assert material["retry_after"] > 0
        assert calls == []  # 拒绝发生在处理器之前：没有任何副作用

        error = response_error(material)
        assert isinstance(error, HostNotReadyError) and error.stage == HostStage.MATERIALS

        # 不依赖阶段的请求一装配处理器就放行
        assert _dispatch(server, ActionType.TOPIC_SUBSCRIBE, {"topic": "/a"})["ok"] is True

        server.mark_ready(HostStage.MATERIALS)
        assert _dispatch(server, ActionType.MATERIAL_SEARCH, {"name": "plate"})["data"] == {
            "done": ActionType.MATERIAL_SEARCH
        }
        assert _dispatch(server, ActionType.DEVICE_CALL, {})["not_ready"] == HostStage.DEVICES
        assert _dispatch(server, "material.unknown")["not_ready"] == HostStage.STARTUP

        server.mark_ready(HostStage.DEVICES)
        assert server.pending_stages() == [] and not server.starting
        assert _dispatch(server, ActionType.DEVICE_CALL, {})["ok"] is True
        unknown = _dispatch(server, "material.unknown")
        assert unknown["ok"] is False and "unknown action" in unknown["error"]
        assert "not_ready" not in unknown
        assert _dispatch(server, ActionType.HELLO, {"machine_name": "gate-slave"})["data"]["pending_stages"] == []
    finally:
        server.stop()


def test_forwarded_not_ready_error_info_is_not_retried() -> None:
    """嵌套调用转发回来的 error_info 即使带 host_not_ready 也不是门控拒绝，不能重试。"""

    forwarded = {
        "v": PROTOCOL_VERSION,
        "kind": "resp",
        "id": "x",
        "ok": False,
        "error": "inner failure",
        "error_info": {"code": HOST_NOT_READY, "stage": HostStage.DEVICES},
    }
    error = response_error(forwarded)
    assert not isinstance(error, HostNotReadyError)


def test_ungated_server_keeps_unknown_action_behavior() -> None:
    server = HostLinkServer("127.0.0.1", 0)
    try:
        response = _dispatch(server, ActionType.MATERIAL_SEARCH, {"name": "x"})
        assert response["ok"] is False and "unknown action" in response["error"]
        assert "not_ready" not in response
        assert _dispatch(server, ActionType.HELLO, {"machine_name": "s"})["data"]["pending_stages"] == []
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Host：端口先开，设备后起
# ---------------------------------------------------------------------------


def test_host_port_accepts_slaves_before_devices_finish_starting(hostlink_config) -> None:
    gate = _Gate()
    host = HostLinkBackend(_runtime(HOST_DEVICE, _blocking_driver(gate)), is_slave=False)
    thread, outcome = _start_in_thread(host)
    client: Optional[HostLinkClient] = None
    try:
        assert gate.entered.wait(10), "Host 设备没有开始启动"
        server = host.server
        assert server is not None and server.port > 0
        # 设备 post_init 仍卡着：端口已经可连
        assert thread.is_alive() and not gate.release.is_set()
        assert _port_open(server.port)
        assert get_hostlink_server() is server

        client = _slave_client(server.port)
        assert client.connect_blocking(timeout=5), client.last_error
        assert HostStage.DEVICES in client.hello_info["pending_stages"]
        assert HostStage.MATERIALS not in client.hello_info["pending_stages"]
        # 登记与心跳：Host 在设备就绪前就看得到 Slave 及其设备
        assert server.has_device(SLAVE_DEVICE)
        client.heartbeat_now()
        assert _wait_until(lambda: any(p["online"] for p in server.peers()))
        assert host.devices()[SLAVE_DEVICE]["location"] == "remote"

        # 依赖 Host 设备的请求：明确的「Host 启动中」，不是错误结果
        with pytest.raises(HostNotReadyError) as exc_info:
            client._request_once(ActionType.DEVICE_CALL, _device_call(5), None, require_online=True)
        assert exc_info.value.stage == HostStage.DEVICES

        gate.release.set()
        thread.join(10)
        assert not thread.is_alive() and "error" not in outcome
        assert server.pending_stages() == []
        response = client.request(ActionType.DEVICE_CALL, _device_call(5))
        assert response["result"] == 5
        assert host.server is server  # 同一个 server，端口全程未变
    finally:
        gate.release.set()
        thread.join(10)
        if client is not None:
            client.close()
        host.stop()


def test_not_ready_requests_are_retried_until_host_is_ready(hostlink_config) -> None:
    gate = _Gate()
    host = HostLinkBackend(_runtime(HOST_DEVICE, _blocking_driver(gate)), is_slave=False)
    thread, outcome = _start_in_thread(host)
    client: Optional[HostLinkClient] = None
    try:
        assert gate.entered.wait(10)
        assert host.server is not None
        not_ready = _record_not_ready(host.server)
        client = _slave_client(host.server.port)
        assert client.connect_blocking(timeout=5), client.last_error

        results: dict[str, Any] = {}

        def call_sync() -> None:
            try:
                results["sync"] = client.request(ActionType.DEVICE_CALL, _device_call(3))
            except BaseException as exc:  # noqa: BLE001
                results["sync"] = exc

        def call_async() -> None:
            try:
                results["async"] = asyncio.run(
                    client.request_async(ActionType.DEVICE_CALL, _device_call(4))
                )
            except BaseException as exc:  # noqa: BLE001
                results["async"] = exc

        callers = [threading.Thread(target=call_sync), threading.Thread(target=call_async)]
        for caller in callers:
            caller.start()
        # 两个请求都被拒过至少一次，仍在退避重试中
        assert _wait_until(lambda: not_ready.count(HostStage.DEVICES) >= 4, timeout=10), not_ready
        assert results == {}

        gate.release.set()
        for caller in callers:
            caller.join(10)
        assert results["sync"]["result"] == 3, results
        assert results["async"]["result"] == 4, results
        thread.join(10)
        assert "error" not in outcome
    finally:
        gate.release.set()
        thread.join(10)
        if client is not None:
            client.close()
        host.stop()


def test_not_ready_wait_is_bounded_with_a_clear_error(hostlink_config) -> None:
    server = hostlink_startup.create_host_server()
    server.register_handler(ActionType.DEVICE_CALL, lambda data, peer: {"never": True})
    server.start()
    client = _slave_client(server.port, host_wait_timeout=1.0)
    try:
        assert client.connect_blocking(timeout=5), client.last_error
        started = time.monotonic()
        with pytest.raises(HostNotReadyError) as exc_info:
            client.request(ActionType.DEVICE_CALL, _device_call(1))
        elapsed = time.monotonic() - started
        assert 0.9 <= elapsed < 5.0
        message = str(exc_info.value)
        assert "1s 内仍未就绪" in message and HostStage.DEVICES in message
        assert "host_wait_timeout" in message
        assert exc_info.value.error_info["code"] == HOST_NOT_READY
        assert isinstance(exc_info.value, LinkError)  # 既有 except LinkError 的调用方照常兜住
    finally:
        client.close()
        server.stop()


# ---------------------------------------------------------------------------
# 预绑定：进程启动之初打开端口，设备 runtime 接管同一个 server
# ---------------------------------------------------------------------------


def test_prebound_port_serves_materials_once_backend_attaches(hostlink_config, tmp_path) -> None:
    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.server.backend.composition import set_materials_gateway
    from unilabos.server.services.materials import MaterialsService

    server = hostlink_startup.prebind_host_server()
    assert server is not None and _port_open(server.port)
    assert get_hostlink_server() is server
    assert hostlink_startup.prebind_host_server() is server  # 幂等
    not_ready = _record_not_ready(server)

    client = _slave_client(server.port)
    service = MaterialsService(tmp_path / "materials.db")
    gate = _Gate()
    host = HostLinkBackend(_runtime(HOST_DEVICE, _blocking_driver(gate)), is_slave=False)
    thread: Optional[threading.Thread] = None
    try:
        # 设备 runtime 还没开始：握手照常，物料请求等 materials 阶段
        assert client.connect_blocking(timeout=5), client.last_error
        assert client.hello_info["pending_stages"] == [HostStage.MATERIALS, HostStage.DEVICES]
        result: dict[str, Any] = {}
        searcher = threading.Thread(
            target=lambda: result.update(found=client.request(ActionType.MATERIAL_SEARCH, {"name": "plate"}))
        )
        searcher.start()
        assert _wait_until(lambda: HostStage.MATERIALS in not_ready)
        assert "found" not in result

        set_materials_gateway(LocalMaterialsClient(service))
        thread, outcome = _start_in_thread(host)
        searcher.join(10)
        assert result["found"] == []
        assert host.server is server and server.port == host.server.port
        assert hostlink_startup.adopt_prebound_host_server() is None  # 只交出一次
        assert gate.entered.wait(10)
        assert server.pending_stages() == [HostStage.DEVICES]
    finally:
        gate.release.set()
        if thread is not None:
            thread.join(10)
        client.close()
        host.stop()
        set_materials_gateway(None)
        service.close()


def test_release_prebound_server_closes_the_port(hostlink_config) -> None:
    server = hostlink_startup.prebind_host_server()
    assert server is not None and _port_open(server.port)
    hostlink_startup.release_prebound_host_server()
    assert get_hostlink_server() is None
    assert hostlink_startup.adopt_prebound_host_server() is None
    assert not _port_open(server.port)


def test_prebind_failure_defers_to_backend_bind(hostlink_config, monkeypatch) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        monkeypatch.setattr(HostLinkConfig, "port", blocker.getsockname()[1])
        # 端口暂被占（如上一个 Host 还没退干净）：不在这里终止进程，留给 backend 按原路径报错
        assert hostlink_startup.prebind_host_server() is None
        assert get_hostlink_server() is None
    finally:
        blocker.close()


@pytest.mark.parametrize(
    ("args", "kwargs", "expected"),
    [
        ({"backend": "hostlink"}, {}, True),
        ({"backend": "hostlink", "no_safe_restart": True}, {}, True),
        ({"backend": "ros2"}, {}, False),
        ({"backend": "hostlink", "role": "backend"}, {}, False),
        ({"backend": "hostlink"}, {"is_slave": True}, False),
        ({"backend": "hostlink"}, {"check_mode": True}, False),
        ({"backend": "hostlink"}, {"runs_local_authority": True}, False),
    ],
)
def test_only_the_device_host_process_prebinds(hostlink_config, args, kwargs, expected) -> None:
    options = {"is_slave": False, "check_mode": False, "runs_local_authority": False, **kwargs}
    assert hostlink_startup.should_prebind_host_server(args, **options) is expected


def test_disabled_hostlink_does_not_prebind(hostlink_config, monkeypatch) -> None:
    monkeypatch.setattr(HostLinkConfig, "enable", False)
    assert not hostlink_startup.should_prebind_host_server(
        {"backend": "hostlink"}, is_slave=False, check_mode=False, runs_local_authority=False
    )


# ---------------------------------------------------------------------------
# Slave：先于 Host 启动、超时报错、Host 重启后迅速重连
# ---------------------------------------------------------------------------


def test_slave_started_before_host_connects_once_host_is_up(hostlink_config, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(HostLinkConfig, "port", port)
    slave = HostLinkBackend(_runtime(SLAVE_DEVICE, EchoDriver), is_slave=True)
    slave_thread, slave_outcome = _start_in_thread(slave)
    host = HostLinkBackend(_runtime(HOST_DEVICE, EchoDriver), is_slave=False)
    try:
        # 远超单次 connect_timeout（0.5s）仍在等 Host，而不是放弃
        time.sleep(2.0)
        assert slave_thread.is_alive() and "error" not in slave_outcome
        assert slave.client is not None and not slave.client.online

        host.start()
        assert host.server is not None and host.server.port == port
        slave_thread.join(10)
        assert not slave_thread.is_alive() and "error" not in slave_outcome, slave_outcome
        assert host.server.has_device(SLAVE_DEVICE)
        assert host.call_action(SLAVE_DEVICE, "echo", value=9) == 9
    finally:
        slave.stop()
        slave_thread.join(5)
        host.stop()


def test_slave_without_host_fails_after_wait_timeout_with_clear_error(hostlink_config, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(HostLinkConfig, "port", port)
    monkeypatch.setattr(HostLinkConfig, "host_wait_timeout", 1.5)
    slave = HostLinkBackend(_runtime(SLAVE_DEVICE, EchoDriver), is_slave=True)
    started = time.monotonic()
    with pytest.raises(LinkError) as exc_info:
        slave.start()
    elapsed = time.monotonic() - started
    assert 1.4 <= elapsed < 6.0
    message = str(exc_info.value)
    assert f"127.0.0.1:{port}" in message
    assert "1.5s 内无法连接" in message
    assert "最后一次错误" in message and "host_wait_timeout" in message


def test_relaunched_host_rebinds_promptly_and_slave_reconnects_before_devices(
    hostlink_config, monkeypatch
) -> None:
    """Host 子进程重启（退出码 75 后同参数拉起）：新 Host 立刻重绑同一端口，Slave 迅速重连并登记。"""

    port = _free_port()
    monkeypatch.setattr(HostLinkConfig, "port", port)
    first: Optional[HostLinkBackend] = HostLinkBackend(_runtime(HOST_DEVICE, EchoDriver), is_slave=False)
    first.start()
    slave = HostLinkBackend(_runtime(SLAVE_DEVICE, EchoDriver), is_slave=True)
    slave.start()
    gate = _Gate()
    second: Optional[HostLinkBackend] = None
    thread: Optional[threading.Thread] = None
    try:
        assert slave.client is not None and slave.client.online
        first.stop()  # 旧 Host 退出（退出码 75）：端口与全部连接随之关闭
        first = None
        assert _wait_until(lambda: not slave.client.online, timeout=5)

        # 重新拉起的 Host：进程启动之初就重绑同一端口（POSIX 下 SO_REUSEADDR，不受 TIME_WAIT 影响）
        relaunched = hostlink_startup.prebind_host_server()
        assert relaunched is not None and relaunched.port == port
        second = HostLinkBackend(_runtime(HOST_DEVICE, _blocking_driver(gate)), is_slave=False)
        thread, outcome = _start_in_thread(second)
        assert gate.entered.wait(10)

        # 新 Host 的设备还卡着，Slave 已经重连（退避封顶 2s）并重新登记设备
        assert _wait_until(lambda: slave.client.online, timeout=6)
        assert relaunched.has_device(SLAVE_DEVICE)
        assert relaunched.pending_stages() == [HostStage.DEVICES]

        gate.release.set()
        thread.join(10)
        assert "error" not in outcome
        assert second.call_action(SLAVE_DEVICE, "echo", value=11) == 11
    finally:
        gate.release.set()
        if thread is not None:
            thread.join(10)
        slave.stop()
        if second is not None:
            second.stop()
        if first is not None:
            first.stop()
