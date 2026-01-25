import collections
from dataclasses import dataclass, field
import json
import threading
import time
import traceback
import uuid
from typing import TYPE_CHECKING, Optional, Dict, Any, List, ClassVar, Set, Union
from typing_extensions import TypedDict

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Point
from rclpy.action import ActionClient, get_action_server_names_and_types_by_node
from rclpy.service import Service
from unilabos_msgs.msg import Resource  # type: ignore
from unilabos_msgs.srv import (
    ResourceAdd,
    ResourceDelete,
    ResourceUpdate,
    ResourceList,
    SerialCommand,
)  # type: ignore
from unilabos_msgs.srv._serial_command import SerialCommand_Request, SerialCommand_Response
from unique_identifier_msgs.msg import UUID

from unilabos.registry.registry import lab_registry
from unilabos.resources.container import RegularContainer
from unilabos.resources.graphio import initialize_resource
from unilabos.resources.registry import add_schema
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.ros.msgs.message_converter import (
    get_msg_type,
    get_ros_type_by_msgname,
    convert_from_ros_msg,
    convert_to_ros_msg,
    msg_converter_manager,
)
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, ROS2DeviceNode, DeviceNodeResourceTracker
from unilabos.ros.nodes.presets.controller_node import ControllerNode
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeSet,
    ResourceTreeInstance,
)
from unilabos.utils import logger
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.log import warning
from unilabos.utils.type_check import serialize_result_info
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot

if TYPE_CHECKING:
    from unilabos.app.ws_client import QueueItem


@dataclass
class DeviceActionStatus:
    job_ids: Dict[str, float] = field(default_factory=dict)


class TestResourceReturn(TypedDict):
    resources: List[List[ResourceDict]]
    devices: List[DeviceSlot]


