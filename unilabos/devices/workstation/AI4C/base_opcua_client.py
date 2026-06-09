"""AI4C OPC UA 通信基类。

该文件替代历史上引用的 ``AI4M.base_opcua_client``，复用通用 OPC UA
BaseClient 的节点注册、CSV 解析和读写能力，并补齐 AI4C 驱动需要的缓存
和订阅接口。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import pandas as pd
from opcua import Client

from unilabos.device_comms.opcua_client.client import BaseClient
from unilabos.device_comms.opcua_client.node.uniopcua import DataType, Method, NodeType, Variable
from unilabos.utils.log import logger


def _parse_enum(enum_cls, value):
    if value is None or pd.isna(value):
        return None
    try:
        return enum_cls[str(value)]
    except KeyError:
        logger.warning(f"无法解析 {enum_cls.__name__}: {value}")
        return None


class OpcUaClientWithSubscription(BaseClient):
    def __init__(
        self,
        url: str,
        username: str = None,
        password: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0,
        subscription_interval: int = 500,
        *args,
        **kwargs,
    ):
        logging.getLogger("opcua").setLevel(logging.WARNING)
        super().__init__()

        client = Client(url)
        if username and password:
            client.set_user(username)
            client.set_password(password)

        self._use_subscription = use_subscription
        self._subscription = None
        self._subscription_handles = {}
        self._subscription_interval = subscription_interval
        self._node_values: dict[str, dict[str, Any]] = {}
        self._cache_timeout = cache_timeout
        self._client_lock = threading.RLock()
        self._connection_monitor_running = False
        self._connection_monitor_thread: Optional[threading.Thread] = None

        self._set_client(client)
        self._connect()
        self._start_connection_monitor()

    def _connect(self) -> None:
        logger.info("尝试连接到 OPC UA 服务器...")
        with self._client_lock:
            super()._connect()
            if self._use_subscription:
                self._setup_subscriptions()
            else:
                logger.info("订阅模式已禁用，将使用按需读取模式")

    class SubscriptionHandler:
        def __init__(self, outer):
            self.outer = outer

        def datachange_notification(self, node, val, data):
            self.outer._on_subscription_datachange(node, val, data)

        def event_notification(self, event):
            pass

    def _setup_subscriptions(self) -> None:
        if not self.client or not self._use_subscription:
            return

        try:
            self._subscription = self.client.create_subscription(
                self._subscription_interval,
                self.SubscriptionHandler(self),
            )
            subscribed_count = 0
            for node_name, node in self._node_registry.items():
                if node.type != NodeType.VARIABLE or not node.node_id:
                    continue
                try:
                    handle = self._subscription.subscribe_data_change(self.client.get_node(node.node_id))
                    self._subscription_handles[node_name] = handle
                    subscribed_count += 1
                except Exception as exc:
                    logger.warning(f"订阅节点 {node_name} 失败: {exc}")
            logger.info(f"OPC UA 订阅设置完成: 成功 {subscribed_count} 个")
        except Exception as exc:
            self._use_subscription = False
            logger.warning(f"订阅模式设置失败，已切换到按需读取模式: {exc}")

    def _on_subscription_datachange(self, node, val, data) -> None:
        node_id = str(node.nodeid)
        current_time = time.time()
        for node_name, node_obj in self._node_registry.items():
            if node_obj.node_id == node_id:
                self._node_values[node_name] = {
                    "value": val,
                    "timestamp": current_time,
                    "source": "subscription",
                }
                return

    def load_nodes_from_csv(self, csv_path: str) -> None:
        self._register_nodes_from_csv_node_ids(csv_path)
        if self._use_subscription:
            self._setup_subscriptions()

    def _register_nodes_from_csv_node_ids(self, csv_path: str) -> None:
        """按 CSV 中的 NodeId 直接注册节点，避免递归 browse 全地址空间。"""
        df = pd.read_csv(csv_path).drop_duplicates(subset="Name", keep="first")
        registered_count = 0
        skipped_count = 0

        for _, row in df.iterrows():
            name = row.get("Name")
            node_id = row.get("NodeId")
            node_type = _parse_enum(NodeType, row.get("NodeType"))
            data_type = _parse_enum(DataType, row.get("DataType"))

            if not name or pd.isna(name) or not node_id or pd.isna(node_id) or node_type is None:
                skipped_count += 1
                continue

            name = str(name)
            node_id = str(node_id)

            english_name = row.get("EnglishName")
            node_language = row.get("NodeLanguage", "English")
            if english_name and not pd.isna(english_name) and node_language == "Chinese":
                self._name_mapping[str(english_name)] = name
                self._reverse_mapping[name] = str(english_name)

            self._variables_to_find[name] = {
                "node_type": node_type,
                "data_type": data_type,
                "node_id": node_id,
            }

            if node_type == NodeType.VARIABLE:
                self._node_registry[name] = Variable(self.client, name, node_id, data_type)
            elif node_type == NodeType.METHOD:
                parent_node_id = row.get("ParentNodeId")
                if not parent_node_id or pd.isna(parent_node_id):
                    skipped_count += 1
                    logger.warning(f"方法节点 {name} 缺少 ParentNodeId，已跳过")
                    continue
                self._node_registry[name] = Method(self.client, name, node_id, str(parent_node_id), data_type)
            else:
                skipped_count += 1
                continue

            registered_count += 1

        logger.info(f"按 NodeId 直接注册 OPC UA 节点完成: 成功 {registered_count} 个, 跳过 {skipped_count} 个")

    def get_node_value(self, name: str, use_cache: bool = True, force_read: bool = False) -> Any:
        node_name = self._name_mapping.get(name, name)
        if node_name not in self._node_registry:
            raise ValueError(f"未找到名称为 '{name}' 的节点")

        if force_read:
            return self._read_node_and_cache(node_name, "forced_read")

        if use_cache and node_name in self._node_values:
            cache_entry = self._node_values[node_name]
            cache_age = time.time() - cache_entry["timestamp"]
            if cache_entry.get("source") == "subscription" or cache_age < self._cache_timeout:
                return cache_entry["value"]

        return self._read_node_and_cache(node_name, "on_demand_read")

    def set_node_value(self, name: str, value: Any) -> bool:
        node_name = self._name_mapping.get(name, name)
        with self._client_lock:
            node = self.use_node(node_name)
            error = node.write(value)
        if not error:
            self._node_values[node_name] = {
                "value": value,
                "timestamp": time.time(),
                "source": "write",
            }
            return True
        return False

    def _read_node_and_cache(self, node_name: str, source: str) -> Any:
        with self._client_lock:
            value, error = self.use_node(node_name).read()
        if error:
            raise RuntimeError(f"读取节点失败: {node_name}")
        self._node_values[node_name] = {
            "value": value,
            "timestamp": time.time(),
            "source": source,
        }
        return value

    def _start_connection_monitor(self) -> None:
        self._connection_monitor_running = True
        self._connection_monitor_thread = threading.Thread(
            target=self._connection_monitor_worker,
            daemon=True,
            name="ai4c-opcua-monitor",
        )
        self._connection_monitor_thread.start()

    def _connection_monitor_worker(self) -> None:
        while self._connection_monitor_running:
            time.sleep(30.0)
            if not self.client:
                continue
            try:
                self.client.get_namespace_array()
            except Exception as exc:
                logger.warning(f"OPC UA 连接检查失败: {exc}")

    def disconnect(self) -> None:
        self._connection_monitor_running = False
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            self._connection_monitor_thread.join(timeout=2.0)

        if self._subscription is not None:
            try:
                self._subscription.delete()
            except Exception as exc:
                logger.warning(f"删除 OPC UA 订阅失败: {exc}")

        if self.client:
            self.client.disconnect()
            logger.info("OPC UA client disconnected")
