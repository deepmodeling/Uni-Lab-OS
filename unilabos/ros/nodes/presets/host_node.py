import collections
import importlib
import json
import threading
import time
import traceback
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Point
from rclpy.action import ActionClient, get_action_server_names_and_types_by_node
from rclpy.service import Service
from typing_extensions import TypedDict
from unilabos_msgs.srv._serial_command import SerialCommand_Request, SerialCommand_Response
from unique_identifier_msgs.msg import UUID

from unilabos.config.config import BasicConfig
from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    DataSource,
    ExecutorKind,
    NodeType,
    action,
    device,
    legacy_action,
)
from unilabos.registry.placeholder_type import (
    PLACEHOLDER_DEDUCT_REAGENT,
    PLACEHOLDER_DEDUCT_RESOURCE,
    PLACEHOLDER_DEVICES,
    PLACEHOLDER_MANUAL_CONFIRM,
    PLACEHOLDER_NODES,
    DeviceSlot,
    ResourceSlot,
)
from unilabos.registry.registry import lab_registry
from unilabos.resources.container import RegularContainer
from unilabos.resources.graphio import initialize_resource
from unilabos.resources.liquids import apply_substances
from unilabos.resources.registry import add_schema
from unilabos.resources.resource_tracker import (
    JSON_UNILABOS_PARAM,
    PARAM_SAMPLE_UUIDS,
    RETURN_UNILABOS_SAMPLES,
    LabSample,
    ResourceDict,
    ResourceDictInstance,
    ResourceDictType,
    ResourceTreeInstance,
    ResourceTreeSet,
    SampleUUIDsType,
)
from unilabos.ros.action_transport import (
    required_action_endpoint_ids,
    wait_for_action_endpoints,
)
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.ros.msgs.message_converter import (
    convert_from_ros_msg,
    convert_to_ros_msg,
    get_msg_type,
    get_ros_type_by_msgname,
    msg_converter_manager,
)
from unilabos.ros.nodes.base_device_node import (
    BaseROS2DeviceNode,
    DeviceNodeResourceTracker,
    ROS2DeviceNode,
    _stable_resource_uuid,
)
from unilabos.ros.nodes.presets.controller_node import ControllerNode
from unilabos.utils import logger
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.log import is_detailed_logging_enabled, warning
from unilabos.utils.tools import fast_dumps_str as _fast_dumps_str
from unilabos.utils.tools import fast_loads as _fast_loads
from unilabos.utils.type_check import serialize_result_info
from unilabos_msgs.action import EmptyIn, ResourceCreateFromOuter, ResourceCreateFromOuterEasy, StrSingleInput
from unilabos_msgs.msg import Resource  # type: ignore
from unilabos_msgs.srv import (
    ResourceAdd,
    ResourceDelete,
    ResourceList,
    ResourceUpdate,
    SerialCommand,
)  # type: ignore

if TYPE_CHECKING:
    from unilabos.app.ws_client import QueueItem


@dataclass
class DeviceActionStatus:
    job_ids: Dict[str, float] = field(default_factory=dict)


class TestResourceReturn(TypedDict):
    resources: List[List[ResourceDictType]]
    devices: List[DeviceSlot]
    unilabos_samples: List[LabSample]


class CreateResourceReturn(TypedDict):
    created_resource_tree: List[List[ResourceDictType]]
    liquid_input_resource_tree: List[Dict[str, Any]]
    unilabos_samples: List[LabSample]


class DeductResourceReturn(CreateResourceReturn):
    """apply_deduct_resource 返回值：在创建结果之外，额外输出实际挂载到的目标物料树。"""

    mount_resource: List[List[ResourceDictType]]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferResourceReturn:
    """转移记账动作的类型化结果合同，同时保留运行时字典返回形状。

    ``resource`` 与 ``mount_resource`` 在工作流创作合同中都是物料占位符
    （ResourceSlot），从而可以继续连接下游动作；运行时仍返回原有四键字典，
    不改变旧调用方读取 ``resource/mount_resource/site/result`` 的方式。
    """

    resource: ResourceSlot
    mount_resource: ResourceSlot
    site: str
    result: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferManualReturn:
    """人工搬运闸门的类型化结果：透传物料、目标设备和库位，不做系统转移。

    ``resource`` 与 ``mount_resource`` 在工作流创作合同中都是物料占位符
    （ResourceSlot），从而可以继续连接下游动作；运行时返回单物料 UUID 引用。
    """

    resource: ResourceSlot
    mount_resource: ResourceSlot
    target_device: str
    site: str


def _dump_resource_slot(resource: Any) -> Dict[str, str]:
    """把单个物料序列化为工作流 ``ResourceSlot`` 的规范 UUID 引用。

    ``ResourceSlot`` 在已发布动作合同中是一个对象，而不是资源树数组。设备动作
    的入参水合仍可在执行边界根据 UUID 取回完整资源树；返回完整树会令下游严格
    JSON Schema 收到 ``[[resource]]``，从而把合法的单物料连接误判为数组。
    """

    resource_uuid = _stable_resource_uuid(resource).strip()
    if not resource_uuid:
        raise ValueError("物料占位符缺少稳定 UUID")
    return {"uuid": resource_uuid}


class TestLatencyReturn(TypedDict):
    """test_latency方法的返回值类型"""

    avg_rtt_ms: float
    avg_time_diff_ms: float
    max_time_error_ms: float
    task_delay_ms: float
    raw_delay_ms: float
    test_count: int
    status: str


def _device_is_simulated(host: Any, device_id: str) -> bool:
    """整个设备运行时处于模拟动作模式，或机构运行时把这台设备配置为不构造驱动（direct）。"""

    mechanisms = getattr(host, "_mechanisms", None)
    return BasicConfig.action_mode == "simulate" or (
        mechanisms is not None and mechanisms.simulated(device_id)
    )


