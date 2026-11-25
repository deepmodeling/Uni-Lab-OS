"""ROS2 形态的 Host 执行编排节点。

「host_node」在系统中拆成两层：

1. **host_node 服务设备** —— :class:`unilabos.client.host.host_services.HostServices`
   是唯一的动作定义源（registry 单独扫描 ``client/host/host_services.py``），
   ROS2 形态经通用管线 ``initialize_device_from_dict`` 从外部初始化；
2. **执行编排（本类）** —— DDS 设备发现、ActionClient 派发、设备状态订阅、
   slave 上报处理、控制器初始化。HostLink 对应物是
   :class:`unilabos.client.host.hostlink.host_node.HostNode`，两者共享
   :class:`HostAdapterBase` 的簿记/桥接通知/ping-pong/test_mode 逻辑。

物料/设备管理下行是 :mod:`unilabos.client.host.hostlink.downlink` 的模块级函数，
不挂在本类上。微后端只依赖 ``adapter_registry.get_execution_adapter()`` 契约。
"""

from __future__ import annotations

import collections
import json
import threading
import time
import traceback
import uuid

from typing import TYPE_CHECKING, Any, ClassVar, Dict, Iterable, List, Optional, Set, Tuple

import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient, get_action_server_names_and_types_by_node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from unilabos_msgs.action import StrSingleInput  # type: ignore
from unilabos_msgs.srv import SerialCommand  # type: ignore
from unique_identifier_msgs.msg import UUID

