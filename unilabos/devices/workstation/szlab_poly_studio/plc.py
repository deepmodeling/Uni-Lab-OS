import csv
import io
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from opcua import Client, ua

from unilabos.device_comms.opcua_client.node.uniopcua import NodeType, Variable
try:
    from unilabos.devices.workstation.post_process.post_process import BaseClient, OpcUaNode
except ModuleNotFoundError as exc:
    if exc.name != "pylabrobot":
        raise

    class BaseClient:  # type: ignore[no-redef]
        """无 pylabrobot 时为 standalone OPC 客户端保留最小基类契约。"""

        def __init__(self) -> None:
            self._name_mapping: Dict[str, str] = {}
            self._reverse_mapping: Dict[str, str] = {}

        def use_node(self, _node_name: str) -> Any:
            raise RuntimeError("当前环境缺少 pylabrobot，不能使用 BaseClient 节点")

        def _connect(self) -> None:
            raise RuntimeError("当前环境缺少 pylabrobot，不能连接 BaseClient")

    OpcUaNode = None
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    SENSOR_ARRAY_COUNT,
    SENSOR_BITS_PER_ARRAY,
    SensorBase,
    load_sensor_bit_metadata_from_csv,
    load_stack_sensor_groups_from_json,
    read_plc_csv_text,
    wait_sensor_conditions,
    wait_variable_equal,
    wait_variable_true,
)
from unilabos.devices.workstation.szlab_poly_studio.error_codes import (
    PLC_ALARM_MAP,
    read_active_plc_alarms,
)
from unilabos.devices.workstation.szlab_poly_studio.stack_status import build_stack_status
from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.utils.log import logger


DEFAULT_CSV_NAME = "szlab_plc_0721.csv"


def _resolve_csv_path(csv_path: Optional[str]) -> str:
    if csv_path is None:
        csv_path = DEFAULT_CSV_NAME
    if os.path.isabs(csv_path):
        return csv_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), csv_path)


def load_variable_definitions_from_csv(csv_path: str) -> tuple[List[str], Dict[str, str]]:
    """Load PLC variable names and optional NodeId mappings from CSV."""
    text = read_plc_csv_text(csv_path)
    for delimiter in (",", "\t"):
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        fieldnames = reader.fieldnames or []
        if "变量名" not in fieldnames:
            continue
        names: List[str] = []
        node_id_map: Dict[str, str] = {}
        seen = set()
        node_id_field = next(
            (
                field
                for field in fieldnames
                if field.strip().lower() in {"node_id", "nodeid"}
            ),
            None,
        )
        for row in reader:
            name = (row.get("变量名") or "").strip()
            node_id = (
                (row.get(node_id_field) or "").strip()
                if node_id_field
                else ""
            )
            if node_id_field and not node_id:
                continue
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if node_id:
                node_id_map[name] = node_id
        return names, node_id_map
    return [], {}