@device(id="host_node", category=[], description="Host Node", icon="icon_device.webp")
class HostNode(BaseROS2DeviceNode):
    """
    主机节点类，负责管理设备、资源和控制器

    作为单例模式实现，确保整个应用中只有一个主机节点实例
    """

    _instance: ClassVar[Optional["HostNode"]] = None
    _ready_event: ClassVar[threading.Event] = threading.Event()
    _shutting_down: ClassVar[bool] = False  # Flag to signal shutdown to background threads
    _background_threads: ClassVar[List[threading.Thread]] = []  # Track all background threads for cleanup
    _device_action_status: ClassVar[collections.defaultdict[str, DeviceActionStatus]] = collections.defaultdict(
        DeviceActionStatus
    )
    _resource_tracker: ClassVar[DeviceNodeResourceTracker] = DeviceNodeResourceTracker()  # 资源管理器实例

    @classmethod
    def get_instance(cls, timeout=None) -> Optional["HostNode"]:
        if cls._ready_event.wait(timeout):
            return cls._instance
        return None

    @classmethod
    def shutdown_background_threads(cls, timeout: float = 5.0) -> None:
        """
        Gracefully shutdown all background threads for clean exit or restart.

        This method:
        1. Sets shutdown flag to stop background operations
        2. Waits for background threads to finish with timeout
        3. Cleans up finished threads from tracking list

        Args:
            timeout: Maximum time to wait for each thread (seconds)
        """
        cls._shutting_down = True

        # Wait for background threads to finish
        active_threads = []
        for t in cls._background_threads:
            if t.is_alive():
                t.join(timeout=timeout)
                if t.is_alive():
                    active_threads.append(t.name)

        if active_threads:
            logger.warning(f"[Host Node] Some background threads still running: {active_threads}")

        # Clear the thread list
        cls._background_threads.clear()
        logger.info(f"[Host Node] Background threads shutdown complete")

    @classmethod
    def reset_state(cls) -> None:
        """
        Reset the HostNode singleton state for restart or clean exit.
        Call this after destroying the instance.
        """
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
        resources_edge_config: list[dict],
        physical_setup_graph: Optional[Dict[str, Any]] = None,
        controllers_config: Optional[Dict[str, Any]] = None,
        bridges: Optional[List[Any]] = None,
        discovery_interval: float = 180.0,  # 设备发现间隔，单位为秒
    ):
        """
        初始化主机节点

        Args:
            device_id: 节点名称
            devices_config: 设备配置
            resources_config: 资源配置
            physical_setup_graph: 物理设置图
            controllers_config: 控制器配置
            bridges: 桥接器列表
            discovery_interval: 设备发现间隔（秒），默认5秒
        """
        if self._instance is not None:
            self._instance.lab_logger().critical("[Host Node] HostNode instance already exists.")

        # 设置单例实例
        self.__class__._instance = self

        # 初始化配置
        self.server_latest_timestamp = 0.0  #
        self.devices_config = devices_config
        self.resources_config = resources_config  # 直接保存 ResourceTreeSet
        self.resources_edge_config = resources_edge_config
        self.physical_setup_graph = physical_setup_graph
        if controllers_config is None:
            controllers_config = {}
        self.controllers_config = controllers_config
        if bridges is None:
            bridges = []
        self.bridges = bridges

        # 创建 host_node 作为一个单独的 ResourceTree
        host_node_dict = {
            "id": "host_node",
            "uuid": str(uuid.uuid4()),
            "parent_uuid": "",
            "name": "host_node",
            "type": "device",
            "class": "host_node",
            "config": {},
            "data": {},
            "children": [],
            "description": "",
            "schema": {},
            "model": {},
            "icon": "",
        }

        # 创建 host_node 的 ResourceTree
        host_node_instance = ResourceDictInstance.get_resource_instance_from_dict(host_node_dict)
        host_node_tree = ResourceTreeInstance(host_node_instance)
        resources_config.trees.insert(0, host_node_tree)
        try:
            for bridge in self.bridges:
                if hasattr(bridge, "resource_tree_add") and resources_config:
                    from unilabos.app.web.client import HTTPClient

                    client: HTTPClient = bridge
                    resource_start_time = time.time()
                    # 传递 ResourceTreeSet 对象，在 client 中转换为字典并获取 UUID 映射
                    uuid_mapping = client.resource_tree_add(resources_config, "", True)
                    device_uuid = resources_config.root_nodes[0].res_content.uuid
                    resource_end_time = time.time()
                    logger.info(
                        f"[Host Node-Resource] 物料上传 {round(resource_end_time - resource_start_time, 5) * 1000} ms"
                    )
                    for edge in self.resources_edge_config:
                        edge["source_uuid"] = uuid_mapping.get(edge["source_uuid"], edge["source_uuid"])
                        edge["target_uuid"] = uuid_mapping.get(edge["target_uuid"], edge["target_uuid"])
                    resource_add_res = client.resource_edge_add(self.resources_edge_config)
                    resource_edge_end_time = time.time()
                    logger.info(
                        f"[Host Node-Resource] 物料关系上传 {round(resource_edge_end_time - resource_end_time, 5) * 1000} ms"
                    )
                    # resources_config 通过各个设备的 resource_tracker 进行uuid更新，利用uuid_mapping
                    # resources_config 的 root node 是
                    # # 创建反向映射：new_uuid -> old_uuid
                    # reverse_uuid_mapping = {new_uuid: old_uuid for old_uuid, new_uuid in uuid_mapping.items()}
                    for tree in resources_config.trees:
                        node = tree.root_node
                        if node.res_content.type == "device":
                            continue
                        else:
                            try:
                                for plr_resource in ResourceTreeSet([tree]).to_plr_resources():
                                    self._resource_tracker.add_resource(plr_resource)
                            except Exception as ex:
                                warning(f"[Host Node-Resource] 根节点物料{tree}序列化失败！")
        except Exception as ex:
            logger.error(f"[Host Node-Resource] 添加物料出错！\n{traceback.format_exc()}")
        # 初始化Node基类，传递空参数覆盖列表
        BaseROS2DeviceNode.__init__(
            self,
            driver_instance=self,
            device_id=device_id,
            registry_name="host_node",
            device_uuid=host_node_dict["uuid"],
            status_types={},
            action_value_mappings=lab_registry.device_type_registry["host_node"]["class"]["action_value_mappings"],
            hardware_interface={},
            print_publish=False,
            resource_tracker=self._resource_tracker,  # host node并不是通过initialize 包一层传进来的
        )

        # 创建设备、动作客户端和目标存储
        self.devices_names: Dict[str, str] = {device_id: self.namespace}  # 存储设备名称和命名空间的映射
        self.devices_instances: Dict[str, ROS2DeviceNode] = {}  # 存储设备实例
        self.device_machine_names: Dict[str, str] = {
            device_id: "本地",
        }  # 存储设备ID到机器名称的映射
        self._action_clients: Dict[str, ActionClient] = {  # 为了方便了解实际的数据类型，host的默认写好
            "/devices/host_node/create_resource": ActionClient(
                self,
                ResourceCreateFromOuterEasy,
                "/devices/host_node/create_resource",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/create_resource_detailed": ActionClient(
                self,
                ResourceCreateFromOuter,
                "/devices/host_node/create_resource_detailed",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/test_latency": ActionClient(
                self,
                EmptyIn,
                "/devices/host_node/test_latency",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/test_resource": ActionClient(
                self,
                EmptyIn,
                "/devices/host_node/test_resource",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/_execute_driver_command": ActionClient(
                self,
                StrSingleInput,
                "/devices/host_node/_execute_driver_command",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/_execute_driver_command_async": ActionClient(
                self,
                StrSingleInput,
                "/devices/host_node/_execute_driver_command_async",
                callback_group=self.callback_group,
            ),
        }  # 用来存储多个ActionClient实例
        self._action_value_mappings: Dict[str, Dict] = {
            device_id: self._action_value_mappings
        }  # device_id -> action_value_mappings(本地+远程设备统一存储)
        self._goals: Dict[str, Any] = {}  # 用来存储多个目标的状态
        self._online_devices: Set[str] = {f"{self.namespace}/{device_id}"}  # 用于跟踪在线设备
        # 托管 dry-run Edge 只投影设备目录，不创建 ROS/硬件端点；这些设备的
        # 在线事实不能被周期 ROS 图发现误判为离线。
        self._simulated_device_keys: Set[str] = set()
        # 可选的工站仿真装配（UNILABOS_SIMULATION_COMPOSITION）：按资产行为类执行或跟随设备动作。
        # 只在设备运行时进程里打开；未配置时为 None，行为与未启用完全相同。
        self._mechanisms = None
        if BasicConfig.process_role != "workspace_backend":
            from unilabos.simulation.mechanism_runtime import edge_mechanisms

            self._mechanisms = edge_mechanisms(log=lambda message: self.lab_logger().warning(f"[Mechanism] {message}"))
        self._last_discovery_time = 0.0  # 上次设备发现的时间
        self._discovery_lock = threading.Lock()  # 设备发现的互斥锁
        self._subscribed_topics = set()  # 用于跟踪已订阅的话题

        # 创建物料增删改查服务（非客户端）
        self._init_host_service()

        self.device_status = {}  # 用来存储设备状态
        self.device_status_timestamps = {}  # 用来存储设备状态最后更新时间
        time.sleep(1)  # 等待通信连接稳定
        # 首次发现网络中的设备
        self._discover_devices()

        # Workspace Backend 保留完整物理图用于 Authoring/Inventory，但设备驱动只由
        # 独立 Edge Runtime 初始化设备，工站后端不加载驱动。
        if BasicConfig.process_role != "workspace_backend":
            local_machine = BasicConfig.machine_name
            for device_config in devices_config.root_nodes:
                device_id = device_config.res_content.id
                if device_config.res_content.type != "device":
                    continue
                dev_machine = device_config.res_content.machine_name
                if dev_machine and local_machine and dev_machine != local_machine:
                    self.lab_logger().info(
                        f"[Host Node] Device {device_id} belongs to machine '{dev_machine}', "
                        f"local is '{local_machine}', skipping initialization."
                    )
                    continue
                if device_id not in self.devices_names:
                    self._initialize_configured_device(device_id, device_config)
                else:
                    self.lab_logger().warning(f"[Host Node] Device {device_id} already existed, skipping.")
            self.update_device_status_subscriptions()
        else:
            self.lab_logger().info(
                "[Host Node] Workspace Backend 已就绪，等待独立 Edge Runtime 注册设备"
            )
        # 控制器同样属于设备运行时；Workspace Backend 不实例化硬件控制器。
        if (
            controllers_config
            and BasicConfig.process_role != "workspace_backend"
            and not (
                BasicConfig.process_role == "edge_runtime"
                and BasicConfig.action_mode == "simulate"
            )
        ):
            update_rate = controllers_config["controller_manager"]["ros__parameters"]["update_rate"]
            for controller_id, controller_config in controllers_config["controller_manager"]["ros__parameters"][
                "controllers"
            ].items():
                controller_config["update_rate"] = update_rate
                self.initialize_controller(controller_id, controller_config)

        # 创建定时器，定期发现设备
        self._discovery_timer = self.create_timer(
            discovery_interval, self._discovery_devices_callback, callback_group=self.callback_group
        )

        # 添加ping-pong相关属性
        self._ping_responses = {}  # 存储ping响应
        self._ping_lock = threading.Lock()

        self.lab_logger().info("[Host Node] Host node initialized.")
        HostNode._ready_event.set()

        # 发送host_node ready信号到所有桥接器
        for bridge in self.bridges:
            if hasattr(bridge, "publish_host_ready"):
                bridge.publish_host_ready()
                self.lab_logger().debug(f"Host ready signal sent via {bridge.__class__.__name__}")

    def _send_re_register(self, sclient, device_namespace: str):
        """
        Send re-register command to a device. This is a one-time operation.

        Args:
            sclient: The service client
            device_namespace: The device namespace for logging
        """
        try:
            # Use timeout to prevent indefinite blocking
            if not sclient.wait_for_service(timeout_sec=10.0):
                self.lab_logger().debug(f"[Host Node] Re-register timeout for {device_namespace}")
                return

            # Check shutdown flag after wait
            if self._shutting_down:
                self.lab_logger().debug(f"[Host Node] Re-register aborted for {device_namespace} (shutdown)")
                return

            request = SerialCommand.Request()
            request.command = ""
            future = sclient.call_async(request)
            # Use timeout for result as well
            future.result()
        except Exception as e:
            # Gracefully handle destruction during shutdown
            if "destruction was requested" in str(e) or self._shutting_down:
                self.lab_logger().debug(f"[Host Node] Re-register aborted for {device_namespace} (cleanup)")
            else:
                self.lab_logger().warning(f"[Host Node] Re-register failed for {device_namespace}: {e}")

    def _discover_devices(self) -> None:
        """
        发现网络中的设备

        检测ROS2网络中的所有设备节点，并为它们创建ActionClient
        同时检测设备离线情况
        """
        if is_detailed_logging_enabled():
            self.lab_logger().trace("[Host Node] 开始发现网络中的设备。")

        # 获取当前所有设备
        nodes_and_names = self.get_node_names_and_namespaces()

        # 跟踪本次发现的设备，用于检测离线设备
        current_devices = set(self._simulated_device_keys)

        for device_id, namespace in nodes_and_names:
            if not namespace.startswith("/devices/"):
                continue
            edge_device_id = namespace[9:]
            # 将设备添加到当前设备集合
            device_key = f"{namespace}/{edge_device_id}"  # namespace已经包含device_id了，这里复写一遍
            current_devices.add(device_key)

            # 如果是新设备，记录并创建ActionClient
            if edge_device_id not in self.devices_names:
                self.lab_logger().info(f"[Host Node] Discovered new device: {edge_device_id}")
                self.devices_names[edge_device_id] = namespace
                self._create_action_clients_for_device(device_id, namespace)
                self._online_devices.add(device_key)
                sclient = self.create_client(SerialCommand, f"/srv{namespace}/re_register_device")
                t = threading.Thread(
                    target=self._send_re_register,
                    args=(sclient, namespace),
                    daemon=True,
                    name=f"ROSDevice{self.device_id}_re_register_device_{namespace}",
                )
                self._background_threads.append(t)
                t.start()
            elif device_key not in self._online_devices:
                # 设备重新上线
                self.lab_logger().info(f"[Host Node] Device reconnected: {device_key}")
                self._online_devices.add(device_key)
                sclient = self.create_client(SerialCommand, f"/srv{namespace}/re_register_device")
                t = threading.Thread(
                    target=self._send_re_register,
                    args=(sclient, namespace),
                    daemon=True,
                    name=f"ROSDevice{self.device_id}_re_register_device_{namespace}",
                )
                self._background_threads.append(t)
                t.start()

        # Discovery Server v2 may withhold the remote node graph while still
        # matching the Action endpoints we created from Slave registration.
        # Keep a device online only when at least one of those ROS clients is
        # 实际 ROS 动作服务必须就绪。
        for edge_device_id, namespace in self.devices_names.items():
            if edge_device_id == self.device_id or not namespace.startswith(
                "/devices/"
            ):
                continue
            device_key = f"{namespace}/{edge_device_id}"
            if device_key in current_devices:
                continue
            action_prefix = f"{namespace}/"
            action_clients = [
                client
                for action_id, client in self._action_clients.items()
                if action_id.startswith(action_prefix)
            ]
            if self._has_ready_action_client(action_clients):
                current_devices.add(device_key)
                if device_key not in self._online_devices:
                    self._report_action_locks_free(
                        self._routable_reported_action_pairs(edge_device_id)
                    )

        # 检测离线设备
        offline_devices = self._online_devices - current_devices
        for device_key in offline_devices:
            self.lab_logger().warning(f"[Host Node] Device offline: {device_key}")
            self._online_devices.discard(device_key)

        # 更新在线设备列表
        self._online_devices = current_devices
        if is_detailed_logging_enabled():
            self.lab_logger().trace(f"[Host Node] 在线设备总数: {len(self._online_devices)}")

    def _discovery_devices_callback(self) -> None:
        """
        设备发现定时器回调函数
        """
        # 使用互斥锁确保同时只有一个发现过程
        if self._discovery_lock.acquire(blocking=False):
            try:
                self._discover_devices()
                # 发现新设备后，更新设备状态订阅
                self.update_device_status_subscriptions()
            finally:
                self._discovery_lock.release()
        else:
            if is_detailed_logging_enabled():
                self.lab_logger().trace("[Host Node] 设备发现已在进行，跳过本轮。")

    def _report_action_locks_free(self, action_pairs: List[Tuple[str, str]]) -> None:
        """向所有桥接器主动上报新发现 action 的锁状态为 free(report_action_lock)。

        服务端直接下发 job 模式下，需要在发现新设备/新 action 时主动告知其可用，
        而不再依赖 query_action_state。
        """
        if not action_pairs:
            return
        # _execute_driver_command[_async] 是通用驱动命令入口，并非具体业务动作，
        # 不作为锁上报（与 WebSocketClient.report_all_action_locks 的过滤保持一致）。
        locks = [
            {"device_id": dev, "action_name": act, "free": True}
            for dev, act in action_pairs
            if not act.startswith("_execute_driver_command")
        ]
        if not locks:
            return
        for bridge in self.bridges:
            if hasattr(bridge, "publish_action_locks"):
                try:
                    bridge.publish_action_locks(locks)
                except Exception as e:
                    self.lab_logger().warning(f"[Host Node] publish_action_locks failed: {e}")

    def _create_action_clients_for_device(self, device_id: str, namespace: str) -> None:
        """
        为设备创建所有必要的ActionClient

        Args:
            device_id: 设备ID
            namespace: 设备命名空间
        """
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
                    action_name = action_id[len(namespace) + 1 :]
                    new_action_pairs.append((edge_device_id, action_name))
                except Exception as e:
                    self.lab_logger().error(f"[Host Node] Failed to create ActionClient for {action_id}: {str(e)}")

        # 补充 _action_value_mappings 中其余动作：UniLabJsonCommand 类型动作不建独立
        # ROS ActionServer，不会出现在 get_action_server_names_and_types_by_node 的结果里；
        # ``auto_prefix=True`` 注册成的遗留 ``auto-`` 动作（如 workbench.prepare_materials）。
        # 同理。它们仍是可经 _execute_driver_command 调用的能力，发现新设备时必须全量补报其
        # free 锁，否则服务端永远感知不到这些动作。_execute_driver_command[_async] 由
        # _report_action_locks_free 统一过滤，不在此处特判。
        already = {action_name for _, action_name in new_action_pairs}
        for action_name in self._action_value_mappings.get(edge_device_id, {}).keys():
            if action_name in already:
                continue
            new_action_pairs.append((edge_device_id, action_name))

        # 发现新 action 后主动上报其 free 锁状态
        self._report_action_locks_free(new_action_pairs)

    @staticmethod
    def _has_ready_action_client(
        clients: Iterable[Any],
        wait_timeout: float = 0.0,
    ) -> bool:
        """Return whether any ROS Action endpoint has actually matched.

        ``server_is_ready`` may raise while an rclpy context is shutting down;
        discovery polling must treat that as unavailable instead of killing its
        timer callback.  ``wait_timeout`` is a total budget shared by every
        client, not a per-action delay.
        """

        candidates = list(clients)
        deadline = time.monotonic() + max(float(wait_timeout), 0.0)
        while candidates:
            for client in candidates:
                try:
                    if client.server_is_ready():
                        return True
                except Exception:  # noqa: BLE001 - rclpy teardown/race
                    continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(deadline - time.monotonic(), 0.0)))
        return False

    def _routable_reported_action_pairs(
        self,
        device_id: str,
    ) -> List[Tuple[str, str]]:
        """Actions with a concrete ROS client, including JSON command multiplexing."""

        namespace = f"/devices/{device_id}"
        pairs: List[Tuple[str, str]] = []
        for action_name, mapping in self._action_value_mappings.get(device_id, {}).items():
            if not isinstance(mapping, dict):
                continue
            mapping_type = str(mapping.get("type") or "")
            if action_name.startswith("auto-") or mapping_type.startswith("UniLabJsonCommand"):
                generic = (
                    "_execute_driver_command_async"
                    if mapping_type.startswith("UniLabJsonCommandAsync")
                    else "_execute_driver_command"
                )
                action_id = f"{namespace}/{generic}"
            else:
                action_id = f"{namespace}/{action_name}"
            if action_id in self._action_clients:
                pairs.append((device_id, action_name))
        return pairs

    async def create_resource_detailed(
        self,
        resources: list[Union[list["Resource"], "Resource"]],
        device_ids: list[str],
        bind_parent_ids: list[str],
        bind_locations: list[Point],
        other_calling_params: list[str],
    ) -> List[str]:
        responses = []
        for resource, device_id, bind_parent_id, bind_location, other_calling_param in zip(
            resources, device_ids, bind_parent_ids, bind_locations, other_calling_params
        ):
            # 这里要求device_id传入必须是edge_device_id
            if device_id not in self.devices_names:
                self.lab_logger().error(
                    f"[Host Node] Device {device_id} not found in devices_names. Create resource failed."
                )
                raise ValueError(f"[Host Node] Device {device_id} not found in devices_names. Create resource failed.")

            device_key = f"{self.devices_names[device_id]}/{device_id}"
            if device_key not in self._online_devices:
                self.lab_logger().error(f"[Host Node] Device {device_key} is offline. Create resource failed.")
                raise ValueError(f"[Host Node] Device {device_key} is offline. Create resource failed.")

            namespace = self.devices_names[device_id]
            srv_address = f"/srv{namespace}/append_resource"
            sclient = self.create_client(SerialCommand, srv_address)
            sclient.wait_for_service()
            request = SerialCommand.Request()
            request.command = json.dumps(
                {
                    "resource": resource,  # 单个/单组 可为 list[list[Resource]]
                    "namespace": namespace,
                    "edge_device_id": device_id,
                    "bind_parent_id": bind_parent_id,
                    "bind_location": {
                        "x": bind_location.x,
                        "y": bind_location.y,
                        "z": bind_location.z,
                    },
                    "other_calling_param": json.loads(other_calling_param) if other_calling_param else {},
                },
                ensure_ascii=False,
            )
            response: SerialCommand.Response = await sclient.call_async(request)
            responses.append(response.response)
        return responses

    async def create_resource(
        self,
        device_id: DeviceSlot,
        res_id: str,
        class_name: str,
        parent: ResourceSlot,
        bind_locations: Point,
        liquid_input_slot: list[int] = [],
        liquid_type: list[str] = [],
        liquid_volume: list[int] = [],
        slot_on_deck: str = "",
    ) -> CreateResourceReturn:
        # 暂不支持多对同名父子同时存在
        res_creation_input = {
            "id": res_id.split("/")[-1],
            "name": res_id.split("/")[-1],
            "class": class_name,
            "parent": parent.split("/")[-1],
            "position": {
                "x": bind_locations.x,
                "y": bind_locations.y,
                "z": bind_locations.z,
            },
        }
        # 注: 容器自身液体 (liquid_input_slot == [-1]) 不再通过 data.liquids 预埋
        # （initialize_resource 仅按 class+name 重建，data 会被丢弃），统一由设备侧
        # _append_resource_inner 在创建后通过 apply_substances 写入。
        init_new_res = initialize_resource(res_creation_input)  # flatten的格式
        if len(init_new_res) > 1:  # 一个物料，多个子节点
            init_new_res = [init_new_res]
        resources: List[Resource] | List[List[Resource]] = init_new_res  # initialize_resource已经返回list[dict]
        device_ids = [device_id.split("/")[-1]]
        bind_parent_id = [res_creation_input["parent"]]
        bind_location = [bind_locations]
        other_calling_param = [
            json.dumps(
                {
                    "ADD_LIQUID_TYPE": liquid_type,
                    "LIQUID_VOLUME": liquid_volume,
                    "LIQUID_INPUT_SLOT": liquid_input_slot,
                    "initialize_full": False,
                    "slot": slot_on_deck,
                }
            )
        ]
        response: List[str] = await self.create_resource_detailed(
            resources, device_ids, bind_parent_id, bind_location, other_calling_param
        )

        assert len(response) == 1, "Create Resource应当只返回一个结果"
        for i in response:
            res = json.loads(i)
            if "suc" in res and not res["suc"]:
                raise ValueError(res.get("error", "未知错误"))
            return res
        raise ValueError(f"创建资源时失败！响应为空")

    def initialize_device(self, device_id: str, device_config: ResourceDictInstance) -> None:
        """初始化本地设备并在 ROS 动作端点全部就绪后宣告可调度。

        参数：``device_id`` 是设备实例唯一身份；``device_config`` 是包含设备定义、
        稳定 UUID 与初始化参数的物理资源图（Physical Resource Graph）实例。
        返回：无；驱动定义非法或既有设备初始化返回空结果时保留原有跳过语义。
        异常：必需 ROS 动作端点未在共享等待预算内就绪时抛出 ``RuntimeError``，
        同时禁止把设备加入在线集合或上报动作空闲状态。
        """
        self.lab_logger().info(f"[Host Node] Initializing device: {device_id}")

        try:
            d = initialize_device_from_dict(device_id, device_config)
        except DeviceClassInvalid as e:
            self.lab_logger().error(f"[Host Node] Device class invalid: {e}")
            d = None
        if d is None:
            return
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
                        f"创建ActionClient失败，Device: {device_id}, Action Name: {action_name}, Action Type: {action_type}, Error: {e}")
                    continue
                self.lab_logger().trace(
                    f"[Host Node] Created ActionClient (Local): {action_id}"
                )  # 子设备再创建用的是Discover发现的
                new_action_pairs.append((device_id, action_name))
            else:
                self.lab_logger().warning(f"[Host Node] ActionClient {action_id} already exists.")
        # 锁上报需全量：auto-/UniLabJsonCommand 动作虽不建 ActionClient，但仍是可经
        # _execute_driver_command 调用的能力(如 workbench 的 prepare_materials 等)，必须一并
        # 上报 free 锁，与 report_all_action_locks 的全量快照保持一致。_execute_driver_command
        # [_async] 由 _report_action_locks_free 统一过滤。
        # noinspection PyProtectedMember
        already = {action_name for _, action_name in new_action_pairs}
        for action_name in d._ros_node._action_value_mappings.keys():
            if action_name in already:
                continue
            new_action_pairs.append((device_id, action_name))
        # ``required_endpoint_ids`` 是该设备全部业务动作真正依赖的原生或通用 ROS 端点。
        required_endpoint_ids = required_action_endpoint_ids(
            device_id,
            d._ros_node._action_value_mappings,
        )
        # 本地服务端与客户端在同一启动阶段创建；给 DDS 发现一个有界共享预算，
        # 仍未就绪就按关闭式失败处理，绝不提前宣告设备可调度。
        if not wait_for_action_endpoints(
            self._action_clients,
            required_endpoint_ids,
            wait_timeout=2.0,
        ):
            transport_error = (
                "device_action_transport_not_ready: "
                f"device={device_id}, required_endpoints={required_endpoint_ids}"
            )
            self.lab_logger().error(transport_error)
            raise RuntimeError(transport_error)
        device_key = f"{self.devices_names[device_id]}/{device_id}"  # 这里不涉及二级device_id
        # 添加到在线设备列表
        self._online_devices.add(device_key)
        # 新注册本地设备 action 后主动上报其 free 锁状态
        self._report_action_locks_free(new_action_pairs)

    def update_device_status_subscriptions(self) -> None:
        """
        更新设备状态订阅

        扫描所有设备话题，为新的话题创建订阅，确保不会重复订阅
        """
        topic_names_and_types = self.get_topic_names_and_types()
        for topic, types in topic_names_and_types:
            # 检查是否为设备状态话题且未订阅过
            if (
                topic.startswith("/devices/")
                and not types[0].endswith("FeedbackMessage")
                and "_action" not in topic
                and topic not in self._subscribed_topics
            ):

                # 解析设备名和属性名
                parts = topic.split("/")
                if len(parts) >= 4:  # 可能有WorkstationNode，创建更长的设备
                    device_id = "/".join(parts[2:-1])
                    property_name = parts[-1]

                    # 初始化设备状态字典
                    if device_id not in self.device_status:
                        self.device_status[device_id] = {}
                        self.device_status_timestamps[device_id] = {}

                    # 默认初始化属性值为 None
                    self.device_status[device_id] = collections.defaultdict()
                    self.device_status_timestamps[device_id][property_name] = 0  # 初始化时间戳

                    # 动态创建订阅
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
                            # 标记为已订阅
                            self._subscribed_topics.add(topic)
                            self.lab_logger().trace(f"[Host Node] Subscribed to new topic: {topic}")
                    except (NameError, SyntaxError) as e:
                        self.lab_logger().error(f"[Host Node] Failed to create subscription for topic {topic}: {e}")

    """设备相关"""

    def _initialize_configured_device(
        self,
        device_id: str,
        device_config: ResourceDictInstance,
    ) -> None:
        """按当前进程语义初始化真实驱动或注册无 I/O 的模拟设备。

        参数：``device_id`` 是物理图设备身份；``device_config`` 是冻结的设备实例。
        返回：无；托管 dry-run Edge 只安装动作目录，其他进程继续调用真实初始化。
        异常：设备定义无法唯一解析或动作目录结构非法时关闭式失败。
        """

        if BasicConfig.process_role == "edge_runtime" and _device_is_simulated(self, device_id):
            self._register_simulated_device(device_id, device_config)
            return
        self.initialize_device(device_id, device_config)

    def _register_simulated_device(
        self,
        device_id: str,
        device_config: ResourceDictInstance,
    ) -> None:
        """从 Registry 投影可调度能力，绝不加载或构造设备驱动。

        参数：``device_id`` 是物理图设备身份；``device_config`` 提供唯一设备定义。
        返回：无；建立 Edge 注册和模拟动作回执所需的目录与在线事实。
        异常：定义缺失、歧义或动作目录不是对象时抛出 ``DeviceClassInvalid``。
        """

        definition_identity = device_config.res_content.klass
        if not isinstance(definition_identity, str) or not definition_identity.strip():
            raise DeviceClassInvalid(
                f"Device [{device_id}] class must be a non-empty string"
            )
        try:
            registry_entry = lab_registry.resolve_definition(
                "device",
                definition_identity,
            )
        except Exception as error:
            raise DeviceClassInvalid(
                f"Device [{device_id}] class {definition_identity} cannot be uniquely resolved"
            ) from error
        class_entry = (
            registry_entry.get("class")
            if isinstance(registry_entry, dict)
            else None
        )
        action_mappings = (
            class_entry.get("action_value_mappings")
            if isinstance(class_entry, dict)
            else None
        )
        if not isinstance(action_mappings, dict):
            raise DeviceClassInvalid(
                f"Device [{device_id}] class {definition_identity} has no valid action catalog"
            )

        namespace = f"/devices/{device_id}"
        device_key = f"{namespace}/{device_id}"
        frozen_mappings = deepcopy(action_mappings)
        self.devices_names[device_id] = namespace
        self.device_machine_names[device_id] = "dry-run"
        self._action_value_mappings[device_id] = frozen_mappings
        self.device_status.setdefault(device_id, {})
        self.device_status_timestamps.setdefault(device_id, {})
        self._online_devices.add(device_key)
        self._simulated_device_keys.add(device_key)
        self._report_action_locks_free(
            [(device_id, action_name) for action_name in frozen_mappings]
        )
        self.lab_logger().info(
            f"[Host Node] Dry-run device catalog registered: {device_id} "
            f"({len(frozen_mappings)} actions); hardware initialization skipped"
        )

    def property_callback(self, msg, device_id: str, property_name: str) -> None:
        """
        更新设备状态字典中的属性值，并发送到桥接器。

        Args:
            msg: 接收到的消息
            device_id: 设备ID
            property_name: 属性名称
        """
        # 更新设备状态字典
        if hasattr(msg, "data"):
            bChange = False
            bCreate = False
            if isinstance(msg.data, (float, int, str)):
                if property_name not in self.device_status[device_id]:
                    bCreate = True
                    bChange = True
                    self.device_status[device_id][property_name] = msg.data
                elif (
                    type(self.device_status[device_id][property_name]) is not type(msg.data)
                    or self.device_status[device_id][property_name] != msg.data
                ):
                    bChange = True
                    self.device_status[device_id][property_name] = msg.data
                # 更新时间戳
                self.device_status_timestamps[device_id][property_name] = time.time()
            else:
                self.lab_logger().debug(
                    f"[Host Node] Unsupported data type for {device_id}/{property_name}: {type(msg.data)}"
                )

            # 所有 Bridge 对象都应具有 publish_device_status 方法；都会收到设备状态更新
            if bChange:
                for bridge in self.bridges:
                    if hasattr(bridge, "publish_device_status"):
                        bridge.publish_device_status(self.device_status, device_id, property_name)
                        if bCreate:
                            self.lab_logger().trace(f"Status created: {device_id}.{property_name} = {msg.data}")
                        else:
                            self.lab_logger().trace(f"Status updated: {device_id}.{property_name} = {msg.data}")

    def send_goal(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, str],
        server_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        向设备发送目标请求

        Args:
            action_type: 动作类型
            action_kwargs: 动作参数
            server_info: 服务器发送信息，包含发送时间戳等
        """
        u = uuid.UUID(item.job_id)
        device_id = item.device_id
        action_name = item.action_name

        if _device_is_simulated(self, device_id):
            action_id = f"/devices/{device_id}/{action_name}"
            self.lab_logger().info(
                f"[SIMULATE ACTION] 模拟执行: {action_id} "
                f"(job={item.job_id[:8]}), 参数: {str(action_kwargs)[:500]}"
            )
            # 根据注册表 handles 构建模拟返回值
            mock_return = self._build_simulated_action_return(
                device_id, action_name, action_kwargs
            )
            from unilabos.app.edge_control.execution_start import confirm_execution_start
            confirm_execution_start(item)
            mechanisms = getattr(self, "_mechanisms", None)
            if mechanisms is not None and mechanisms.has(device_id, action_name):
                # 资产行为类绑定了这个动作：按机构运动的时长执行到完成，再回报结果。
                self._publish_job_started(item)
                mechanisms.execute(
                    device_id, action_name, action_kwargs,
                    on_success=lambda evidence: self._handle_simulated_action_result(
                        item, action_id, {**mock_return, "mechanism": evidence}, started=True),
                    on_failure=lambda code, reason: self._handle_simulated_action_failure(
                        item, action_id, code, reason),
                )
                return
            self._handle_simulated_action_result(item, action_id, mock_return)
            return
        if getattr(self, "_mechanisms", None) is not None:
            # 由驱动执行的设备：记下动作，开始/结束时让画面按同一资产行为类推演（不影响动作本身）。
            self._mechanisms.remember(item.job_id, device_id, action_name, action_kwargs)

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
        if action_name == "test_latency" and server_info is not None:
            self.server_latest_timestamp = server_info.get("send_timestamp", 0.0)
        if action_id not in self._action_clients:
            raise ValueError(f"ActionClient {action_id} not found.")

        action_client: ActionClient = self._action_clients[action_id]
        goal_msg = convert_to_ros_msg(action_client._action_type.Goal(), action_kwargs)

        target_wrapper = self.devices_instances.get(device_id)
        target_node = getattr(target_wrapper, "_ros_node", None) if target_wrapper is not None else None
        if target_node is not None and hasattr(target_node, "register_job_context"):
            target_node.register_job_context(
                item.job_id,
                item.task_id,
                item.action_name,
                trace_context=item.trace_context,
                station_controlled=getattr(item, "start_guard", None) is not None,
                execution_source=getattr(item, "execution_source", "workflow"),
                start_guard=getattr(item, "start_guard", None),
            )

        # self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {str(goal_msg)[:1000]}")
        self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {action_kwargs}")
        self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {goal_msg}")
        action_client.wait_for_server()
        goal_uuid_obj = UUID(uuid=list(u.bytes))

        if getattr(item, "start_guard", None) is not None and target_node is None:
            # host 自己执行的动作没有独立设备节点，开始屏障由 Host 在发送前确认。
            if device_id == self.device_id:
                from unilabos.app.edge_control.execution_start import confirm_execution_start

                confirm_execution_start(item)
            else:
                raise RuntimeError("设备适配器未提供实际动作开始屏障")

        future = action_client.send_goal_async(
            goal_msg,
            feedback_callback=lambda feedback_msg: self.feedback_callback(item, action_id, feedback_msg),
            goal_uuid=goal_uuid_obj,
        )
        future.add_done_callback(lambda f: self.goal_response_callback(item, action_id, f))

    def resolve_unknown_device_command(
        self,
        device_id: str,
        device_command_id: str,
        resolution_command_uuid: str,
        reason: str,
    ) -> Dict[str, Any]:
        """处理 Backend UNKNOWN；durable 驱动优先提交其本地命令日志。"""

        driver = self._device_driver(device_id)
        if driver is None:
            raise ValueError(f"设备 {device_id} 不存在或驱动尚未就绪，拒绝 UNKNOWN 命令人工对账")
        resolver = getattr(driver, "resolve_unknown_command", None)
        if not callable(resolver):
            dispatch_block_reason = self.device_dispatch_block_reason(device_id)
            unknown_command_ids = self.device_unknown_command_ids(device_id)
            if dispatch_block_reason or unknown_command_ids:
                raise ValueError(f"设备 {device_id} 不支持 UNKNOWN 命令人工对账")
            return {
                "command_id": device_command_id,
                "state": "CANCELED",
                "previous_state": "UNKNOWN",
                "resolution_committed": True,
                "resolution_command_uuid": resolution_command_uuid,
                "message": reason,
                "durable_journal": False,
            }
        result = resolver(
            device_command_id,
            resolution_command_uuid=resolution_command_uuid,
            reason=reason,
        )
        if not isinstance(result, dict):
            raise TypeError(f"设备 {device_id} 返回了无效的 UNKNOWN 对账结果")
        return result

    def device_dispatch_block_reason(self, device_id: str) -> str:
        """读取设备驱动声明的生产派发阻断原因；空字符串表示可派发。"""

        driver = self._device_driver(device_id)
        reader = getattr(driver, "dispatch_block_reason", None)
        if not callable(reader):
            return ""
        return str(reader() or "").strip()

    def device_unknown_command_ids(self, device_id: str) -> List[str]:
        """读取设备驱动声明的结构化 UNKNOWN 命令身份。"""

        reader = getattr(self._device_driver(device_id), "unknown_command_ids", None)
        if not callable(reader):
            return []
        return [str(command_id).strip() for command_id in reader() if str(command_id).strip()]

    def retire_settled_device_command(self, device_command_id: str) -> int:
        """向所有持久设备驱动广播 Backend 已确认的物理结算身份。

        ``device_command_id`` 是工作流节点作业（WorkflowNodeJob）派生的稳定
        设备命令身份；返回实际退役记录数。重复 ACK 必须保持幂等；驱动拥有
        自己的账本语义，UNKNOWN 和非终态记录必须拒删，驱动异常原样传播以便
        上层保留事件并重试。
        """

        command_id = str(device_command_id).strip()
        if not command_id:
            return 0
        retired = 0
        for device_id in list(self.devices_instances):
            retire = getattr(
                self._device_driver(str(device_id)),
                "retire_settled_command",
                None,
            )
            if callable(retire) and bool(retire(command_id)):
                retired += 1
        return retired

    def _device_driver(self, device_id: str) -> Any:
        """隐藏 HostNode 设备包装器的内部导航并返回底层驱动实例。"""

        wrapper = self.devices_instances.get(device_id)
        target_node = getattr(wrapper, "_ros_node", None) if wrapper is not None else None
        return getattr(target_node, "driver_instance", None)

    def _build_simulated_action_return(
        self, device_id: str, action_name: str, action_kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按冻结动作合同构建模拟动作回执。

        同名输出优先透传输入，保证物料（Material）在动作链中保持
        UUID；其余字段按 JSON 结果模式生成确定性值。旧 ``handles``
        合同继续根据 ``@flatten`` 层数构造数组。
        """

        def schema_value(schema: Any) -> Any:
            if not isinstance(schema, dict):
                return None
            enum_values = schema.get("enum")
            if isinstance(enum_values, list) and enum_values:
                return "SUCCEEDED" if "SUCCEEDED" in enum_values else deepcopy(enum_values[0])
            value_type = schema.get("type")
            if value_type == "boolean":
                return True
            if value_type == "string":
                return ""
            if value_type == "integer":
                return 0
            if value_type == "number":
                return 0.0
            if value_type == "array":
                return []
            if value_type == "object":
                return {}
            return None

        mock_return: Dict[str, Any] = {
            "action_mode": "simulate",
            "action_name": action_name,
        }
        action_mappings = self._action_value_mappings.get(device_id, {})
        action_mapping = action_mappings.get(action_name, {})
        handles = action_mapping.get("handles", {})
        if isinstance(handles, dict):
            for output_handle in handles.get("output", []):
                data_key = output_handle.get("data_key", "")
                handler_key = output_handle.get("handler_key", "")
                output_key = handler_key or data_key.split(".@flatten", 1)[0]
                if not output_key:
                    continue
                if output_key in action_kwargs:
                    mock_return[output_key] = deepcopy(action_kwargs[output_key])
                    continue
                # 根据 @flatten 层数构建嵌套数组，叶子为空字典
                flatten_count = data_key.count("@flatten")
                value: Any = {}
                for _ in range(flatten_count):
                    value = [value]
                mock_return[output_key] = value

        result_schema = action_mapping.get("result", {})
        if not (
            isinstance(result_schema, dict)
            and isinstance(result_schema.get("properties"), dict)
        ):
            action_schema = action_mapping.get("schema", {})
            action_properties = (
                action_schema.get("properties", {})
                if isinstance(action_schema, dict)
                else {}
            )
            result_schema = (
                action_properties.get("result", {})
                if isinstance(action_properties, dict)
                else {}
            )
        if isinstance(result_schema, dict):
            properties = result_schema.get("properties", {})
            if isinstance(properties, dict):
                for output_key, property_schema in properties.items():
                    if output_key in action_kwargs:
                        mock_return[output_key] = deepcopy(action_kwargs[output_key])
                    else:
                        mock_return[output_key] = schema_value(property_schema)
        return mock_return

    def _handle_simulated_action_failure(
        self, item: "QueueItem", action_id: str, code: str, reason: str
    ) -> None:
        """机构动作被资产拒绝或执行出错：按失败作业回报，不伪造成功。"""

        self.lab_logger().warning(
            f"[SIMULATE ACTION] {action_id} ({item.job_id[:8]}) failed: {reason}"
        )
        failure = {"action_mode": "simulate", "action_name": item.action_name, "mechanism_error": code}
        return_info = serialize_result_info(reason, False, failure)
        from unilabos.app.web.controller import store_job_result
        store_job_result(item.job_id, "failed", return_info, failure)
        for bridge in self.bridges:
            if hasattr(bridge, "publish_job_status"):
                bridge.publish_job_status(failure, item, "failed", return_info)

    def _handle_simulated_action_result(
        self, item: "QueueItem", action_id: str, mock_return: Dict[str, Any], *, started: bool = False
    ) -> None:
        """
        模拟动作模式下直接构建结果并走正常的结果回调流程（跳过 ROS）

        ``started`` 为 True 表示开始通知已经发过（按时长执行的机构动作）。
        """
        job_id = item.job_id
        status = "success"
        return_info = serialize_result_info("", True, mock_return)

        self.lab_logger().info(
            f"[SIMULATE ACTION] Result for {action_id} ({job_id[:8]}): {status}"
        )

        from unilabos.app.web.controller import store_job_result
        store_job_result(job_id, status, return_info, mock_return)

        if not started:
            self._publish_job_started(item)

        # 发布状态到桥接器
        for bridge in self.bridges:
            if hasattr(bridge, "publish_job_status"):
                bridge.publish_job_status(mock_return, item, status, return_info)

    def goal_response_callback(self, item: "QueueItem", action_id: str, future) -> None:
        """目标响应回调"""
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.lab_logger().warning(f"[Host Node] Goal {item.action_name} ({item.job_id}) rejected")
            return_info = serialize_result_info("ROS goal rejected", False, {})
            for bridge in self.bridges:
                if hasattr(bridge, "publish_job_status"):
                    bridge.publish_job_status({}, item, "failed", return_info)
            return

        self.lab_logger().info(f"[Host Node] Goal {action_id} ({item.job_id}) accepted")
        self._publish_job_started(item)
        if getattr(self, "_mechanisms", None) is not None:
            self._mechanisms.job_started(item.job_id)
        self._goals[item.job_id] = goal_handle
        goal_future = goal_handle.get_result_async()
        goal_future.add_done_callback(lambda f: self.get_result_callback(item, action_id, f))
        goal_future.result()

    def _publish_job_started(self, item: "QueueItem") -> None:
        """ROS 接受 Goal 后，通过支持该能力的 Bridge 发送短 started 通知。"""

        for bridge in self.bridges:
            if hasattr(bridge, "publish_job_started"):
                bridge.publish_job_started(item)

    def feedback_callback(self, item: "QueueItem", action_id: str, feedback_msg) -> None:
        """反馈回调"""
        feedback_data = convert_from_ros_msg(feedback_msg)
        feedback_data.pop("goal_id")
        self.lab_logger().trace(f"[Host Node] Feedback for {action_id} ({item.job_id}): {feedback_data}")

        for bridge in self.bridges:
            if hasattr(bridge, "publish_job_status"):
                bridge.publish_job_status(feedback_data, item, "running")

    def get_result_callback(self, item: "QueueItem", action_id: str, future) -> None:
        """获取结果回调"""
        job_id = item.job_id

        try:
            result = future.result()
            result_msg = result.result
            goal_status = result.status

            # 检查是否是被取消的任务
            if goal_status == GoalStatus.STATUS_CANCELED:
                self.lab_logger().info(f"[Host Node] Goal {action_id} ({job_id[:8]}) was cancelled")
                status = "failed"
                return_info = serialize_result_info("Job was cancelled", False, {})
                # ``status=failed`` 保持既有 Bridge wire 兼容；稳定分类让本地调度器
                # 能区分“设备明确取消”与普通业务失败，并据此安全释放执行占用。
                return_info["suc_type"] = "canceled"
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
                        self.lab_logger().critical("错误的return_info类型，请断点修复")
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

            self.lab_logger().info(f"[Host Node] Result for {action_id} ({job_id[:8]}): {status}")
            if getattr(self, "_mechanisms", None) is not None:
                self._mechanisms.job_finished(job_id, status == "success")
            if goal_status != GoalStatus.STATUS_CANCELED:
                self.lab_logger().trace(f"[Host Node] Result data: {result_data}")

            # 清理 _goals 中的记录
            if job_id in self._goals:
                del self._goals[job_id]
                self.lab_logger().trace(f"[Host Node] Removed goal {job_id[:8]} from _goals")

            # 存储结果供 HTTP API 查询
            try:
                from unilabos.app.web.controller import store_job_result

                if goal_status == GoalStatus.STATUS_CANCELED:
                    store_job_result(job_id, status, return_info, {})
                else:
                    store_job_result(job_id, status, return_info, result_data)
            except ImportError:
                pass  # controller 模块可能未加载

            # 发布状态到桥接器
            if job_id:
                for bridge in self.bridges:
                    if hasattr(bridge, "publish_job_status"):
                        if goal_status == GoalStatus.STATUS_CANCELED:
                            bridge.publish_job_status({}, item, status, return_info)
                        else:
                            bridge.publish_job_status(result_data, item, status, return_info)

        except Exception as e:
            self.lab_logger().error(
                f"[Host Node] Error in get_result_callback for {action_id} ({job_id[:8]}): {str(e)}"
            )
            import traceback

            self.lab_logger().error(traceback.format_exc())

            # 清理 _goals 中的记录
            if job_id in self._goals:
                del self._goals[job_id]

            # 发布失败状态
            for bridge in self.bridges:
                if hasattr(bridge, "publish_job_status"):
                    bridge.publish_job_status(
                        {}, item, "failed", serialize_result_info(f"Callback error: {str(e)}", False, {})
                    )

    def cancel_goal(
        self,
        goal_uuid: str,
        on_response=None,
    ) -> bool:
        """异步请求取消一个 ROS Goal。

        参数：``goal_uuid`` 是作业 UUID；``on_response`` 可选地接收执行器是否明确
        接受取消。返回：找到 Goal 并成功发起异步请求时为真，否则为假。异常：
        发起请求异常向调用方传播；异步响应异常会记录并通过回调返回假。

        返回真只表示请求已发出；只有 ``on_response(True)`` 表示执行器已受理，
        最终安全停止仍以 ``get_result_callback`` 的取消终态为准。
        """
        if goal_uuid in self._goals:
            self.lab_logger().info(f"[Host Node] Cancelling goal {goal_uuid[:8]}")
            goal_handle = self._goals[goal_uuid]

            # 发起异步取消请求
            cancel_future = goal_handle.cancel_goal_async()

            # 添加取消完成的回调
            cancel_future.add_done_callback(
                lambda future: self._cancel_goal_callback(
                    goal_uuid,
                    future,
                    on_response=on_response,
                )
            )
            return True
        else:
            self.lab_logger().warning(f"[Host Node] Goal {goal_uuid[:8]} not found in _goals, cannot cancel")
            return False

    def _cancel_goal_callback(self, goal_uuid: str, future, on_response=None) -> None:
        """记录 ROS 取消响应并回传明确受理事实。

        参数：``goal_uuid`` 是作业 UUID；``future`` 持有 ROS 取消响应；
        ``on_response`` 可选地接收布尔受理结果。返回无。异常：ROS Future 异常仅
        记录并回调假，不把“请求已发出”误当成“执行器已接受”。
        """

        accepted = False
        try:
            cancel_response = future.result()
            if cancel_response.goals_canceling:
                accepted = True
                self.lab_logger().info(f"[Host Node] Goal {goal_uuid[:8]} cancel request accepted")
            else:
                self.lab_logger().warning(f"[Host Node] Goal {goal_uuid[:8]} cancel request rejected")
        except Exception as e:
            self.lab_logger().error(f"[Host Node] Error cancelling goal {goal_uuid[:8]}: {str(e)}")
            import traceback

            self.lab_logger().error(traceback.format_exc())
        finally:
            if callable(on_response):
                try:
                    on_response(accepted)
                except Exception as callback_error:  # noqa: BLE001
                    self.lab_logger().error(
                        f"[Host Node] Error reporting cancel response for "
                        f"{goal_uuid[:8]}: {callback_error}"
                    )

    def get_goal_status(self, job_id: str) -> int:
        """获取目标状态"""
        if job_id in self._goals:
            g = self._goals[job_id]
            status = g.status
            self.lab_logger().debug(f"[Host Node] Goal status for {job_id}: {status}")
            return status
        self.lab_logger().warning(f"[Host Node] Goal {job_id} not found, status unknown")
        return GoalStatus.STATUS_UNKNOWN

    """Controller Node"""

    def initialize_controller(self, controller_id: str, controller_config: Dict[str, Any]) -> None:
        """
        初始化控制器

        Args:
            controller_id: 控制器ID
            controller_config: 控制器配置
        """
        self.lab_logger().info(f"[Host Node] Initializing controller: {controller_id}")

        class_name = controller_config.pop("type")
        controller_func = globals()[class_name]

        for input_name, input_info in controller_config["inputs"].items():
            controller_config["inputs"][input_name]["type"] = get_msg_type(eval(input_info["type"]))
        for output_name, output_info in controller_config["outputs"].items():
            controller_config["outputs"][output_name]["type"] = get_msg_type(eval(output_info["type"]))

        if controller_config["parameters"] is None:
            controller_config["parameters"] = {}

        controller = ControllerNode(controller_id, controller_func=controller_func, **controller_config)
        self.lab_logger().info(f"[Host Node] Controller {controller_id} created.")
        # rclpy.get_global_executor().add_node(controller)

    """Resource"""

    def _init_host_service(self):
        self._resource_services: Dict[str, Service] = {
            "resource_add": self.create_service(
                ResourceAdd, "/resources/add", self._resource_add_callback, callback_group=self.callback_group
            ),
            "resource_get": self.create_service(
                SerialCommand, "/resources/get", self._resource_get_callback, callback_group=self.callback_group
            ),
            "resource_delete": self.create_service(
                ResourceDelete,
                "/resources/delete",
                self._resource_delete_callback,
                callback_group=self.callback_group,
            ),
            "resource_update": self.create_service(
                ResourceUpdate,
                "/resources/update",
                self._resource_update_callback,
                callback_group=self.callback_group,
            ),
            "resource_list": self.create_service(
                ResourceList, "/resources/list", self._resource_list_callback, callback_group=self.callback_group
            ),
            "c2s_update_resource_tree": self.create_service(
                SerialCommand,
                "/c2s_update_resource_tree",
                self._resource_tree_update_callback,
                callback_group=self.callback_group,
            ),
        }

    async def _resource_tree_action_add_callback(self, data: dict, response: SerialCommand_Response):  # OK
        resource_tree_set = ResourceTreeSet.load(data["data"])
        mount_uuid = data["mount_uuid"]
        first_add = data["first_add"]

        self.lab_logger().info(
            f"[Host Node-Resource] Loaded ResourceTreeSet with {len(resource_tree_set.trees)} trees, "
            f"{len(resource_tree_set.all_nodes)} total nodes"
        )

        # 处理资源添加逻辑
        success = True
        uuid_mapping = {}
        legacy_http_bridge = next(
            (
                bridge
                for bridge in self.bridges
                if hasattr(bridge, "resource_tree_add")
            ),
            None,
        )
        if legacy_http_bridge is not None:

            resource_start_time = time.time()
            uuid_mapping = legacy_http_bridge.resource_tree_add(
                resource_tree_set,
                mount_uuid,
                first_add,
            )
            resource_end_time = time.time()
            self.lab_logger().info(
                f"[Host Node-Resource] 物料创建上传 {round(resource_end_time - resource_start_time, 5) * 1000} ms"
            )
            if uuid_mapping:
                self.lab_logger().info(f"[Host Node-Resource] UUID映射: {len(uuid_mapping)} 个节点")

        if success:
            from unilabos.resources.graphio import physical_setup_graph
            from unilabos.resources.resource_tracker import TRACKER_STATE_KEYS

            # 将资源添加到本地图中
            for node in resource_tree_set.all_nodes:
                resource_dict = node.res_content.model_dump(by_alias=True)
                if resource_dict.get("id") not in physical_setup_graph.nodes:
                    physical_setup_graph.add_node(resource_dict["id"], **resource_dict)
                else:
                    graph_node = physical_setup_graph.nodes[resource_dict["id"]]
                    graph_node["data"].update(resource_dict.get("data", {}))
                    # 液体状态在根字段（与 dump 形态一致），已存在节点同步刷新，避免与 data 双真相漂移
                    for state_key in TRACKER_STATE_KEYS:
                        if resource_dict.get(state_key) is not None:
                            graph_node[state_key] = resource_dict[state_key]
                    # 条码同样以根字段为准，同步刷新避免与 config 双真相漂移
                    if resource_dict.get("barcode"):
                        graph_node["barcode"] = resource_dict["barcode"]
                        graph_node["barcode_symbology"] = resource_dict.get("barcode_symbology") or ""

        response.response = _fast_dumps_str(uuid_mapping) if success else "FAILED"
        self.lab_logger().info(f"[Host Node-Resource] Resource tree add completed, success: {success}")

    async def _resource_tree_action_get_callback(self, data: dict, response: SerialCommand_Response):
        """ROS 查询与驱动查询使用同一份 Backend 身份投影，不回落本地配置。"""
        from unilabos.ros.nodes.resource_slot_hydration import query_production_resource_nodes_sync

        response.response = ""
        try:
            requested = data["data"]
            nodes = query_production_resource_nodes_sync(requested)
            if not data.get("with_children", True):
                nodes = [node for node in nodes if node.get("uuid") in set(requested)]
            response.response = json.dumps(nodes)
        except Exception as error:
            self.lab_logger().error(f"[Host Node-Resource] Backend 资源投影查询失败: {error}")

    async def _resource_tree_action_remove_callback(self, data: dict, response: SerialCommand_Response):
        """
        子节点通知Host物料树删除
        """
        self.lab_logger().info(f"[Host Node-Resource] Resource tree remove request received")
        response.response = "OK"
        self.lab_logger().info(f"[Host Node-Resource] Resource tree remove completed")

    async def _resource_tree_action_update_callback(self, data: dict, response: SerialCommand_Response):
        """
        子节点通知Host物料树更新
        """
        resource_tree_set = ResourceTreeSet.load(data["data"])

        self.lab_logger().info(
            f"[Host Node-Resource] Loaded ResourceTreeSet with {len(resource_tree_set.trees)} trees, "
            f"{len(resource_tree_set.all_nodes)} total nodes"
        )

        uuid_to_trees: Dict[str, List[ResourceTreeInstance]] = collections.defaultdict(list)
        for tree in resource_tree_set.trees:
            uuid_to_trees[tree.root_node.res_content.parent_uuid].append(tree)

        for uid, trees in uuid_to_trees.items():
            new_tree_set = ResourceTreeSet(trees)
            self.lab_logger().info(
                f"[Host Node-Resource] 物料 {[root_node.res_content.id for root_node in new_tree_set.root_nodes]} {uid} 挂载 {trees[0].root_node.res_content.parent_uuid} 请求更新上传"
            )
            legacy_http_bridge = next(
                (
                    bridge
                    for bridge in self.bridges
                    if hasattr(bridge, "resource_tree_add")
                ),
                None,
            )
            uuid_mapping = {}
            if legacy_http_bridge is not None:
                resource_start_time = time.time()
                uuid_mapping = legacy_http_bridge.resource_tree_add(
                    new_tree_set,
                    uid,
                    False,
                )
                resource_end_time = time.time()
                self.lab_logger().info(
                    f"[Host Node-Resource] 物料更新上传 {round(resource_end_time - resource_start_time, 5) * 1000} ms"
                )
            if uuid_mapping:
                self.lab_logger().info(f"[Host Node-Resource] UUID映射: {len(uuid_mapping)} 个节点")
            # 还需要加入到资源图中，暂不实现，考虑资源图新的获取方式
            response.response = json.dumps(uuid_mapping)
            self.lab_logger().info(
                "[Host Node-Resource] Resource tree update completed, "
                f"legacy_cloud_sync={legacy_http_bridge is not None}"
            )

    async def _resource_tree_update_callback(self, request: SerialCommand_Request, response: SerialCommand_Response):
        """
        子节点通知Host物料树更新

        接收序列化的 ResourceTreeSet 数据并进行处理
        """
        try:
            # 解析请求数据
            data = _fast_loads(request.command)
            action = data["action"]
            inner = data.get("data", {})
            if action == "add":
                mount_uuid = inner.get("mount_uuid", "?")[:8] if isinstance(inner, dict) else "?"
                tree_data = inner.get("data", []) if isinstance(inner, dict) else inner
                node_count = len(tree_data) if isinstance(tree_data, list) else "?"
                source = f"mount={mount_uuid}.. nodes≈{node_count}"
            elif action in ("get", "remove"):
                uid_list = inner.get("data", inner) if isinstance(inner, dict) else inner
                source = f"uuids={len(uid_list) if isinstance(uid_list, list) else '?'}"
            elif action == "update":
                tree_data = inner.get("data", []) if isinstance(inner, dict) else inner
                node_count = len(tree_data) if isinstance(tree_data, list) else "?"
                source = f"nodes≈{node_count}"
            else:
                source = ""
            self.lab_logger().info(
                f"[Host Node-Resource] Resource tree {action} request received ({source})"
            )
            data = data["data"]
            if action == "add":
                await self._resource_tree_action_add_callback(data, response)
            elif action == "get":
                await self._resource_tree_action_get_callback(data, response)
            elif action == "update":
                await self._resource_tree_action_update_callback(data, response)
            elif action == "remove":
                await self._resource_tree_action_remove_callback(data, response)
            else:
                self.lab_logger().error(f"[Host Node-Resource] Invalid action: {action}")
                response.response = "ERROR"
        except Exception as e:
            self.lab_logger().error(f"[Host Node-Resource] Error adding resource tree: {e}")
            self.lab_logger().error(traceback.format_exc())
            response.response = f"ERROR: {str(e)}"

        return response

    def _resource_add_callback(self, request, response):
        """
        添加资源回调

        处理添加资源请求，将资源数据传递到桥接器

        Args:
            request: 包含资源数据的请求对象
            response: 响应对象

        Returns:
            响应对象，包含操作结果
        """
        resources = [convert_from_ros_msg(resource) for resource in request.resources]
        self.lab_logger().info(f"[Host Node-Resource] Add request received: {len(resources)} resources")

        success = True
        legacy_resource_bridge = next(
            (
                bridge
                for bridge in self.bridges
                if hasattr(bridge, "resource_add")
            ),
            None,
        )
        if legacy_resource_bridge is not None:
            r = legacy_resource_bridge.resource_add(add_schema(resources))
            success = bool(r)

        response.success = success

        if success:
            from unilabos.resources.graphio import physical_setup_graph

            for resource in resources:
                if resource.get("id") not in physical_setup_graph.nodes:
                    physical_setup_graph.add_node(resource["id"], **resource)
                else:
                    from unilabos.resources.resource_tracker import TRACKER_STATE_KEYS
                    graph_node = physical_setup_graph.nodes[resource["id"]]
                    graph_node["data"].update(resource.get("data", {}))
                    # 已存在节点同步刷新根字段，避免与 data/config 双真相漂移（与
                    # _resource_tree_add 路径的刷新保持一致）。
                    data = resource.get("data") or {}
                    for state_key in TRACKER_STATE_KEYS:
                        if state_key in data:
                            graph_node[state_key] = data[state_key]
                    barcode = (resource.get("config") or {}).get("barcode")
                    if isinstance(barcode, dict):
                        graph_node["barcode"] = barcode.get("data", "")
                        graph_node["barcode_symbology"] = barcode.get("symbology", "")

        self.lab_logger().info(f"[Host Node-Resource] Add request completed, success: {success}")
        return response

    def _resource_get_process(self, data: Dict[str, Any]):
        r = data["data"]
        self.lab_logger().debug(f"[Host Node-Resource] Retrieved from bridge: {len(r)} resources")
        resources = [convert_to_ros_msg(Resource, resource) for resource in r]
        return resources

    def _resource_get_callback(self, request: SerialCommand.Request, response: SerialCommand.Response):
        """按 UUID 或注册别名读取 Backend 投影；错误时返回空响应，不使用启动图兜底。"""
        from unilabos.ros.nodes.resource_slot_hydration import (
            query_production_resource_by_local_id_sync,
            query_production_resource_nodes_sync,
        )

        response.response = ""
        try:
            data = json.loads(request.command)
            with_children = data.get("with_children", True)
            if resource_uuid := data.get("uuid"):
                nodes = query_production_resource_nodes_sync([resource_uuid])
                if not with_children:
                    nodes = [node for node in nodes if node.get("uuid") == resource_uuid]
            else:
                nodes = query_production_resource_by_local_id_sync(data.get("id") or "", with_children=with_children)
            response.response = json.dumps(nodes)
        except Exception as error:
            # 网络补查失败也必须留在服务边界内，不能终止 ROS 执行器。
            self.lab_logger().error(f"[Host Node-Resource] Backend 资源投影查询失败: {error}")
        return response

    def _resource_delete_callback(self, request, response):
        """
        删除资源回调

        处理删除资源请求，将删除指令传递到桥接器

        Args:
            request: 包含资源ID的请求对象
            response: 响应对象

        Returns:
            响应对象，包含操作结果
        """
        self.lab_logger().info(f"[Host Node-Resource] Delete request for ID: {request.id}")

        success = True
        legacy_resource_bridge = next(
            (
                bridge
                for bridge in self.bridges
                if hasattr(bridge, "resource_delete")
            ),
            None,
        )
        if legacy_resource_bridge is not None:
            try:
                r = legacy_resource_bridge.resource_delete(request.id)
                success = bool(r)
            except Exception as e:
                self.lab_logger().error(f"[Host Node-Resource] Error deleting resource: {str(e)}")

        response.success = success
        self.lab_logger().info(f"[Host Node-Resource] Delete request completed, success: {success}")
        return response

    def _resource_update_callback(self, request, response):
        """
        更新资源回调

        处理更新资源请求，将更新指令传递到桥接器

        Args:
            request: 包含资源数据的请求对象
            response: 响应对象

        Returns:
            响应对象，包含操作结果
        """
        resources = [convert_from_ros_msg(resource) for resource in request.resources]
        self.lab_logger().info(f"[Host Node-Resource] Update request received: {len(resources)} resources")

        success = True
        legacy_resource_bridge = next(
            (
                bridge
                for bridge in self.bridges
                if hasattr(bridge, "resource_update")
            ),
            None,
        )
        if legacy_resource_bridge is not None:
            try:
                r = legacy_resource_bridge.resource_update(add_schema(resources))
                success = bool(r)
            except Exception as e:
                self.lab_logger().error(f"[Host Node-Resource] Error updating resources: {str(e)}")

        response.success = success
        self.lab_logger().info(f"[Host Node-Resource] Update request completed, success: {success}")
        return response

    def _resource_list_callback(self, request, response):
        """
        列出资源回调

        处理列出资源请求，返回所有可用资源

        Args:
            request: 请求对象
            response: 响应对象

        Returns:
            响应对象，包含资源列表
        """
        self.lab_logger().info(f"[Host Node-Resource] List request received")
        # 这里可以实现返回资源列表的逻辑
        self.lab_logger().debug(f"[Host Node-Resource] List parameters: {request}")
        return response

    def test_latency(self) -> TestLatencyReturn:
        """
        测试网络延迟的action实现
        通过5次ping-pong机制校对时间误差并计算实际延迟

        Returns:
            TestLatencyReturn: 包含延迟测试结果的字典，包括：
                - avg_rtt_ms: 平均往返时间（毫秒）
                - avg_time_diff_ms: 平均时间差（毫秒）
                - max_time_error_ms: 最大时间误差（毫秒）
                - task_delay_ms: 实际任务延迟（毫秒），-1表示无法计算
                - raw_delay_ms: 原始时间差（毫秒），-1表示无法计算
                - test_count: 有效测试次数
                - status: 测试状态，"success"表示成功，"all_timeout"表示全部超时
        """
        import uuid as uuid_module

        self.lab_logger().info("=" * 60)
        self.lab_logger().info("开始网络延迟测试...")

        # 记录任务开始执行的时间
        task_start_time = time.time()

        # 进行5次ping-pong测试
        ping_results = []

        for i in range(5):
            self.lab_logger().info(f"第{i+1}/5次ping-pong测试...")

            # 生成唯一的ping ID
            ping_id = str(uuid_module.uuid4())

            # 记录发送时间
            send_timestamp = time.time()

            # 发送ping
            from unilabos.app.communication import get_communication_client

            comm_client = get_communication_client()
            comm_client.send_ping(ping_id, send_timestamp)

            # 等待pong响应
            timeout = 10.0
            start_wait_time = time.time()

            while time.time() - start_wait_time < timeout:
                with self._ping_lock:
                    if ping_id in self._ping_responses:
                        pong_data = self._ping_responses.pop(ping_id)
                        break
                time.sleep(0.001)
            else:
                self.lab_logger().error(f"❌ 第{i+1}次测试超时")
                continue

            # 计算本次测试结果
            receive_timestamp = time.time()
            client_timestamp = pong_data["client_timestamp"]
            server_timestamp = pong_data["server_timestamp"]

            # 往返时间
            rtt_ms = (receive_timestamp - send_timestamp) * 1000

            # 客户端与服务端时间差（客户端时间 - 服务端时间）
            # 假设网络延迟对称，取中间点的服务端时间
            mid_point_time = send_timestamp + (receive_timestamp - send_timestamp) / 2
            time_diff_ms = (mid_point_time - server_timestamp) * 1000

            ping_results.append({"rtt_ms": rtt_ms, "time_diff_ms": time_diff_ms})

            self.lab_logger().info(f"✅ 第{i+1}次: 往返时间={rtt_ms:.2f}ms, 时间差={time_diff_ms:.2f}ms")

            time.sleep(0.1)

        if not ping_results:
            self.lab_logger().error("❌ 所有ping-pong测试都失败了")
            return {
                "avg_rtt_ms": -1.0,
                "avg_time_diff_ms": -1.0,
                "max_time_error_ms": -1.0,
                "task_delay_ms": -1.0,
                "raw_delay_ms": -1.0,
                "test_count": 0,
                "status": "all_timeout",
            }

        # 统计分析
        rtts = [r["rtt_ms"] for r in ping_results]
        time_diffs = [r["time_diff_ms"] for r in ping_results]

        avg_rtt_ms = sum(rtts) / len(rtts)
        avg_time_diff_ms = sum(time_diffs) / len(time_diffs)
        max_time_diff_error_ms: float = max(abs(min(time_diffs)), abs(max(time_diffs)))

        self.lab_logger().info("-" * 50)
        self.lab_logger().info("[测试统计]")
        self.lab_logger().info(f"有效测试次数: {len(ping_results)}/5")
        self.lab_logger().info(f"平均往返时间: {avg_rtt_ms:.2f}ms")
        self.lab_logger().info(f"平均时间差: {avg_time_diff_ms:.2f}ms")
        self.lab_logger().info(f"时间差范围: {min(time_diffs):.2f}ms ~ {max(time_diffs):.2f}ms")
        self.lab_logger().info(f"最大时间误差: ±{max_time_diff_error_ms:.2f}ms")

        # 计算任务执行延迟
        if hasattr(self, "server_latest_timestamp") and self.server_latest_timestamp > 0:
            self.lab_logger().info("-" * 50)
            self.lab_logger().info("[任务执行延迟分析]")
            self.lab_logger().info(f"服务端任务下发时间: {self.server_latest_timestamp:.6f}")
            self.lab_logger().info(f"客户端任务开始时间: {task_start_time:.6f}")

            # 原始时间差（不考虑时间同步误差）
            raw_delay_ms = (task_start_time - self.server_latest_timestamp) * 1000

            # 考虑时间同步误差后的延迟（用平均时间差校正）
            corrected_delay_ms = raw_delay_ms - avg_time_diff_ms

            self.lab_logger().info(f"📊 原始时间差: {raw_delay_ms:.2f}ms")
            self.lab_logger().info(f"🔧 时间同步校正: {avg_time_diff_ms:.2f}ms")
            self.lab_logger().info(f"⏰ 实际任务延迟: {corrected_delay_ms:.2f}ms")
            self.lab_logger().info(f"📏 误差范围: ±{max_time_diff_error_ms:.2f}ms")

            # 给出延迟范围
            min_delay = corrected_delay_ms - max_time_diff_error_ms
            max_delay = corrected_delay_ms + max_time_diff_error_ms
            self.lab_logger().info(f"📋 延迟范围: {min_delay:.2f}ms ~ {max_delay:.2f}ms")

        else:
            self.lab_logger().warning("⚠️ 无法获取服务端任务下发时间，跳过任务延迟分析")
            raw_delay_ms = -1
            corrected_delay_ms = -1

        self.lab_logger().info("=" * 60)

        res: TestLatencyReturn = {
            "avg_rtt_ms": avg_rtt_ms,
            "avg_time_diff_ms": avg_time_diff_ms,
            "max_time_error_ms": max_time_diff_error_ms,
            "task_delay_ms": corrected_delay_ms if corrected_delay_ms > 0 else -1,
            "raw_delay_ms": (
                raw_delay_ms if hasattr(self, "server_latest_timestamp") and self.server_latest_timestamp > 0 else -1
            ),
            "test_count": len(ping_results),
            "status": "success",
        }
        return res

    @action(
        displayname="调试断点",
        description="工作流就绪标记，执行时立即完成。",
        always_free=True,
        node_type=NodeType.ILAB,
        estimate_duration_fixed=0.0,
    )
    def debug_checkpoint(self) -> None:
        """放行就绪后继，断点暂停由工作流调试配置管理。"""
        return None

    @legacy_action(always_free=True, node_type=NodeType.MANUAL_CONFIRM, placeholder_keys={
        "assignee_user_ids": PLACEHOLDER_MANUAL_CONFIRM
    }, goal_default={
        "timeout_seconds": 3600,
        "assignee_user_ids": []
    })
    def manual_confirm(self, timeout_seconds: int, assignee_user_ids: list[str], **kwargs) -> dict:
        """
        timeout_seconds: 超时时间（秒），默认3600秒
        修改的结果无效，是只读的
        """
        return kwargs

    @legacy_action(
        description="申请扣减物料并挂载（接收服务端已扣减的单个根物料，挂载到目标设备的目标物料上）",
        always_free=True,
        placeholder_keys={
            "resource": PLACEHOLDER_DEDUCT_RESOURCE,
            "device_id": PLACEHOLDER_DEVICES,
            "mount_resource": PLACEHOLDER_NODES,
        },
        handles=[
            ActionInputHandle(
                key="device_id",
                data_type="device_id",
                label="目标设备",
                data_key="device_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="mount_resource",
                data_type="resource",
                label="挂载目标",
                data_key="mount_resource",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="labware",
                data_type="resource",
                label="物料创建结果",
                data_key="created_resource_tree.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
            ActionOutputHandle(
                key="mount_resource",
                data_type="resource",
                label="挂载目标",
                data_key="mount_resource.@flatten",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    async def apply_deduct_resource(
        self,
        resource: ResourceSlot,
        device_id: DeviceSlot = "",
        mount_resource: ResourceSlot = None,
        bind_locations: Point = None,
        slot_on_deck: str = "",
    ) -> DeductResourceReturn:
        """
        申请扣减物料，并可选挂载到目标设备的目标物料上。

        与 transfer_resource / transfer_manual 同构：resource / mount_resource 均为**单个物料**
        （单 ResourceSlot）。服务端已完成扣减并回传实际物料，框架在 send_goal 把以下两种入参形态
        解析为单个 PLR 实例：
        - list：一棵树的扁平节点组（上游 handle 的 @flatten）→ 装配成一个物料（这一组必须只有一个根）。
        - dict：资源引用 → 按 uuid with_children 拉取一个物料。

        两种用法：
        - 仅登记/透传（不传 device_id 或 mount_resource）：只校验并把已扣减物料经 labware 输出，
          方便后续 set_substance 设置内容物、再由 transfer_resource / transfer_manual 派发。
        - 扣减并挂载（device_id + mount_resource 都给）：复用 create_resource_detailed →
          append_resource，把该已存在物料挂到所选设备的挂载目标（相当于从仓库放到仓储设备上）。

        与 create_resource 的区别：资源不是按 class+name 新建，而是直接 dump 已扣减实例作为
        挂载载荷（initialize_full=False，不重建）。

        输出 handle：labware = 已扣减/挂载得到的物料树；mount_resource = 实际挂载到的目标物料树
        （未挂载时为空），便于下游节点继续引用挂载位置。

        Args:
            resource[扣减物料]: 已扣减的单个根物料（前端用扣减选择器选择，dict/list 两形态均解析为一个物料）。
            device_id[目标设备]: 挂载到的边缘设备 id（可选；不传则仅登记/透传，可由图 handle 连入）。
            mount_resource[挂载目标]: 实际挂载到的单个目标物料/父节点（可选；不传则仅登记/透传，可由图 handle 连入，dict/list 两形态）。
            bind_locations[挂载位置]: 挂载目标坐标系下的挂载坐标（挂载时使用）。
            slot_on_deck[Deck库位]: 挂载目标为 Deck 时按库位挂载（可选）。
        """
        if resource is None:
            raise ValueError("申请扣减失败：未接收到已扣减物料")
        if getattr(resource, "unilabos_uuid", None) is None:
            raise ValueError(f"物料 {getattr(resource, 'name', resource)} 缺少 unilabos_uuid，无法处理")
        # 已存在的扣减物料：dump 现有实例（不重新 initialize），单根取 [0] 的扁平节点列表
        dumped = ResourceTreeSet.from_plr_resources([resource]).dump()
        if not dumped:
            raise ValueError(f"物料 {getattr(resource, 'name', resource)} 序列化为空")
        flatten_nodes: List[Dict[str, Any]] = dumped[0]
        barcode = flatten_nodes[0].get("barcode", "") if flatten_nodes else ""
        # 是否执行挂载：device_id 与 mount_resource 都给齐才挂载，否则仅登记/透传
        do_mount = bool(str(device_id)) and mount_resource is not None and not (
            isinstance(mount_resource, str) and not mount_resource
        )
        if not do_mount:
            self.lab_logger().info(
                f"[apply_deduct_resource] 仅登记/透传物料 name={getattr(resource, 'name', '')} "
                f"barcode={barcode}（未指定 device_id/mount_resource，不挂载）"
            )
            return {
                "created_resource_tree": dumped,
                "liquid_input_resource_tree": [],
                "mount_resource": [],
            }
        mount_name = mount_resource.name if hasattr(mount_resource, "name") else str(mount_resource).split("/")[-1]
        self.lab_logger().info(
            f"[apply_deduct_resource] 挂载物料 name={getattr(resource, 'name', '')} "
            f"barcode={barcode} -> device={device_id} mount_resource={mount_name}"
        )
        # 挂载坐标归一化：动作装饰器路径可能传 dict，ROS 路径为 Point；缺省取原点。
        if isinstance(bind_locations, dict):
            point = Point(
                x=float(bind_locations.get("x", 0.0)),
                y=float(bind_locations.get("y", 0.0)),
                z=float(bind_locations.get("z", 0.0)),
            )
        elif bind_locations is None:
            point = Point(x=0.0, y=0.0, z=0.0)
        else:
            point = bind_locations
        other_calling_param = json.dumps({"initialize_full": False, "slot": slot_on_deck})
        responses = await self.create_resource_detailed(
            [flatten_nodes],
            [str(device_id).split("/")[-1]],
            [mount_name],
            [point],
            [other_calling_param],
        )
        assert len(responses) == 1, "apply_deduct_resource 应当只返回一个结果"
        res = json.loads(responses[0])
        if "suc" in res and not res["suc"]:
            raise ValueError(res.get("error", "未知错误"))
        # 额外输出实际挂载到的目标物料树，方便下游 handle 继续引用挂载位置
        res["mount_resource"] = (
            ResourceTreeSet.from_plr_resources([mount_resource]).dump()
            if not isinstance(mount_resource, str)
            else []
        )
        return res

    @legacy_action(
        description="设置物料内容物（液体/固体，默认单位 微升/微克）；接收单个物料，设置后输出",
        always_free=True,
        placeholder_keys={"resource": PLACEHOLDER_DEDUCT_REAGENT},
        handles=[
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="目标物料",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
            ActionOutputHandle(
                key="resource",
                data_type="resource",
                label="目标物料",
                data_key="resource",
                data_source=DataSource.EXECUTOR,
            ),
        ],
    )
    async def set_substance(
        self,
        resource: ResourceSlot,
        substance_names: List[str],
        amounts: List[float],
        slots: List[str] = [],
        is_solid: List[bool] = [],
    ) -> dict:
        """
        设置单个物料的内容物（液体或固体）。

        接收的物料必须是单个，且为以下之一：
        - container：直接设置在自身的 tracker 上；
        - well（带标号的容器）：同样设置在自身；
        - carrier / plate 带 container：按 slots 设置在对应子容器的 tracker 上（支持 tracker 输入）。

        设置目标只有两种：物料自身，或物料下面 children 的孔位。由 slots 区分（空=自身）。
        单位固定默认：固体=微克(ug)、液体=微升(ul)，由 is_solid 区分（unilab 定制 PLR 的
        set_liquids 仅支持 ul/ug）。底层走 set_liquids 三元组 (名称, 量, 单位)。

        Args:
            resource[目标物料]: 单个物料（container / well / 带子容器的 carrier|plate）。
            substance_names[物质名称]: 每个目标的物质名（液体名或固体名）。
            amounts[用量]: 每个目标的用量（液体=体积/微升，固体=质量/微克）。
            slots[子孔位]: 子孔位 id/索引；为空=设在物料自身，非空=设在对应子容器。
            is_solid[是否固体]: 每个目标是否固体（可选，缺省按液体处理；决定单位 ug/ul）。
        """
        if resource is None:
            raise ValueError("设置内容物失败：未接收到物料")
        # 统一走 apply_substances：目标解析 + ug/ul 单位 + set_liquids 三元组
        apply_substances(resource, substance_names, amounts, slots=slots, is_solid=is_solid)
        # 同步整棵树到云端（含被修改的子孔位）
        await self.update_resource([resource])
        dumped = ResourceTreeSet.from_plr_resources([resource]).dump()
        return {"resource": dumped[0] if dumped else []}

    @legacy_action(
        description="废弃台面物料（指定设备 + uuid：云端销毁并通知该设备本地移除）",
        always_free=True,
        placeholder_keys={
            "resource": PLACEHOLDER_NODES,
            "device_id": PLACEHOLDER_DEVICES,
        },
        handles=[
            ActionInputHandle(
                key="device_id",
                data_type="device_id",
                label="所属设备",
                data_key="device_id",
                data_source=DataSource.HANDLE,
            ),
            ActionInputHandle(
                key="resource",
                data_type="resource",
                label="废弃物料",
                data_key="resource",
                data_source=DataSource.HANDLE,
            ),
        ],
    )
    async def discard_resource(self, resource: ResourceSlot, device_id: DeviceSlot) -> dict:
        """从 Edge Runtime 的设备资源树移除单个台面物料并返回执行回执。

        与 apply_deduct_resource 对称（扣减→挂载到设备 / 废弃→从设备移除并销毁）：接收单个
        已存在物料与所属设备，只通知对应边缘设备移除本地资源树。Runtime 不读取或
        写入 Scheduler Inventory；正式废弃事实必须由 Scheduler 在验证 Claim/Fence
        与成功回执后通过 PhysicalSettlement 提交。

        说明：设备需显式指定，与 ``apply_deduct_resource`` 对称。

        Args:
            resource[废弃物料]: 要废弃的单个台面物料（须带 unilabos_uuid）。
            device_id[所属设备]: 物料所在的边缘设备 id（用于通知该设备本地移除）。
        """
        if resource is None:
            raise ValueError("废弃失败：未接收到物料")
        res_uuid = getattr(resource, "unilabos_uuid", None)
        if res_uuid is None:
            raise ValueError(f"物料 {getattr(resource, 'name', resource)} 缺少 unilabos_uuid，无法废弃")
        edge_id = str(device_id).split("/")[-1]
        dumped = ResourceTreeSet.from_plr_resources([resource]).dump()
        barcode = dumped[0][0].get("barcode", "") if dumped and dumped[0] else ""
        self.lab_logger().info(
            f"[discard_resource] 废弃物料 name={getattr(resource, 'name', '')} "
            f"barcode={barcode} uuid={res_uuid} device={edge_id}"
        )
        # Runtime 只执行本地资源树移除；库存结算属于 Scheduler 独占职责。
        notified = self.notify_resource_tree_update(edge_id, "remove", [res_uuid])
        if notified is not True:
            raise RuntimeError(
                f"废弃执行失败：通知设备 {edge_id} 移除物料 {res_uuid} 未成功"
            )
        return {
            "code": 0,
            "uuids": [res_uuid],
            "device_id": edge_id,
            "status": "runtime_removed",
        }

    async def _do_transfer_resource(
        self,
        resource: "ResourceSlot",
        target_device: DeviceSlot,
        mount_resource: "ResourceSlot",
        site: str = "",
        site_uuid: str = "",
    ) -> TransferResourceReturn:
        """在 Edge Runtime 资源树中执行已经获准的物料挂载动作。

        与 apply_deduct_resource 一致：入参均为「单个物料」（单 ResourceSlot），框架在 send_goal 已把
        list（一棵树扁平节点组→装配成一个物料）或 dict（资源引用→with_children 拉取）解析为单个 PLR 实例。

        复用 base_device_node.transfer_resource_to_another 完成设备进程内资源树切换并
        返回执行回执。Runtime 不读取或写入 Scheduler 的 Inventory Authority；正式
        库存物理事实只允许 Scheduler 在校验 Claim/Fence 后通过 PhysicalSettlement
        提交。物理搬运由前序节点（manual_confirm/机械臂 pick+place）保证。

        site_uuid：目标库位的稳定身份，仅供派发回执关联。Scheduler 必须在派发前
        完成 UUID、归属、占用和名称校验，并同时下发规范 ``site``；Runtime 禁止
        通过 UUID 反查调度库存。
        site：目标父级（carrier/deck/plate 等带 _ordering 的容器）上的库位名，显式指定物料落在哪个库位；
        目标端通过 resolve_site_spot（与 set_substance 同一套 slot/site 解析：int 索引 / "A1" 标签 /
        名称匹配）换算成 assign_child_resource 的 spot。空串视作不指定（由父级默认排布）。
        若物料 extra 里带了前端隐式写入的 ``update_resource_site``，它只在未显式
        指定 ``site`` 时作为旧调用兜底，不能覆盖已经按稳定身份校验的目标。

        注意：底层按"运行该动作的节点"作为来源执行本地移除，host 运行时来源即 host（根节点）。
        若物料此前已被 apply_deduct_resource 挂到某边缘设备，该设备的本地副本不会在此处被移除，
        需依赖下次同步对齐（详见 cursor_docs 记录的源设备移除限制）。

        Args:
            resource: 待转移的具体物料（Material）实例。
            target_device: 接收物料的目标设备身份；可带 ``/devices/`` 前缀。
            mount_resource: 目标设备上承载该物料的父物料实例。
            site: 目标父物料中的库位（Site）名称；空串表示使用默认排布。
            site_uuid: 可选库位稳定身份；非空时优先使用并校验归属、名称与占用。

        Returns:
            返回四键字典；物料和目标父物料使用规范 ``ResourceSlot`` UUID 引用，
            ``site`` 回传库位名，``result`` 是底层转移结果的稳定字符串。

        Raises:
            ValueError: 必需物料缺失、只收到 UUID 而缺少 Scheduler 解析的规范库位
                名，或底层执行拒绝时抛出。
        """
        if resource is None:
            raise ValueError("转移失败：未接收到待转移物料")
        if mount_resource is None:
            raise ValueError("转移失败：未指定挂载目标孔位")
        target_id = str(target_device).split("/")[-1]
        if site_uuid and not str(site).strip():
            raise ValueError(
                "转移失败：Scheduler 必须随 site_uuid 下发已经校验的规范库位名 site"
            )
        result = await self.transfer_resource_to_another(
            [resource], target_id, [mount_resource], [site if site else None]
        )
        return {
            "resource": _dump_resource_slot(resource),
            "mount_resource": _dump_resource_slot(mount_resource),
            "site": site,
            "result": str(result),
        }

    @action(
        description="转移物料（系统派发）：执行设备进程内资源树挂载并返回回执；正式库存由 Scheduler PhysicalSettlement 提交",
        always_free=True,
        node_type=NodeType.ILAB,
        executor_kind=ExecutorKind.MATERIAL_TRANSFER,
        placeholder_keys={
            "target_device": PLACEHOLDER_DEVICES,
            "mount_resource": PLACEHOLDER_NODES,
        },
    )
    async def transfer_resource(
        self,
        resource: ResourceSlot,
        target_device: str,
        mount_resource: ResourceSlot,
        site: str = "",
        site_uuid: str = "",
    ) -> TransferResourceReturn:
        """
        转移物料到目标设备的目标孔位（Runtime 执行回执，不直接修改调度库存）。
        物理搬运由前序节点保证：
        - 人工：apply_deduct_resource → transfer_manual → transfer_manual → transfer_resource
        - 机械臂：apply_deduct_resource → 机械臂 pick → 机械臂 place → transfer_resource

        与 apply_deduct_resource 同构：resource / mount_resource 均为**单个物料**（单 ResourceSlot）。
        单物料有两种入参形态，框架在 send_goal 自动解析为一个 PLR 实例：
        - list：一棵树的扁平节点组（上游 handle 的 @flatten）→ 装配成一个物料（这一组必须只有一个根）。
        - dict：资源引用 → 按 uuid with_children 拉取一个物料。

        Args:
            resource[待转移物料]: 待转移的单个物料（须带 unilabos_uuid，可由图 handle 连入，list/dict 两形态）。
            target_device[目标设备]: 接收物料的目标设备 id。
            mount_resource[目标孔位]: 目标设备上的单个挂载孔位/父物料（list/dict 两形态）。
            site[目标库位]: 目标父级容器上的库位名，显式指定物料落在哪个库位（carrier/deck/plate 等按
                _ordering 换算成 spot）；不传则由父级默认排布。
            site_uuid[目标库位 UUID]: 可选稳定身份；非空时 Scheduler 必须同时下发
                已校验的规范 ``site``，Runtime 不访问库存反查名称。为兼容旧工作流
                可继续只传 ``site``。

        Returns:
            四键运行结果字典；两个物料字段按规范单对象引用返回。静态类型化动作
            （Typed Action）合同把它们发布为物料占位符（ResourceSlot），供下游
            工作流节点继续连接。

        Raises:
            ValueError: 必需物料缺失、目标库位校验失败或转移执行失败时抛出。
        """
        return await self._do_transfer_resource(
            resource,
            target_device,
            mount_resource,
            site,
            site_uuid,
        )

    @action(
        description="人工搬运闸门：到该步暂停等人工确认（人工把物料搬运到位），仅透传物料，不做系统转移（人工工作流中间步，对应机械臂 pick/place）",
        always_free=True,
        node_type=NodeType.MANUAL_CONFIRM,
        placeholder_keys={
            "assignee_user_ids": PLACEHOLDER_MANUAL_CONFIRM,
            "target_device": PLACEHOLDER_DEVICES,
            "mount_resource": PLACEHOLDER_NODES,
        },
        goal_default={
            "timeout_seconds": 3600,
            "assignee_user_ids": [],
            "site": "",
        },
    )
    async def transfer_manual(
        self,
        resource: ResourceSlot,
        target_device: str,
        mount_resource: ResourceSlot,
        timeout_seconds: int = 3600,
        assignee_user_ids: list[str] = [],
        site: str = "",
    ) -> TransferManualReturn:
        """
        人工搬运闸门：工作流执行到本节点时暂停、等待人工确认（确认即表示人工已把物料搬运到位），
        本身**只透传**物料/目标设备/目标孔位/库位，不做任何系统转移——它是机械臂 pick/place 的人工对应物。

        实际的系统转移（记账）由工作流末步 transfer_resource 统一完成（两条流一致）：
        - 人工：apply_deduct_resource → transfer_manual → transfer_manual → transfer_resource
        - 机械臂：apply_deduct_resource → 机械臂 pick → 机械臂 place → transfer_resource

        与 apply_deduct_resource / transfer_resource 同构：resource / mount_resource 均为**单个物料**
        （单 ResourceSlot），框架在 send_goal 自动把 list（一棵树扁平节点组→装配成一个物料）或
        dict（资源引用→with_children 拉取）解析为一个 PLR 实例。

        site 在此显式指定/透传，避免只能依赖前端隐式写入物料 extra（update_resource_site）；
        透传到末步 transfer_resource 后据此把物料落到目标父级的对应库位。

        Args:
            resource[待搬运物料]: 待人工搬运的单个物料（须带 unilabos_uuid，可由图 handle 连入并透传，list/dict 两形态）。
            target_device[目标设备]: 物料要搬到的目标设备 id（透传给下游）。
            mount_resource[目标孔位]: 目标设备上的单个目标孔位/父物料（透传给下游，list/dict 两形态）。
            timeout_seconds[超时时间]: 人工确认超时时间，单位秒，默认 3600。
            assignee_user_ids[确认人]: 指定处理人工确认的用户 id 列表。
            site[目标库位]: 目标父级容器上的库位名，显式指定物料落在哪个库位（透传给下游）。
        """
        return {
            "resource": _dump_resource_slot(resource),
            "mount_resource": _dump_resource_slot(mount_resource),
            "target_device": str(target_device) if target_device is not None else "",
            "site": site,
        }

    def test_resource(
        self,
        sample_uuids: SampleUUIDsType,
        resource: ResourceSlot = None,
        resources: List[ResourceSlot] = None,
        device: DeviceSlot = None,
        devices: List[DeviceSlot] = None,
    ) -> TestResourceReturn:
        if resources is None:
            resources = []
        if devices is None:
            devices = []
        if resource is None:
            resource = RegularContainer("test_resource传入None")
        return {
            "resources": ResourceTreeSet.from_plr_resources([resource, *resources], known_newly_created=True).dump(),
            "devices": [device, *devices],
            "unilabos_samples": [LabSample(sample_uuid=sample_uuid, oss_path="", extra={"material_uuid": content} if isinstance(content, str) else content.serialize()) for sample_uuid, content in sample_uuids.items()]
        }

    def handle_pong_response(self, pong_data: dict):
        """
        处理pong响应
        """
        ping_id = pong_data.get("ping_id")
        if ping_id:
            with self._ping_lock:
                self._ping_responses[ping_id] = pong_data

            # 详细信息合并为一条日志
            client_timestamp = pong_data.get("client_timestamp", 0)
            server_timestamp = pong_data.get("server_timestamp", 0)
            current_time = time.time()

            self.lab_logger().debug(
                f"📨 Pong | ID:{ping_id[:8]}.. | C→S→C: {client_timestamp:.3f}→{server_timestamp:.3f}→{current_time:.3f}"
            )
        else:
            self.lab_logger().warning("⚠️ 收到无效的Pong响应（缺少ping_id）")

    def notify_resource_tree_update(
        self, device_id: str, action: str, resource_uuid_list: List[str]
    ) -> Optional[bool]:
        """
        通知设备节点更新资源树

        Args:
            device_id: 目标设备ID
            action: 操作类型 "add", "update", "remove"
            resource_uuid_list: 资源UUIDs

        Returns:
            True if the update completed, False if it failed, None if it was intentionally skipped.
        """
        try:
            if device_id not in self.devices_names:
                self.lab_logger().info(
                    f"[Host Node-Resource] 在线增加设备暂不支持，跳过设备 {device_id} 的资源树 {action} 更新"
                )
                return None

            namespace = self.devices_names[device_id]
            device_key = f"{namespace}/{device_id}"

            # 检查设备是否在线
            if device_key not in self._online_devices:
                self.lab_logger().error(f"[Host Node-Resource] Device {device_key} is offline")
                return False

            # 构建服务地址
            srv_address = f"/srv{namespace}/s2c_resource_tree"
            self.lab_logger().trace(
                f"[Host Node-Resource] Host -> {device_id} ResourceTree {action} operation started -------"
            )

            # 创建服务客户端
            sclient = self.create_client(SerialCommand, srv_address)

            # 等待服务可用（设置超时）
            if not sclient.wait_for_service(timeout_sec=5.0):
                self.lab_logger().error(f"[Host Node-Resource] Service {srv_address} not available")
                return False

            # 构建请求数据
            request_data = [
                {
                    "action": action,
                    "data": resource_uuid_list,
                }
            ]

            # 创建请求
            request = SerialCommand.Request()
            request.command = json.dumps(request_data, ensure_ascii=False)

            # 发送异步请求
            future = sclient.call_async(request)

            # 等待响应
            timeout = 30.0
            start_time = time.time()
            while not future.done():
                if time.time() - start_time > timeout:
                    self.lab_logger().error(f"[Host Node-Resource] Timeout waiting for response from {device_id}")
                    return False
                time.sleep(0.05)

            response = future.result()
            self.lab_logger().trace(
                f"[Host Node-Resource] Host -> {device_id} ResourceTree {action} operation completed -------"
            )
            return True

        except Exception as e:
            self.lab_logger().error(f"[Host Node-Resource] Error notifying resource tree update: {str(e)}")
            self.lab_logger().error(traceback.format_exc())
            return False

    # ------------------------------------------------------------------
    # Device lifecycle (add / remove) — pure forwarder
    # ------------------------------------------------------------------

    def notify_device_manage(self, target_node_id: str, action: str, config: ResourceDictType) -> bool:
        """Forward an add/remove device command to the target node via ROS2 SerialCommand.

        The HostNode does NOT interpret the command; it simply resolves the
        target namespace and forwards the request to ``s2c_device_manage``.

        If *target_node_id* equals the HostNode's own device_id (i.e. the
        command targets the host itself), we call our local ``create_device``
        / ``destroy_device`` directly instead of going through ROS2.
        """
        try:
            # If the target is the host itself, handle locally
            device_id = config["id"]
            if target_node_id == self.device_id:
                if action == "add":
                    return self.create_device(device_id, config).get("success", False)
                elif action == "remove":
                    return self.destroy_device(device_id).get("success", False)

            if target_node_id not in self.devices_names:
                self.lab_logger().error(
                    f"[Host Node-DeviceMgr] Target {target_node_id} not found in devices_names"
                )
                return False

            namespace = self.devices_names[target_node_id]
            device_key = f"{namespace}/{target_node_id}"
            if device_key not in self._online_devices:
                self.lab_logger().error(f"[Host Node-DeviceMgr] Target {device_key} is offline")
                return False

            srv_address = f"/srv{namespace}/s2c_device_manage"
            self.lab_logger().info(
                f"[Host Node-DeviceMgr] Forwarding {action}_device to {target_node_id} ({srv_address})"
            )

            sclient = self.create_client(SerialCommand, srv_address)
            if not sclient.wait_for_service(timeout_sec=5.0):
                self.lab_logger().error(f"[Host Node-DeviceMgr] Service {srv_address} not available")
                return False

            request = SerialCommand.Request()
            request.command = json.dumps({"action": action, "data": config}, ensure_ascii=False)

            future = sclient.call_async(request)
            timeout = 30.0
            start_time = time.time()
            while not future.done():
                if time.time() - start_time > timeout:
                    self.lab_logger().error(
                        f"[Host Node-DeviceMgr] Timeout waiting for {action}_device on {target_node_id}"
                    )
                    return False
                time.sleep(0.05)

            response = future.result()
            self.lab_logger().info(
                f"[Host Node-DeviceMgr] {action}_device on {target_node_id} completed"
            )
            return True

        except Exception as e:
            self.lab_logger().error(f"[Host Node-DeviceMgr] Error: {e}")
            self.lab_logger().error(traceback.format_exc())
            return False

    def create_device(self, device_id: str, config: ResourceDictType) -> dict:
        """Dynamically create a root-level device on the host."""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id in self.devices_names:
            return {"success": False, "error": f"Device {device_id} already exists"}

        try:
            config.setdefault("id", device_id)
            config.setdefault("type", "device")
            config.setdefault("machine_name", BasicConfig.machine_name or "本地")
            res_dict = ResourceDictInstance.get_resource_instance_from_dict(config)

            self._initialize_configured_device(device_id, res_dict)

            if device_id not in self.devices_names:
                return {"success": False, "error": f"initialize_device failed for {device_id}"}

            # Add to config tree (devices_config)
            tree = ResourceTreeInstance(res_dict)
            self.devices_config.trees.append(tree)

            # Add to resource tracker so s2c_resource_tree can find it
            try:
                for plr_resource in ResourceTreeSet([tree]).to_plr_resources():
                    self._resource_tracker.add_resource(plr_resource)
            except Exception as ex:
                self.lab_logger().warning(f"[Host Node-DeviceMgr] PLR resource registration skipped: {ex}")

            self.lab_logger().info(f"[Host Node-DeviceMgr] Device {device_id} created successfully")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            self.lab_logger().error(f"[Host Node-DeviceMgr] Failed to create {device_id}: {e}")
            self.lab_logger().error(traceback.format_exc())
            return {"success": False, "error": str(e)}

    def destroy_device(self, device_id: str) -> dict:
        """Remove a root-level device from the host."""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id not in self.devices_names:
            return {"success": False, "error": f"Device {device_id} not found"}

        if device_id == self.device_id:
            return {"success": False, "error": "Cannot destroy host_node itself"}

        try:
            namespace = self.devices_names[device_id]
            device_key = f"{namespace}/{device_id}"

            # Remove action clients
            action_prefix = f"/devices/{device_id}/"
            to_remove = [k for k in self._action_clients if k.startswith(action_prefix)]
            for k in to_remove:
                try:
                    self._action_clients[k].destroy()
                except Exception:
                    pass
                del self._action_clients[k]

            # Remove from config tree (devices_config)
            self.devices_config.trees = [
                t for t in self.devices_config.trees
                if t.root_node.res_content.id != device_id
            ]

            # Remove from resource tracker
            try:
                tracked = self._resource_tracker.uuid_to_resources.copy()
                for uid, res in tracked.items():
                    res_id = res.get("id") if isinstance(res, dict) else getattr(res, "name", None)
                    if res_id == device_id:
                        self._resource_tracker.remove_resource(res)
            except Exception as ex:
                self.lab_logger().warning(f"[Host Node-DeviceMgr] Resource tracker cleanup: {ex}")

            # Clean internal state
            self._online_devices.discard(device_key)
            self._simulated_device_keys.discard(device_key)
            self.devices_names.pop(device_id, None)
            self.device_machine_names.pop(device_id, None)
            self._action_value_mappings.pop(device_id, None)
            self.device_status.pop(device_id, None)
            self.device_status_timestamps.pop(device_id, None)

            # Destroy the ROS2 node of the device
            instance = self.devices_instances.pop(device_id, None)
            if instance is not None:
                try:
                    # noinspection PyProtectedMember
                    ros_node = getattr(instance, "_ros_node", None)
                    if ros_node is not None:
                        ros_node.destroy_node()
                except Exception as e:
                    self.lab_logger().warning(f"[Host Node-DeviceMgr] Error destroying ROS node for {device_id}: {e}")

            self.lab_logger().info(f"[Host Node-DeviceMgr] Device {device_id} destroyed")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            self.lab_logger().error(f"[Host Node-DeviceMgr] Failed to destroy {device_id}: {e}")
            self.lab_logger().error(traceback.format_exc())
            return {"success": False, "error": str(e)}
