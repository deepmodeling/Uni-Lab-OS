"""由本地驱动运行时与 HostLink 组成的无 ROS 分布式 backend。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from unilabos.config.config import BasicConfig, HostLinkConfig
from unilabos.backend.runtime.action import ActionCancelled, ActionContext
from unilabos.backend.runtime.async_utils import run_node_coroutine
from unilabos.backend.runtime.resource import AuthorityResourceService
from unilabos.backend.hostlink.service import normalize_service_name
from unilabos.backend.hostlink.topic import (
    TopicEvent,
    message_to_value,
    normalize_topic,
)
from unilabos.backend.hostlink.client import HostLinkClient, set_hostlink_client
from unilabos.backend.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.backend.hostlink.protocol import ActionType, LinkError
from unilabos.backend.hostlink.server import (
    HostLinkServer,
    HostStage,
    set_hostlink_server,
)
from unilabos.utils import logger


def to_wire_value(value: Any) -> Any:
    """Convert driver values, including ROS messages, for HostLink JSON."""

    return message_to_value(value)


def _device_call_payload(
    caller_device_id: str,
    device_id: str,
    action_name: str,
    arguments: Optional[Dict[str, Any]],
    action_id: str,
    test_mode: bool,
) -> Dict[str, Any]:
    """Slave 发起跨设备调用的 ``DEVICE_CALL`` 载荷；测试模式才带 ``test_mode``（旧 Host 忽略该字段）。"""

    payload: Dict[str, Any] = {
        "caller_device_id": caller_device_id,
        "device_id": device_id,
        "action": action_name,
        "arguments": dict(arguments or {}),
        "action_id": action_id,
    }
    if test_mode:
        payload["test_mode"] = True
    return payload


class HostLinkBackend:
    """Run local Python drivers and expose Slave devices to one Host."""

    def __init__(
        self,
        local: HostLinkLocalRuntime,
        *,
        is_slave: bool,
    ) -> None:
        self.local = local
        self.is_slave = bool(is_slave)
        self.server: Optional[HostLinkServer] = None
        self.client: Optional[HostLinkClient] = None
        self._started = False
        self._actions: Dict[str, tuple[str, ActionContext]] = {}
        self._action_callers: Dict[str, str] = {}
        self._actions_lock = threading.Lock()
        self._remote_topic_subscriptions: Dict[str, set[str]] = {}
        self._remote_topic_lock = threading.Lock()
        self._io_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="hostlink-backend-io",
        )
        self._topic_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="hostlink-backend-topic",
        )
        for node in self.local.devices.values():
            node.set_action_router(self)
            node.set_service_bus(self)
        self.local.add_device_change_listener(self._on_local_device_change)
        self.local.topic_bus.add_outbound_listener(self._on_local_topic)
        self.local.topic_bus.add_subscription_listener(
            self._on_local_subscription_change
        )

    def start(
        self,
        populate: Optional[Callable[[HostLinkLocalRuntime], None]] = None,
    ) -> None:
        """启动 backend。``populate`` 在物料权威可达之后、本地设备启动之前装配设备图。

        Slave 先建链再装配：设备装配（``resolve_device_definition``）按权威优先取 Site
        与持有的物料，需要 HostLink 已连上 Host；与 Host / ROS2 Slave「先对齐权威、再
        建设备」的顺序一致。设备仍由 ``local.start()`` 反向启动（工作站 post_init 前
        其子设备已就绪），随后把完整设备描述立刻通告 Host。

        Host 先开端口再装配设备：接管进程启动时预绑定的 server（没有则当场绑定），
        Slave 从此可以握手、登记与心跳；物料请求在物料权威接入后放行，设备 / service
        调用在本机设备全部启动、host 服务注册后放行，之前一律回「Host 启动中」。
        """

        if self._started:
            return
        if not HostLinkConfig.enable:
            raise ValueError("hostlink backend 不能与 --disable-hostlink 同时使用")
        try:
            if self.is_slave:
                self._start_slave()
                self._connect_slave()
                if populate is not None:
                    populate(self.local)
                # 驱动 post_init / 定时发布启动前先确认 Host 已登记设备归属。
                # 保持当前连接身份，由心跳更新设备集合，避免重握手迁移订阅。
                if self.client is not None:
                    self.client.configure_device_descriptors(self.local.descriptors())
                    self.client.heartbeat_now()
                    logger.info(
                        "[HostLink] Slave 注册确认: machine_name=%s, Host=%s:%s, devices=%s",
                        self.client.machine_name, self.client.host, self.client.port,
                        self.client.device_ids,
                    )
                self.local.start()
                if self.client is not None:
                    self.client.configure_device_descriptors(self.local.descriptors())
                    self.announce_devices()
            else:
                from unilabos.server.backend.composition import (
                    get_materials_gateway,
                )

                # 1) 端口最先可连：Slave 的握手、登记与心跳从这里起就被接受
                server = self._attach_host_server()
                self.local.set_resource_service(
                    AuthorityResourceService(
                        gateway_provider=get_materials_gateway
                    )
                )
                # 2) 物料权威与 Host 开机物料对齐都在设备 runtime 启动前完成
                #    （app/main.py），Slave 的物料请求从此放行
                server.mark_ready(HostStage.MATERIALS)
                if populate is not None:
                    populate(self.local)
                self.local.start()
                # 内置 host 服务设备：承载 transfer/出库/加试剂等物料编排动作，
                # 与 ROS2 HostNode 的 @action 同 device_id、同 schema。
                from unilabos.backend.hostlink.host_services import register_host_services

                register_host_services(self)
                self._bind_material_transfer_dispatcher()
                # 3) 本机设备与 host 服务全部就绪：放行设备 / service 调用
                server.hello_payload["devices"] = self.local.descriptors()
                server.mark_ready(HostStage.DEVICES)
                logger.info(
                    "[HostLink backend] Host 已就绪：%s:%d，本地设备=%s",
                    HostLinkConfig.bind,
                    server.port,
                    sorted(self.local.devices),
                )
        except Exception:
            self.stop()
            raise
        self._started = True

    def _attach_host_server(self) -> HostLinkServer:
        """接管进程启动时预绑定的 HostLink server（没有则当场绑定），并装配请求处理器。

        server 处于启动期门控：依赖物料权威 / 本机设备的请求在 :meth:`start` 标记对应
        阶段就绪前回「Host 启动中」，Slave 原样重试，不会拿到错误结果。
        """

        from unilabos.backend.hostlink.startup import (
            adopt_prebound_host_server,
            create_host_server,
        )

        server = adopt_prebound_host_server()
        prebound = server is not None
        if server is None:
            server = create_host_server()
        self.server = server
        server.hello_payload = {
            "backend": "hostlink",
            "role": "host",
            "devices": self.local.descriptors(),
        }
        self.server.register_handler(
            ActionType.ACTION_FEEDBACK,
            self._handle_action_feedback,
        )
        self.server.register_handler(
            ActionType.ACTION_CANCEL,
            self._handle_peer_action_cancel,
        )
        self.server.register_handler(
            ActionType.DEVICE_CALL,
            self._handle_peer_device_call,
        )
        self.server.register_handler(
            ActionType.SERVICE_CALL,
            self._handle_peer_service_call,
        )
        self.server.register_handler(
            ActionType.TOPIC_PUBLISH,
            self._handle_topic_publish,
        )
        self.server.register_handler(
            ActionType.TOPIC_SUBSCRIBE,
            self._handle_topic_subscribe,
        )
        self.server.register_handler(
            ActionType.TOPIC_UNSUBSCRIBE,
            self._handle_topic_unsubscribe,
        )
        self.server.register_handler(
            ActionType.MATERIAL_TEMPLATE_LIST,
            self._handle_material_template_list,
        )
        self.server.register_handler(
            ActionType.MATERIAL_TEMPLATE_CREATE,
            self._handle_material_template_create,
        )
        self.server.register_handler(
            ActionType.MATERIAL_CREATE,
            self._handle_material_create,
        )
        self.server.register_handler(
            ActionType.MATERIAL_GET_TREE,
            self._handle_material_get_tree,
        )
        self.server.register_handler(
            ActionType.MATERIAL_GET_BY_RESOURCE_ID,
            self._handle_material_get_by_resource_id,
        )
        self.server.register_handler(
            ActionType.MATERIAL_SEARCH,
            self._handle_material_search,
        )
        self.server.register_handler(
            ActionType.MATERIAL_DATA_PUT,
            self._handle_material_data_put,
        )
        self.server.register_handler(
            ActionType.MATERIAL_MOVE,
            self._handle_material_move,
        )
        self.server.register_handler(
            ActionType.MATERIAL_TRANSFER,
            self._handle_material_transfer,
        )
        self.server.register_handler(
            ActionType.MATERIAL_DELETE,
            self._handle_material_delete,
        )
        self.server.register_handler(
            ActionType.MATERIAL_COMPARE_SNAPSHOT,
            self._handle_material_compare_snapshot,
        )
        self.server.register_handler(
            ActionType.MATERIAL_APPLY_SNAPSHOT,
            self._handle_material_apply_snapshot,
        )
        self.server.register_handler(
            ActionType.MATERIAL_APPLY_DELTA,
            self._handle_material_apply_delta,
        )
        if not prebound:
            server.start()
            set_hostlink_server(server)
        logger.info(
            "[HostLink backend] Host 端口%s：%s:%d；等待 %s 就绪后放行物料与设备请求",
            "已在进程启动时打开" if prebound else "已打开",
            HostLinkConfig.bind,
            server.port,
            "、".join(server.pending_stages()) or "（无）",
        )
        return server

    @staticmethod
    def _handle_material_template_list(
        data: dict[str, Any], peer: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from unilabos.backend.hostlink.materials_proxy import template_list
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        return template_list(gateway, data, peer)

    @staticmethod
    def _handle_material_template_create(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import ResourceTemplateWrite
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        value = ResourceTemplateWrite.model_validate(mutation.payload)
        return gateway.create_template(mutation, value).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_create(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        """Proxy Slave creation through the Host-selected authority."""

        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialTreeCreate
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        value = MaterialTreeCreate.model_validate(mutation.payload)
        result = gateway.create_tree(mutation, value)
        return result.model_dump(mode="json", exclude_none=False)

    @staticmethod
    def _handle_material_get_tree(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        root_material_uuid = str(data.get("root_material_uuid") or "").strip()
        if not root_material_uuid:
            raise ValueError("material.tree.get requires root_material_uuid")
        return gateway.get_tree(root_material_uuid).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_get_by_resource_id(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        resource_id = str(data.get("resource_id") or "").strip()
        if not resource_id:
            raise ValueError("material.resource-id.get requires resource_id")
        return gateway.get_material_by_resource_id(resource_id).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_search(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("material.search requires name")
        return [
            item.model_dump(mode="json", exclude_none=False)
            for item in gateway.search_materials(name)
        ]

    @staticmethod
    def _handle_material_data_put(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialDataWrite
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        material_uuid = str(data.get("material_uuid") or "").strip()
        if not material_uuid:
            raise ValueError("material.data.put requires material_uuid")
        mutation_data = dict(data)
        mutation_data.pop("material_uuid", None)
        mutation = InventoryMutation.model_validate(mutation_data)
        value = MaterialDataWrite.model_validate(mutation.payload)
        return gateway.put_data(mutation, material_uuid, value).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_move(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialMove
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        value = MaterialMove.model_validate(mutation.payload)
        return gateway.move_material(mutation, value).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_transfer(
        data: dict[str, Any], peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialTransfer
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        value = MaterialTransfer.model_validate(mutation.payload)
        owned = {str(item) for item in peer.get("device_ids") or []}
        if owned and value.source_device_id not in owned:
            raise PermissionError(
                "Slave 未注册物料转移来源设备："
                f"{value.source_device_id!r}"
            )
        return gateway.transfer_material(mutation, value).model_dump(
            mode="json", exclude_none=False
        )

    def _bind_material_transfer_dispatcher(self) -> None:
        """让嵌入式 MaterialsService 通过当前 HostLink runtime 调设备服务。"""

        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        service = getattr(gateway, "service", None)
        setter = getattr(service, "set_resource_sync_dispatcher", None)
        if callable(setter):
            setter(self.dispatch_material_sync)

    def dispatch_material_sync(self, command: Any) -> dict[str, Any]:
        """微后端 transfer 投影下发：本机设备直调协程，跨机经 HostLink RPC。

        与资源树同步 / 挂载下行同构，不依赖 per-device service。
        """

        data = command.model_dump(mode="json", exclude_none=False)
        device_id = str(data["device_id"])
        node = self.local.get_device(device_id)
        if node is not None:
            result = run_node_coroutine(node, node.material_sync(dict(data)))
        elif self.server is not None and self.server.has_device(device_id):
            result = self.server.request_device(
                device_id, ActionType.MATERIAL_SYNC, data, timeout=30.0
            )
        else:
            raise RuntimeError(
                f"设备 {device_id!r} 不在本进程、也不在 HostLink 在线表，无法同步物料"
            )
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError(
                f"设备 {device_id!r} material_sync 失败：{result}"
            )
        return result

    @staticmethod
    def _handle_material_delete(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialDelete
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        value = MaterialDelete.model_validate(mutation.payload)
        return gateway.delete_material(mutation, value).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_compare_snapshot(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import MaterialSnapshot
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        snapshot = MaterialSnapshot.model_validate(data)
        return gateway.compare_snapshot(snapshot).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_apply_snapshot(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation
        from unilabos.protocol.materials import MaterialSnapshot
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        snapshot = MaterialSnapshot.model_validate(mutation.payload)
        return gateway.apply_snapshot(mutation, snapshot).model_dump(
            mode="json", exclude_none=False
        )

    @staticmethod
    def _handle_material_apply_delta(
        data: dict[str, Any], _peer: dict[str, Any]
    ) -> dict[str, Any]:
        from unilabos.protocol.materials import InventoryMutation, MaterialDelta
        from unilabos.server.backend.composition import get_materials_gateway

        gateway = get_materials_gateway()
        if gateway is None:
            raise RuntimeError("Host 尚未配置 materials authority")
        mutation = InventoryMutation.model_validate(data)
        delta = MaterialDelta.model_validate(mutation.payload)
        return gateway.apply_delta(mutation, delta).model_dump(
            mode="json", exclude_none=False
        )

    # ------------------------------------------------------------------
    # 物料下行链路（Host → 设备投影），与 presets/downlink 的本地执行语义一致
    # ------------------------------------------------------------------

    def _require_local_resource_node(self, device_id: str) -> Any:
        node = self.local.get_device(device_id)
        if node is None:
            raise ValueError(f"本进程没有设备节点: {device_id!r}")
        return node

    def _handle_resource_tree_sync(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """``RESOURCE_TREE_SYNC``：分发资源树变更到本进程设备节点。"""
        device_id = str(data.get("device_id") or "")
        operations = list(data.get("operations") or [])
        node = self._require_local_resource_node(device_id)
        return run_node_coroutine(node, node.apply_resource_tree_update(operations))

    def _handle_resource_append(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """``RESOURCE_APPEND``：把微后端权威物料挂载到本进程设备节点。"""
        device_id = str(data.get("device_id") or "")
        node = self._require_local_resource_node(device_id)
        payload = {key: value for key, value in data.items() if key != "device_id"}
        return node.append_resource_blocking(payload)

    def _handle_material_sync(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """``MATERIAL_SYNC``：微后端 transfer 的设备侧投影（unload/load）。"""
        device_id = str(data.get("device_id") or "")
        node = self._require_local_resource_node(device_id)
        return run_node_coroutine(node, node.material_sync(dict(data)))

    def _handle_device_manage(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """``DEVICE_MANAGE``：动态 add/remove 本进程设备。"""
        device_id = str(data.get("device_id") or "")
        node = self._require_local_resource_node(device_id)
        payload = {key: value for key, value in data.items() if key != "device_id"}
        return run_node_coroutine(node, node.device_manage(payload))

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
            on_status_change=self._on_client_status_change,
            host_wait_timeout=HostLinkConfig.host_wait_timeout,
        )
        from unilabos.client.materials import HostLinkMaterialsClient

        self.local.set_resource_service(
            AuthorityResourceService(HostLinkMaterialsClient(self.client))
        )
        self.client.register_handler(ActionType.DEVICE_CALL, self._handle_device_call)
        self.client.register_handler(
            ActionType.SERVICE_CALL,
            self._handle_service_call,
        )
        self.client.register_handler(
            ActionType.DEVICE_STATE,
            self._handle_device_state,
        )
        self.client.register_handler(
            ActionType.ACTION_CANCEL,
            self._handle_action_cancel,
        )
        self.client.register_handler(
            ActionType.ACTION_FEEDBACK,
            self._handle_incoming_action_feedback,
        )
        self.client.register_handler(
            ActionType.TOPIC_DELIVER,
            self._handle_topic_deliver,
        )
        # 物料下行链路（Host → 本 Slave 的设备投影），与 ROS slave 的
        # register_hostlink_resource_handlers 语义一致。
        self.client.register_handler(
            ActionType.RESOURCE_TREE_SYNC,
            self._handle_resource_tree_sync,
        )
        self.client.register_handler(
            ActionType.RESOURCE_APPEND,
            self._handle_resource_append,
        )
        self.client.register_handler(
            ActionType.MATERIAL_SYNC,
            self._handle_material_sync,
        )
        self.client.register_handler(
            ActionType.DEVICE_MANAGE,
            self._handle_device_manage,
        )
        for node in self.local.devices.values():
            node.add_status_listener(self._on_local_status)
        set_hostlink_client(self.client)

    def _connect_slave(self) -> None:
        client = self.client
        if client is None:
            raise RuntimeError("HostLink Slave client 尚未创建")
        host = str(HostLinkConfig.host or "").strip()
        wait_timeout = float(HostLinkConfig.host_wait_timeout)
        if BasicConfig.slave_no_host:
            client.start()
        else:
            # Host 可能晚于 Slave 启动（或正在重启）：按退避重连直到端口可连，而不是一次
            # 连接超时就放弃。Host 端口在其进程启动之初就打开，通常几秒内即可连上。
            logger.info(
                "[HostLink backend] 正在连接 Host=%s:%d（未就绪时按退避重试，最长 %gs）",
                host,
                HostLinkConfig.port,
                wait_timeout,
            )
            if not client.connect_blocking(wait_timeout):
                reason = client.last_error or "未收到 HELLO 应答"
                raise LinkError(
                    f"{wait_timeout:g}s 内无法连接 HostLink Host {host}:{HostLinkConfig.port}"
                    f"（最后一次错误：{reason}）。请确认 Host 已启动、--host_node_ip / "
                    "--hostlink_port 与 Host 一致且端口未被防火墙拦截；Host 启动较慢时可调大 "
                    "HostLinkConfig.host_wait_timeout（--hostlink_host_wait_timeout）"
                )
        logger.info(
            "[HostLink backend] Slave 已连上 Host=%s:%d，本地设备=%s",
            host,
            HostLinkConfig.port,
            sorted(self.local.devices),
        )

    def announce_devices(self) -> None:
        """Slave：把当前设备描述与状态立刻推给 Host，不等下一个心跳。"""

        client = self.client
        if client is None or not client.online:
            return
        try:
            client.heartbeat_now()
        except Exception:  # noqa: BLE001 - 心跳循环随后会补上，通告失败不影响启动
            logger.warning("[HostLink backend] 设备描述即时通告失败，等待下一个心跳", exc_info=True)

    def register_service(
        self,
        name: str,
        callback: Any,
        *,
        owner_device_id: str,
    ) -> None:
        self.local.service_bus.register_service(
            name,
            callback,
            owner_device_id=owner_device_id,
        )

    def unregister_service(
        self,
        name: str,
        *,
        owner_device_id: str,
    ) -> None:
        self.local.service_bus.unregister_service(
            name,
            owner_device_id=owner_device_id,
        )

    @staticmethod
    def _service_target(name: str) -> str:
        parts = normalize_service_name(name).strip("/").split("/")
        if parts and parts[0] == "srv":
            parts = parts[1:]
        if len(parts) >= 3 and parts[0] == "devices":
            return parts[1]
        return ""

    def has_service(self, name: str) -> bool:
        normalized = normalize_service_name(name)
        if self.local.service_bus.has_service(normalized):
            return True
        target = self._service_target(normalized)
        if not target:
            return False
        if self.server is not None:
            remote = self.server.devices(online_only=True).get(target)
            descriptor = (remote or {}).get("device") or {}
            return normalized in descriptor.get("services", [])
        if self.client is not None:
            for descriptor in self.client.hello_info.get("devices") or []:
                if descriptor.get("id") == target:
                    return normalized in descriptor.get("services", [])
        return False

    def call_service(
        self,
        name: str,
        request: Any,
        *,
        caller_device_id: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        normalized = normalize_service_name(name)
        if self.local.service_bus.has_service(normalized):
            return self.local.service_bus.call_service(
                normalized,
                request,
                caller_device_id=caller_device_id,
                timeout=timeout,
            )
        target = self._service_target(normalized)
        if not target:
            raise KeyError(f"未知 HostLink service：{normalized}")
        payload = {
            "caller_device_id": str(caller_device_id),
            "service": normalized,
            "request": to_wire_value(request),
        }
        if self.server is not None:
            response = self.server.request_device(
                target,
                ActionType.SERVICE_CALL,
                payload,
                timeout,
            )
        elif self.client is not None:
            response = self.client.request(
                ActionType.SERVICE_CALL,
                payload,
                timeout,
            )
        else:
            raise KeyError(f"未知 HostLink service：{normalized}")
        return response.get("response") if isinstance(response, dict) else response

    async def call_service_async(
        self,
        name: str,
        request: Any,
        *,
        caller_device_id: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        normalized = normalize_service_name(name)
        if self.local.service_bus.has_service(normalized):
            return await self.local.service_bus.call_service_async(
                normalized,
                request,
                caller_device_id=caller_device_id,
                timeout=timeout,
            )
        target = self._service_target(normalized)
        if not target:
            raise KeyError(f"未知 HostLink service：{normalized}")
        payload = {
            "caller_device_id": str(caller_device_id),
            "service": normalized,
            "request": to_wire_value(request),
        }
        if self.server is not None:
            response = await self.server.request_device_async(
                target,
                ActionType.SERVICE_CALL,
                payload,
                timeout,
            )
        elif self.client is not None:
            response = await self.client.request_async(
                ActionType.SERVICE_CALL,
                payload,
                timeout,
            )
        else:
            raise KeyError(f"未知 HostLink service：{normalized}")
        return response.get("response") if isinstance(response, dict) else response

    def _heartbeat_payload(self) -> Dict[str, Any]:
        return {
            "devices": self.local.descriptors(),
            "states": to_wire_value(self.local.snapshot_states()),
        }

    def _on_local_device_change(self, event: str, node: Any) -> None:
        if event == "added":
            node.set_action_router(self)
            node.set_service_bus(self)
            if self.client is not None:
                node.add_status_listener(self._on_local_status)
        elif event == "removed":
            node.remove_status_listener(self._on_local_status)

        descriptors = self.local.descriptors()
        if self.client is not None:
            self.client.configure_device_descriptors(descriptors)
        if self.server is not None:
            self.server.hello_payload["devices"] = descriptors

    def _on_client_status_change(self, online: bool) -> None:
        if not online:
            return
        try:
            self._topic_executor.submit(self._restore_online_state)
        except RuntimeError:
            pass

    def _restore_online_state(self) -> None:
        """Restore subscriptions and notify opt-in drivers after every reconnect."""

        self._register_topic_subscriptions()
        for node in self.local.devices.values():
            callback = getattr(node.driver, "on_hostlink_connected", None)
            if not callable(callback):
                continue
            try:
                callback()
            except Exception:  # noqa: BLE001 - one driver must not break reconnect
                logger.exception(
                    "[HostLink backend] 设备 %s 重连恢复失败",
                    node.device_id,
                )

    def _register_topic_subscriptions(self) -> None:
        client = self.client
        if client is None or not client.online:
            return
        for topic in self.local.topic_bus.subscribed_topics():
            try:
                client.request(ActionType.TOPIC_SUBSCRIBE, {"topic": topic})
            except LinkError:
                return

    def _on_local_subscription_change(self, topic: str, subscribed: bool) -> None:
        if not self.is_slave:
            return
        client = self.client
        if client is None or not client.online:
            return
        action_type = (
            ActionType.TOPIC_SUBSCRIBE if subscribed else ActionType.TOPIC_UNSUBSCRIBE
        )
        try:
            self._topic_executor.submit(
                client.request,
                action_type,
                {"topic": topic},
            )
        except RuntimeError:
            pass

    def _on_local_topic(self, event: TopicEvent) -> None:
        try:
            if self.is_slave:
                self._topic_executor.submit(self._send_topic_to_host, event)
            else:
                self._topic_executor.submit(self._forward_topic_to_slaves, event)
        except RuntimeError:
            pass

    def _send_topic_to_host(self, event: TopicEvent) -> None:
        client = self.client
        if client is None or not client.online:
            return
        try:
            client.request(ActionType.TOPIC_PUBLISH, {"event": event.to_wire()})
        except LinkError:
            logger.debug(
                "[HostLink backend] topic publish failed while offline: %s",
                event.topic,
            )

    def _forward_topic_to_slaves(
        self,
        event: TopicEvent,
        *,
        exclude_node_id: str = "",
    ) -> None:
        server = self.server
        if server is None:
            return
        with self._remote_topic_lock:
            interested = {
                node_id
                for node_id, topics in self._remote_topic_subscriptions.items()
                if event.topic in topics and node_id != exclude_node_id
            }
        if not interested:
            return
        peers = {
            str(peer.get("node_id") or ""): peer
            for peer in server.peers()
            if peer.get("online")
        }
        for node_id in interested:
            peer = peers.get(node_id)
            if peer is None:
                continue
            try:
                server.request_peer(
                    str(peer["addr"]),
                    ActionType.TOPIC_DELIVER,
                    {"event": event.to_wire()},
                )
            except LinkError:
                logger.debug(
                    "[HostLink backend] topic delivery failed: %s -> %s",
                    event.topic,
                    node_id,
                )

    @staticmethod
    def _topic_from_data(data: Dict[str, Any]) -> str:
        return normalize_topic(str(data.get("topic") or ""))

    def _handle_topic_subscribe(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        topic = self._topic_from_data(data)
        node_id = str(peer.get("node_id") or "")
        if not node_id:
            raise PermissionError("HostLink topic subscriber 缺少 Slave 身份")
        with self._remote_topic_lock:
            self._remote_topic_subscriptions.setdefault(node_id, set()).add(topic)
        return {"accepted": True, "topic": topic}

    def _handle_topic_unsubscribe(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        topic = self._topic_from_data(data)
        node_id = str(peer.get("node_id") or "")
        with self._remote_topic_lock:
            topics = self._remote_topic_subscriptions.get(node_id)
            if topics is not None:
                topics.discard(topic)
                if not topics:
                    self._remote_topic_subscriptions.pop(node_id, None)
        return {"accepted": True, "topic": topic}

    def _handle_topic_publish(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        raw_event = data.get("event")
        if not isinstance(raw_event, dict):
            raise TypeError("topic.publish requires event")
        event = TopicEvent.from_wire(raw_event)
        owned = {str(item) for item in peer.get("device_ids") or []}
        if event.publisher_device_id not in owned:
            raise PermissionError(
                f"Slave 未注册 topic 发布设备：{event.publisher_device_id!r}"
            )
        self.local.topic_bus.publish(event, forward=False)
        try:
            self._topic_executor.submit(
                self._forward_topic_to_slaves,
                event,
                exclude_node_id=str(peer.get("node_id") or ""),
            )
        except RuntimeError:
            pass
        return {
            "accepted": True,
            "topic": event.topic,
            "message_id": event.message_id,
        }

    def _handle_topic_deliver(self, data: Dict[str, Any]) -> Dict[str, Any]:
        raw_event = data.get("event")
        if not isinstance(raw_event, dict):
            raise TypeError("topic.deliver requires event")
        event = TopicEvent.from_wire(raw_event)
        self.local.topic_bus.publish(event, forward=False)
        return {
            "accepted": True,
            "topic": event.topic,
            "message_id": event.message_id,
        }

    def _on_local_status(self, device_id: str, name: str, value: Any) -> None:
        client = self.client
        if client is None or not client.online:
            return
        try:
            self._io_executor.submit(
                self._publish_status,
                client,
                device_id,
                name,
                value,
            )
        except RuntimeError:
            pass

    @staticmethod
    def _publish_status(
        client: HostLinkClient,
        device_id: str,
        name: str,
        value: Any,
    ) -> None:
        try:
            client.request(
                ActionType.DEVICE_STATE,
                {
                    "device_id": device_id,
                    "state": {name: to_wire_value(value)},
                },
            )
        except LinkError:
            # 心跳会在重连后补发完整状态，这里不重放单字段通知。
            pass

    def _handle_service_call(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = normalize_service_name(str(data.get("service") or ""))
        if not self.local.service_bus.has_service(name):
            raise KeyError(f"当前 Slave 没有 service：{name}")
        result = self.local.service_bus.call_service(
            name,
            data.get("request"),
            caller_device_id=str(data.get("caller_device_id") or ""),
        )
        return {"service": name, "response": to_wire_value(result)}

    def _handle_peer_service_call(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        caller_device_id = str(data.get("caller_device_id") or "").strip()
        owned = {str(item) for item in peer.get("device_ids") or []}
        if caller_device_id not in owned:
            raise PermissionError(
                f"Slave 未注册 service 调用设备：{caller_device_id!r}"
            )
        name = normalize_service_name(str(data.get("service") or ""))
        result = self.call_service(
            name,
            data.get("request"),
            caller_device_id=caller_device_id,
        )
        return {"service": name, "response": to_wire_value(result)}

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
        context = ActionContext(
            action_id=str(data.get("action_id") or "") or ActionContext().action_id,
            feedback_callback=self._send_action_feedback,
        )
        if data.get("test_mode"):
            # 调用方在测试模式下发起的跨设备调用：目标驱动在本进程，由这里判定是否模拟
            mocked = self.local.test_mode_mock(
                device_id,
                action,
                test_mode=True,
                caller_device_id=str(data.get("caller_device_id") or ""),
            )
            if mocked is not None:
                return {
                    "device_id": device_id,
                    "action": action,
                    "action_id": context.action_id,
                    "status": "succeeded",
                    "result": to_wire_value(mocked),
                    "state": to_wire_value(self.local.devices[device_id].snapshot_status()),
                }
        with self._actions_lock:
            self._actions[context.action_id] = (device_id, context)
        try:
            result = self.local.call_action(
                device_id,
                action,
                action_context=context,
                **arguments,
            )
            status = "succeeded"
        except ActionCancelled:
            result = None
            status = "cancelled"
        finally:
            with self._actions_lock:
                self._actions.pop(context.action_id, None)
        return {
            "device_id": device_id,
            "action": action,
            "action_id": context.action_id,
            "status": status,
            "result": to_wire_value(result),
            "state": to_wire_value(self.local.devices[device_id].snapshot_status()),
        }

    def _handle_peer_device_call(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        caller_device_id = str(data.get("caller_device_id") or "").strip()
        owned = {str(item) for item in peer.get("device_ids") or []}
        if caller_device_id not in owned:
            raise PermissionError(f"Slave 未注册调用设备：{caller_device_id!r}")
        device_id = str(data.get("device_id") or "").strip()
        action = str(data.get("action") or "").strip()
        arguments = data.get("arguments")
        if not device_id or not action:
            raise ValueError("device.call requires device_id and action")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise TypeError("device.call arguments must be an object")
        action_id = str(data.get("action_id") or "") or ActionContext().action_id
        peer_addr = str(peer.get("addr") or "")

        def forward_feedback(
            feedback_action_id: str,
            feedback: Dict[str, Any],
        ) -> None:
            server = self.server
            if server is None or not peer_addr:
                return
            try:
                server.request_peer(
                    peer_addr,
                    ActionType.ACTION_FEEDBACK,
                    {
                        "action_id": feedback_action_id,
                        "device_id": device_id,
                        "feedback": to_wire_value(feedback),
                    },
                )
            except LinkError:
                logger.warning(
                    "[HostLink backend] Action feedback 转发失败：%s",
                    feedback_action_id,
                )

        context = ActionContext(
            action_id=action_id,
            feedback_callback=forward_feedback,
        )
        test_mode = bool(data.get("test_mode"))
        if test_mode:
            mocked = self.local.test_mode_mock(
                device_id, action, test_mode=True, caller_device_id=caller_device_id
            )
            if mocked is not None:
                return {
                    "device_id": device_id,
                    "action": action,
                    "action_id": action_id,
                    "status": "succeeded",
                    "result": to_wire_value(mocked),
                }
        try:
            result = self.call_action(
                device_id,
                action,
                action_context=context,
                _test_mode=test_mode,
                **arguments,
            )
            status = "succeeded"
        except ActionCancelled:
            result = None
            status = "cancelled"
        return {
            "device_id": device_id,
            "action": action,
            "action_id": action_id,
            "status": status,
            "result": to_wire_value(result),
        }

    def _send_action_feedback(
        self,
        action_id: str,
        feedback: Dict[str, Any],
    ) -> None:
        client = self.client
        if client is None:
            return
        with self._actions_lock:
            active = self._actions.get(action_id)
        device_id = active[0] if active is not None else ""
        try:
            client.request(
                ActionType.ACTION_FEEDBACK,
                {
                    "action_id": action_id,
                    "device_id": device_id,
                    "feedback": to_wire_value(feedback),
                },
            )
        except LinkError:
            logger.warning(
                "[HostLink backend] Action feedback 发送失败：%s",
                action_id,
            )

    def _handle_action_feedback(
        self,
        data: Dict[str, Any],
        _peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._deliver_action_feedback(data)

    def _handle_incoming_action_feedback(
        self,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._deliver_action_feedback(data)

    def _deliver_action_feedback(
        self,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        action_id = str(data.get("action_id") or "")
        with self._actions_lock:
            active = self._actions.get(action_id)
        if active is None:
            return {"accepted": False, "action_id": action_id}
        feedback = data.get("feedback")
        try:
            active[1].publish_feedback(feedback if isinstance(feedback, dict) else {})
        except Exception:  # noqa: BLE001 - feedback 回调不能中断远端动作
            logger.exception(
                "[HostLink backend] Action feedback 回调失败：%s",
                action_id,
            )
        return {"accepted": True, "action_id": action_id}

    def _handle_action_cancel(self, data: Dict[str, Any]) -> Dict[str, Any]:
        action_id = str(data.get("action_id") or "")
        with self._actions_lock:
            active = self._actions.get(action_id)
        if active is None:
            return {"accepted": False, "action_id": action_id}
        active[1].request_cancel()
        return {"accepted": True, "action_id": action_id}

    def _handle_peer_action_cancel(
        self,
        data: Dict[str, Any],
        peer: Dict[str, Any],
    ) -> Dict[str, Any]:
        caller_device_id = str(data.get("caller_device_id") or "")
        owned = {str(item) for item in peer.get("device_ids") or []}
        if caller_device_id not in owned:
            raise PermissionError(
                f"Slave 未注册取消动作的调用设备：{caller_device_id!r}"
            )
        action_id = str(data.get("action_id") or "")
        return {
            "accepted": self.cancel_action(action_id),
            "action_id": action_id,
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
        /,
        *,
        action_context: Optional[ActionContext] = None,
        _test_mode: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Call a local device, or route a Host call to an online Slave.

        ``device_id`` / ``action_name`` 是 positional-only：host_node 的
        apply_deduct_resource / discard_resource 等动作自身就带 ``device_id``
        参数，经 ``**kwargs`` 展开时不得与位置参数撞名。

        ``_test_mode``：跨设备调用来自测试模式下运行的驱动；目标在 Slave 上时随请求带过去，
        由 Slave 按目标驱动的 ``run_in_test_mode`` 判定是否模拟。
        """

        device_id = str(device_id)
        context = action_context or ActionContext()
        if device_id in self.local.devices:
            with self._actions_lock:
                self._actions[context.action_id] = (device_id, context)
            try:
                return self.local.call_action(
                    device_id,
                    action_name,
                    action_context=context,
                    **kwargs,
                )
            finally:
                with self._actions_lock:
                    self._actions.pop(context.action_id, None)
        if self.server is None:
            raise KeyError(f"未知 HostLink 设备：{device_id}")
        with self._actions_lock:
            self._actions[context.action_id] = (device_id, context)
        try:
            response = self.server.call_device(
                device_id,
                action_name,
                kwargs,
                action_id=context.action_id,
                test_mode=_test_mode,
            )
        finally:
            with self._actions_lock:
                self._actions.pop(context.action_id, None)
        if isinstance(response, dict) and response.get("status") == "cancelled":
            context.request_cancel()
            raise ActionCancelled(f"action cancelled: {context.action_id}")
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    async def call_action_async(
        self,
        device_id: str,
        action_name: str,
        /,
        *,
        action_context: Optional[ActionContext] = None,
        request_timeout: Optional[float] = None,
        _test_mode: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Await a local or remote device action without blocking a thread."""

        device_id = str(device_id)
        context = action_context or ActionContext()
        with self._actions_lock:
            self._actions[context.action_id] = (device_id, context)
        try:
            if device_id in self.local.devices:
                return await self.local.call_action_async(
                    device_id,
                    action_name,
                    action_context=context,
                    **kwargs,
                )
            if self.server is None:
                raise KeyError(f"未知 HostLink 设备：{device_id}")
            response = await self.server.call_device_async(
                device_id,
                action_name,
                kwargs,
                timeout=request_timeout,
                action_id=context.action_id,
                test_mode=_test_mode,
            )
        except asyncio.CancelledError:
            context.request_cancel()
            try:
                await asyncio.shield(self.cancel_action_async(context.action_id))
            except Exception as exc:  # noqa: BLE001 - cancellation must propagate
                logger.warning(
                    "[HostLink backend] 异步动作取消转发失败：%s (%s)",
                    context.action_id,
                    exc,
                )
            raise
        finally:
            with self._actions_lock:
                self._actions.pop(context.action_id, None)
        if isinstance(response, dict) and response.get("status") == "cancelled":
            context.request_cancel()
            raise ActionCancelled(f"action cancelled: {context.action_id}")
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    def route_action(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        target = self.local._normalize_device_id(device_id)
        if target == caller_device_id:
            raise ValueError("跨设备动作不能回调当前设备自身")
        # --test_mode：目标在本机且驱动未声明 run_in_test_mode 时模拟；跨机时随请求带给目标进程判定
        mocked = self.local.test_mode_mock(target, action_name, caller_device_id=caller_device_id)
        if mocked is not None:
            return mocked
        test_mode = bool(BasicConfig.test_mode)
        context = options.get("action_context")
        if context is None and (
            options.get("action_id") or options.get("feedback_callback")
        ):
            context = ActionContext(
                action_id=str(options.get("action_id") or "")
                or ActionContext().action_id,
                feedback_callback=options.get("feedback_callback"),
            )
        if target in self.local.devices or self.server is not None:
            return self.call_action(
                target,
                action_name,
                action_context=context,
                _test_mode=test_mode,
                **dict(arguments or {}),
            )
        client = self.client
        if client is None:
            raise KeyError(f"未知 HostLink 设备：{target}")
        context = context or ActionContext()
        with self._actions_lock:
            self._actions[context.action_id] = (target, context)
            self._action_callers[context.action_id] = caller_device_id
        try:
            response = client.request(
                ActionType.DEVICE_CALL,
                _device_call_payload(
                    caller_device_id, target, action_name, arguments, context.action_id, test_mode
                ),
                timeout=options.get("timeout"),
            )
        finally:
            with self._actions_lock:
                self._actions.pop(context.action_id, None)
                self._action_callers.pop(context.action_id, None)
        if isinstance(response, dict) and response.get("status") == "cancelled":
            context.request_cancel()
            raise ActionCancelled(f"action cancelled: {context.action_id}")
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    async def route_action_async(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        target = self.local._normalize_device_id(device_id)
        if target == caller_device_id:
            raise ValueError("跨设备动作不能回调当前设备自身")
        mocked = self.local.test_mode_mock(target, action_name, caller_device_id=caller_device_id)
        if mocked is not None:
            return mocked
        test_mode = bool(BasicConfig.test_mode)
        context = options.get("action_context")
        if context is None and (
            options.get("action_id") or options.get("feedback_callback")
        ):
            context = ActionContext(
                action_id=str(options.get("action_id") or "")
                or ActionContext().action_id,
                feedback_callback=options.get("feedback_callback"),
            )
        if target in self.local.devices or self.server is not None:
            return await self.call_action_async(
                target,
                action_name,
                action_context=context,
                request_timeout=options.get("timeout"),
                _test_mode=test_mode,
                **dict(arguments or {}),
            )
        client = self.client
        if client is None:
            raise KeyError(f"未知 HostLink 设备：{target}")
        context = context or ActionContext()
        with self._actions_lock:
            self._actions[context.action_id] = (target, context)
            self._action_callers[context.action_id] = caller_device_id
        try:
            response = await client.request_async(
                ActionType.DEVICE_CALL,
                _device_call_payload(
                    caller_device_id, target, action_name, arguments, context.action_id, test_mode
                ),
                timeout=options.get("timeout"),
            )
        except asyncio.CancelledError:
            context.request_cancel()
            try:
                await asyncio.shield(self.cancel_action_async(context.action_id))
            except Exception as exc:  # noqa: BLE001 - cancellation must propagate
                logger.warning(
                    "[HostLink backend] 异步动作取消转发失败：%s (%s)",
                    context.action_id,
                    exc,
                )
            raise
        finally:
            with self._actions_lock:
                self._actions.pop(context.action_id, None)
                self._action_callers.pop(context.action_id, None)
        if isinstance(response, dict) and response.get("status") == "cancelled":
            context.request_cancel()
            raise ActionCancelled(f"action cancelled: {context.action_id}")
        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    def cancel_action(self, action_id: str) -> bool:
        with self._actions_lock:
            active = self._actions.get(str(action_id))
        if active is None:
            return False
        device_id, context = active
        context.request_cancel()
        if self.server is not None and device_id not in self.local.devices:
            response = self.server.cancel_device_action(device_id, context.action_id)
            return bool((response or {}).get("accepted"))
        client = self.client
        if client is not None and device_id not in self.local.devices:
            with self._actions_lock:
                caller_device_id = self._action_callers.get(context.action_id, "")
            response = client.request(
                ActionType.ACTION_CANCEL,
                {
                    "caller_device_id": caller_device_id,
                    "device_id": device_id,
                    "action_id": context.action_id,
                },
            )
            return bool((response or {}).get("accepted"))
        return True

    async def cancel_action_async(self, action_id: str) -> bool:
        """Cancel an active action using the same non-blocking RPC path."""

        with self._actions_lock:
            active = self._actions.get(str(action_id))
        if active is None:
            return False
        device_id, context = active
        context.request_cancel()
        if self.server is not None and device_id not in self.local.devices:
            response = await self.server.cancel_device_action_async(
                device_id,
                context.action_id,
            )
            return bool((response or {}).get("accepted"))
        client = self.client
        if client is not None and device_id not in self.local.devices:
            with self._actions_lock:
                caller_device_id = self._action_callers.get(
                    context.action_id,
                    "",
                )
            response = await client.request_async(
                ActionType.ACTION_CANCEL,
                {
                    "caller_device_id": caller_device_id,
                    "device_id": device_id,
                    "action_id": context.action_id,
                },
            )
            return bool((response or {}).get("accepted"))
        return True

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

    def request_stop(self) -> None:
        """Ask the backend entrypoint to leave its wait loop and clean up."""

        self.local.request_stop()

    def stop(self) -> None:
        self.local.remove_device_change_listener(self._on_local_device_change)
        self.local.topic_bus.remove_outbound_listener(self._on_local_topic)
        self.local.topic_bus.remove_subscription_listener(
            self._on_local_subscription_change
        )
        for node in self.local.devices.values():
            node.remove_status_listener(self._on_local_status)
        client, self.client = self.client, None
        if client is not None:
            client.close()
            set_hostlink_client(None)
        server, self.server = self.server, None
        if server is not None:
            server.stop()
            set_hostlink_server(None)
        self.local.stop()
        self._io_executor.shutdown(wait=False, cancel_futures=True)
        self._topic_executor.shutdown(wait=False, cancel_futures=True)
        self._started = False


__all__ = ["HostLinkBackend", "to_wire_value"]