from unilabos.client.host.hostlink.adapter_registry import set_execution_adapter
from unilabos.client.host.ros2.msgs.message_converter import (
    convert_from_ros_msg,
    convert_to_ros_msg,
    get_msg_type,
    get_ros_type_by_msgname,
    msg_converter_manager,
)
from unilabos.client.host.ros2.presets.controller_node import ControllerNode
from unilabos.client.host.runtime.definition import is_host_node_config
from unilabos.client.host.runtime.exception import DeviceClassInvalid, DeviceMaintenanceError, DeviceStartupError
from unilabos.client.host.runtime.host_adapter import (
    HostAdapterBase,
    execution_result_bridges,
)
from unilabos.client.host.execution_start import ExecutionNotStarted
from unilabos.config.config import BasicConfig, HOST_NODE_REGISTRY_NAME
from unilabos.registry.action_policy import SUCCESS_TYPE_CANCELLATION, SUCCESS_TYPE_EXECUTION_UNKNOWN
from unilabos.resources.resource_tracker import (
    JSON_UNILABOS_PARAM,
    PARAM_SAMPLE_UUIDS,
    RETURN_UNILABOS_SAMPLES,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.utils import logger
from unilabos.utils.serialization import serialize_result_info

if TYPE_CHECKING:
    from unilabos.server.execution_queue import QueueItem

#: 设备 Action server 尚未出现在 ROS 图上时的等待上限（秒）。
ACTION_SERVER_WAIT_S = 30.0
#: Slave 的 HostLink 连接状态巡检周期（秒）：连接断开 / 心跳超时后它的设备随即离线，
#: 不必等 DDS 租约过期和下一轮 DDS 发现。
HOSTLINK_LIVENESS_INTERVAL_S = 1.0


def authority_host_node_uuid(device_id: str) -> str:
    """物料权威里已有的同名 host node 物料 uuid；没有、不是 host node 或查不到时返回空串。

    图里没声明 host node 时 Host 每次启动都会插入一棵默认树。沿用权威已有的身份，
    重启时就不会再拿新 uuid 去创建同一个 ``resource_id``（权威按 resource_id 唯一）。
    0.13.0a1 建的库里这个 uuid 是随机的，只能按 resource_id 查，不能靠推导。
    """

    from unilabos.resources.materials import resolve_materials_gateway

    try:
        gateway = resolve_materials_gateway()
    except RuntimeError:
        return ""
    try:
        aggregate = gateway.get_material_by_resource_id(device_id)
    except Exception:  # noqa: BLE001 - 查不到即视为权威尚无该 host node
        return ""
    material = getattr(aggregate, "material", None)
    if material is None:
        return ""
    if str(getattr(material, "template_name", "") or "").casefold() != HOST_NODE_REGISTRY_NAME:
        return ""
    if getattr(material, "parent_material_uuid", None):
        return ""
    return str(getattr(material, "material_uuid", "") or "")


def ensure_host_node_resource(
    resources_config: ResourceTreeSet, device_id: str
) -> ResourceDictInstance:
    """返回本机 host 服务设备的资源根节点，必要时插入默认定义。

    设备图可以显式声明 host node（按 ``template_name == "host_node"`` 判别，
    且全图只能有一个）；声明时复用图中的 uuid 等身份信息，实例 id 统一为
    配置的 ``--host_node_id``。未声明时按默认值创建一棵新树：物料权威已有
    同名 host node 时沿用它的 uuid，否则新发一个。
    """

    host_roots = [
        tree.root_node
        for tree in resources_config.trees
        if is_host_node_config(tree.root_node.res_content, device_id)
    ]
    if len(host_roots) > 1:
        raise ValueError(
            "图中只能声明一个 host node（template_name=host_node），当前有: "
            + ", ".join(root.res_content.id for root in host_roots)
        )
    if host_roots:
        root = host_roots[0]
        content = root.res_content
        if content.id != device_id:
            logger.info(
                f"[Host Node] 图中 host node id '{content.id}' 与配置实例名 "
                f"'{device_id}' 不一致，以配置为准"
            )
            if content.display_name in ("", content.id):
                content.display_name = device_id
            content.id = device_id
        content.type = "device"
        # 设备实例的 template_name 必须与注册表名一致（缺省会回退到
        # type="device"），否则通用管线的 Site 校验会拒绝；class 仅作旧字段镜像。
        content.template_name = HOST_NODE_REGISTRY_NAME
        content.klass = HOST_NODE_REGISTRY_NAME
        # host 服务设备没有位点；声明权威空快照以通过「Site 必须由微后端
        # 实例化」校验。
        content.sites_initialized = True
        if content.sites is None:
            content.sites = []
        return root

    existing_uuid = authority_host_node_uuid(device_id)
    if existing_uuid:
        logger.info(f"[Host Node] 沿用物料权威中 {device_id} 的身份 {existing_uuid}")
    host_node_dict = {
        "id": device_id,
        "uuid": existing_uuid or str(uuid.uuid4()),
        "parent_uuid": "",
        # 物料 name 等于 uuid（ResourceDict 缺省补齐），人读标签是 display_name
        "display_name": device_id,
        "type": "device",
        "class": HOST_NODE_REGISTRY_NAME,
        "template_name": HOST_NODE_REGISTRY_NAME,
        "sites_initialized": True,
        "config": {},
        "data": {},
        "description": "",
        "schema": {},
        "model": {},
        "icon": "",
    }
    instance = ResourceDictInstance.get_resource_instance_from_dict(host_node_dict)
    resources_config.trees.insert(0, ResourceTreeInstance(instance))
    return instance


def iter_host_managed_device_configs(
    devices_config: ResourceTreeSet, host_device_id: str
):
    """迭代 host 负责初始化的设备配置。

    根节点逐一给出；host node 树本体（按 template_name 判别）由构造函数提前
    初始化，此处改为下发其直接子设备（图中挂在 host 下的设备）。
    """

    for root in devices_config.root_nodes:
        if not is_host_node_config(root.res_content, host_device_id):
            yield root
            continue
        for child in root.children:
            if child.res_content.type == "device":
                yield child


class HostNode(HostAdapterBase, Node):
    """ROS2 的 host 执行编排节点（单例）。

    本类是纯编排节点（非设备节点）：host_node 服务设备经通用管线从外部
    初始化，占据 ``/devices/<device_id>`` 命名空间；本类以
    ``host_coordinator`` 名义加入全局 executor。
    """

    supports_execution_start_guard = True

    _instance: ClassVar[Optional["HostNode"]] = None
    _ready_event: ClassVar[threading.Event] = threading.Event()
    _shutting_down: ClassVar[bool] = False
    _background_threads: ClassVar[List[threading.Thread]] = []

    @classmethod
    def get_instance(cls, timeout=None) -> Optional["HostNode"]:
        if cls._ready_event.wait(timeout):
            return cls._instance
        return None

    @classmethod
    def shutdown_background_threads(cls, timeout: float = 5.0) -> None:
        """优雅关闭所有后台线程（退出或重启前调用）。"""
        cls._shutting_down = True
        active_threads = []
        for t in cls._background_threads:
            if t.is_alive():
                t.join(timeout=timeout)
                if t.is_alive():
                    active_threads.append(t.name)
        if active_threads:
            logger.warning(f"[Host Node] Some background threads still running: {active_threads}")
        cls._background_threads.clear()
        logger.info("[Host Node] Background threads shutdown complete")

    @classmethod
    def reset_state(cls) -> None:
        """重置单例状态（销毁实例后调用，供重启/干净退出）。"""
        cls._instance = None
        cls._ready_event.clear()
        cls._shutting_down = False
        cls._background_threads.clear()
        logger.info("[Host Node] State reset complete")

    def __init__(
        self,
        device_id: str,
        devices_config: ResourceTreeSet,
        resources_config: ResourceTreeSet,
        physical_setup_graph: Optional[Dict[str, Any]] = None,
        controllers_config: Optional[Dict[str, Any]] = None,
        bridges: Optional[List[Any]] = None,
        discovery_interval: float = 180.0,
    ):
        """初始化 ROS2 主机编排节点。

        Args:
            device_id: host_node 服务设备的 device_id
            devices_config: 设备配置
            resources_config: 资源配置
            physical_setup_graph: 物理设置图
            controllers_config: 控制器配置
            bridges: 桥接器列表
            discovery_interval: DDS 设备发现间隔秒
        """
        if self._instance is not None:
            self._instance.lab_logger().critical("[Host Node] HostNode instance already exists.")
        self.__class__._instance = self

        # 共享簿记（devices_names / device_status / ping / goal 状态等）
        HostAdapterBase.__init__(self, bridges=bridges)

        self.device_id = device_id
        self.devices_config = devices_config
        self.resources_config = resources_config
        self.physical_setup_graph = physical_setup_graph
        self.controllers_config = controllers_config or {}

        # 手动构造 Node 父类（与 main_slave_run 的初始化模式一致）。
        Node.__init__(self, "host_coordinator")
        self.namespace = f"/devices/{self.device_id}"
        self.callback_group = ReentrantCallbackGroup()

        # host_node 资源树：优先复用图中声明的节点（uuid 等身份保持稳定，
        # 支持多 host node 的图描述），未声明时插入运行时默认定义。
        host_node_instance = ensure_host_node_resource(
            self.resources_config, self.device_id
        )

        # 创建设备、动作客户端和目标存储
        self.devices_instances: Dict[str, Any] = {}
        self._action_clients: Dict[str, Any] = {}
        self._slave_registry_configs: Dict[str, Dict] = {}  # registry_name -> registry_config
        self._discovery_lock = threading.Lock()
        # 离线簿记：DDS 上消失的远端设备、HostLink 连接已断开的 Slave 上报的设备。
        # 能力快照据此剔除离线设备（与 HostLink backend 只上报在线设备一致）。
        self._liveness_lock = threading.Lock()
        self._dds_offline_devices: Set[str] = set()
        self._hostlink_offline_devices: Set[str] = set()
        self._hostlink_owned_devices: Set[str] = set()
        # 受管 Slave 进程没在运行的设备及原因（unavailable_devices）的上一版，变了就重发能力快照
        self._unavailable_fingerprint = "{}"
        # 已发出、尚未收尾的 goal（job_id -> QueueItem）：设备离线时据此收尾，
        # 不让作业和设备锁一直挂着；_settled_jobs 记下已收尾的，忽略它们迟到的回调。
        self._goal_items: Dict[str, Any] = {}
        self._settled_jobs: Set[str] = set()

        # host_node 服务设备（backend 无关的 HostServices）从外部初始化：
        # 与其他设备走同一条通用管线，ActionServer / 注册表登记由包装节点
        # 完成；物料下行通道由 HostServices 默认落 hostlink.downlink。
        self.initialize_device(self.device_id, host_node_instance)
        if self.device_id not in self.devices_instances:
            raise RuntimeError(f"host_node 服务设备初始化失败: {self.device_id}")
        # 物料必须挂在设备下（host 名下物料 parent 即 host_node 设备），
        # 编排器不自持 tracker，直接使用 host_node 设备节点的那份。
        # noinspection PyProtectedMember
        self.resource_tracker = self.devices_instances[self.device_id]._ros_node.resource_tracker

        # 创建物料增删改查服务（非客户端）
        self._init_host_service()

        time.sleep(1)  # 等待通信连接稳定
        # 首次发现网络中的设备
        self._discover_devices()

        # 初始化所有本机设备节点，多一次过滤，防止重复初始化
        local_machine = BasicConfig.machine_name
        for device_config in iter_host_managed_device_configs(
            self.devices_config, self.device_id
        ):
            dev_id = device_config.res_content.id
            if dev_id == self.device_id:
                continue  # host 服务设备已在构造早期初始化
            if device_config.res_content.type != "device":
                continue
            dev_machine = device_config.res_content.machine_name
            if dev_machine and local_machine and dev_machine != local_machine:
                self.lab_logger().info(
                    f"[Host Node] Device {dev_id} belongs to machine '{dev_machine}', "
                    f"local is '{local_machine}', skipping initialization."
                )
                continue
            if dev_id not in self.devices_names:
                self.initialize_device(dev_id, device_config)
            else:
                self.lab_logger().warning(f"[Host Node] Device {dev_id} already existed, skipping.")
        self.update_device_status_subscriptions()
        # 控制器继承 controller_manager 的统一更新频率。
        if self.controllers_config:
            update_rate = self.controllers_config["controller_manager"]["ros__parameters"]["update_rate"]
            for controller_id, controller_config in self.controllers_config["controller_manager"][
                "ros__parameters"
            ]["controllers"].items():
                controller_config["update_rate"] = update_rate
                self.initialize_controller(controller_id, controller_config)

        # 创建定时器，定期发现设备
        self._discovery_timer = self.create_timer(
            discovery_interval, self._discovery_devices_callback, callback_group=self.callback_group
        )
        self._hostlink_liveness_timer = self.create_timer(
            HOSTLINK_LIVENESS_INTERVAL_S, self._hostlink_liveness_callback, callback_group=self.callback_group
        )

        # 编排节点自身加入全局 executor（设备节点由各自包装类加入）
        rclpy.get_global_executor().add_node(self)

        self.lab_logger().info("[Host Node] Host node initialized (backend=ros2).")
        HostNode._ready_event.set()
        set_execution_adapter(self)
        self.notify_ready()

    # ------------------------------------------------------------------
    # transport 契约实现
    # ------------------------------------------------------------------

    def send_goal(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, Any],
        server_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._goal_state_lock:
            if item.job_id in self._canceled_jobs:
                self.lab_logger().info(f"[Host Node] Skip canceled goal {item.job_id[:8]}")
                return
        if item.action_name == "test_latency" and server_info is not None:
            self.server_latest_timestamp = float(server_info.get("send_timestamp", 0.0))
        self._send_goal_ros2(item, action_type, action_kwargs, sample_material)

    def cancel_job(self, job_id: str) -> bool:
        """取消运行中或仍在等待 ROS Goal 响应的执行。"""
        with self._goal_state_lock:
            goal_handle = self._goals.get(job_id)
            is_inflight = job_id in self._inflight_goal_jobs
            if goal_handle is None and not is_inflight:
                self.lab_logger().warning(f"[Host Node] Job {job_id[:8]} not found, cannot cancel")
                return False
            self._canceled_jobs.add(job_id)

        if goal_handle is not None:
            self.lab_logger().info(f"[Host Node] Cancelling goal {job_id[:8]}")
            self._request_goal_cancel(job_id, goal_handle)
        else:
            self.lab_logger().info(
                f"[Host Node] Marked in-flight goal {job_id[:8]} canceled before acceptance"
            )
        return True

    def get_goal_status(self, job_id: str) -> int:
        if job_id in self._goals:
            status = self._goals[job_id].status
            self.lab_logger().debug(f"[Host Node] Goal status for {job_id}: {status}")
            return status
        if job_id in self._inflight_goal_jobs:
            return GoalStatus.STATUS_EXECUTING
        self.lab_logger().warning(f"[Host Node] Goal {job_id} not found, status unknown")
        return GoalStatus.STATUS_UNKNOWN

    # ------------------------------------------------------------------
    # DDS 发现 + 设备初始化
    # ------------------------------------------------------------------

    def _send_re_register(self, sclient, device_namespace: str):
        """向设备发送一次性 re-register 指令。"""
        try:
            if not sclient.wait_for_service(timeout_sec=10.0):
                self.lab_logger().debug(f"[Host Node] Re-register timeout for {device_namespace}")
                return
            if self._shutting_down:
                self.lab_logger().debug(f"[Host Node] Re-register aborted for {device_namespace} (shutdown)")
                return
            request = SerialCommand.Request()
            request.command = ""
            future = sclient.call_async(request)
            future.result()
        except Exception as e:
            if "destruction was requested" in str(e) or self._shutting_down:
                self.lab_logger().debug(f"[Host Node] Re-register aborted for {device_namespace} (cleanup)")
            else:
                self.lab_logger().warning(f"[Host Node] Re-register failed for {device_namespace}: {e}")

    def _discover_devices(self) -> None:
        """发现 ROS2 网络中的设备节点，创建 ActionClient 并检测离线。"""
        self.lab_logger().trace("[Host Node] Discovering devices in the network...")

        nodes_and_names = self.get_node_names_and_namespaces()
        current_devices = set()
        previous_device_names = set(self.devices_names)
        previous_online_devices = set(self._online_devices)

        for device_id, namespace in nodes_and_names:
            if not namespace.startswith("/devices/"):
                continue
            edge_device_id = namespace[9:]
            device_key = f"{namespace}/{edge_device_id}"  # namespace已经包含device_id了，这里复写一遍
            current_devices.add(device_key)

            if edge_device_id not in self.devices_names:
                self.lab_logger().info(f"[Host Node] Discovered new device: {edge_device_id}")
                self.devices_names[edge_device_id] = namespace
                self._create_action_clients_for_device(device_id, namespace)
                self._online_devices.add(device_key)
                self._spawn_re_register(namespace)
            elif device_key not in self._online_devices:
                self.lab_logger().info(f"[Host Node] Device reconnected: {device_key}")
                self._online_devices.add(device_key)

        for device_key in previous_online_devices - current_devices:
            self.lab_logger().warning(f"[Host Node] Device offline: {device_key}")
            self._online_devices.discard(device_key)

        self._online_devices = current_devices
        absent = self._remote_devices_absent_from(current_devices)
        with self._liveness_lock:
            newly_absent = absent - self._dds_offline_devices
            self._dds_offline_devices = absent
            hostlink_owned = set(self._hostlink_owned_devices)
        # 经 HostLink 接入的 Slave 设备由连接状态收尾在途作业（断开即离线，不等 DDS 租约）；
        # 没经 HostLink 接入的（如 --disable_hostlink）只能按 DDS 判定
        dds_only = newly_absent - hostlink_owned
        if dds_only:
            self._settle_jobs_on_offline_devices(dds_only, reason="设备已不在 ROS 图上（DDS 发现超时）")
        if previous_device_names != set(self.devices_names) or previous_online_devices != current_devices:
            self._notify_capabilities_changed()
        self.lab_logger().trace(f"[Host Node] Total online devices: {len(self._online_devices)}")

    def _remote_devices_absent_from(self, current_devices: Set[str]) -> Set[str]:
        """发现过、但这一轮 DDS 发现里不在 ROS 图上的远端设备（本机设备随进程存亡，不算）。"""

        return {
            device_id
            for device_id, namespace in self.devices_names.items()
            if device_id not in self.devices_instances and f"{namespace}/{device_id}" not in current_devices
        }

    @property
    def offline_device_ids(self) -> Set[str]:
        """离线的远端设备：DDS 上消失，或上报它的 Slave 的 HostLink 连接已断开 / 心跳超时。

        能力快照（``build_endpoint_capabilities``）剔除这些设备，权威的 ``/devices``、
        ``/online-devices`` 与调度随之把它们当离线；派发给它们的动作直接失败。
        """

        with self._liveness_lock:
            offline = self._dds_offline_devices | self._hostlink_offline_devices
        return offline - set(self.devices_instances)

    @property
    def unavailable_devices(self) -> Dict[str, Dict[str, Any]]:
        """所在受管 Slave 进程没在运行（崩溃 / 退避重启 / 已停止）的设备及原因。

        能力快照把其中不在线的设备登记为停用 route，权威的 ``/devices`` 据此显示不可用及原因。
        """

        from unilabos.server.services.device_processes import managed_device_unavailability

        records = managed_device_unavailability()
        # 进入维护模式的本机设备（持有的物料建不成 PLR 实例、装配 / 构造失败）
        records.update(self.__dict__.get("_maintenance_devices") or {})
        return records

    def _hostlink_liveness_callback(self) -> None:
        """按 HostLink 连接状态更新 Slave 设备的在线状态。

        Slave 进程退出、被杀或心跳超时后，HostLink 立即（或在心跳超时内）知道；DDS 要等
        参与者租约过期，再等下一轮设备发现。重连后、Slave 报告设备就绪（设备节点已建好）时撤销离线
        标记，DDS 仍看得到设备即恢复在线。受管 Slave 进程的状态（崩溃 / 重启中）变化时也重发能力快照。
        """

        from unilabos.client.host.hostlink.server import get_hostlink_server

        server = get_hostlink_server()
        if server is None:
            return
        online_ids: Set[str] = set()
        disconnected_ids: Set[str] = set()
        starting_ids: Set[str] = set()
        for peer in server.peers():
            descriptors = peer.get("devices") or {}
            for device_id in peer.get("device_ids") or []:
                # Slave 声明「未就绪」的设备（重启后刚接回 HostLink、设备节点还没建好）仍按离线算：
                # DDS 里可能还留着旧进程的 Action server，这时派发的 goal 没有接收方。不带描述的旧 Slave 按就绪算。
                ready = (descriptors.get(device_id) or {}).get("ready", True) is not False
                if not peer.get("online"):
                    disconnected_ids.add(str(device_id))
                elif ready:
                    online_ids.add(str(device_id))
                else:
                    starting_ids.add(str(device_id))
        # 同一设备由在线的 Slave 上报（如换了连接重新接入）时以在线为准
        disconnected_ids -= online_ids
        starting_ids -= online_ids | disconnected_ids
        offline_ids = disconnected_ids | starting_ids
        with self._liveness_lock:
            previous = self._hostlink_offline_devices
            self._hostlink_offline_devices = offline_ids
            self._hostlink_owned_devices = online_ids | offline_ids
        unavailable = getattr(self, "unavailable_devices", None) or {}
        fingerprint = json.dumps(unavailable, ensure_ascii=False, sort_keys=True, default=str)
        unavailable_changed = fingerprint != getattr(self, "_unavailable_fingerprint", "{}")
        self._unavailable_fingerprint = fingerprint
        if offline_ids == previous:
            if unavailable_changed:
                self._notify_capabilities_changed()
            return
        went_offline = offline_ids - previous
        came_back = previous - offline_ids
        if went_offline & disconnected_ids:
            self.lab_logger().warning(
                f"[Host Node] Slave 的 HostLink 连接已断开或心跳超时，设备离线: {sorted(went_offline & disconnected_ids)}"
            )
            self._settle_jobs_on_offline_devices(
                went_offline & disconnected_ids, reason="Slave 的 HostLink 连接已断开或心跳超时，设备离线"
            )
        if went_offline & starting_ids:
            # 首次接入，或进程重启得太快、两次检查之间没看到断开：旧进程上的在途作业同样收尾
            self.lab_logger().info(
                f"[Host Node] Slave 已接入 HostLink，等待设备节点就绪: {sorted(went_offline & starting_ids)}"
            )
            self._settle_jobs_on_offline_devices(
                went_offline & starting_ids, reason="Slave 已重新接入但设备尚未就绪（进程已重启），设备离线"
            )
        if came_back:
            self.lab_logger().info(f"[Host Node] Slave 已接入 HostLink 且设备就绪，恢复在线: {sorted(came_back)}")
        self._notify_capabilities_changed()

    def _settle_jobs_on_offline_devices(self, device_ids: Iterable[str], *, reason: str) -> None:
        """设备离线时收尾发给它的在途作业，释放作业与设备锁。

        已请求取消的按取消收尾（Slave 已不在，动作不会再跑）；其余按结果未知收尾，
        交给核实流程（与 HostLink 传输中断的口径一致）。迟到的回调会被忽略。
        """

        targets = set(device_ids)
        with self._goal_state_lock:
            pending = [
                (job_id, item, job_id in self._canceled_jobs)
                for job_id, item in self._goal_items.items()
                if item.device_id in targets
            ]
            for job_id, _item, _canceled in pending:
                self._goal_items.pop(job_id, None)
                self._settled_jobs.add(job_id)
        for job_id, item, canceled in pending:
            if canceled:
                return_info = serialize_result_info(
                    f"Job was cancelled; {reason}", False, {}, suc_type=SUCCESS_TYPE_CANCELLATION
                )
            else:
                return_info = serialize_result_info(reason, False, {}, suc_type=SUCCESS_TYPE_EXECUTION_UNKNOWN)
                return_info["uncertainty_reason"] = reason
            self.lab_logger().warning(
                f"[Host Node] {item.device_id}/{item.action_name} ({job_id[:8]}) 的设备已离线，"
                f"作业按{'取消' if canceled else '结果未知'}收尾"
            )
            self._publish_terminal_result(item, "failed", return_info, {})

    def _publish_terminal_result(
        self,
        item: "QueueItem",
        status: str,
        return_info: Dict[str, Any],
        result_data: Dict[str, Any],
    ) -> None:
        with self._goal_state_lock:
            self._goal_items.pop(item.job_id, None)
        super()._publish_terminal_result(item, status, return_info, result_data)

    def _take_settled(self, job_id: str) -> bool:
        """作业是否已因设备离线收尾（是则消费掉这条记录，调用方忽略迟到的回调）。"""

        with self._goal_state_lock:
            if job_id in self._settled_jobs:
                self._settled_jobs.discard(job_id)
                return True
            return False

    def _spawn_re_register(self, namespace: str) -> None:
        sclient = self.create_client(SerialCommand, f"/srv{namespace}/re_register_device")
        t = threading.Thread(
            target=self._send_re_register,
            args=(sclient, namespace),
            daemon=True,
            name=f"ROSDevice{self.device_id}_re_register_device_{namespace}",
        )
        self._background_threads.append(t)
        t.start()

    def _discovery_devices_callback(self) -> None:
        """设备发现定时器回调。"""
        if self._discovery_lock.acquire(blocking=False):
            try:
                self._discover_devices()
                self.update_device_status_subscriptions()
            finally:
                self._discovery_lock.release()
        else:
            self.lab_logger().debug("[Host Node] Device discovery already in progress, skipping.")

    def _create_action_clients_for_device(self, device_id: str, namespace: str) -> None:
        """为（DDS 发现的）设备创建所有必要的 ActionClient。"""
        new_action_pairs: List[Tuple[str, str]] = []
        edge_device_id = namespace[9:]
        for action_id, action_types in get_action_server_names_and_types_by_node(self, device_id, namespace):
            if action_id not in self._action_clients:
                try:
                    action_type = get_ros_type_by_msgname(action_types[0])
                    self._action_clients[action_id] = ActionClient(
                        self, action_type, action_id, callback_group=self.callback_group
                    )
                    self.lab_logger().trace(f"[Host Node] Created ActionClient (Discovery): {action_id}")
                    action_name = action_id[len(namespace) + 1:]
                    new_action_pairs.append((edge_device_id, action_name))
                except Exception as e:
                    self.lab_logger().error(f"[Host Node] Failed to create ActionClient for {action_id}: {str(e)}")

        # 补充 _action_value_mappings 中其余动作：UniLabJsonCommand 类型动作不建独立
        # ROS ActionServer；auto- 动作同理。它们仍是可经 _execute_driver_command 调用
        # 的能力，发现新设备时必须全量补报其 free 锁。
        already = {action_name for _, action_name in new_action_pairs}
        for action_name in self._action_value_mappings.get(edge_device_id, {}).keys():
            if action_name in already:
                continue
            new_action_pairs.append((edge_device_id, action_name))
        self._report_action_locks_free(new_action_pairs)

    def initialize_device(self, device_id: str, device_config: ResourceDictInstance) -> None:
        """根据配置初始化本机设备（经 ROS2 通用管线），并创建动作客户端。"""
        from unilabos.client.host.ros2 import initialize_device as initializer

        self.lab_logger().info(f"[Host Node] Initializing device: {device_id}")

        # Host 进程里的设备：装配 / 构造失败只让这台设备进入维护模式，Host 与其余设备照常。
        # 自己有 Slave 的设备（--root_devices_as_slaves）不经这里，失败照旧由 Slave 退出与重启策略处理。
        maintenance: Optional[DeviceMaintenanceError] = None
        failed = object()

        def attempt(stage: str, step: Any) -> Any:
            nonlocal maintenance
            try:
                return step()
            except DeviceMaintenanceError as e:
                maintenance = e
            except DeviceClassInvalid as e:
                self.lab_logger().error(f"[Host Node] Device class invalid: {e}")
            except Exception as e:  # noqa: BLE001 - 见上
                self.lab_logger().error(f"[Host Node] 设备 {device_id} 启动失败: {e}\n{traceback.format_exc()}")
                maintenance = DeviceStartupError(stage=stage, error=e)
            return failed

        d = None
        definition = attempt(
            "definition", lambda: initializer.resolve_device_definition(device_id, device_config, backend_name="ros2")
        )
        if definition is not failed:
            d = attempt(
                "construct", lambda: initializer.initialize_device_from_definition(device_id, device_config, definition)
            )
            if d is failed:
                d = None
            elif d is None:
                # 驱动实例创建返回空（DeviceInitError）
                maintenance = DeviceStartupError(stage="construct", error=RuntimeError("设备实例创建失败"))
        if maintenance is not None:
            # 维护模式：设备不启动；与 HostLink 同一份不可用记录
            maintenance.device_id = device_id
            self.__dict__.setdefault("_maintenance_devices", {})[device_id] = {
                "device_id": device_id,
                "registry_name": str(device_config.res_content.template_name or ""),
                "backend": "ros2",
                **maintenance.unavailable_record(),
            }
            self.lab_logger().error(f"[Host Node] {maintenance}")
        if d is None:
            return
        self._register_local_device(device_id, d)

    def _register_local_device(self, device_id: str, d: Any) -> None:
        """登记已初始化的设备及其工作站子设备，不依赖 DDS 延后发现本机子节点。

        工作站负责构造子设备，但 Host 同样需要它们的动作声明、派发客户端和锁状态。
        只登记父工作站会让 endpoint 快照漏报子动作，也会使工作流导入/调度无法寻址。
        """

        existing = self.devices_instances.get(device_id)
        if existing is d:
            return
        if existing is not None:
            raise ValueError(f"本机设备身份重复：{device_id}")
        # noinspection PyProtectedMember
        self.devices_names[device_id] = d._ros_node.namespace  # 这里不涉及二级device_id
        self.device_machine_names[device_id] = "本地"
        self.devices_instances[device_id] = d
        # noinspection PyProtectedMember
        self._action_value_mappings[device_id] = d._ros_node._action_value_mappings
        new_action_pairs: List[Tuple[str, str]] = []
        # 仅为建独立 ROS ActionServer 的动作创建 ActionClient：
        # auto-/UniLabJsonCommand 动作无 ROS action server，无法也无需建 ActionClient。
        # noinspection PyProtectedMember
        for action_name, action_value_mapping in d._ros_node._action_value_mappings.items():
            if action_name.startswith("auto-") or str(action_value_mapping.get("type", "")).startswith(
                "UniLabJsonCommand"
            ):
                continue
            action_id = f"/devices/{device_id}/{action_name}"
            if action_id not in self._action_clients:
                action_type = action_value_mapping["type"]
                try:
                    self._action_clients[action_id] = ActionClient(self, action_type, action_id)
                except Exception as e:
                    self.lab_logger().error(
                        f"创建ActionClient失败，Device: {device_id}, Action Name: {action_name}, "
                        f"Action Type: {action_type}, Error: {e}"
                    )
                    continue
                self.lab_logger().trace(f"[Host Node] Created ActionClient (Local): {action_id}")
                new_action_pairs.append((device_id, action_name))
            else:
                self.lab_logger().warning(f"[Host Node] ActionClient {action_id} already exists.")
        # 锁上报需全量：auto-/UniLabJsonCommand 动作虽不建 ActionClient，但仍是可经
        # _execute_driver_command 调用的能力，必须一并上报 free 锁。
        # noinspection PyProtectedMember
        already = {action_name for _, action_name in new_action_pairs}
        for action_name in d._ros_node._action_value_mappings.keys():
            if action_name in already:
                continue
            new_action_pairs.append((device_id, action_name))
        device_key = f"{self.devices_names[device_id]}/{device_id}"  # 这里不涉及二级device_id
        self._online_devices.add(device_key)
        self._report_action_locks_free(new_action_pairs)
        for child_id, child in getattr(d._ros_node, "sub_devices", {}).items():
            if child is not None:
                self._register_local_device(child_id, child)

    def update_device_status_subscriptions(self) -> None:
        """扫描所有设备话题，为新话题创建订阅（不重复订阅）。"""
        topic_names_and_types = self.get_topic_names_and_types()
        for topic, types in topic_names_and_types:
            if (
                topic.startswith("/devices/")
                and not types[0].endswith("FeedbackMessage")
                and "_action" not in topic
                and topic not in self._subscribed_topics
            ):
                parts = topic.split("/")
                if len(parts) >= 4:  # 可能有WorkstationNode，创建更长的设备
                    device_id = "/".join(parts[2:-1])
                    property_name = parts[-1]

                    if device_id not in self.device_status:
                        self.device_status[device_id] = {}
                        self.device_status_timestamps[device_id] = {}

                    self.device_status[device_id] = collections.defaultdict()
                    self.device_status_timestamps[device_id][property_name] = 0

                    try:
                        type_class = msg_converter_manager.search_class(types[0].replace("/", "."))
                        if type_class is None:
                            self.lab_logger().error(f"[Host Node] Invalid type {types[0]} for {topic}")
                        else:
                            self.create_subscription(
                                type_class,
                                topic,
                                lambda msg, d=device_id, p=property_name: self.property_callback(msg, d, p),
                                1,
                                callback_group=self.callback_group,
                            )
                            self._subscribed_topics.add(topic)
                            self.lab_logger().trace(f"[Host Node] Subscribed to new topic: {topic}")
                    except (NameError, SyntaxError) as e:
                        self.lab_logger().error(f"[Host Node] Failed to create subscription for topic {topic}: {e}")

    def property_callback(self, msg, device_id: str, property_name: str) -> None:
        """更新设备状态字典中的属性值，并发送到桥接器。"""
        if hasattr(msg, "data"):
            bChange = False
            bCreate = False
            if isinstance(msg.data, (float, int, str)):
                if property_name not in self.device_status[device_id]:
                    bCreate = True
                    bChange = True
                    self.device_status[device_id][property_name] = msg.data
                elif self.device_status[device_id][property_name] != msg.data:
                    bChange = True
                    self.device_status[device_id][property_name] = msg.data
                self.device_status_timestamps[device_id][property_name] = time.time()
            else:
                self.lab_logger().debug(
                    f"[Host Node] Unsupported data type for {device_id}/{property_name}: {type(msg.data)}"
                )

            if bChange:
                for bridge in self.bridges:
                    if hasattr(bridge, "publish_device_status"):
                        bridge.publish_device_status(self.device_status, device_id, property_name)
                        if bCreate:
                            self.lab_logger().trace(f"Status created: {device_id}.{property_name} = {msg.data}")
                        else:
                            self.lab_logger().trace(f"Status updated: {device_id}.{property_name} = {msg.data}")

    # ------------------------------------------------------------------
    # Goal 派发与回调
    # ------------------------------------------------------------------

    def _send_goal_ros2(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, Any],
    ) -> None:
        """向设备发送 ROS Goal。"""
        u = uuid.UUID(item.job_id)
        device_id = item.device_id
        action_name = item.action_name
        if not action_type:
            # 工作流节点可能不带 action_type（如 @workflow ctx.run 指向不在
            # host 启动图中的 slave 设备，构建时查不到类）。派发时以 host 持
            # 有的设备能力副本兜底：本地设备装配时写入，slave 设备经
            # node_info_update 上报镜像，二者均以动作裸名/auto- 名索引。
            fallback_mappings = self._action_value_mappings.get(device_id) or {}
            fallback_mapping = fallback_mappings.get(action_name) or fallback_mappings.get(f"auto-{action_name}")
            if isinstance(fallback_mapping, dict):
                raw_type = fallback_mapping.get("type")
                if isinstance(raw_type, str) and raw_type:
                    action_type = raw_type
                    self.lab_logger().info(
                        f"[Host Node] action_type 兜底解析: {device_id}/{action_name} -> {action_type}"
                    )
        if BasicConfig.test_mode:
            from unilabos.client.host.execution_start import confirm_execution_start
            confirm_execution_start(item)
            action_id = f"/devices/{device_id}/{action_name}"
            self.lab_logger().info(
                f"[TEST MODE] 模拟执行: {action_id} (job={item.job_id[:8]}), 参数: {str(action_kwargs)[:500]}"
            )
            mock_return = self._build_test_mode_return(device_id, action_name, action_kwargs)
            return_info = serialize_result_info("", True, mock_return)
            self.lab_logger().info(f"[TEST MODE] Result for {action_id} ({item.job_id[:8]}): success")
            self._publish_terminal_result(item, "success", return_info, mock_return)
            return

        if device_id in self.offline_device_ids:
            # 离线设备不等 Action server 超时：goal 没发出、许可没领用，是明确失败
            raise ExecutionNotStarted(
                f"设备 {device_id} 已离线（Slave 已断开或不在 ROS 图上），动作 {action_name} 未下发"
            )
        if action_type.startswith("UniLabJsonCommand"):
            if action_name.startswith("auto-"):
                action_name = action_name[5:]
            action_id = f"/devices/{device_id}/_execute_driver_command"
            json_command: Dict[str, Any] = {
                "function_name": action_name,
                "function_args": action_kwargs,
                JSON_UNILABOS_PARAM: {
                    PARAM_SAMPLE_UUIDS: sample_material,
                },
            }
            action_kwargs = {"string": json.dumps(json_command)}
            if action_type.startswith("UniLabJsonCommandAsync"):
                action_id = f"/devices/{device_id}/_execute_driver_command_async"
        else:
            action_id = f"/devices/{device_id}/{action_name}"
        if action_id not in self._action_clients:
            # UniLabJsonCommand 走设备节点固定存在的 _execute_driver_command[_async]
            # （StrSingleInput）server；client 只靠 DDS discovery 建立会引入时序竞态
            # （slave 远端设备/刚启动的设备可能尚未被扫描到），此处按固定类型现场
            # 补建，可达性交由下方带超时的 wait_for_server 判定。
            if action_id.endswith(("/_execute_driver_command", "/_execute_driver_command_async")):
                self._action_clients[action_id] = ActionClient(
                    self, StrSingleInput, action_id, callback_group=self.callback_group
                )
                self.lab_logger().info(f"[Host Node] Created ActionClient (OnDemand): {action_id}")
            else:
                raise ValueError(f"ActionClient {action_id} not found.")

        self._inflight_goal_jobs.add(item.job_id)
        try:
            action_client = self._action_clients[action_id]
            goal_msg = convert_to_ros_msg(action_client._action_type.Goal(), action_kwargs)

            self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {action_kwargs}")
            self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {goal_msg}")
            if not action_client.wait_for_server(timeout_sec=ACTION_SERVER_WAIT_S):
                # 还没进开始边界：许可没领用、goal 没发出，是明确失败而不是结果未知
                raise ExecutionNotStarted(
                    f"Action server {action_id} 在 {ACTION_SERVER_WAIT_S:.0f}s 内不可达"
                    "（设备离线或尚未完成启动），动作未下发"
                )
            goal_uuid_obj = UUID(uuid=list(u.bytes))

            from unilabos.client.host.execution_start import confirm_execution_start
            confirm_execution_start(item)
            with self._goal_state_lock:
                self._goal_items[item.job_id] = item
            future = action_client.send_goal_async(
                goal_msg,
                feedback_callback=lambda feedback_msg: self.feedback_callback(
                    item,
                    action_id,
                    feedback_msg,
                ),
                goal_uuid=goal_uuid_obj,
            )
        except Exception:
            self._inflight_goal_jobs.discard(item.job_id)
            with self._goal_state_lock:
                self._goal_items.pop(item.job_id, None)
            raise
        future.add_done_callback(
            lambda f: self.goal_response_callback(
                item,
                action_id,
                f,
            )
        )

    def goal_response_callback(self, item: "QueueItem", action_id: str, future) -> None:
        """目标响应回调。"""
        self._inflight_goal_jobs.discard(item.job_id)
        if self._take_settled(item.job_id):
            # 设备离线时已收尾：迟到的受理只请求取消，不再登记结果
            try:
                goal_handle = future.result()
                if goal_handle is not None and goal_handle.accepted:
                    self._request_goal_cancel(item.job_id, goal_handle)
            except Exception:  # noqa: BLE001 - 已收尾的作业不再上报
                pass
            self.lab_logger().info(f"[Host Node] Goal {item.job_id[:8]} 已因设备离线收尾，忽略迟到的受理")
            return
        try:
            goal_handle = future.result()
        except Exception as ex:  # noqa: BLE001 - 转成 job 终态失败
            self.lab_logger().error(
                f"[Host Node] Goal {item.action_name} ({item.job_id}) response failed: {ex}"
            )
            self._publish_terminal_result(
                item,
                "failed",
                serialize_result_info(traceback.format_exc(), False, {}),
                {},
            )
            return
        if not goal_handle.accepted:
            self.lab_logger().warning(f"[Host Node] Goal {item.action_name} ({item.job_id}) rejected")
            self._publish_terminal_result(
                item,
                "failed",
                serialize_result_info("Goal was rejected", False, {}),
                {},
            )
            return

        self.lab_logger().info(f"[Host Node] Goal {action_id} ({item.job_id}) accepted")
        self._goals[item.job_id] = goal_handle
        goal_future = goal_handle.get_result_async()
        goal_future.add_done_callback(
            lambda f: self.get_result_callback(
                item,
                action_id,
                f,
            )
        )
        with self._goal_state_lock:
            canceled = item.job_id in self._canceled_jobs
        if canceled:
            self.lab_logger().info(
                f"[Host Node] Goal {item.job_id[:8]} accepted after cancel; cancel immediately"
            )
            self._request_goal_cancel(item.job_id, goal_handle)
            return
        goal_future.result()

    def feedback_callback(self, item: "QueueItem", action_id: str, feedback_msg) -> None:
        """反馈回调。"""
        feedback_data = convert_from_ros_msg(feedback_msg)
        feedback_data.pop("goal_id")
        self.lab_logger().trace(f"[Host Node] Feedback for {action_id} ({item.job_id}): {feedback_data}")

        for bridge in execution_result_bridges(self.bridges):
            if hasattr(bridge, "publish_job_status"):
                bridge.publish_job_status(feedback_data, item, "running")

    def get_result_callback(self, item: "QueueItem", action_id: str, future) -> None:
        """获取结果回调。"""
        job_id = item.job_id
        if self._take_settled(job_id):
            self.lab_logger().info(f"[Host Node] Goal {job_id[:8]} 已因设备离线收尾，忽略迟到的结果")
            return

        try:
            result = future.result()
            result_msg = result.result
            goal_status = result.status
            with self._goal_state_lock:
                cancel_requested = job_id in self._canceled_jobs

            if cancel_requested or goal_status == GoalStatus.STATUS_CANCELED:
                self.lab_logger().info(f"[Host Node] Goal {action_id} ({job_id[:8]}) was cancelled")
                status = "failed"
                return_info = serialize_result_info(
                    "Job was cancelled",
                    False,
                    {},
                    suc_type=SUCCESS_TYPE_CANCELLATION,
                )
            else:
                result_data = convert_from_ros_msg(result_msg)
                status = "success"
                return_info_str = result_data.get("return_info")
                if return_info_str is not None:
                    try:
                        return_info = json.loads(return_info_str)
                        # 适配后端的一些额外处理
                        return_value = return_info.get("return_value")
                        if isinstance(return_value, dict):
                            unilabos_samples = return_value.pop(RETURN_UNILABOS_SAMPLES, None)
                            if isinstance(unilabos_samples, list) and unilabos_samples:
                                self.lab_logger().info(
                                    f"[Host Node] Job {job_id[:8]} returned {len(unilabos_samples)} sample(s): "
                                    f"{[s.get('name', s.get('id', 'unknown')) if isinstance(s, dict) else str(s)[:20] for s in unilabos_samples[:5]]}"
                                    f"{'...' if len(unilabos_samples) > 5 else ''}"
                                )
                                return_info["samples"] = unilabos_samples
                        suc = return_info.get("suc", False)
                        if not suc:
                            status = "failed"
                    except json.JSONDecodeError:
                        status = "failed"
                        return_info = serialize_result_info("", False, result_data)
                        self.lab_logger().critical("动作返回了无效的 return_info 类型")
                else:
                    # 无 return_info 字段时，回退到 success 字段（若存在）
                    suc_field = result_data.get("success")
                    if isinstance(suc_field, bool):
                        status = "success" if suc_field else "failed"
                        return_info = serialize_result_info("", suc_field, result_data)
                    else:
                        # 最保守的回退：标记失败并返回空JSON
                        status = "failed"
                        return_info = serialize_result_info("缺少return_info", False, result_data)

            terminal_result_data = (
                {} if cancel_requested or goal_status == GoalStatus.STATUS_CANCELED else result_data
            )
            self.lab_logger().info(f"[Host Node] Result for {action_id} ({job_id[:8]}): {status}")
            if not cancel_requested and goal_status != GoalStatus.STATUS_CANCELED:
                self.lab_logger().trace(f"[Host Node] Result data: {result_data}")
            self._publish_terminal_result(item, status, return_info, terminal_result_data)

        except Exception as e:
            self.lab_logger().error(
                f"[Host Node] Error in get_result_callback for {action_id} ({job_id[:8]}): {str(e)}"
            )
            self.lab_logger().error(traceback.format_exc())
            self._publish_terminal_result(
                item,
                "failed",
                serialize_result_info(f"Callback error: {str(e)}", False, {}),
                {},
            )

    def _request_goal_cancel(self, job_id: str, goal_handle: Any) -> None:
        """向已受理的 ROS Goal 发起取消。"""
        cancel_future = goal_handle.cancel_goal_async()
        cancel_future.add_done_callback(
            lambda future: self._cancel_goal_callback(job_id, future)
        )

    def _cancel_goal_callback(self, goal_uuid: str, future) -> None:
        """取消目标的回调。"""
        try:
            cancel_response = future.result()
            if cancel_response.goals_canceling:
                self.lab_logger().info(f"[Host Node] Goal {goal_uuid[:8]} cancel request accepted")
            else:
                self.lab_logger().warning(f"[Host Node] Goal {goal_uuid[:8]} cancel request rejected")
        except Exception as e:
            self.lab_logger().error(f"[Host Node] Error cancelling goal {goal_uuid[:8]}: {str(e)}")
            self.lab_logger().error(traceback.format_exc())

    # ------------------------------------------------------------------
    # 控制器与 host 服务
    # ------------------------------------------------------------------

    def initialize_controller(self, controller_id: str, controller_config: Dict[str, Any]) -> None:
        """初始化控制器。"""
        self.lab_logger().info(f"[Host Node] Initializing controller: {controller_id}")

        class_name = controller_config.pop("type")
        controller_func = globals()[class_name]

        for input_name, input_info in controller_config["inputs"].items():
            controller_config["inputs"][input_name]["type"] = get_msg_type(eval(input_info["type"]))
        for output_name, output_info in controller_config["outputs"].items():
            controller_config["outputs"][output_name]["type"] = get_msg_type(eval(output_info["type"]))

        if controller_config["parameters"] is None:
            controller_config["parameters"] = {}

        ControllerNode(
            controller_id,
            controller_func=controller_func,
            **controller_config,
        )
        self.lab_logger().info(f"[Host Node] Controller {controller_id} created.")

    def _init_host_service(self):
        # ROS 服务仅承载节点信息上报；物料操作通过 materials API 与
        # HostLink 下行链路完成。
        self._resource_services: Dict[str, Any] = {
            "node_info_update": self.create_service(
                SerialCommand,
                "/node_info_update",
                self._node_info_update_callback,
                callback_group=self.callback_group,
            ),
        }

    @staticmethod
    def _local_registry_action_mappings(registry_name: str) -> Dict[str, Any]:
        """本机注册表里 ``registry_name`` 的动作映射（没有时为空）。"""

        from unilabos.registry.registry import lab_registry

        entry = (getattr(lab_registry, "device_type_registry", None) or {}).get(registry_name) or {}
        mappings = (entry.get("class") or {}).get("action_value_mappings") if isinstance(entry, dict) else None
        return dict(mappings) if isinstance(mappings, dict) else {}

    def _node_info_update_callback(self, request, response):
        """更新节点信息回调。

        首次上报包含 ``devices_config`` 与 ``registry_config``；设备重注册消息
        ``SYNC_SLAVE_NODE_INFO`` 使用 ``registry_name`` 恢复对应的动作映射。
        """
        self.lab_logger().trace(f"[Host Node] Node info update request received: {request}")
        try:
            info = json.loads(request.command)
            if "SYNC_SLAVE_NODE_INFO" in info:
                info = info["SYNC_SLAVE_NODE_INFO"]
                machine_name = info["machine_name"]
                edge_device_id = info["edge_device_id"]
                registry_name = info.get("registry_name", "")
                self.device_machine_names[edge_device_id] = machine_name

                # 用 registry_name 索引已存储的 registry_config,获取 action_value_mappings
                if registry_name and registry_name in self._slave_registry_configs:
                    action_mappings = (
                        self._slave_registry_configs[registry_name].get("class", {}).get("action_value_mappings", {})
                    )
                    if action_mappings:
                        self._action_value_mappings[edge_device_id] = action_mappings
                        self.lab_logger().info(
                            f"[Host Node] Loaded {len(action_mappings)} action mappings "
                            f"for remote device {edge_device_id} (registry: {registry_name})"
                        )
                elif registry_name:
                    # Host 重启后没有 Slave 首次上报的注册表，设备重注册只带 registry_name：
                    # 用本机注册表的同名条目兜底，否则 Slave 设备在能力快照里没有动作
                    action_mappings = self._local_registry_action_mappings(registry_name)
                    if action_mappings:
                        self._action_value_mappings[edge_device_id] = action_mappings
                        self.lab_logger().info(
                            f"[Host Node] Loaded {len(action_mappings)} action mappings "
                            f"for remote device {edge_device_id} from the local registry (registry: {registry_name})"
                        )
                    else:
                        self.lab_logger().warning(
                            f"[Host Node] 远端设备 {edge_device_id} 的注册表 {registry_name} 不在本机，"
                            "重启 Slave 后才有动作能力"
                        )
            else:
                devices_config = info.pop("devices_config")
                registry_config = info.pop("registry_config")
                if registry_config:
                    # 按注册表名称缓存 Slave 动作定义，供设备重注册查询。
                    for reg_name, reg_data in registry_config.items():
                        if isinstance(reg_data, dict) and "class" in reg_data:
                            self._slave_registry_configs[reg_name] = reg_data

                # 解析 devices_config,建立 device_id -> action_value_mappings 映射
                if devices_config:
                    machine_name = info["machine_name"]
                    # Stamp machine_name on each device dict before parsing
                    for device_tree in devices_config:
                        for device_dict in device_tree:
                            device_dict["machine_name"] = machine_name
                            device_id = device_dict.get("id", "")
                            registry_name = device_dict.get("template_name", "")
                            if device_id and registry_name and registry_name in self._slave_registry_configs:
                                action_mappings = (
                                    self._slave_registry_configs[registry_name]
                                    .get("class", {})
                                    .get("action_value_mappings", {})
                                )
                                if action_mappings:
                                    self._action_value_mappings[device_id] = action_mappings
                                    self.lab_logger().info(
                                        f"[Host Node] Stored {len(action_mappings)} action mappings "
                                        f"for remote device {device_id} (registry: {registry_name})"
                                    )

                    # Merge slave devices_config into self.devices_config tree
                    try:
                        slave_tree_set = ResourceTreeSet.load(devices_config)  # slave一定是根节点的tree
                        for tree in slave_tree_set.trees:
                            self.devices_config.trees.append(tree)
                        self.lab_logger().info(
                            f"[Host Node] Merged {len(slave_tree_set.trees)} slave device trees "
                            f"(machine: {machine_name}) into devices_config"
                        )
                    except Exception as e:
                        self.lab_logger().error(f"[Host Node] Failed to merge slave devices_config: {e}")

            self.lab_logger().debug(f"[Host Node] Node info update: {info}")
            # slave 的 action_value_mappings 已更新，刷新 runtime.v1 能力快照
            self._notify_capabilities_changed()
            response.response = "OK"
        except Exception as e:
            self.lab_logger().error(f"[Host Node] Error updating node info: {e.args}")
            response.response = "ERROR"
        return response


__all__ = [
    "HostNode",
    "authority_host_node_uuid",
    "ensure_host_node_resource",
    "is_host_node_config",
    "iter_host_managed_device_configs",
]
