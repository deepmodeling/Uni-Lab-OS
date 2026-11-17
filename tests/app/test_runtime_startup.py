"""Host 启动提示、管理端端口预检，以及启动编排：就绪横幅 / 受管 Slave 拉起必须等设备 runtime 真正起来。"""

from __future__ import annotations

import errno
import socket
import sys
import threading

import pytest

from unilabos.app import runtime_startup
from unilabos.app.cli.parser import build_parser
from unilabos.app.runtime_startup import build_slave_launch_command
from unilabos.app.supervisor import PORT_IN_USE_EXIT_CODE
from unilabos.backend import BACKEND_PROFILES
from unilabos.backend.hostlink import adapter_registry
from unilabos.server.api.app import ManagementPortInUseError, ensure_port_available


def test_cli_defaults_to_hostlink_backend() -> None:
    args = build_parser().parse_args(["-g", "graph.json"])
    assert args.backend == "hostlink"


def test_slave_launch_command_uses_hostlink_target() -> None:
    command = build_slave_launch_command(
        backend="hostlink",
        host_ip="192.168.1.10",
        hostlink_port=7302,
        hostlink_enabled=True,
    )
    assert command == (
        "unilab --backend hostlink --is_slave --host_node_ip 192.168.1.10 "
        "--hostlink_port 7302 -g <图文件.json>"
    )


def test_slave_launch_command_without_hostlink_falls_back_to_dds() -> None:
    command = build_slave_launch_command(
        backend="ros2",
        host_ip="192.168.1.10",
        hostlink_port=7302,
        hostlink_enabled=False,
    )
    assert command == "unilab --backend ros2 --is_slave --disable_hostlink -g <图文件.json>"


def test_port_check_reports_actionable_hint_when_in_use() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(ManagementPortInUseError) as excinfo:
            ensure_port_available("127.0.0.1", port)
    finally:
        holder.close()
    message = excinfo.value.strerror
    assert str(port) in message
    assert f"--port {port + 1}" in message
    # 端口释放后预检必须放行
    ensure_port_available("127.0.0.1", port)


# ── 启动编排：等设备 runtime 真正就绪 ──────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不复用 TIME_WAIT 端口")
def test_port_check_allows_closed_connections_but_rejects_live_reusable_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        with pytest.raises(ManagementPortInUseError):
            ensure_port_available("127.0.0.1", port)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _ = holder.accept()
            accepted.close()  # 服务端主动关闭，留下真实 TIME_WAIT 连接。
            assert client.recv(1) == b""
    ensure_port_available("127.0.0.1", port)


@pytest.fixture()
def _clean_adapter_registry():
    adapter_registry.clear_execution_adapter()
    yield
    adapter_registry.clear_execution_adapter()


def _thread(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@pytest.mark.usefixtures("_clean_adapter_registry")
def test_ready_banner_and_managed_processes_wait_for_execution_adapter(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime_startup, "print_slave_launch_hint", lambda: calls.append("hint"))
    monkeypatch.setattr(
        runtime_startup, "_start_managed_device_processes", lambda: calls.append("managed")
    )
    monkeypatch.setattr(runtime_startup, "BACKEND_READY_TIMEOUT_S", 5.0)

    release = threading.Event()
    stop = threading.Event()

    def backend():
        release.wait(5.0)
        adapter_registry.set_execution_adapter(object())  # HostNode 建好、HostLink 已监听
        stop.wait(5.0)

    backend_thread = _thread(backend)
    announcer = _thread(lambda: runtime_startup._announce_ready_and_start_managed(backend_thread))
    try:
        announcer.join(0.3)
        assert announcer.is_alive() and calls == []  # 端口还没绑上：什么都不做
        release.set()
        announcer.join(3.0)
        assert not announcer.is_alive()
        assert calls == ["hint", "managed"]
    finally:
        release.set()
        stop.set()


@pytest.mark.usefixtures("_clean_adapter_registry")
def test_nothing_is_launched_when_backend_dies_before_ready(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime_startup, "print_slave_launch_hint", lambda: calls.append("hint"))
    monkeypatch.setattr(
        runtime_startup, "_start_managed_device_processes", lambda: calls.append("managed")
    )
    monkeypatch.setattr(runtime_startup, "BACKEND_READY_TIMEOUT_S", 5.0)

    # 模拟 HostLink 端口被占：backend 线程未注册适配器就结束了
    backend_thread = _thread(lambda: None)
    backend_thread.join(2.0)
    runtime_startup._announce_ready_and_start_managed(backend_thread)
    assert calls == []


def test_exit_code_follows_backend_failure_kind(monkeypatch) -> None:
    from unilabos import backend as backend_module

    monkeypatch.setattr(backend_module, "_fatal_failure", None)
    runtime_startup._exit_on_backend_failure()  # 正常运行：不退出

    profile = BACKEND_PROFILES["hostlink"]
    monkeypatch.setattr("unilabos.server.api.app.abort_serving", lambda: None)

    def port_taken(*_):
        raise OSError(errno.EADDRINUSE, "HostLink 端口 7302 已被占用")

    backend_module._run_entrypoint(profile, port_taken, (None,))
    with pytest.raises(SystemExit) as excinfo:
        runtime_startup._exit_on_backend_failure()
    assert excinfo.value.code == PORT_IN_USE_EXIT_CODE  # 监督进程退避重试，占用者退出后自动恢复

    def driver_crashed(*_):
        raise RuntimeError("driver init failed")

    monkeypatch.setattr(backend_module, "_fatal_failure", None)
    backend_module._run_entrypoint(profile, driver_crashed, (None,))
    with pytest.raises(SystemExit) as excinfo:
        runtime_startup._exit_on_backend_failure()
    assert excinfo.value.code == 1
