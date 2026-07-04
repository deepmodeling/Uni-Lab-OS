from __future__ import annotations

import logging
import time
from typing import Any

from opcua import Client, ua


class SzlabS08OpcUaClient:
    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        browse_depth: int = 8,
        browse_limit: int = 5000,
        node_id_map: dict[str, str] | None = None,
        allow_recursive_browse: bool = False,
        object_name: str = "VirtualS08",
    ):
        logging.getLogger("opcua").setLevel(logging.WARNING)
        self.url = url
        self._object_name = object_name
        self._browse_depth = int(browse_depth)
        self._browse_limit = int(browse_limit)
        self._node_id_map = node_id_map or {}
        self._allow_recursive_browse = bool(allow_recursive_browse)
        self.client = Client(url)
        if username and password:
            self.client.set_user(username)
            self.client.set_password(password)
        self.client.connect()
        self._nodes_by_name = self._browse_device_nodes()

    def _browse_device_nodes(self) -> dict[str, Any]:
        if self._node_id_map:
            return {name: self.client.get_node(node_id) for name, node_id in self._node_id_map.items()}

        objects = self.client.get_objects_node()
        virtual_object = None
        top_children = objects.get_children()
        for child in top_children:
            if child.get_browse_name().Name == self._object_name:
                virtual_object = child
                break
        if virtual_object is not None:
            return {child.get_browse_name().Name: child for child in virtual_object.get_children()}

        if not self._allow_recursive_browse:
            top_names = []
            for child in top_children:
                try:
                    top_names.append(f"{child.get_browse_name().Name}({child.nodeid})")
                except Exception:
                    top_names.append(str(child.nodeid))
            raise RuntimeError(
                f"OPC UA 中未找到 {self._object_name} 对象。真机节点树较大，已停止自动递归扫描以避免卡住；"
                "请先用 OPC UA 浏览工具找到 S08 变量 NodeId，并写入 decap_s08_debug.json 的 "
                f"device.opcua_node_id_map。顶层对象: {top_names}"
            )

        nodes = self._browse_nodes_recursively(objects)
        if not nodes:
            raise RuntimeError(
                f"OPC UA 中未找到 {self._object_name} 对象，也没有递归扫描到可用变量节点；"
                "请确认真机 OPC UA 变量是否已发布"
            )
        return nodes

    def _browse_nodes_recursively(self, root: Any) -> dict[str, Any]:
        nodes_by_name: dict[str, Any] = {}
        visited = 0
        stack: list[tuple[Any, int]] = [(root, 0)]

        while stack and visited < self._browse_limit:
            node, depth = stack.pop()
            visited += 1
            try:
                children = node.get_children()
            except Exception:
                continue

            for child in children:
                try:
                    browse_name = child.get_browse_name().Name
                except Exception:
                    browse_name = ""
                try:
                    display_name = child.get_display_name().Text
                except Exception:
                    display_name = ""
                for name in (browse_name, display_name):
                    if name and name not in nodes_by_name:
                        nodes_by_name[name] = child
                if depth < self._browse_depth:
                    stack.append((child, depth + 1))

        logging.getLogger(__name__).info(
            "未找到 %s，已递归扫描 OPC UA 节点: visited=%s indexed=%s",
            self._object_name,
            visited,
            len(nodes_by_name),
        )
        return nodes_by_name

    def read(self, name: str) -> Any:
        return self._node(name).get_value()

    def get_variables(self, variable_names: list[str], use_cache: bool = False) -> dict[str, dict[str, Any]]:
        del use_cache
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

    def check_variable_accessible(self, variable_name: str) -> tuple[bool, str | None]:
        node = self._nodes_by_name.get(variable_name)
        if node is None:
            return False, "配置中未找到该变量"
        try:
            node.get_data_type_as_variant_type()
        except Exception as exc:
            return False, f"{node.nodeid}: {exc}"
        return True, str(node.nodeid)

    def write(self, name: str, value: Any) -> None:
        node = self._node(name)
        variant_type = None
        try:
            variant_type = node.get_data_type_as_variant_type()
            self._write_value_only(node, value, variant_type)
        except ua.UaStatusCodeError as exc:
            raise RuntimeError(f"写入 OPC UA 变量失败: {name} ({node.nodeid}) = {value!r}: {exc}") from exc
        except Exception:
            if variant_type is None:
                self._write_value_only(node, value, None)
            else:
                node.set_value(value, variant_type)

    def _write_value_only(self, node: Any, value: Any, variant_type: Any | None) -> None:
        variant = ua.Variant(value, variant_type) if variant_type is not None else ua.Variant(value)
        data_value = ua.DataValue(variant)
        data_value.StatusCode = None
        data_value.SourceTimestamp = None
        data_value.ServerTimestamp = None
        node.set_attribute(ua.AttributeIds.Value, data_value)

    def wait_equal(self, name: str, expected: Any, timeout: float = 300.0, interval: float = 0.2) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.read(name) == expected:
                return True
            time.sleep(interval)
        return False

    def disconnect(self) -> None:
        self.client.disconnect()

    def _node(self, name: str):
        try:
            return self._nodes_by_name[name]
        except KeyError as exc:
            raise KeyError(f"未找到 OPC UA 节点: {name}") from exc
