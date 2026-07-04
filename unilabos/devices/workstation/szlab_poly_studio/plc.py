import csv
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from opcua import Client, ua

from unilabos.device_comms.opcua_client.node.uniopcua import NodeType, Variable
try:
    from unilabos.devices.workstation.post_process.post_process import BaseClient, OpcUaNode
except ModuleNotFoundError as exc:
    if exc.name != "pylabrobot":
        raise
    BaseClient = object
    OpcUaNode = None
from unilabos.devices.workstation.szlab_poly_studio.stack_status import build_stack_status
from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.utils.log import logger


DEFAULT_CSV_NAME = "szlab_plc_0702.csv"
DEFAULT_STACK_SENSOR_LAYOUT_NAME = "stack_sensor_layout.json"


def wait_variable_equal(
    reader: Any,
    variable_name: str,
    expected: Any,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
) -> bool:
    started_at = time.time()
    start_recorder = getattr(reader, "_record_opc_wait_start", None)
    if callable(start_recorder):
        start_recorder(variable_name, expected, timeout=timeout, interval=interval)

    success = False
    last_value = None
    error = None
    try:
        while time.time() - started_at <= timeout:
            last_value = reader.read_variable(variable_name, use_cache=False)
            if last_value == expected:
                success = True
                return True
            time.sleep(interval)
        return False
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        finish_recorder = getattr(reader, "_record_opc_wait_finish", None)
        if callable(finish_recorder):
            finish_recorder(
                variable_name,
                expected,
                timeout=timeout,
                interval=interval,
                success=success,
                last_value=last_value,
                elapsed=time.time() - started_at,
                error=error,
            )


def wait_variable_true(
    reader: Any,
    variable_name: str,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
) -> bool:
    return wait_variable_equal(reader, variable_name, True, timeout=timeout, interval=interval)


def _resolve_csv_path(csv_path: Optional[str]) -> str:
    if csv_path is None:
        csv_path = DEFAULT_CSV_NAME
    if os.path.isabs(csv_path):
        return csv_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), csv_path)


def _resolve_config_path(config_path: Optional[str]) -> str:
    if config_path is None:
        config_path = DEFAULT_STACK_SENSOR_LAYOUT_NAME
    if os.path.isabs(config_path):
        return config_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), config_path)


