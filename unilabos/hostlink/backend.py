"""由 Basic 驱动运行时与 HostLink 组成的无 ROS 分布式 backend。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Dict, Optional

from unilabos.basic.runtime import BasicRuntime
from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.hostlink.client import HostLinkClient, set_hostlink_client
from unilabos.hostlink.protocol import ActionType, LinkError
from unilabos.hostlink.server import HostLinkServer, set_hostlink_server
from unilabos.utils import logger


def to_wire_value(value: Any) -> Any:
    """Convert common driver return values into JSON-compatible structures."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_wire_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_wire_value(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_wire_value(model_dump(mode="json"))
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        return to_wire_value(legacy_dict())
    if isinstance(value, dict):
        return {str(key): to_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_wire_value(item) for item in value]
    return repr(value)


class HostLinkBackendRuntime:
    """Run local Python drivers and expose Slave devices to one Host."""

    def __init__(self, local: BasicRuntime, *, is_slave: bool) -> None:
        self.local = local
        self.is_slave = bool(is_slave)
        self.server: Optional[HostLinkServer] = None
        self.client: Optional[HostLinkClient] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if not HostLinkConfig.enable:
            raise ValueError("hostlink backend 不能与 --disable-hostlink 同时使用")
        self.local.start()
        try:
            if self.is_slave:
                self._start_slave()
            else:
                self._start_host()
        except Exception:
            self.stop()
            raise
        self._started = True

    def _start_host(self) -> None:
        self.server = HostLinkServer(
            bind=HostLinkConfig.bind,
            port=HostLinkConfig.port,
            heartbeat_timeout=HostLinkConfig.heartbeat_timeout,
            request_timeout=HostLinkConfig.request_timeout,
        )
        self.server.hello_payload = {
            "backend": "hostlink",
            "role": "host",
            "devices": self.local.descriptors(),
        }
        self.server.start()
        set_hostlink_server(self.server)
        logger.info(
            "[HostLink backend] Host 已启动：%s:%d，本地设备=%s",
            HostLinkConfig.bind,
            self.server.port,
            sorted(self.local.devices),
        )

    def _start_slave(self) -> None:
        host = str(HostLinkConfig.host or "").strip()
        if not host:
            raise ValueError(
                "hostlink backend 的 Slave 必须通过 --host-node-ip 指定 Host"
            )
        self.client = HostLinkClient(
            host=host,
            port=HostLinkConfig.port,
            machine_name=BasicConfig.machine_name,
            heartbeat_interval=HostLinkConfig.heartbeat_interval,
            connect_timeout=HostLinkConfig.connect_timeout,
            request_timeout=HostLinkConfig.request_timeout,
            device_descriptors=self.local.descriptors(),
            heartbeat_payload_provider=self._heartbeat_payload,
        )
        self.client.register_handler(ActionType.DEVICE_CALL, self._handle_device_call)
        self.client.register_handler(
            ActionType.DEVICE_STATE,
            self._handle_device_state,
        )
        set_hostlink_client(self.client)
        if BasicConfig.slave_no_host:
            self.client.start()
        elif not self.client.connect_blocking(HostLinkConfig.connect_timeout):
            raise LinkError(f"无法连接 HostLink Host：{host}:{HostLinkConfig.port}")
        logger.info(
            "[HostLink backend] Slave 已启动：Host=%s:%d，本地设备=%s",
            host,
            HostLinkConfig.port,
            sorted(self.local.devices),
        )

    def _heartbeat_payload(self) -> Dict[str, Any]:
        return {"states": to_wire_value(self.local.snapshot_states())}

    def _handle_device_call(self, data: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(data.get("device_id") or "").strip()
        action = str(data.get("action") or "").strip()
        arguments = data.get("arguments")
        if not device_id or not action:
            raise ValueError("device.call requires device_id and action")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise TypeError("device.call arguments must be an object")
        result = self.local.call_action(device_id, action, **arguments)
        return {
            "device_id": device_id,
            "action": action,
            "result": to_wire_value(result),
            "state": to_wire_value(self.local.devices[device_id].snapshot_status()),
        }

    def _handle_device_state(self, data: Dict[str, Any]) -> Dict[str, Any]:
        device_id = str(data.get("device_id") or "").strip()
        states = self.local.snapshot_states()
        if not device_id:
            return {"states": to_wire_value(states)}
        if device_id not in states:
            raise KeyError(f"未知 HostLink 设备：{device_id}")
        return {"device_id": device_id, "state": to_wire_value(states[device_id])}

    def call_action(
        self,
        device_id: str,
        action_name: str,
        **kwargs: Any,
    ) -> Any:
        """Call a local device, or route a Host call to an online Slave."""

        device_id = str(device_id)
        if device_id in self.local.devices:
            return self.local.call_action(device_id, action_name, **kwargs)
        if self.server is None:
            raise KeyError(f"未知 HostLink 设备：{device_id}")
        response = self.server.call_device(device_id, action_name, kwargs)
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    def devices(self, online_only: bool = True) -> Dict[str, Dict[str, Any]]:
        result = {
            item["id"]: {
                "device": item,
                "state": to_wire_value(
                    self.local.devices[item["id"]].snapshot_status()
                ),
                "location": "local",
                "online": True,
            }
            for item in self.local.descriptors()
        }
        if self.server is not None:
            for device_id, peer in self.server.devices(online_only).items():
                remote = dict(peer)
                remote["location"] = "remote"
                result.setdefault(device_id, remote)
        return result

    def stop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.close()
            set_hostlink_client(None)
        server, self.server = self.server, None
        if server is not None:
            server.stop()
            set_hostlink_server(None)
        self.local.stop()
        self._started = False


__all__ = ["HostLinkBackendRuntime", "to_wire_value"]
