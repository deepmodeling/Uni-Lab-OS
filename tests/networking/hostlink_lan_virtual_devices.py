"""README LAN demo adapted into spawn-safe virtual devices for CI."""

from __future__ import annotations

import threading
import time
from typing import Any

from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.backend import HostLinkBackendRuntime
from unilabos.utils.decorator import subscribe, topic_config


SUB_DEVICE_ID = "sub_reporter"


class VirtualLanHub:
    """订阅远端计数器，并在达到阈值后调用远端停止动作。"""

    def __init__(
        self,
        device_id: str | None = None,
        event_queue: Any = None,
        terminate_after: int = 3,
        **_kwargs: Any,
    ) -> None:
        self.device_id = device_id or "hub_node"
        self._event_queue = event_queue
        self._terminate_after = int(terminate_after)
        self._node: Any = None
        self._received_count = 0
        self._triggered = False
        self._states: list[str] = []

    def post_init(self, node: Any) -> None:
        self._node = node

    @subscribe(device_id=SUB_DEVICE_ID, status_name="counter")
    def on_sub_counter(self, value: Any) -> None:
        counter = int(value)
        if counter <= 0 or self._triggered:
            return
        self._received_count += 1
        self._event_queue.put(
            ("hub_counter", {"value": counter, "count": self._received_count})
        )
        if self._received_count < self._terminate_after:
            return
        self._triggered = True
        threading.Thread(
            target=self._terminate_sub,
            daemon=True,
            name="virtual-lan-stop-action",
        ).start()

    @subscribe(
        device_id=SUB_DEVICE_ID,
        status_name="state",
        trigger_when_change=True,
    )
    def on_sub_state(self, state: Any) -> None:
        normalized = str(state)
        self._states.append(normalized)
        self._event_queue.put(("hub_state", normalized))

    def _terminate_sub(self) -> None:
        try:
            result = self._node.call_device_action(
                SUB_DEVICE_ID,
                "stop_counting",
                {},
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001 - 子进程需把错误送回 pytest
            self._event_queue.put(("worker_error", f"hub action: {exc!r}"))
            return
        self._event_queue.put(
            (
                "closed_loop",
                {
                    "received_count": self._received_count,
                    "states": list(self._states),
                    "result": result,
                },
            )
        )


class VirtualLanReporter:
    """周期状态由 Slave 心跳读取，并通过 HostLink Topic 发布。"""

    def __init__(
        self,
        device_id: str | None = None,
        event_queue: Any = None,
        count_rate: float = 100.0,
        **_kwargs: Any,
    ) -> None:
        self.device_id = device_id or SUB_DEVICE_ID
        self._event_queue = event_queue
        self._count_rate = float(count_rate)
        self._started_at = time.monotonic()
        self._paused = False

    def post_init(self, _node: Any) -> None:
        self._started_at = time.monotonic()
        self._paused = False

    @property
    @topic_config(period=0.02)
    def counter(self) -> int:
        if self._paused:
            return 0
        return max(1, int((time.monotonic() - self._started_at) * self._count_rate))

    @property
    @topic_config(period=0.02)
    def state(self) -> str:
        return "paused" if self._paused else "running"

    def stop_counting(self) -> dict[str, Any]:
        stopped_at = self.counter
        self._paused = True
        result = {
            "success": True,
            "stopped_at": stopped_at,
            "device_id": self.device_id,
        }
        self._event_queue.put(("reporter_stopped", result))
        return result


def _configure_hostlink() -> None:
    HostLinkConfig.enable = True
    HostLinkConfig.heartbeat_interval = 0.05
    HostLinkConfig.heartbeat_timeout = 1.0
    HostLinkConfig.connect_timeout = 3.0
    HostLinkConfig.request_timeout = 5.0
    BasicConfig.slave_no_host = False


def run_virtual_lan_host(event_queue: Any, stop_event: Any) -> None:
    """Start the virtual Hub in its own Host process."""

    runtime: HostLinkBackendRuntime | None = None
    try:
        _configure_hostlink()
        HostLinkConfig.bind = "0.0.0.0"
        HostLinkConfig.port = 0
        BasicConfig.machine_name = "virtual-lan-host"
        local = BasicRuntime("hostlink")
        local.add_driver(
            BasicDriverSpec(
                device_id="hub_node",
                driver_class=VirtualLanHub,
                config={
                    "event_queue": event_queue,
                    "terminate_after": 3,
                },
                registry_name="hub_node_demo",
            )
        )
        runtime = HostLinkBackendRuntime(local, is_slave=False)
        runtime.start()
        assert runtime.server is not None
        event_queue.put(("host_ready", {"port": runtime.server.port}))
        stop_event.wait(15.0)
    except Exception as exc:  # noqa: BLE001 - 子进程需把错误送回 pytest
        event_queue.put(("worker_error", f"host: {exc!r}"))
        raise
    finally:
        if runtime is not None:
            runtime.stop()


def run_virtual_lan_slave(
    host: str,
    port: int,
    event_queue: Any,
    stop_event: Any,
) -> None:
    """Start the virtual Reporter in a separate Slave process."""

    runtime: HostLinkBackendRuntime | None = None
    try:
        _configure_hostlink()
        HostLinkConfig.host = str(host)
        HostLinkConfig.port = int(port)
        BasicConfig.machine_name = "virtual-lan-slave"
        local = BasicRuntime("hostlink")
        local.add_driver(
            BasicDriverSpec(
                device_id=SUB_DEVICE_ID,
                driver_class=VirtualLanReporter,
                config={"event_queue": event_queue, "count_rate": 100.0},
                registry_name="status_reporter_demo",
                action_names=("stop_counting",),
                status_names=("counter", "state"),
            )
        )
        runtime = HostLinkBackendRuntime(local, is_slave=True)
        runtime.start()
        event_queue.put(("slave_ready", {"host": host, "port": port}))
        stop_event.wait(15.0)
    except Exception as exc:  # noqa: BLE001 - 子进程需把错误送回 pytest
        event_queue.put(("worker_error", f"slave: {exc!r}"))
        raise
    finally:
        if runtime is not None:
            runtime.stop()


__all__ = ["run_virtual_lan_host", "run_virtual_lan_slave"]
