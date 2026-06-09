from __future__ import annotations

import logging
import time
from typing import Any

from opcua import Client


class SzlabMixerOpcUaClient:
    def __init__(self, url: str, username: str | None = None, password: str | None = None):
        logging.getLogger("opcua").setLevel(logging.WARNING)
        self.url = url
        self.client = Client(url)
        if username and password:
            self.client.set_user(username)
            self.client.set_password(password)
        self.client.connect()
        self._nodes_by_name = self._browse_virtual_mixer_nodes()

    def _browse_virtual_mixer_nodes(self) -> dict[str, Any]:
        objects = self.client.get_objects_node()
        virtual_mixer = None
        for child in objects.get_children():
            if child.get_browse_name().Name == "VirtualMixer":
                virtual_mixer = child
                break
        if virtual_mixer is None:
            raise RuntimeError("OPC UA 中未找到 VirtualMixer 对象")
        return {child.get_browse_name().Name: child for child in virtual_mixer.get_children()}

    def read(self, name: str) -> Any:
        return self._node(name).get_value()

    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        values = {}
        for name in variable_names:
            try:
                node = self._node(name)
                values[name] = {
                    "success": True,
                    "value": node.get_value(),
                    "node_id": str(node.nodeid),
                }
            except Exception as exc:
                values[name] = {"success": False, "error": str(exc)}
        return values

    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        node = self._nodes_by_name.get(variable_name)
        return variable_name, str(node.nodeid) if node is not None else None

    def write(self, name: str, value: Any) -> None:
        self._node(name).set_value(value)

    def pulse(self, name: str, value: Any = True, reset_value: Any = False, reset_delay: float = 0.1) -> None:
        self.write(name, value)
        time.sleep(reset_delay)
        self.write(name, reset_value)

    def wait_equal(self, name: str, expected: Any, timeout: float = 300.0, interval: float = 0.2) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.read(name) == expected:
                return True
            time.sleep(interval)
        return False

    def wait_new_cycle_done(self, name: str, timeout: float = 300.0, interval: float = 0.2) -> bool:
        start = time.time()
        if bool(self.read(name)):
            if not self.wait_equal(name, False, timeout=timeout, interval=interval):
                return False
        elapsed = time.time() - start
        return self.wait_equal(name, True, timeout=max(timeout - elapsed, 0.0), interval=interval)

    def disconnect(self) -> None:
        self.client.disconnect()

    def _node(self, name: str):
        try:
            return self._nodes_by_name[name]
        except KeyError as exc:
            raise KeyError(f"未找到 OPC UA 节点: {name}") from exc