def load_variable_aliases_from_csv(csv_path: str) -> Dict[str, str]:
    """加载 ``EnglishName -> CSV Name`` 映射；CSV Name 始终是 PLC 真实节点名。"""
    text = read_plc_csv_text(csv_path)
    for delimiter in (",", "\t"):
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        fieldnames = reader.fieldnames or []
        canonical_field = (
            "变量名" if "变量名" in fieldnames
            else "Name" if "Name" in fieldnames
            else None
        )
        if canonical_field is None or "EnglishName" not in fieldnames:
            continue
        aliases: Dict[str, str] = {}
        for row in reader:
            canonical = (row.get(canonical_field) or "").strip()
            alias = (row.get("EnglishName") or "").strip()
            if canonical and alias and alias != canonical:
                aliases[alias] = canonical
        return aliases
    return {}


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
    class _SensorArraySubscriptionHandler:
        def __init__(self, device: "SZLabPolyPLCDevice") -> None:
            self._device = device

        def datachange_notification(self, node: Any, value: Any, data: Any) -> None:
            del data
            self._device._on_sensor_array_datachange(node, value)

        def event_notification(self, event: Any) -> None:
            del event

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
        opcua_session_timeout_ms: float = 8 * 60 * 60 * 1000,
        opcua_secure_channel_timeout_ms: float = 60 * 60 * 1000,
        auto_reconnect: bool = True,
        reconnect_attempts: int = 3,
        reconnect_interval: float = 1.0,
        mixing_alarm_poll_interval: float = 0.5,
        stack_sensor_layout_path: Optional[str] = None,
        ignore_opcua_token_time_drift: bool = False,
        *args,
        **kwargs,
    ):
        standalone_opcua_client = csv_path is False
        self._standalone_opcua_client = standalone_opcua_client
        self._opc_io_lock = threading.RLock()
        if OpcUaNode is None and not standalone_opcua_client:
            raise ModuleNotFoundError("SZLabPolyPLCDevice 需要可选依赖 pylabrobot，请在 unilab 环境中运行")
        super().__init__()
        self._opc_wait_tls = threading.local()
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
        self._sensor_array_subscription: Any = None
        self._sensor_array_subscription_handles: List[Any] = []
        self._sensor_array_node_indexes: Dict[str, int] = {}
        self._sensor_array_subscription_values: Dict[int, List[bool]] = {}
        self._sensor_change_callbacks: List[Callable[[int, List[bool]], None]] = []
        self._sensor_array_subscription_interval_ms = 200
        self._sensor_subscription_lock = threading.RLock()
        self._reconnect_lock = threading.RLock()
        self._session_generation = 0
        self._auto_reconnect = bool(auto_reconnect)
        self._reconnect_attempts = max(int(reconnect_attempts), 1)
        self._reconnect_interval = max(float(reconnect_interval), 0.0)
        self.mixing_alarm_poll_interval = max(float(mixing_alarm_poll_interval), 0.1)
        self._fallback_node_id_prefix = fallback_node_id_prefix
        self._opcua_object_name = opcua_object_name
        self._opcua_browse_depth = int(opcua_browse_depth)
        self._opcua_browse_limit = int(opcua_browse_limit)
        self._opcua_allow_recursive_browse = bool(opcua_allow_recursive_browse)

        if self.csv_path is None:
            variable_names: List[str] = []
            csv_node_id_map: Dict[str, str] = {}
            self._sensor_bit_metadata: Dict[str, Dict[str, str]] = {}
        else:
            variable_names, csv_node_id_map = load_variable_definitions_from_csv(self.csv_path)
            self._sensor_bit_metadata = load_sensor_bit_metadata_from_csv(self.csv_path)
        csv_name_mapping = (
            load_variable_aliases_from_csv(self.csv_path)
            if self.csv_path is not None
            else {}
        )
        self._name_mapping.update(csv_name_mapping)
        self._reverse_mapping.update(
            {canonical: alias for alias, canonical in csv_name_mapping.items()}
        )
        explicit_node_id_map = {
            **dict(node_id_map or {}),
            **dict(opcua_node_id_map or {}),
        }
        # 报警位来自 PLC 导出的《报错信息.csv》。将符号变量加入 OPC UA
        # 发现列表，确保未单独配置 NodeId 时仍可通过浏览找到这些报警节点。
        for alarm in PLC_ALARM_MAP.values():
            parent_name = alarm.variable_name.split(".NO[", 1)[0]
            root_name = parent_name.split("[", 1)[0]
            register_name = alarm.address.split(".", 1)[0]
            for alarm_name in (
                alarm.variable_name,
                parent_name,
                root_name,
                register_name,
            ):
                if alarm_name not in variable_names:
                    variable_names.append(alarm_name)
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
        client.session_timeout = int(opcua_session_timeout_ms)
        client.secure_channel_timeout = int(opcua_secure_channel_timeout_ms)
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
        with self._opc_io_lock:
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
                logger.error(
                    "client connect failed: %r",
                    exc,
                    exc_info=True,
                )
                raise

    @not_action
    def _is_recoverable_connection_error(self, exc: BaseException) -> bool:
        markers = (
            "BadSessionIdInvalid",
            "BadSessionClosed",
            "BadSecureChannelIdInvalid",
            "BadSecureChannelClosed",
            "BadConnectionClosed",
            "BadServerNotConnected",
            "BadCommunicationError",
            "BadNoCommunication",
            "BadRequestTimeout",
            "BadTimeout",
            "BadShutdown",
            "The session id is not valid",
            "The secure channel id is not valid",
            "Connection is closed",
            "Not connected",
            "Socket is closed",
            "Bad file descriptor",
            "CancelledError",
            "Broken pipe",
            "Connection reset",
            "Connection aborted",
            "'NoneType' object has no attribute 'write'",
            "EOFError",
            "TimeoutError",
            "timed out",
        )
        current: BaseException | None = exc
        while current is not None:
            text = f"{type(current).__name__}: {current}"
            if any(marker in text for marker in markers):
                return True
            current = current.__cause__ or current.__context__
        return False

    @not_action
    def _drop_sensor_array_subscription(self, *, clear_callbacks: bool, delete: bool = True) -> None:
        with self._sensor_subscription_lock:
            subscription = self._sensor_array_subscription
            self._sensor_array_subscription = None
            self._sensor_array_subscription_handles = []
            self._sensor_array_node_indexes = {}
            self._sensor_array_subscription_values = {}
            if clear_callbacks:
                self._sensor_change_callbacks = []
        if delete and subscription is not None:
            try:
                subscription.delete()
            except Exception as exc:
                logger.warning(f"删除 PLC 传感器订阅失败: {exc}")

    @not_action
    def _reconnect_after_failure(
        self,
        observed_generation: int,
        reason: BaseException,
        *,
        force: bool = False,
    ) -> None:
        if not self._auto_reconnect and not force:
            raise RuntimeError(f"OPC UA 会话已失效，自动重连未启用: {reason}") from reason
        with self._reconnect_lock:
            if observed_generation != self._session_generation:
                return
            callbacks = list(self._sensor_change_callbacks)
            subscription_was_active = self._sensor_array_subscription is not None
            interval_ms = self._sensor_array_subscription_interval_ms
            self._drop_sensor_array_subscription(clear_callbacks=False, delete=False)
            last_error: BaseException = reason
            for attempt in range(1, self._reconnect_attempts + 1):
                try:
                    if self.client is None:
                        raise RuntimeError("PLC OPC UA 客户端尚未初始化")
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    self.client.connect()
                    if subscription_was_active and callbacks:
                        self.start_sensor_array_subscription(callbacks[0], interval_ms=interval_ms)
                    self._session_generation += 1
                    logger.info(
                        "PLC OPC UA 会话重连成功: generation=%s attempt=%s",
                        self._session_generation,
                        attempt,
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "PLC OPC UA 会话重连失败 (%s/%s): %s",
                        attempt,
                        self._reconnect_attempts,
                        exc,
                    )
                    if attempt < self._reconnect_attempts and self._reconnect_interval > 0:
                        time.sleep(self._reconnect_interval)
            raise RuntimeError(f"OPC UA 会话失效且重连失败: {last_error}") from last_error

    @action(auto_prefix=True, always_free=True, description="重新建立 PLC OPC UA 会话")
    def reconnect(self) -> Dict[str, Any]:
        try:
            self._reconnect_after_failure(
                self._session_generation,
                RuntimeError("用户请求手动重连"),
                force=True,
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": "PLC OPC UA 会话重连成功",
            "session_generation": self._session_generation,
        }

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
        with self._opc_io_lock:
            for name, opc_node in self._browse_device_nodes().items():
                self._register_variable_node_id(name, str(opc_node.nodeid))

    @not_action
    def _browse_device_nodes(self) -> Dict[str, Any]:
        with self._opc_io_lock:
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
        with self._opc_io_lock:
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
        with self._opc_io_lock:
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
        observed_generation = getattr(self, "_session_generation", 0)
        try:
            return self._read_variable_once(node_name)
        except Exception as exc:
            if not self._is_recoverable_connection_error(exc):
                raise
            self._reconnect_after_failure(observed_generation, exc)
            return self._read_variable_once(node_name)

    @not_action
    def _read_variable_once(self, node_name: str) -> Any:
        node = self.use_node(node_name)
        # OPC UA 客户端会按请求 ID 匹配并发响应；读取不再使用设备级全局锁，
        # 以便传感器条件的一轮检查可以真正同时发出多个读取请求。
        value, error = node.read()
        if error:
            sensor_bit = self._parse_sensor_bit_name(node_name)
            if sensor_bit is not None:
                if node_name not in self._sensor_read_warning_names:
                    self._sensor_read_warning_names.add(node_name)
                    logger.warning(
                        f"读取 PLC 传感器标量失败，回退到数组读取: "
                        f"variable={node_name}, error={error}"
                    )
                return self._read_sensor_array(sensor_bit[0])[sensor_bit[1]]
            if node_name in self._direct_node_id_map:
                direct_node_id = self._direct_node_id_map[node_name]
                detail = getattr(node, "_last_read_error", None) or (
                    f"OPC read 失败（{type(node).__name__}，未记录底层异常）"
                )
                raise RuntimeError(
                    f"读取 PLC 变量失败: {node_name}: NodeId={direct_node_id}: {detail} "
                    f"(endpoint={self.url})"
                )
            raise RuntimeError(f"读取 PLC 变量失败: {node_name}")
        return value

    @not_action
    def _parse_sensor_bit_name(self, variable_name: str) -> Optional[tuple[int, int]]:
        return SensorBase.parse_bit_name(variable_name)

    @not_action
    def _read_sensor_array(self, group_index: int) -> List[bool]:
        variable_name = SensorBase.array(group_index)
        node = self.use_node(variable_name)
        value, error = node.read()
        if error:
            raise RuntimeError(f"读取 PLC 传感器数组失败: {variable_name}")
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"PLC 传感器数组类型错误: {variable_name}: {type(value).__name__}")
        if len(value) < SENSOR_BITS_PER_ARRAY:
            raise ValueError(
                f"PLC 传感器数组长度不足: {variable_name}: "
                f"{len(value)} < {SENSOR_BITS_PER_ARRAY}"
            )
        return [bool(item) for item in value[:SENSOR_BITS_PER_ARRAY]]

    @not_action
    def start_sensor_array_subscription(
        self,
        callback: Callable[[int, List[bool]], None],
        interval_ms: int = 200,
    ) -> None:
        """只订阅 10 个传感器数组，并在数组内容变化时通知前端。"""
        with self._opc_io_lock:
            with self._sensor_subscription_lock:
                if callback not in self._sensor_change_callbacks:
                    self._sensor_change_callbacks.append(callback)
                self._sensor_array_subscription_interval_ms = int(interval_ms)
                if self._sensor_array_subscription is not None:
                    return
                if not self.client:
                    raise RuntimeError("PLC OPC UA 客户端尚未连接")

            subscription: Any = None
            handles: List[Any] = []
            node_indexes: Dict[str, int] = {}
            try:
                subscription = self.client.create_subscription(
                    interval_ms,
                    self._SensorArraySubscriptionHandler(self),
                )
                for group_index in range(SENSOR_ARRAY_COUNT):
                    variable_name = SensorBase.array(group_index)
                    node_id = self._direct_node_id_map.get(variable_name)
                    opc_node = self.client.get_node(node_id) if node_id else self.use_node(variable_name)._get_node()
                    node_indexes[str(opc_node.nodeid)] = group_index
                    handles.append(subscription.subscribe_data_change(opc_node))
            except Exception:
                if subscription is not None:
                    subscription.delete()
                with self._sensor_subscription_lock:
                    self._sensor_array_subscription = None
                    self._sensor_array_subscription_handles = []
                    self._sensor_array_node_indexes = {}
                raise

            with self._sensor_subscription_lock:
                self._sensor_array_subscription = subscription
                self._sensor_array_subscription_handles = handles
                self._sensor_array_node_indexes = node_indexes

    @not_action
    def _on_sensor_array_datachange(self, node: Any, value: Any) -> None:
        node_id = str(node.nodeid)
        with self._sensor_subscription_lock:
            group_index = self._sensor_array_node_indexes.get(node_id)
            if group_index is None or not isinstance(value, (list, tuple)):
                return
            values = [bool(item) for item in value[:SENSOR_BITS_PER_ARRAY]]
            if len(values) < SENSOR_BITS_PER_ARRAY:
                return
            if self._sensor_array_subscription_values.get(group_index) == values:
                return
            self._sensor_array_subscription_values[group_index] = values
            callbacks = list(self._sensor_change_callbacks)

        for callback in callbacks:
            try:
                callback(group_index, values)
            except Exception as exc:
                logger.warning(f"处理 PLC 传感器变化通知失败: {exc}")

    @not_action
    def stop_sensor_array_subscription(self) -> None:
        self._drop_sensor_array_subscription(clear_callbacks=True)
        with self._opc_io_lock:
            with self._sensor_subscription_lock:
                subscription = self._sensor_array_subscription
                self._sensor_array_subscription = None
                self._sensor_array_subscription_handles = []
                self._sensor_array_node_indexes = {}
                self._sensor_array_subscription_values = {}
                self._sensor_change_callbacks = []
            if subscription is not None:
                try:
                    subscription.delete()
                except Exception as exc:
                    logger.warning(f"删除 PLC 传感器订阅失败: {exc}")

    @not_action
    def write_variable(self, node_name: str, value: Any) -> bool:
        node = self.use_node(node_name)
        observed_generation = getattr(self, "_session_generation", 0)
        try:
            with self._opc_io_lock:
                self._write_value_only(node, value)
        except Exception as exc:
            if self._is_recoverable_connection_error(exc):
                try:
                    self._reconnect_after_failure(observed_generation, exc)
                except Exception as reconnect_exc:
                    raise RuntimeError(f"写入 PLC 变量失败: {node_name}: {reconnect_exc}") from exc
                raise RuntimeError(
                    f"写入 PLC 变量失败: {node_name}: OPC UA 会话已恢复；"
                    "为避免重复写入，本次写操作未自动重试"
                ) from exc
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
        with self._opc_io_lock:
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
        with self._opc_io_lock:
            self.heartbeat_on = False
            if self._heartbeat_timer:
                self._heartbeat_timer.cancel()
                self._heartbeat_timer = None
            self.stop_sensor_array_subscription()
            if self.client:
                self.client.disconnect()

    @not_action
    def read(self, node_name: str, use_cache: bool = True) -> Any:
        return self.read_variable(node_name, use_cache=use_cache)

    @not_action
    def read_mixing_alarms(self, stations: set[str] | None = None) -> List[Dict[str, Any]]:
        """读取当前为 ON 的已知 Mixing PLC 报警位。"""
        return [
            {
                "code": alarm.code,
                "error_code": alarm.code,
                "station": alarm.station,
                "title": alarm.title,
                "plc_address": alarm.address,
                "plc_variable": alarm.variable_name,
                "recovery": alarm.recovery,
            }
            for alarm in read_active_plc_alarms(self, stations=stations)
        ]

    @not_action
    def clear_last_wait_alarm(self) -> None:
        """清除当前执行线程上一次中止 PLC 等待的报警。"""
        self._opc_wait_tls.last_wait_alarm = None
        self._opc_wait_tls.last_alarm_poll_at = 0.0

    @not_action
    def get_last_wait_alarm(self, *, clear: bool = False) -> Dict[str, Any] | None:
        alarm = getattr(self._opc_wait_tls, "last_wait_alarm", None)
        if clear:
            self._opc_wait_tls.last_wait_alarm = None
        return dict(alarm) if isinstance(alarm, dict) else None

    @not_action
    def _mixing_wait_should_abort(self) -> bool:
        """等待循环中发现任一 Mixing PLC 报警时立即要求调用方结束等待。"""
        now = time.monotonic()
        last_poll_at = float(getattr(self._opc_wait_tls, "last_alarm_poll_at", 0.0))
        if now - last_poll_at < self.mixing_alarm_poll_interval:
            return bool(getattr(self._opc_wait_tls, "last_wait_alarm", None))
        self._opc_wait_tls.last_alarm_poll_at = now
        alarms = self.read_mixing_alarms()
        if not alarms:
            return False
        self._opc_wait_tls.last_wait_alarm = alarms[0]
        return True

    @action(description="读取当前为 ON 的 Mixing PLC 报警位及对应 UniLab 报错码")
    def get_active_mixing_alarms(self) -> Dict[str, Any]:
        alarms = self.read_mixing_alarms()
        return {
            "success": True,
            "alarm_count": len(alarms),
            "has_alarm": bool(alarms),
            "alarms": alarms,
        }

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
        interval: float = 0.2,
        timeout: float | None = None,
    ) -> bool:
        return self.wait_variable_equal(
            node_name, expected, interval=interval, timeout=timeout
        )

    @not_action
    def wait_variable_equal(
        self,
        node_name: str,
        expected: Any,
        interval: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        return wait_variable_equal(
            self, node_name, expected, interval=interval, timeout=timeout
        )

    @not_action
    def wait_variable_true(
        self,
        node_name: str,
        interval: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        return wait_variable_true(
            self, node_name, interval=interval, timeout=timeout
        )

    @not_action
    def wait_sensor_conditions(
        self,
        conditions: Dict[str, bool],
        interval: float = 0.2,
        context: str | None = None,
        timeout: float | None = None,
    ) -> tuple[bool, Dict[str, Any]]:
        return wait_sensor_conditions(
            self,
            conditions,
            interval=interval,
            context=context,
            timeout=timeout,
        )

    @not_action
    def _opc_wait_thread_state(self) -> Any:
        tls = getattr(self, "_opc_wait_tls", None)
        if tls is None:
            tls = threading.local()
            self._opc_wait_tls = tls
        return tls

    @not_action
    def drain_opc_wait_events(self) -> List[Dict[str, Any]]:
        """仅回收当前线程缓存的 OPC 等待事件，避免并行 Action 串日志。"""
        state = self._opc_wait_thread_state()
        events = list(getattr(state, "events", None) or [])
        state.events = []
        return events

    @not_action
    def set_opc_wait_event_writer(self, writer: Any | None) -> None:
        """绑定当前线程的 OPC 等待日志回调；并行 Task 动作互不覆盖。"""
        state = self._opc_wait_thread_state()
        if writer is None:
            if hasattr(state, "writer"):
                del state.writer
            return
        state.writer = writer

    @not_action
    def _emit_or_store_opc_wait_event(self, event: Dict[str, Any]) -> None:
        state = self._opc_wait_thread_state()
        writer = getattr(state, "writer", None)
        if callable(writer):
            writer(event)
            return
        pending = getattr(state, "events", None)
        if pending is None:
            pending = []
            state.events = pending
        pending.append(event)

    @not_action
    def _opc_wait_variable_detail(self, node_name: str) -> Dict[str, Any]:
        display_name = node_name
        node_id = None
        sensor_metadata = getattr(self, "_sensor_bit_metadata", {}).get(node_name, {})
        sensor_label = sensor_metadata.get("label")
        try:
            display_name, node_id = self.get_opc_variable_metadata(node_name)
        except (KeyError, ValueError):
            pass
        if sensor_label:
            display_name = sensor_label
        detail = {"display_name": display_name}
        if node_id:
            detail["node_id"] = node_id
            detail["label"] = f"{display_name} ({node_id})"
        else:
            detail["label"] = display_name
        return detail

    @not_action
    def _opc_sensor_condition_details(
        self,
        conditions: Dict[str, bool],
        values: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        details = []
        for variable, expected in conditions.items():
            item = {
                "variable": variable,
                "expected": expected,
                "actual": values.get(variable),
                "satisfied": values.get(variable) == expected,
            }
            item.update(self._opc_wait_variable_detail(variable))
            details.append(item)
        return details

    @staticmethod
    def _opc_sensor_wait_target_text(items: List[Dict[str, Any]]) -> str:
        return "；".join(
            f"{item['display_name']} [{item['variable']}]={item['expected']}（当前 {item['actual']}）"
            for item in items
        )

    @not_action
    def _record_opc_sensor_wait_start(
        self,
        conditions: Dict[str, bool],
        values: Dict[str, Any],
        *,
        interval: float,
        context: str | None,
    ) -> None:
        items = self._opc_sensor_condition_details(conditions, values)
        unmet = [item for item in items if not item["satisfied"]]
        satisfied_count = len(items) - len(unmet)
        label = context or "传感器条件"
        if unmet:
            message = (
                f"{label}：已满足 {satisfied_count}/{len(items)}；"
                f"仍等待 {self._opc_sensor_wait_target_text(unmet)}"
            )
        else:
            message = f"{label}：{len(items)}/{len(items)} 已满足，无需继续等待"
        detail = {
            "type": "opc_wait",
            "wait_kind": "sensor_conditions",
            "phase": "start",
            "context": context,
            "conditions": items,
            "satisfied_count": satisfied_count,
            "total_count": len(items),
            "interval": interval,
        }
        self._emit_or_store_opc_wait_event({"phase": "start", "message": message, "detail": detail})

    @not_action
    def _record_opc_sensor_wait_change(
        self,
        conditions: Dict[str, bool],
        previous_values: Dict[str, Any],
        values: Dict[str, Any],
        *,
        context: str | None,
    ) -> None:
        items = self._opc_sensor_condition_details(conditions, values)
        changed = [item for item in items if previous_values.get(item["variable"]) != item["actual"]]
        unmet = [item for item in items if not item["satisfied"]]
        changes_text = "；".join(
            f"{item['display_name']} {previous_values.get(item['variable'])} → {item['actual']}" for item in changed
        )
        if unmet:
            waiting_text = f"；仍等待 {self._opc_sensor_wait_target_text(unmet)}"
        else:
            waiting_text = ""
        label = context or "传感器条件"
        message = f"{label}状态变化：{changes_text}；已满足 {len(items) - len(unmet)}/{len(items)}{waiting_text}"
        detail = {
            "type": "opc_wait",
            "wait_kind": "sensor_conditions",
            "phase": "change",
            "context": context,
            "conditions": items,
            "changes": changed,
            "satisfied_count": len(items) - len(unmet),
            "total_count": len(items),
        }
        self._emit_or_store_opc_wait_event({"phase": "change", "message": message, "detail": detail})

    @not_action
    def _record_opc_sensor_wait_finish(
        self,
        conditions: Dict[str, bool],
        values: Dict[str, Any],
        *,
        interval: float,
        success: bool,
        elapsed: float,
        context: str | None,
        error: str | None = None,
    ) -> None:
        items = self._opc_sensor_condition_details(conditions, values)
        unmet = [item for item in items if not item["satisfied"]]
        label = context or "传感器条件"
        if success:
            message = f"{label}完成：{len(items)}/{len(items)} 已满足，耗时 {elapsed:.1f}s"
        elif error:
            message = f"{label}读取失败：{error}；最终仍等待 {self._opc_sensor_wait_target_text(unmet)}"
        else:
            message = f"{label}等待结束：最终仍等待 {self._opc_sensor_wait_target_text(unmet)}"
        detail = {
            "type": "opc_wait",
            "wait_kind": "sensor_conditions",
            "phase": "finish",
            "context": context,
            "conditions": items,
            "success": success,
            "satisfied_count": len(items) - len(unmet),
            "total_count": len(items),
            "interval": interval,
            "elapsed": elapsed,
        }
        if error:
            detail["error"] = error
        self._emit_or_store_opc_wait_event({"phase": "finish", "message": message, "detail": detail})

    @not_action
    def _record_opc_wait_start(
        self,
        node_name: str,
        expected: Any,
        *,
        interval: float,
    ) -> None:
        detail = {
            "type": "opc_wait",
            "phase": "start",
            "variable": node_name,
            "expected": expected,
            "interval": interval,
        }
        detail.update(self._opc_wait_variable_detail(node_name))
        self._emit_or_store_opc_wait_event(
            {
                "phase": "start",
                "message": f"等待 OPC 变量 {node_name} == {expected} (interval={interval}s)",
                "detail": detail,
            }
        )

    @not_action
    def _record_opc_wait_change(
        self,
        node_name: str,
        expected: Any,
        previous_value: Any,
        current_value: Any,
        *,
        timeout: float,
        interval: float,
    ) -> None:
        detail = {
            "type": "opc_wait",
            "phase": "change",
            "variable": node_name,
            "expected": expected,
            "previous_value": previous_value,
            "last_value": current_value,
            "timeout": timeout,
            "interval": interval,
        }
        detail.update(self._opc_wait_variable_detail(node_name))
        message = (
            f"OPC 变量变化 {node_name}: {previous_value} → {current_value} "
            f"(期望 {expected})"
        )
        self._emit_or_store_opc_wait_event(
            {"phase": "change", "message": message, "detail": detail}
        )

    @not_action
    def _record_opc_wait_finish(
        self,
        node_name: str,
        expected: Any,
        *,
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
        interval: float = 0.2,
        timeout: float | None = None,
    ) -> bool:
        started_at = time.monotonic()
        if bool(self.read(node_name)):
            if not self.wait_equal(
                node_name, False, interval=interval, timeout=timeout
            ):
                return False
        remaining = None
        if timeout is not None:
            remaining = max(0.0, timeout - (time.monotonic() - started_at))
        return self.wait_equal(
            node_name, True, interval=interval, timeout=remaining
        )

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
            with self._opc_io_lock:
                node._get_node().get_data_type_as_variant_type()
        except Exception as exc:
            return False, str(exc)
        return True, node.node_id

    @not_action
    def get_variables(self, node_names: Optional[List[str]] = None, use_cache: bool = False) -> Dict[str, Any]:
        del use_cache
        names = node_names or list(self._variables_to_find)
        result: Dict[str, Any] = {}
        connection_error: Optional[str] = None
        for name in names:
            if connection_error is not None:
                result[name] = {"success": False, "error": connection_error}
                continue
            try:
                value = self.read_variable(name, use_cache=False)
                result[name] = {
                    "success": True,
                    "value": value,
                    "node_id": self.use_node(name).node_id,
                }
            except Exception as exc:
                result[name] = {"success": False, "error": str(exc)}
                if self._is_recoverable_connection_error(exc):
                    connection_error = str(exc)
        return result

    @not_action
    def _read_sensor_group(self, sensors: Dict[str, str]) -> Dict[str, Optional[bool]]:
        result: Dict[str, Optional[bool]] = {}
        array_cache: Dict[int, Optional[List[bool]]] = {}
        for site_key, variable_name in sensors.items():
            try:
                sensor_bit = self._parse_sensor_bit_name(variable_name)
                if sensor_bit is None:
                    result[site_key] = bool(self.read_variable(variable_name))
                    continue
                group_index, bit_index = sensor_bit
                if group_index not in array_cache:
                    try:
                        array_cache[group_index] = self._read_sensor_array(group_index)
                    except Exception:
                        array_cache[group_index] = None
                array_value = array_cache[group_index]
                if array_value is not None:
                    result[site_key] = array_value[bit_index]
                    continue
                node = self.use_node(variable_name)
                with self._opc_io_lock:
                    value, error = node.read()
                if error:
                    raise RuntimeError(f"读取 PLC 传感器位失败: {variable_name}")
                result[site_key] = bool(self.read_variable(variable_name, use_cache=False))
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
        selected_groups = list(self.stack_sensor_groups) if group_names is None else group_names
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

    @action(auto_prefix=True, always_free=True, description="读取全部实机传感器数组")
    def get_sensor_arrays(self) -> Dict[str, Any]:
        groups: List[Dict[str, Any]] = []
        successful_groups = 0
        connection_error: Optional[str] = None
        for group_index in range(SENSOR_ARRAY_COUNT):
            array_name = SensorBase.array(group_index)
            error: Optional[str] = None
            if connection_error is not None:
                values = [None] * SENSOR_BITS_PER_ARRAY
                error = connection_error
            else:
                try:
                    values = self._read_sensor_array(group_index)
                    successful_groups += 1
                except Exception as exc:
                    error = str(exc)
                    values = []
                    if self._is_recoverable_connection_error(exc):
                        connection_error = error
                        values = [None] * SENSOR_BITS_PER_ARRAY
                    else:
                        for bit_index in range(SENSOR_BITS_PER_ARRAY):
                            bit_name = f"{array_name}[{bit_index}]"
                            try:
                                node = self.use_node(bit_name)
                                with self._opc_io_lock:
                                    value, read_error = node.read()
                                values.append(None if read_error else bool(value))
                            except Exception:
                                values.append(None)
                        if any(value is not None for value in values):
                            successful_groups += 1

            bits = []
            for bit_index, value in enumerate(values):
                bit_name = f"{array_name}[{bit_index}]"
                metadata = self._sensor_bit_metadata.get(bit_name, {})
                bits.append(
                    {
                        "index": bit_index,
                        "name": bit_name,
                        "value": value,
                        "label": metadata.get("label") or "",
                        "address": metadata.get("address") or "",
                        "node_id": metadata.get("node_id") or self._direct_node_id_map.get(bit_name),
                    }
                )
            groups.append(
                {
                    "index": group_index,
                    "name": array_name,
                    "node_id": self._direct_node_id_map.get(array_name),
                    "values": values,
                    "bits": bits,
                    "error": error if not any(value is not None for value in values) else None,
                }
            )
        return {
            "success": successful_groups == SENSOR_ARRAY_COUNT,
            "partial": 0 < successful_groups < SENSOR_ARRAY_COUNT,
            "schema": "szlab_poly_studio.sensor_arrays.v1",
            "groups": groups,
        }

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

    @not_action
    def registered_variable_aliases(self) -> Dict[str, str]:
        """返回已注册变量的 ``EnglishName -> CSV Name`` 安全公开映射。"""
        registered = set(self._variables_to_find)
        return {
            alias: canonical
            for alias, canonical in self._name_mapping.items()
            if canonical in registered
        }

    @topic_config(period=1.0)
    def stack_status(self) -> Dict[str, Any]:
        return self.get_stack_status()
