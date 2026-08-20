"""Multi-process LAN test adapted from README's LabDeviceLanDemo."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from queue import Empty
import socket
import time
from typing import Any

import pytest

from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.backend import HostLinkBackend

from tests.networking.hostlink_lan_virtual_devices import (
    HOST_NODE_ID,
    SUB_DEVICE_ID,
    run_virtual_lan_host,
    run_virtual_lan_slave,
)


def _primary_lan_ipv4() -> str | None:
    """Return a local non-loopback IPv4 address without sending network data."""

    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        candidates.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    finally:
        probe.close()
    try:
        candidates.extend(
            str(item[4][0])
            for item in socket.getaddrinfo(
                socket.gethostname(),
                None,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
        )
    except OSError:
        pass
    return next(
        (
            address
            for address in dict.fromkeys(candidates)
            if address
            and not address.startswith("127.")
            and not address.startswith("169.254.")
            and address != "0.0.0.0"
        ),
        None,
    )


def _wait_for_event(
    event_queue: Any,
    kind: str,
    seen: list[tuple[str, Any]],
    *,
    timeout: float,
) -> Any:
    for event_kind, payload in seen:
        if event_kind == kind:
            return payload
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event_kind, payload = event_queue.get(
                timeout=min(0.2, max(0.01, deadline - time.monotonic()))
            )
        except Empty:
            continue
        seen.append((event_kind, payload))
        if event_kind == "worker_error":
            pytest.fail(str(payload))
        if event_kind == kind:
            return payload
    pytest.fail(f"等待虚拟 LAN 事件超时：{kind}；已收到：{seen}")


def _stop_process(process: multiprocessing.Process, stop_event: Any) -> None:
    stop_event.set()
    process.join(timeout=5.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=3.0)


_LAN_IPV4 = _primary_lan_ipv4()


@pytest.mark.parametrize(
    ("connection_name", "host_address"),
    [
        ("loopback", "127.0.0.1"),
        pytest.param(
            "lan",
            _LAN_IPV4,
            marks=pytest.mark.skipif(
                _LAN_IPV4 is None,
                reason="当前 runner 没有非回环 IPv4 地址",
            ),
        ),
    ],
)
def test_host_node_and_device_complete_network_subscribe_and_action_loop(
    connection_name: str,
    host_address: str | None,
) -> None:
    """验证真实传输闭环，而不是仅验证 HostLink 端口可连接。

    流程：

    1. Host 进程启动 ``host_node`` 并监听 HostLink；
    2. Slave 进程携带设备身份加入，Host 确认设备在线；
    3. Slave 设备反向执行 HostNode action；
    4. 设备周期发布状态，HostNode 的 ``@subscribe`` 收到状态；
    5. HostNode 根据状态向设备下发停止 action，并核对远端执行结果。
    """

    assert host_address is not None
    context = multiprocessing.get_context("spawn")
    event_queue = context.Queue()
    stop_event = context.Event()
    seen: list[tuple[str, Any]] = []
    host_process = context.Process(
        target=run_virtual_lan_host,
        args=(event_queue, stop_event),
        name=f"hostlink-{connection_name}-host",
    )
    slave_process: multiprocessing.Process | None = None
    host_process.start()
    try:
        host_ready = _wait_for_event(
            event_queue,
            "host_ready",
            seen,
            timeout=8.0,
        )
        slave_process = context.Process(
            target=run_virtual_lan_slave,
            args=(host_address, int(host_ready["port"]), event_queue, stop_event),
            name=f"hostlink-{connection_name}-slave",
        )
        slave_process.start()
        slave_ready = _wait_for_event(
            event_queue,
            "slave_ready",
            seen,
            timeout=8.0,
        )
        device_online = _wait_for_event(
            event_queue,
            "device_online",
            seen,
            timeout=8.0,
        )
        host_action = _wait_for_event(
            event_queue,
            "host_action_executed",
            seen,
            timeout=8.0,
        )
        device_to_host = _wait_for_event(
            event_queue,
            "device_to_host_result",
            seen,
            timeout=8.0,
        )
        closed_loop = _wait_for_event(
            event_queue,
            "closed_loop",
            seen,
            timeout=8.0,
        )
        reporter_stopped = _wait_for_event(
            event_queue,
            "reporter_stopped",
            seen,
            timeout=2.0,
        )

        assert host_ready["host_node_id"] == HOST_NODE_ID
        assert slave_ready["host"] == host_address
        assert device_online == {
            "device_id": SUB_DEVICE_ID,
            "location": "remote",
            "actions": ["stop_counting"],
        }
        assert host_action == {
            "accepted": True,
            "host_node_id": HOST_NODE_ID,
            "source_device": SUB_DEVICE_ID,
        }
        assert device_to_host == host_action
        assert closed_loop["received_count"] >= 3
        assert closed_loop["result"] == reporter_stopped
        assert closed_loop["result"]["success"] is True
        assert closed_loop["result"]["device_id"] == SUB_DEVICE_ID
        assert any(
            kind == "host_subscribed_counter" for kind, _payload in seen
        )
        assert ("host_subscribed_state", "running") in seen
    finally:
        if slave_process is not None:
            _stop_process(slave_process, stop_event)
        _stop_process(host_process, stop_event)
        event_queue.close()

    assert host_process.exitcode == 0
    assert slave_process is not None and slave_process.exitcode == 0


def test_readme_lan_demo_actual_drivers_close_the_hostlink_loop(
    monkeypatch,
) -> None:
    """Run the pinned LabDeviceLanDemo drivers when CI checked them out."""

    examples_root = os.environ.get("UNILABOS_README_EXAMPLES_ROOT")
    if not examples_root:
        pytest.skip("README 外部设备包只在 CI 检出后运行")
    package_root = Path(examples_root) / "LabDeviceLanDemo"
    assert package_root.is_dir(), f"缺少 README LAN 示例仓库：{package_root}"
    monkeypatch.syspath_prepend(str(package_root))

    from lan_demo.hub_node import HubNodeDemo
    from lan_demo.status_reporter import StatusReporterDemo

    monkeypatch.setattr(HostLinkConfig, "enable", True)
    monkeypatch.setattr(HostLinkConfig, "bind", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "host", "127.0.0.1")
    monkeypatch.setattr(HostLinkConfig, "port", 0)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_interval", 0.05)
    monkeypatch.setattr(HostLinkConfig, "heartbeat_timeout", 1.0)
    monkeypatch.setattr(HostLinkConfig, "connect_timeout", 2.0)
    monkeypatch.setattr(HostLinkConfig, "request_timeout", 2.0)
    monkeypatch.setattr(BasicConfig, "machine_name", "readme-lan-demo")
    monkeypatch.setattr(BasicConfig, "slave_no_host", False)

    host_local = HostLinkLocalRuntime()
    hub = host_local.add_driver(
        HostLinkDriverSpec(
            device_id="hub_node",
            driver_class=HubNodeDemo,
            config={"sub_device": "sub_reporter", "terminate_after": 3},
            registry_name="hub_node_demo",
            status_names=("received_count", "terminations", "last_action"),
        )
    )
    slave_local = HostLinkLocalRuntime()
    reporter = slave_local.add_driver(
        HostLinkDriverSpec(
            device_id="sub_reporter",
            driver_class=StatusReporterDemo,
            config={
                "count_rate": 100.0,
                "cycle_pause": 60.0,
                "auto_start": True,
            },
            registry_name="status_reporter_demo",
            action_names=("stop_counting", "start_counting", "echo"),
            status_names=("counter", "heartbeat", "state"),
        )
    )
    host = HostLinkBackend(host_local, is_slave=False)
    slave = HostLinkBackend(slave_local, is_slave=True)
    host.start()
    assert host.server is not None
    HostLinkConfig.port = host.server.port
    try:
        slave.start()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if hub.driver._terminations >= 1 and reporter.driver._paused:
                break
            time.sleep(0.05)
        assert hub.driver._terminations >= 1
        assert reporter.driver._paused is True
        assert not hub.driver._last_action.startswith("终止失败")
    finally:
        slave.stop()
        host.stop()