def load_stack_sensor_groups_from_json(config_path: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Load stack UI sensor layout: business position -> PLC variable name."""
    resolved_path = _resolve_config_path(config_path)
    with open(resolved_path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    sensor_groups = config.get("sensor_groups", {})
    return {
        str(group_name): {
            str(site_key): str(variable_name)
            for site_key, variable_name in group.items()
        }
        for group_name, group in sensor_groups.items()
    }


def load_variable_definitions_from_csv(csv_path: str) -> tuple[List[str], Dict[str, str]]:
    """Load PLC variable names and optional NodeId mappings from CSV."""
    names: List[str] = []
    node_id_map: Dict[str, str] = {}
    seen = set()
    last_error: Optional[UnicodeDecodeError] = None
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "gb18030", "gbk"):
        for delimiter in (",", "\t"):
            try:
                with open(csv_path, newline="", encoding=encoding) as csv_file:
                    reader = csv.DictReader(csv_file, delimiter=delimiter)
                    fieldnames = reader.fieldnames or []
                    if "变量名" not in fieldnames:
                        names.clear()
                        node_id_map.clear()
                        seen.clear()
                        continue
                    node_id_field = next(
                        (field for field in fieldnames if field.strip().lower() in {"node_id", "nodeid"}),
                        None,
                    )
                    for row in reader:
                        name = (row.get("变量名") or "").strip()
                        node_id = (row.get(node_id_field) or "").strip() if node_id_field else ""
                        if node_id_field and not node_id:
                            continue
                        if not name or name in seen:
                            continue
                        seen.add(name)
                        names.append(name)
                        if node_id:
                            node_id_map[name] = node_id
                return names, node_id_map
            except UnicodeDecodeError as exc:
                names.clear()
                node_id_map.clear()
                seen.clear()
                last_error = exc
                break
    if last_error:
        raise last_error
    return names, node_id_map


def load_variable_names_from_csv(csv_path: str) -> List[str]:
    """Load PLC variable names from the CSV column named '变量名'."""
    names, _node_id_map = load_variable_definitions_from_csv(csv_path)
    return names


def _patch_opcua_token_time_drift_check() -> None:
    """兼容 PLC/OPC UA Server 时间严重漂移导致的 security token 超时。"""
    from opcua.common.connection import SecureConnection

    def _check_sym_header_ignore_prev_token_timeout(self: Any, security_header: Any) -> None:
        assert isinstance(
            security_header,
            ua.SymmetricAlgorithmHeader,
        ), "Expected SymAlgHeader, got: {0}".format(security_header)
        if security_header.TokenId != self.security_token.TokenId:
            if security_header.TokenId != self.next_security_token.TokenId:
                if self._allow_prev_token and security_header.TokenId == self.prev_security_token.TokenId:
                    return
                raise ua.UaError(
                    "Invalid security token id {}, expected {} or {}".format(
                        security_header.TokenId,
                        self.security_token.TokenId,
                        self.next_security_token.TokenId,
                    )
                )
            self.revolve_tokens()
            self.security_policy.make_remote_symmetric_key(self.local_nonce, self.remote_nonce)
            self.prev_security_token = ua.ChannelSecurityToken()
        if self.prev_security_token.TokenId != 0:
            self.security_policy.make_remote_symmetric_key(self.local_nonce, self.remote_nonce)
            self.prev_security_token = ua.ChannelSecurityToken()

    SecureConnection._check_sym_header = _check_sym_header_ignore_prev_token_timeout


@device(
    id="szlab_poly_plc",
    display_name="苏州实验室 PLC",
    category=["custom"],
    description="苏州实验室聚合物工作站 PLC/OPC UA 通讯设备，负责变量读写和传感器状态发布",
)
class SZLabPolyPLCDevice(BaseClient):
    def __init__(
        self,
        url: str,
        csv_path: Optional[str] | bool = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        heartbeat_node: str = "Heart_Beat",
        auto_connect: bool = True,
        opcua_log_level: str = "WARNING",
        opcua_node_id_map: Optional[Dict[str, str]] = None,
        node_id_map: Optional[Dict[str, str]] = None,
        opcua_node_id_prefix: Optional[str] = None,
        fallback_node_id_prefix: Optional[str] = None,
        opcua_object_name: Optional[str] = None,
        opcua_browse_depth: int = 8,
        opcua_browse_limit: int = 5000,
        opcua_allow_recursive_browse: bool = False,
        opcua_timeout: Optional[float] = None,
        stack_sensor_layout_path: Optional[str] = None,
        ignore_opcua_token_time_drift: bool = False,
        *args,
        **kwargs,
    ):
        standalone_opcua_client = csv_path is False
        self._standalone_opcua_client = standalone_opcua_client
        if OpcUaNode is None and not standalone_opcua_client:
            raise ModuleNotFoundError("SZLabPolyPLCDevice 需要可选依赖 pylabrobot，请在 unilab 环境中运行")
        super().__init__()
        self._opc_wait_events: List[Dict[str, Any]] = []
        self._node_registry: Dict[str, Any] = {}
        self._variables_to_find: Dict[str, Dict[str, Any]] = {}
        self._found_node_objects: Dict[str, Any] = {}
        self.url = url
        self.csv_path = None if csv_path is False else _resolve_csv_path(csv_path)
        self.stack_sensor_groups = load_stack_sensor_groups_from_json(stack_sensor_layout_path)
        self.heartbeat_node = heartbeat_node
        self.heartbeat_on = False
        self._heartbeat_timer: Optional[threading.Timer] = None
        self._sensor_read_warning_names: set[str] = set()
        self._fallback_node_id_prefix = fallback_node_id_prefix
        self._opcua_object_name = opcua_object_name
        self._opcua_browse_depth = int(opcua_browse_depth)
        self._opcua_browse_limit = int(opcua_browse_limit)
        self._opcua_allow_recursive_browse = bool(opcua_allow_recursive_browse)

        if self.csv_path is None:
            variable_names: List[str] = []
            csv_node_id_map: Dict[str, str] = {}
        else:
            variable_names, csv_node_id_map = load_variable_definitions_from_csv(self.csv_path)
        explicit_node_id_map = {
            **dict(node_id_map or {}),
            **dict(opcua_node_id_map or {}),
        }
        for name in explicit_node_id_map:
            if name not in variable_names:
                variable_names.append(name)
        nodes = []
        if not self._standalone_opcua_client:
            nodes = [
                OpcUaNode(name=name, node_type=NodeType.VARIABLE, data_type=None)
                for name in variable_names
            ]
        prefix_node_id_map = (
            {name: f"{opcua_node_id_prefix}{name}" for name in variable_names}
            if opcua_node_id_prefix
            else {}
        )
        self._direct_node_id_map = {
            **prefix_node_id_map,
            **csv_node_id_map,
            **explicit_node_id_map,
        }
        if self._standalone_opcua_client:
            self._register_variable_definitions(variable_names)
        else:
            self.register_node_list(nodes)

        logging.getLogger("opcua").setLevel(getattr(logging, opcua_log_level.upper(), logging.WARNING))
        if ignore_opcua_token_time_drift:
            _patch_opcua_token_time_drift_check()
        client = Client(url, timeout=opcua_timeout) if opcua_timeout is not None else Client(url)
        if username and password:
            client.set_user(username)
            client.set_password(password)
        if self._standalone_opcua_client:
            self.client = client
        else:
            self._set_client(client)
        if self._direct_node_id_map:
            self._register_direct_node_ids(nodes)
        if auto_connect:
            self._connect()

    @not_action
    def _connect(self) -> None:
        if not self._direct_node_id_map and not self._opcua_object_name and not self._opcua_allow_recursive_browse:
            return super()._connect()
        logger.info("try to connect client...")
        if not self.client:
            raise ValueError("client is not initialized")
        try:
            self.client.connect()
            logger.info("client connected!")
            if not self._direct_node_id_map:
                self._register_browsed_opcua_nodes()
            else:
                missing = sorted(set(self._variables_to_find) - set(self._node_registry))
                if missing:
                    logger.warning(f"以下节点缺少 NodeId 映射，未执行自动浏览: {', '.join(missing)}")
        except Exception as exc:
            logger.error(f"client connect failed: {exc}")
            raise

    @not_action
    def _register_variable_definitions(self, variable_names: List[str]) -> None:
        for name in variable_names:
            self._variables_to_find.setdefault(
                name,
                {
                    "node_type": NodeType.VARIABLE,
                    "data_type": None,
                    "node_id": self._direct_node_id_map.get(name),
                },
            )

    @not_action
    def _register_direct_node_ids(self, nodes: List[OpcUaNode]) -> None:
        if not self.client:
            raise ValueError("client is not initialized")
        nodes_by_name = {node.name: node for node in nodes}
        for name, node_id in self._direct_node_id_map.items():
            node = nodes_by_name.get(name)
            if node is None and not self._standalone_opcua_client:
                continue
            if node is not None and node.node_type != NodeType.VARIABLE:
                continue
            data_type = node.data_type if node is not None else None
            self._node_registry[name] = Variable(self.client, name, node_id, data_type)
            self._variables_to_find.setdefault(
                name,
                {
                    "node_type": NodeType.VARIABLE,
                    "data_type": data_type,
                    "node_id": node_id,
                },
            )

    @not_action
    def _register_browsed_opcua_nodes(self) -> None:
        for name, opc_node in self._browse_device_nodes().items():
            self._register_variable_node_id(name, str(opc_node.nodeid))

    @not_action
    def _browse_device_nodes(self) -> Dict[str, Any]:
        if not self.client:
            raise ValueError("client is not initialized")
        objects = self.client.get_objects_node()
        top_children = objects.get_children()
        if self._opcua_object_name:
            for child in top_children:
                if child.get_browse_name().Name == self._opcua_object_name:
                    return {node.get_browse_name().Name: node for node in child.get_children()}

        if not self._opcua_allow_recursive_browse:
            top_names = []
            for child in top_children:
                try:
                    top_names.append(f"{child.get_browse_name().Name}({child.nodeid})")
                except Exception:
                    top_names.append(str(child.nodeid))
            object_hint = f"{self._opcua_object_name} 对象" if self._opcua_object_name else "指定对象"
            raise RuntimeError(
                f"OPC UA 中未找到 {object_hint}。真机节点树较大，已停止自动递归扫描以避免卡住；"
                "请先用 OPC UA 浏览工具找到变量 NodeId，并写入设备配置的 opcua_node_id_map。"
                f"顶层对象: {top_names}"
            )

        nodes = self._browse_nodes_recursively(objects)
        if not nodes:
            raise RuntimeError("OPC UA 中没有递归扫描到可用变量节点；请确认变量是否已发布")
        return nodes

    @not_action
    def _browse_nodes_recursively(self, root: Any) -> Dict[str, Any]:
        nodes_by_name: Dict[str, Any] = {}
        visited = 0
        stack: list[tuple[Any, int]] = [(root, 0)]

        while stack and visited < self._opcua_browse_limit:
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
                if depth < self._opcua_browse_depth:
                    stack.append((child, depth + 1))

        logging.getLogger(__name__).info(
            "已递归扫描 OPC UA 节点: object=%s visited=%s indexed=%s",
            self._opcua_object_name,
            visited,
            len(nodes_by_name),
        )
        return nodes_by_name

    @not_action
    def _register_variable_node_id(self, name: str, node_id: str) -> None:
        if not self.client:
            raise ValueError("client is not initialized")
        self._node_registry[name] = Variable(self.client, name, node_id, None)
        self._variables_to_find.setdefault(
            name,
            {
                "node_type": NodeType.VARIABLE,
                "data_type": None,
                "node_id": node_id,
            },
        )

    @not_action
    def use_node(self, node_name: str) -> Any:
        if not self._standalone_opcua_client:
            try:
                return super().use_node(node_name)
            except Exception:
                if not self._fallback_node_id_prefix:
                    raise

        node = self._node_registry.get(node_name)
        if node is not None:
            return node
        if self._fallback_node_id_prefix:
            node_id = f"{self._fallback_node_id_prefix}{node_name}"
            self._register_variable_node_id(node_name, node_id)
            return self._node_registry[node_name]
        raise KeyError(f"未找到 OPC UA 节点: {node_name}")

    @not_action
    def read_variable(self, node_name: str, use_cache: bool = True) -> Any:
        del use_cache  # BaseClient reads directly from the OPC UA node.
        node = self.use_node(node_name)
        value, error = node.read()
        if error:
            if node_name in self._direct_node_id_map:
                direct_node_id = self._direct_node_id_map[node_name]
                raise RuntimeError(f"读取 PLC 变量失败: {node_name}: 直连 NodeId 无效: {direct_node_id}")
            raise RuntimeError(f"读取 PLC 变量失败: {node_name}")
        return value

    @not_action
    def write_variable(self, node_name: str, value: Any) -> bool:
        node = self.use_node(node_name)
        try:
            self._write_value_only(node, value)
        except Exception as exc:
            if self._is_bad_node_id_unknown(exc):
                direct_node_id = self._direct_node_id_map.get(node_name)
                direct_node_detail = f": {direct_node_id}" if direct_node_id else ""
                raise RuntimeError(f"写入 PLC 变量失败: {node_name}: 直连 NodeId 无效{direct_node_detail}") from exc
            raise RuntimeError(f"写入 PLC 变量失败: {node_name}: {exc}") from exc
        return True

    @not_action
    def _is_bad_node_id_unknown(self, exc: Exception) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if "BadNodeIdUnknown" in str(current):
                return True
            current = current.__cause__ or current.__context__
        return False

    @not_action
    def _write_value_only(self, node: Any, value: Any) -> None:
        opc_node = node._get_node()
        variant_type = opc_node.get_data_type_as_variant_type()
        data_value = ua.DataValue()
        data_value.Value = ua.Variant(value, variant_type)
        data_value.StatusCode = None
        data_value.SourceTimestamp = None
        data_value.ServerTimestamp = None
        data_value.SourcePicoseconds = None
        data_value.ServerPicoseconds = None

        write_value = ua.WriteValue()
        write_value.NodeId = opc_node.nodeid
        write_value.AttributeId = ua.AttributeIds.Value
        write_value.Value = data_value

        params = ua.WriteParameters()
        params.NodesToWrite = [write_value]
        results = self.client.uaclient.write(params)
        if results and not results[0].is_good():
            raise RuntimeError(str(results[0]))

    @not_action
    def disconnect(self) -> None:
        self.heartbeat_on = False
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None
        if self.client:
            self.client.disconnect()

    @not_action
    def read(self, node_name: str, use_cache: bool = True) -> Any:
        return self.read_variable(node_name, use_cache=use_cache)

    @not_action
    def write(self, node_name: str, value: Any) -> None:
        self.write_variable(node_name, value)

    @not_action
    def pulse(
        self,
        node_name: str,
        value: Any = True,
        reset_value: Any = False,
        reset_delay: float = 0.1,
    ) -> None:
        self.write(node_name, value)
        time.sleep(reset_delay)
        self.write(node_name, reset_value)

    @not_action
    def wait_equal(
        self,
        node_name: str,
        expected: Any,
        timeout: float = 300.0,
        interval: float = 0.2,
    ) -> bool:
        return self.wait_variable_equal(node_name, expected, timeout=timeout, interval=interval)

    @not_action
    def wait_variable_equal(
        self,
        node_name: str,
        expected: Any,
        timeout: float = 300.0,
        interval: float = 1.0,
    ) -> bool:
        return wait_variable_equal(self, node_name, expected, timeout=timeout, interval=interval)

    @not_action
    def wait_variable_true(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 1.0,
    ) -> bool:
        return wait_variable_true(self, node_name, timeout=timeout, interval=interval)

    @not_action
    def drain_opc_wait_events(self) -> List[Dict[str, Any]]:
        events = list(getattr(self, "_opc_wait_events", []))
        self._opc_wait_events = []
        return events

    @not_action
    def set_opc_wait_event_writer(self, writer: Any | None) -> None:
        self._opc_wait_event_writer = writer

    @not_action
    def _emit_or_store_opc_wait_event(self, event: Dict[str, Any]) -> None:
        writer = getattr(self, "_opc_wait_event_writer", None)
        if callable(writer):
            writer(event)
            return
        self._opc_wait_events.append(event)

    @not_action
    def _opc_wait_variable_detail(self, node_name: str) -> Dict[str, Any]:
        display_name = node_name
        node_id = None
        try:
            display_name, node_id = self.get_opc_variable_metadata(node_name)
        except (KeyError, ValueError):
            pass
        detail = {"display_name": display_name}
        if node_id:
            detail["node_id"] = node_id
            detail["label"] = f"{display_name} ({node_id})"
        else:
            detail["label"] = display_name
        return detail

    @not_action
    def _record_opc_wait_start(
        self,
        node_name: str,
        expected: Any,
        *,
        timeout: float,
        interval: float,
    ) -> None:
        detail = {
            "type": "opc_wait",
            "phase": "start",
            "variable": node_name,
            "expected": expected,
            "timeout": timeout,
            "interval": interval,
        }
        detail.update(self._opc_wait_variable_detail(node_name))
        self._emit_or_store_opc_wait_event(
            {
                "phase": "start",
                "message": f"等待 OPC 变量 {node_name} == {expected} (timeout={timeout}s, interval={interval}s)",
                "detail": detail,
            }
        )

    @not_action
    def _record_opc_wait_finish(
        self,
        node_name: str,
        expected: Any,
        *,
        timeout: float,
        interval: float,
        success: bool,
        last_value: Any,
        elapsed: float,
        error: str | None = None,
    ) -> None:
        detail = {
            "type": "opc_wait",
            "phase": "finish",
            "variable": node_name,
            "expected": expected,
            "timeout": timeout,
            "interval": interval,
            "success": success,
            "last_value": last_value,
            "elapsed": elapsed,
        }
        detail.update(self._opc_wait_variable_detail(node_name))
        if error:
            detail["error"] = error
        message = f"OPC 变量等待完成 {node_name} == {expected}: success={success}, last_value={last_value}"
        if error:
            message = f"{message}, error={error}"
        self._emit_or_store_opc_wait_event({"phase": "finish", "message": message, "detail": detail})

    @not_action
    def wait_new_cycle_done(
        self,
        node_name: str,
        timeout: float = 300.0,
        interval: float = 0.2,
    ) -> bool:
        start = time.time()
        if bool(self.read(node_name)):
            if not self.wait_equal(node_name, False, timeout=timeout, interval=interval):
                return False
        elapsed = time.time() - start
        return self.wait_equal(node_name, True, timeout=max(timeout - elapsed, 0.0), interval=interval)

    @not_action
    def get_opc_variable_metadata(self, node_name: str) -> tuple[str, str | None]:
        try:
            return node_name, self.use_node(node_name).node_id
        except (KeyError, ValueError):
            return node_name, None

    @not_action
    def check_variable_accessible(self, node_name: str) -> tuple[bool, str | None]:
        try:
            node = self.use_node(node_name)
            node._get_node().get_data_type_as_variant_type()
        except Exception as exc:
            return False, str(exc)
        return True, node.node_id

    @not_action
    def get_variables(self, node_names: Optional[List[str]] = None, use_cache: bool = False) -> Dict[str, Any]:
        del use_cache
        names = node_names or list(self._variables_to_find)
        result: Dict[str, Any] = {}
        for name in names:
            try:
                node = self.use_node(name)
                value, error = node.read()
                if error:
                    result[name] = {"success": False, "error": f"读取 PLC 变量失败: {name}"}
                else:
                    result[name] = {
                        "success": True,
                        "value": value,
                        "node_id": node.node_id,
                    }
            except Exception as exc:
                result[name] = {"success": False, "error": str(exc)}
        return result

    @not_action
    def _read_sensor_group(self, sensors: Dict[str, str]) -> Dict[str, Optional[bool]]:
        result: Dict[str, Optional[bool]] = {}
        for site_key, variable_name in sensors.items():
            try:
                result[site_key] = bool(self.read_variable(variable_name))
            except Exception as exc:
                if variable_name not in self._sensor_read_warning_names:
                    logger.warning(f"读取传感器 {variable_name} 失败: {exc}")
                    self._sensor_read_warning_names.add(variable_name)
                else:
                    logger.debug(f"读取传感器 {variable_name} 失败: {exc}")
                result[site_key] = None
        return result

    @not_action
    def _read_stack_sensor_groups(self, group_names: Optional[List[str]] = None) -> Dict[str, Dict[str, Optional[bool]]]:
        selected_groups = group_names or list(self.stack_sensor_groups)
        return {
            group_name: self._read_sensor_group(sensors)
            for group_name, sensors in self.stack_sensor_groups.items()
            if group_name in selected_groups
        }

    @not_action
    def _read_named_sensor_group(self, group_name: str) -> Dict[str, Optional[bool]]:
        sensors = self.stack_sensor_groups.get(group_name)
        if sensors is None:
            raise KeyError(f"stack_sensor_layout.json 缺少传感器分组: {group_name}")
        return self._read_sensor_group(sensors)

    @action(auto_prefix=True, always_free=True, description="启动苏州实验室 PLC 心跳")
    def start_heart_beat(self) -> Dict[str, Any]:
        if self.heartbeat_node not in self._variables_to_find:
            return {
                "success": False,
                "message": f"CSV 中未注册心跳变量 {self.heartbeat_node}",
            }
        if self.heartbeat_on:
            return {"success": True, "message": "心跳已在运行"}
        self.heartbeat_on = True
        self._schedule_heartbeat()
        return {"success": True, "message": "心跳已启动"}

    @action(auto_prefix=True, always_free=True, description="停止苏州实验室 PLC 心跳")
    def stop_heart_beat(self) -> Dict[str, Any]:
        self.heartbeat_on = False
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None
        if self.heartbeat_node in self._variables_to_find:
            try:
                self.write_variable(self.heartbeat_node, False)
            except Exception as exc:
                return {"success": False, "message": str(exc)}
        return {"success": True, "message": "心跳已停止"}

    @not_action
    def _schedule_heartbeat(self) -> None:
        self._heartbeat_timer = threading.Timer(1.0, self._trigger_heart_beat)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    @not_action
    def _trigger_heart_beat(self) -> None:
        if not self.heartbeat_on:
            return
        try:
            current = bool(self.read_variable(self.heartbeat_node))
            self.write_variable(self.heartbeat_node, not current)
        except Exception as exc:
            logger.warning(f"PLC 心跳写入失败: {exc}")
        if self.heartbeat_on:
            self._schedule_heartbeat()

    @action(auto_prefix=True, always_free=True, description="读取指定 PLC 变量")
    def check_variable_status(self, variable_name: str) -> Dict[str, Any]:
        try:
            return {
                "success": True,
                "variable_name": variable_name,
                "value": self.read_variable(variable_name),
            }
        except Exception as exc:
            return {
                "success": False,
                "variable_name": variable_name,
                "error": str(exc),
            }

    @action(auto_prefix=True, always_free=True, description="写入指定 PLC 变量")
    def write_variable_action(self, variable_name: str, value: Any) -> Dict[str, Any]:
        try:
            self.write_variable(variable_name, value)
            return {"success": True, "variable_name": variable_name, "value": value}
        except Exception as exc:
            return {"success": False, "variable_name": variable_name, "error": str(exc)}

    @action(auto_prefix=True, always_free=True, description="读取指定传感器分组")
    def get_sensor_group_status(self, group_name: str) -> Dict[str, Any]:
        sensors = self.stack_sensor_groups.get(group_name)
        if sensors is None:
            return {
                "success": False,
                "group_name": group_name,
                "available_groups": sorted(self.stack_sensor_groups),
            }
        return {
            "success": True,
            "group_name": group_name,
            "status": self._read_sensor_group(sensors),
        }

    @action(auto_prefix=True, always_free=True, description="读取前端堆栈 JSON 状态")
    def get_stack_status(self, group_names: Optional[List[str]] = None) -> Dict[str, Any]:
        return build_stack_status(self._read_stack_sensor_groups(group_names=group_names))

    @action(auto_prefix=True, always_free=True, description="写入 S01 上料过渡仓取料编号和入料产品")
    def set_s1_loading_request(self, pick_index: int, product_type: int) -> Dict[str, Any]:
        try:
            self.write_variable("S01取料编号", int(pick_index))
            self.write_variable("S01入料产品", int(product_type))
            return {
                "success": True,
                "pick_index": pick_index,
                "product_type": product_type,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @topic_config(period=1.0)
    def s2_tip_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s2_tip")

    @topic_config(period=1.0)
    def s3_unused_beaker_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s3_unused_beaker")

    @topic_config(period=1.0)
    def s3_unused_sample_vial_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s3_unused_sample_vial")

    @topic_config(period=1.0)
    def s10_liquid_reagent_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s10_liquid_reagent")

    @topic_config(period=1.0)
    def s11_used_beaker_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s11_used_beaker")

    @topic_config(period=1.0)
    def s11_used_sample_vial_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("s11_used_sample_vial")

    @topic_config(period=1.0)
    def powder_container_occupied(self) -> Dict[str, Optional[bool]]:
        return self._read_named_sensor_group("powder_container")

    @topic_config(period=5.0)
    def registered_variable_count(self) -> int:
        return len(self._variables_to_find)

    @topic_config(period=5.0)
    def registered_variables(self) -> List[str]:
        return sorted(self._variables_to_find)

    @topic_config(period=1.0)
    def stack_status(self) -> Dict[str, Any]:
        return self.get_stack_status()