class TestLatencyReturn(TypedDict):
    """test_latency方法的返回值类型"""
    avg_rtt_ms: float
    avg_time_diff_ms: float
    max_time_error_ms: float
    task_delay_ms: float
    raw_delay_ms: float
    test_count: int
    status: str


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
                lab_registry.ResourceCreateFromOuterEasy,
                "/devices/host_node/create_resource",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/create_resource_detailed": ActionClient(
                self,
                lab_registry.ResourceCreateFromOuter,
                "/devices/host_node/create_resource_detailed",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/test_latency": ActionClient(
                self,
                lab_registry.EmptyIn,
                "/devices/host_node/test_latency",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/test_resource": ActionClient(
                self,
                lab_registry.EmptyIn,
                "/devices/host_node/test_resource",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/_execute_driver_command": ActionClient(
                self,
                lab_registry.StrSingleInput,
                "/devices/host_node/_execute_driver_command",
                callback_group=self.callback_group,
            ),
            "/devices/host_node/_execute_driver_command_async": ActionClient(
                self,
                lab_registry.StrSingleInput,
                "/devices/host_node/_execute_driver_command_async",
                callback_group=self.callback_group,
            ),
        }  # 用来存储多个ActionClient实例
        self._action_value_mappings: Dict[str, Dict] = (
            {}
        )  # 用来存储多个ActionClient的type, goal, feedback, result的变量名映射关系
        self._goals: Dict[str, Any] = {}  # 用来存储多个目标的状态
        self._online_devices: Set[str] = {f"{self.namespace}/{device_id}"}  # 用于跟踪在线设备
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

        # 初始化所有本机设备节点，多一次过滤，防止重复初始化
        for device_config in devices_config.root_nodes:
            device_id = device_config.res_content.id
            if device_config.res_content.type != "device":
                continue
            if device_id not in self.devices_names:
                self.initialize_device(device_id, device_config)
            else:
                self.lab_logger().warning(f"[Host Node] Device {device_id} already existed, skipping.")
        self.update_device_status_subscriptions()
        # TODO: 需要验证 初始化所有控制器节点
        if controllers_config:
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
        self.lab_logger().trace("[Host Node] Discovering devices in the network...")

        # 获取当前所有设备
        nodes_and_names = self.get_node_names_and_namespaces()

        # 跟踪本次发现的设备，用于检测离线设备
        current_devices = set()

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

        # 检测离线设备
        offline_devices = self._online_devices - current_devices
        for device_key in offline_devices:
            self.lab_logger().warning(f"[Host Node] Device offline: {device_key}")
            self._online_devices.discard(device_key)

        # 更新在线设备列表
        self._online_devices = current_devices
        self.lab_logger().trace(f"[Host Node] Total online devices: {len(self._online_devices)}")

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
            self.lab_logger().debug("[Host Node] Device discovery already in progress, skipping.")

    def _create_action_clients_for_device(self, device_id: str, namespace: str) -> None:
        """
        为设备创建所有必要的ActionClient

        Args:
            device_id: 设备ID
            namespace: 设备命名空间
        """
        for action_id, action_types in get_action_server_names_and_types_by_node(self, device_id, namespace):
            if action_id not in self._action_clients:
                try:
                    action_type = get_ros_type_by_msgname(action_types[0])
                    self._action_clients[action_id] = ActionClient(
                        self, action_type, action_id, callback_group=self.callback_group
                    )
                    self.lab_logger().trace(f"[Host Node] Created ActionClient (Discovery): {action_id}")
                    action_name = action_id[len(namespace) + 1 :]
                    edge_device_id = namespace[9:]
                    # from unilabos.app.comm_factory import get_communication_client
                    # comm_client = get_communication_client()
                    # info_with_schema = ros_action_to_json_schema(action_type)
                    # comm_client.publish_actions(action_name, {
                    #     "device_id": edge_device_id,
                    #     "device_type": "",
                    #     "action_name": action_name,
                    #     "schema": info_with_schema,
                    # })
                except Exception as e:
                    self.lab_logger().error(f"[Host Node] Failed to create ActionClient for {action_id}: {str(e)}")

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
    ):
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
        if len(liquid_input_slot) and liquid_input_slot[0] == -1:  # 目前container只逐个创建
            res_creation_input.update(
                {
                    "data": {
                        "liquids": [
                            {
                                "liquid_type": liquid_type[0] if liquid_type else None,
                                "liquid_volume": liquid_volume[0] if liquid_volume else None,
                            }
                        ]
                    }
                }
            )
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

        try:
            assert len(response) == 1, "Create Resource应当只返回一个结果"
            for i in response:
                res = json.loads(i)
                return res
        except Exception as ex:
            pass
        _n = "\n"
        raise ValueError(f"创建资源时失败！\n{_n.join(response)}")

    def initialize_device(self, device_id: str, device_config: ResourceDictInstance) -> None:
        """
        根据配置初始化设备，

        此函数根据提供的设备配置动态导入适当的设备类并创建其实例。
        同时为设备的动作值映射设置动作客户端。

        Args:
            device_id: 设备唯一标识符
            device_config: 设备配置字典，包含类型和其他参数
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
        for action_name, action_value_mapping in d._ros_node._action_value_mappings.items():
            if action_name.startswith("auto-") or str(action_value_mapping.get("type", "")).startswith(
                "UniLabJsonCommand"
            ):
                continue
            action_id = f"/devices/{device_id}/{action_name}"
            if action_id not in self._action_clients:
                action_type = action_value_mapping["type"]
                self._action_clients[action_id] = ActionClient(self, action_type, action_id)
                self.lab_logger().trace(
                    f"[Host Node] Created ActionClient (Local): {action_id}"
                )  # 子设备再创建用的是Discover发现的
                # from unilabos.app.comm_factory import get_communication_client
                # comm_client = get_communication_client()
                # info_with_schema = ros_action_to_json_schema(action_type)
                # comm_client.publish_actions(action_name, {
                #     "device_id": device_id,
                #     "device_type": device_config["class"],
                #     "action_name": action_name,
                #     "schema": info_with_schema,
                # })
            else:
                self.lab_logger().warning(f"[Host Node] ActionClient {action_id} already exists.")
        device_key = f"{self.devices_names[device_id]}/{device_id}"  # 这里不涉及二级device_id
        # 添加到在线设备列表
        self._online_devices.add(device_key)

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
                elif self.device_status[device_id][property_name] != msg.data:
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
        if action_type.startswith("UniLabJsonCommand"):
            if action_name.startswith("auto-"):
                action_name = action_name[5:]
            action_id = f"/devices/{device_id}/_execute_driver_command"
            action_kwargs = {
                "string": json.dumps(
                    {
                        "function_name": action_name,
                        "function_args": action_kwargs,
                    }
                )
            }
            if action_type.startswith("UniLabJsonCommandAsync"):
                action_id = f"/devices/{device_id}/_execute_driver_command_async"
        else:
            action_id = f"/devices/{device_id}/{action_name}"
        if action_name == "test_latency" and server_info is not None:
            self.server_latest_timestamp = server_info.get("send_timestamp", 0.0)
        if action_id not in self._action_clients:
            raise ValueError(f"ActionClient {action_id} not found.")

        action_client: ActionClient = self._action_clients[action_id]

        # 遍历action_kwargs下的所有子dict，将"sample_uuid"的值赋给"sample_id"
        def assign_sample_id(obj):
            if isinstance(obj, dict):
                if "sample_uuid" in obj:
                    obj["sample_id"] = obj["sample_uuid"]
                    obj.pop("sample_uuid")
                for k, v in obj.items():
                    if k != "unilabos_extra":
                        assign_sample_id(v)
            elif isinstance(obj, list):
                for item in obj:
                    assign_sample_id(item)

        assign_sample_id(action_kwargs)
        goal_msg = convert_to_ros_msg(action_client._action_type.Goal(), action_kwargs)

        self.lab_logger().info(f"[Host Node] Sending goal for {action_id}: {str(goal_msg)[:1000]}")
        self.lab_logger().trace(f"[Host Node] Sending goal for {action_id}: {goal_msg}")
        action_client.wait_for_server()
        goal_uuid_obj = UUID(uuid=list(u.bytes))

        future = action_client.send_goal_async(
            goal_msg,
            feedback_callback=lambda feedback_msg: self.feedback_callback(item, action_id, feedback_msg),
            goal_uuid=goal_uuid_obj,
        )
        future.add_done_callback(lambda f: self.goal_response_callback(item, action_id, f))

    def goal_response_callback(self, item: "QueueItem", action_id: str, future) -> None:
        """目标响应回调"""
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.lab_logger().warning(f"[Host Node] Goal {item.action_name} ({item.job_id}) rejected")
            return

        self.lab_logger().info(f"[Host Node] Goal {action_id} ({item.job_id}) accepted")
        self._goals[item.job_id] = goal_handle
        goal_future = goal_handle.get_result_async()
        goal_future.add_done_callback(lambda f: self.get_result_callback(item, action_id, f))
        goal_future.result()

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
                            unilabos_samples = return_value.pop("unilabos_samples", None)
                            if isinstance(unilabos_samples, list) and unilabos_samples:
                                self.lab_logger().info(
                                    f"[Host Node] Job {job_id[:8]} returned {len(unilabos_samples)} sample(s): "
                                    f"{[s.get('name', s.get('id', 'unknown')) if isinstance(s, dict) else str(s)[:20] for s in unilabos_samples[:5]]}"
                                    f"{'...' if len(unilabos_samples) > 5 else ''}"
                                )
                                return_info["unilabos_samples"] = unilabos_samples
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
            if goal_status != GoalStatus.STATUS_CANCELED:
                self.lab_logger().trace(f"[Host Node] Result data: {result_data}")

            # 清理 _goals 中的记录
            if job_id in self._goals:
                del self._goals[job_id]
                self.lab_logger().debug(f"[Host Node] Removed goal {job_id[:8]} from _goals")

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

    def cancel_goal(self, goal_uuid: str) -> bool:
        """
        取消目标

        Args:
            goal_uuid: 目标UUID（job_id）

        Returns:
            bool: 如果找到目标并发起取消请求返回True，否则返回False
        """
        if goal_uuid in self._goals:
            self.lab_logger().info(f"[Host Node] Cancelling goal {goal_uuid[:8]}")
            goal_handle = self._goals[goal_uuid]

            # 发起异步取消请求
            cancel_future = goal_handle.cancel_goal_async()

            # 添加取消完成的回调
            cancel_future.add_done_callback(lambda future: self._cancel_goal_callback(goal_uuid, future))
            return True
        else:
            self.lab_logger().warning(f"[Host Node] Goal {goal_uuid[:8]} not found in _goals, cannot cancel")
            return False

    def _cancel_goal_callback(self, goal_uuid: str, future) -> None:
        """取消目标的回调"""
        try:
            cancel_response = future.result()
            if cancel_response.goals_canceling:
                self.lab_logger().info(f"[Host Node] Goal {goal_uuid[:8]} cancel request accepted")
            else:
                self.lab_logger().warning(f"[Host Node] Goal {goal_uuid[:8]} cancel request rejected")
        except Exception as e:
            self.lab_logger().error(f"[Host Node] Error cancelling goal {goal_uuid[:8]}: {str(e)}")
            import traceback

            self.lab_logger().error(traceback.format_exc())

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
            "node_info_update": self.create_service(
                SerialCommand,
                "/node_info_update",
                self._node_info_update_callback,
                callback_group=self.callback_group,
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
        success = False
        uuid_mapping = {}
        if len(self.bridges) > 0:
            from unilabos.app.web.client import HTTPClient, http_client

            resource_start_time = time.time()
            uuid_mapping = http_client.resource_tree_add(resource_tree_set, mount_uuid, first_add)
            success = True
            resource_end_time = time.time()
            self.lab_logger().info(
                f"[Host Node-Resource] 物料创建上传 {round(resource_end_time - resource_start_time, 5) * 1000} ms"
            )
            if uuid_mapping:
                self.lab_logger().info(f"[Host Node-Resource] UUID映射: {len(uuid_mapping)} 个节点")

        if success:
            from unilabos.resources.graphio import physical_setup_graph

            # 将资源添加到本地图中
            for node in resource_tree_set.all_nodes:
                resource_dict = node.res_content.model_dump(by_alias=True)
                if resource_dict.get("id") not in physical_setup_graph.nodes:
                    physical_setup_graph.add_node(resource_dict["id"], **resource_dict)
                else:
                    physical_setup_graph.nodes[resource_dict["id"]]["data"].update(resource_dict.get("data", {}))

        response.response = json.dumps(uuid_mapping) if success else "FAILED"
        self.lab_logger().info(f"[Host Node-Resource] Resource tree add completed, success: {success}")

    async def _resource_tree_action_get_callback(self, data: dict, response: SerialCommand_Response):  # OK
        uuid_list: List[str] = data["data"]
        with_children: bool = data["with_children"]
        from unilabos.app.web.client import http_client

        resource_response = http_client.resource_tree_get(uuid_list, with_children)
        response.response = json.dumps(resource_response)

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

        from unilabos.app.web.client import http_client

        uuid_to_trees: Dict[str, List[ResourceTreeInstance]] = collections.defaultdict(list)
        for tree in resource_tree_set.trees:
            uuid_to_trees[tree.root_node.res_content.parent_uuid].append(tree)

        for uid, trees in uuid_to_trees.items():
            new_tree_set = ResourceTreeSet(trees)
            resource_start_time = time.time()
            self.lab_logger().info(
                f"[Host Node-Resource] 物料 {[root_node.res_content.id for root_node in new_tree_set.root_nodes]} {uid} 挂载 {trees[0].root_node.res_content.parent_uuid} 请求更新上传"
            )
            uuid_mapping = http_client.resource_tree_add(new_tree_set, uid, False)
            success = bool(uuid_mapping)
            resource_end_time = time.time()
            self.lab_logger().info(
                f"[Host Node-Resource] 物料更新上传 {round(resource_end_time - resource_start_time, 5) * 1000} ms"
            )
            if uuid_mapping:
                self.lab_logger().info(f"[Host Node-Resource] UUID映射: {len(uuid_mapping)} 个节点")
            # 还需要加入到资源图中，暂不实现，考虑资源图新的获取方式
            response.response = json.dumps(uuid_mapping)
            self.lab_logger().info(f"[Host Node-Resource] Resource tree add completed, success: {success}")

    async def _resource_tree_update_callback(self, request: SerialCommand_Request, response: SerialCommand_Response):
        """
        子节点通知Host物料树更新

        接收序列化的 ResourceTreeSet 数据并进行处理
        """
        try:
            # 解析请求数据
            data = json.loads(request.command)
            action = data["action"]
            self.lab_logger().info(f"[Host Node-Resource] Resource tree {action} request received")
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

    def _node_info_update_callback(self, request, response):
        """
        更新节点信息回调
        """
        # self.lab_logger().info(f"[Host Node] Node info update request received: {request}")
        try:
            from unilabos.app.communication import get_communication_client
            from unilabos.app.web.client import HTTPClient, http_client

            info = json.loads(request.command)
            if "SYNC_SLAVE_NODE_INFO" in info:
                info = info["SYNC_SLAVE_NODE_INFO"]
                machine_name = info["machine_name"]
                edge_device_id = info["edge_device_id"]
                self.device_machine_names[edge_device_id] = machine_name
            else:
                devices_config = info.pop("devices_config")
                registry_config = info.pop("registry_config")
                if registry_config:
                    http_client.resource_registry({"resources": registry_config})
            self.lab_logger().debug(f"[Host Node] Node info update: {info}")
            response.response = "OK"
        except Exception as e:
            self.lab_logger().error(f"[Host Node] Error updating node info: {e.args}")
            response.response = "ERROR"
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

        success = False
        if len(self.bridges) > 0:  # 边的提交待定
            from unilabos.app.web.client import HTTPClient, http_client

            r = http_client.resource_add(add_schema(resources))
            success = bool(r)

        response.success = success

        if success:
            from unilabos.resources.graphio import physical_setup_graph

            for resource in resources:
                if resource.get("id") not in physical_setup_graph.nodes:
                    physical_setup_graph.add_node(resource["id"], **resource)
                else:
                    physical_setup_graph.nodes[resource["id"]]["data"].update(resource["data"])

        self.lab_logger().info(f"[Host Node-Resource] Add request completed, success: {success}")
        return response

    def _resource_get_process(self, data: Dict[str, Any]):
        r = data["data"]
        self.lab_logger().debug(f"[Host Node-Resource] Retrieved from bridge: {len(r)} resources")
        resources = [convert_to_ros_msg(Resource, resource) for resource in r]
        return resources

    def _resource_get_callback(self, request: SerialCommand.Request, response: SerialCommand.Response):
        """
        获取资源回调
        处理获取资源请求，从桥接器或本地查询资源数据
        Args:
            request: 包含资源ID的请求对象
            response: 响应对象
        Returns:
            响应对象，包含查询到的资源
        """
        try:
            from unilabos.app.web import http_client

            data = json.loads(request.command)
            if "uuid" in data and data["uuid"] is not None:
                http_req = http_client.resource_tree_get([data["uuid"]], data["with_children"])
            elif "id" in data:
                http_req = http_client.resource_get(data["id"], data["with_children"])
            else:
                raise ValueError("没有使用正确的物料 id 或 uuid")
            response.response = json.dumps(http_req["data"])
            return response
        except Exception as e:
            self.lab_logger().error(f"[Host Node-Resource] Error retrieving from bridge: {str(e)}")
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

        success = False
        if len(self.bridges) > 0:
            try:
                r = self.bridges[-1].resource_delete(request.id)
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

        success = False
        if len(self.bridges) > 0:
            try:
                r = self.bridges[-1].resource_update(add_schema(resources))
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

    def test_resource(
        self,
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

    def notify_resource_tree_update(self, device_id: str, action: str, resource_uuid_list: List[str]) -> bool:
        """
        通知设备节点更新资源树

        Args:
            device_id: 目标设备ID
            action: 操作类型 "add", "update", "remove"
            resource_uuid_list: 资源UUIDs

        Returns:
            bool: 操作是否成功
        """
        try:
            # 检查设备是否存在
            if device_id not in self.devices_names:
                self.lab_logger().error(f"[Host Node-Resource] Device {device_id} not found in devices_names")
                return False

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
