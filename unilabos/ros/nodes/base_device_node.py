import inspect
import io
import json
import threading
import time
import traceback
from typing import get_type_hints, TypeVar, Generic, Dict, Any, Type, TypedDict, Optional, List, TYPE_CHECKING, Union, \
    Tuple

from concurrent.futures import ThreadPoolExecutor
import asyncio

import rclpy
import yaml
from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient
from rclpy.action.server import ServerGoalHandle
from rclpy.client import Client
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.service import Service
from unilabos_msgs.action import SendCmd
from unilabos_msgs.srv._serial_command import SerialCommand_Request, SerialCommand_Response
from unilabos.utils.decorator import get_topic_config, get_all_subscriptions

from unilabos.resources.container import RegularContainer
from unilabos.resources.graphio import (
    initialize_resources,
)
from unilabos.resources.plr_additional_res_reg import register
from unilabos.ros.msgs.message_converter import (
    convert_to_ros_msg,
    convert_from_ros_msg_with_mapping,
    convert_to_ros_msg_with_mapping,
)
from unilabos_msgs.srv import (
    ResourceAdd,
    ResourceDelete,
    ResourceUpdate,
    ResourceList,
    SerialCommand,
)  # type: ignore
from unilabos_msgs.msg import Resource  # type: ignore

from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceTreeSet,
    ResourceTreeInstance,
    ResourceDictInstance,
)
from unilabos.ros.x.rclpyx import get_event_loop
from unilabos.ros.utils.driver_creator import WorkstationNodeCreator, PyLabRobotCreator, DeviceClassCreator
from rclpy.task import Task, Future
from unilabos.utils.import_manager import default_manager
from unilabos.utils.log import info, debug, warning, error, critical, logger, trace
from unilabos.utils.type_check import get_type_class, TypeEncoder, get_result_info_str

if TYPE_CHECKING:
    from pylabrobot.resources import Resource as ResourcePLR

T = TypeVar("T")


# 在线设备注册表
registered_devices: Dict[str, "DeviceInfoType"] = {}


# 实现同时记录自定义日志和ROS2日志的适配器
class ROSLoggerAdapter:
    """同时向自定义日志和ROS2日志发送消息的适配器"""

    @property
    def identifier(self):
        return f"{self.namespace}"

    def __init__(self, ros_logger, namespace):
        """
        初始化日志适配器

        Args:
            ros_logger: ROS2日志记录器
            namespace: 命名空间
        """
        self.ros_logger = ros_logger
        self.namespace = namespace
        self.level_2_logger_func = {
            "info": info,
            "debug": debug,
            "trace": trace,
            "warning": warning,
            "error": error,
            "critical": critical,
        }

    def _log(self, level, msg, *args, **kwargs):
        """实际执行日志记录的内部方法"""
        # 添加前缀，使日志更易识别
        msg = f"[{self.identifier}] {msg}"
        # 向ROS2日志发送消息（标准库logging不支持stack_level参数）
        ros_log_func = getattr(self.ros_logger, "debug")  # 默认发送debug，这样不会显示在控制台
        ros_log_func(msg)
        self.level_2_logger_func[level](msg, *args, stack_level=1, **kwargs)

    def trace(self, msg, *args, **kwargs):
        """记录TRACE级别日志"""
        self._log("trace", msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        """记录DEBUG级别日志"""
        self._log("debug", msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        """记录INFO级别日志"""
        self._log("info", msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        """记录WARNING级别日志"""
        self._log("warning", msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        """记录ERROR级别日志"""
        self._log("error", msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        """记录CRITICAL级别日志"""
        self._log("critical", msg, *args, **kwargs)


def init_wrapper(
    self,
    device_id: str,
    device_uuid: str,
    driver_class: type[T],
    device_config: ResourceTreeInstance,
    status_types: Dict[str, Any],
    action_value_mappings: Dict[str, Any],
    hardware_interface: Dict[str, Any],
    print_publish: bool,
    driver_params: Optional[Dict[str, Any]] = None,
    driver_is_ros: bool = False,
    *args,
    **kwargs,
):
    """初始化设备节点的包装函数，和ROS2DeviceNode初始化保持一致"""
    if driver_params is None:
        driver_params = kwargs.copy()
    kwargs["device_id"] = device_id
    kwargs["device_uuid"] = device_uuid
    kwargs["driver_class"] = driver_class
    kwargs["device_config"] = device_config
    kwargs["driver_params"] = driver_params
    kwargs["status_types"] = status_types
    kwargs["action_value_mappings"] = action_value_mappings
    kwargs["hardware_interface"] = hardware_interface
    kwargs["print_publish"] = print_publish
    kwargs["driver_is_ros"] = driver_is_ros
    super(type(self), self).__init__(*args, **kwargs)


class PropertyPublisher:
    def __init__(
        self,
        node: "BaseROS2DeviceNode",
        name: str,
        get_method,
        msg_type,
        initial_period: float = 5.0,
        print_publish=True,
        qos: int = 10,
    ):
        self.node = node
        self.name = name
        self.msg_type = msg_type
        self.get_method = get_method
        self.timer_period = initial_period
        self.print_publish = print_publish
        self.qos = qos

        self._value = None
        try:
            self.publisher_ = node.create_publisher(msg_type, f"{name}", qos)
        except AttributeError as ex:
            self.node.lab_logger().error(
                f"创建发布者 {name} 失败，可能由于注册表有误，类型: {msg_type}，错误: {ex}\n{traceback.format_exc()}"
            )
        self.timer = node.create_timer(self.timer_period, self.publish_property)
        self.__loop = get_event_loop()
        str_msg_type = str(msg_type)[8:-2]
        self.node.lab_logger().trace(f"发布属性: {name}, 类型: {str_msg_type}, 周期: {initial_period}秒, QoS: {qos}")

    def get_property(self):
        if asyncio.iscoroutinefunction(self.get_method):
            # 如果是异步函数，运行事件循环并等待结果
            self.node.lab_logger().trace(f"【.get_property】获取异步属性: {self.name}")
            loop = self.__loop
            if loop:
                future = asyncio.run_coroutine_threadsafe(self.get_method(), loop)
                self._value = future.result()
                return self._value
            else:
                self.node.lab_logger().error(f"【.get_property】事件循环未初始化")
                return None
        else:
            # 如果是同步函数，直接调用并返回结果
            self.node.lab_logger().trace(f"【.get_property】获取同步属性: {self.name}")
            self._value = self.get_method()
            return self._value

    async def get_property_async(self):
        try:
            # 获取异步属性值
            self.node.lab_logger().trace(f"【.get_property_async】异步获取属性: {self.name}")
            self._value = await self.get_method()
        except Exception as e:
            self.node.lab_logger().error(f"【.get_property_async】获取异步属性出错: {str(e)}")

    def publish_property(self):
        try:
            self.node.lab_logger().trace(f"【.publish_property】开始发布属性: {self.name}")
            value = self.get_property()
            if self.print_publish:
                self.node.lab_logger().trace(f"【.publish_property】发布 {self.msg_type}: {value}")
            if value is not None:
                msg = convert_to_ros_msg(self.msg_type, value)
                self.publisher_.publish(msg)
                self.node.lab_logger().trace(f"【.publish_property】属性 {self.name} 发布成功")
        except Exception as e:
            self.node.lab_logger().error(
                f"【.publish_property】发布属性 {self.publisher_.topic} 出错: {str(e)}\n{traceback.format_exc()}"
            )

    def change_frequency(self, period):
        # 动态改变定时器频率
        self.timer_period = period
        self.node.get_logger().info(f"【.change_frequency】修改 {self.name} 定时器周期为: {self.timer_period} 秒")

        # 重置定时器
        self.timer.cancel()
        self.timer = self.node.create_timer(self.timer_period, self.publish_property)


class BaseROS2DeviceNode(Node, Generic[T]):
    """
    ROS2设备节点基类

    这个类提供了ROS2设备节点的基本功能，包括属性发布、动作服务等。
    通过泛型参数T来指定具体的设备类型。
    """

    @property
    def identifier(self):
        return f"{self.namespace}/{self.device_id}"

    node_name: str
    namespace: str
    # 内部共享变量
    _time_spent = 0.0
    _time_remaining = 0.0
    # 是否创建Action
    create_action_server = True

    def __init__(
        self,
        driver_instance: T,
        device_id: str,
        device_uuid: str,
        status_types: Dict[str, Any],
        action_value_mappings: Dict[str, Any],
        hardware_interface: Dict[str, Any],
        print_publish=True,
        resource_tracker: "DeviceNodeResourceTracker" = None,  # type: ignore
    ):
        """
        初始化ROS2设备节点

        Args:
            driver_instance: 设备实例
            device_id: 设备标识符
            device_uuid: 设备标识符
            status_types: 需要发布的状态和传感器信息
            action_value_mappings: 设备动作
            hardware_interface: 硬件接口配置
            print_publish: 是否打印发布信息
        """
        self.driver_instance = driver_instance
        self.device_id = device_id
        self.uuid = device_uuid
        self.publish_high_frequency = False
        self.callback_group = ReentrantCallbackGroup()
        self.resource_tracker = resource_tracker

        # 初始化ROS节点
        self.node_name = f'{device_id.split("/")[-1]}'
        self.namespace = f"/devices/{device_id}"
        Node.__init__(self, self.node_name, namespace=self.namespace)  # type: ignore
        if self.resource_tracker is None:
            self.lab_logger().critical("资源跟踪器未初始化，请检查")

        # 创建自定义日志记录器
        self._lab_logger = ROSLoggerAdapter(self.get_logger(), self.namespace)

        self._action_servers: Dict[str, ActionServer] = {}
        self._property_publishers = {}
        self._status_types = status_types
        self._action_value_mappings = action_value_mappings
        self._hardware_interface = hardware_interface
        self._print_publish = print_publish

        # 创建属性发布者
        for attr_name, msg_type in self._status_types.items():
            if isinstance(attr_name, (int, float)):
                if "param" in msg_type.keys():
                    pass
                else:
                    for k, v in msg_type.items():
                        self.create_ros_publisher(k, v, initial_period=5.0)
            else:
                self.create_ros_publisher(attr_name, msg_type)

        # 创建动作服务
        if self.create_action_server:
            for action_name, action_value_mapping in self._action_value_mappings.items():
                if action_name.startswith("auto-") or str(action_value_mapping.get("type", "")).startswith(
                    "UniLabJsonCommand"
                ):
                    continue
                self.create_ros_action_server(action_name, action_value_mapping)

        # 创建订阅者（通过 @subscribe 装饰器）
        self._topic_subscribers: Dict[str, Any] = {}
        self._setup_decorated_subscribers()

        # 创建线程池执行器
        self._executor = ThreadPoolExecutor(
            max_workers=max(len(action_value_mappings), 1), thread_name_prefix=f"ROSDevice{self.device_id}"
        )

        # 创建资源管理客户端
        self._resource_clients: Dict[str, Client] = {
            "resource_add": self.create_client(ResourceAdd, "/resources/add", callback_group=self.callback_group),
            "resource_get": self.create_client(SerialCommand, "/resources/get", callback_group=self.callback_group),
            "resource_delete": self.create_client(
                ResourceDelete, "/resources/delete", callback_group=self.callback_group
            ),
            "resource_update": self.create_client(
                ResourceUpdate, "/resources/update", callback_group=self.callback_group
            ),
            "resource_list": self.create_client(ResourceList, "/resources/list", callback_group=self.callback_group),
            "c2s_update_resource_tree": self.create_client(
                SerialCommand, "/c2s_update_resource_tree", callback_group=self.callback_group
            ),
        }

        def re_register_device(req, res):
            self.register_device()
            self.lab_logger().info("Host要求重新注册当前节点")
            res.response = ""
            return res

        async def append_resource(req: SerialCommand_Request, res: SerialCommand_Response):
            from pylabrobot.resources.deck import Deck
            from pylabrobot.resources import Coordinate
            from pylabrobot.resources import Plate
            # 物料传输到对应的node节点
            client = self._resource_clients["c2s_update_resource_tree"]
            request = SerialCommand.Request()
            request2 = SerialCommand.Request()
            command_json = json.loads(req.command)
            namespace = command_json["namespace"]
            bind_parent_id = command_json["bind_parent_id"]
            edge_device_id = command_json["edge_device_id"]
            location = command_json["bind_location"]
            other_calling_param = command_json["other_calling_param"]
            input_resources = command_json["resource"]
            initialize_full = other_calling_param.pop("initialize_full", False)
            # 用来增加液体
            ADD_LIQUID_TYPE = other_calling_param.pop("ADD_LIQUID_TYPE", [])
            LIQUID_VOLUME: List[float] = other_calling_param.pop("LIQUID_VOLUME", [])
            LIQUID_INPUT_SLOT: List[int] = other_calling_param.pop("LIQUID_INPUT_SLOT", [])
            slot = other_calling_param.pop("slot", "-1")
            if slot != -1:  # slot为负数的时候采用assign方法
                other_calling_param["slot"] = slot
            # 本地拿到这个物料，可能需要先做初始化
            if isinstance(input_resources, list) and initialize_full:
                input_resources = initialize_resources(input_resources)
            elif initialize_full:
                input_resources = initialize_resources([input_resources])
            rts: ResourceTreeSet = ResourceTreeSet.from_raw_dict_list(input_resources)
            parent_resource = None
            if bind_parent_id != self.node_name:
                parent_resource = self.resource_tracker.figure_resource(
                    {"name": bind_parent_id}
                )
            for r in rts.root_nodes:
                # noinspection PyUnresolvedReferences
                r.res_content.parent_uuid = parent_resource.unilabos_uuid

            if len(LIQUID_INPUT_SLOT) and LIQUID_INPUT_SLOT[0] == -1 and len(rts.root_nodes) == 1 and isinstance(rts.root_nodes[0], RegularContainer):
                # noinspection PyTypeChecker
                container_instance: RegularContainer = rts.root_nodes[0]
                found_resources = self.resource_tracker.figure_resource(
                    {"id": container_instance.name}, try_mode=True
                )
                if not len(found_resources):
                    self.resource_tracker.add_resource(container_instance)
                    logger.info(f"添加物料{container_instance.name}到资源跟踪器")
                else:
                    assert (
                        len(found_resources) == 1
                    ), f"找到多个同名物料: {container_instance.name}, 请检查物料系统"
                    found_resource = found_resources[0]
                    if isinstance(found_resource, RegularContainer):
                        logger.info(f"更新物料{container_instance.name}的数据{found_resource.state}")
                        found_resource.state.update(json.loads(container_instance.state))
                    elif isinstance(found_resource, dict):
                        raise ValueError("已不支持 字典 版本的RegularContainer")
                    else:
                        logger.info(
                            f"更新物料{container_instance.name}出现不支持的数据类型{type(found_resource)} {found_resource}"
                        )
            # noinspection PyUnresolvedReferences
            request.command = json.dumps({
                "action": "add",
                "data": {
                    "data": rts.dump(),
                    "mount_uuid": parent_resource.unilabos_uuid if parent_resource is not None else "",
                    "first_add": False,
                },
            })
            tree_response: SerialCommand.Response = await client.call_async(request)
            uuid_maps = json.loads(tree_response.response)
            self.resource_tracker.loop_update_uuid(input_resources, uuid_maps)
            self.lab_logger().info(f"Resource tree added. UUID mapping: {len(uuid_maps)} nodes")
            final_response = {
                "created_resources": rts.dump(),
                "liquid_input_resources": [],
            }
            res.response = json.dumps(final_response)
            # 如果driver自己就有assign的方法，那就使用driver自己的assign方法
            if hasattr(self.driver_instance, "create_resource"):
                create_resource_func = getattr(self.driver_instance, "create_resource")
                try:
                    ret = create_resource_func(
                        resource_tracker=self.resource_tracker,
                        resources=request.resources,
                        bind_parent_id=bind_parent_id,
                        bind_location=location,
                        liquid_input_slot=LIQUID_INPUT_SLOT,
                        liquid_type=ADD_LIQUID_TYPE,
                        liquid_volume=LIQUID_VOLUME,
                        slot_on_deck=slot,
                    )
                    res.response = get_result_info_str("", True, ret)
                except Exception as e:
                    self.lab_logger().error(
                        f"运行设备的create_resource出错：{create_resource_func}\n{traceback.format_exc()}"
                    )
                    res.response = get_result_info_str(traceback.format_exc(), False, {})
                return res
            try:
                if len(rts.root_nodes) == 1 and parent_resource is not None:
                    plr_instance = rts.to_plr_resources()[0]
                    if isinstance(plr_instance, Plate):
                        empty_liquid_info_in: List[Tuple[Optional[str], float]] = [(None, 0)] * plr_instance.num_items
                        if len(ADD_LIQUID_TYPE) == 1 and len(LIQUID_VOLUME) == 1 and len(LIQUID_INPUT_SLOT) > 1:
                            ADD_LIQUID_TYPE = ADD_LIQUID_TYPE * len(LIQUID_INPUT_SLOT)
                            LIQUID_VOLUME = LIQUID_VOLUME * len(LIQUID_INPUT_SLOT)
                            self.lab_logger().warning(f"增加液体资源时，数量为1，自动补全为 {len(LIQUID_INPUT_SLOT)} 个")
                        for liquid_type, liquid_volume, liquid_input_slot in zip(
                            ADD_LIQUID_TYPE, LIQUID_VOLUME, LIQUID_INPUT_SLOT
                        ):
                            empty_liquid_info_in[liquid_input_slot] = (liquid_type, liquid_volume)
                        plr_instance.set_well_liquids(empty_liquid_info_in)
                        try:
                            # noinspection PyProtectedMember
                            keys = list(plr_instance._ordering.keys())
                            for ind, r in enumerate(LIQUID_INPUT_SLOT[:]):
                                if isinstance(r, int):
                                    # noinspection PyTypeChecker
                                    LIQUID_INPUT_SLOT[ind] = keys[r]
                            input_wells = [plr_instance.get_well(r) for r in LIQUID_INPUT_SLOT]
                        except AttributeError:
                            # 按照id回去失败，回退到children
                            input_wells = []
                            for r in LIQUID_INPUT_SLOT:
                                input_wells.append(plr_instance.children[r])
                        final_response["liquid_input_resources"] = ResourceTreeSet.from_plr_resources(input_wells).dump()
                        res.response = json.dumps(final_response)
                    if issubclass(parent_resource.__class__, Deck) and hasattr(parent_resource, "assign_child_at_slot") and "slot" in other_calling_param:
                        other_calling_param["slot"] = int(other_calling_param["slot"])
                        parent_resource.assign_child_at_slot(plr_instance, **other_calling_param)
                    else:
                        _discard_slot = other_calling_param.pop("slot", -1)
                        parent_resource.assign_child_resource(
                            plr_instance,
                            Coordinate(location["x"], location["y"], location["z"]),
                            **other_calling_param,
                        )
                    # 调整了液体以及Deck之后要重新Assign
                    # noinspection PyUnresolvedReferences
                    rts_with_parent = ResourceTreeSet.from_plr_resources([parent_resource])
                    if rts_with_parent.root_nodes[0].res_content.uuid_parent is None:
                        rts_with_parent.root_nodes[0].res_content.parent_uuid = self.uuid
                    request.command = json.dumps({
                        "action": "add",
                        "data": {
                            "data": rts_with_parent.dump(),
                            "mount_uuid": rts_with_parent.root_nodes[0].res_content.uuid_parent,
                            "first_add": False,
                        },
                    })
                    tree_response: SerialCommand.Response = await client.call_async(request)
                    uuid_maps = json.loads(tree_response.response)
                    self.resource_tracker.loop_update_uuid(input_resources, uuid_maps)
                    self._lab_logger.info(f"Resource tree added. UUID mapping: {len(uuid_maps)} nodes")
                    # 这里created_resources不包含parent_resource
                # 发送给ResourceMeshManager
                action_client = ActionClient(
                    self,
                    SendCmd,
                    "/devices/resource_mesh_manager/add_resource_mesh",
                    callback_group=self.callback_group,
                )
                goal = SendCmd.Goal()
                goal.command = json.dumps(
                    {
                        "resources": input_resources,
                        "bind_parent_id": bind_parent_id,
                    }
                )
                future = action_client.send_goal_async(goal)

                def done_cb(*args):
                    self.lab_logger().info(f"向meshmanager发送新增resource完成")

                future.add_done_callback(done_cb)
            except ImportError:
                self.lab_logger().error("Host请求添加物料时，本环境并不存在pylabrobot")
            except Exception as e:
                self.lab_logger().error("Host请求添加物料时出错")
                self.lab_logger().error(traceback.format_exc())
            return res

        # noinspection PyTypeChecker
        self._service_server: Dict[str, Service] = {
            "re_register_device": self.create_service(
                SerialCommand,
                f"/srv{self.namespace}/re_register_device",
                re_register_device,
                callback_group=self.callback_group,
            ),
            "append_resource": self.create_service(
                SerialCommand,
                f"/srv{self.namespace}/append_resource",
                append_resource,  # type: ignore
                callback_group=self.callback_group,
            ),
            "s2c_resource_tree": self.create_service(
                SerialCommand,
                f"/srv{self.namespace}/s2c_resource_tree",
                self.s2c_resource_tree,  # type: ignore
                callback_group=self.callback_group,
            ),
        }

        # 向全局在线设备注册表添加设备信息
        self.register_device()
        rclpy.get_global_executor().add_node(self)
        self.lab_logger().debug(f"ROS节点初始化完成")

    async def sleep(self, rel_time: float, callback_group=None):
        if callback_group is None:
            callback_group = self.callback_group
        await ROS2DeviceNode.async_wait_for(self, rel_time, callback_group)

    @classmethod
    async def create_task(cls, func, trace_error=True, **kwargs) -> Task:
        return ROS2DeviceNode.run_async_func(func, trace_error, **kwargs)

    async def update_resource(self, resources: List["ResourcePLR"]):
        r = SerialCommand.Request()
        tree_set = ResourceTreeSet.from_plr_resources(resources)
        for tree in tree_set.trees:
            root_node = tree.root_node
            if not root_node.res_content.uuid_parent:
                logger.warning(f"更新无父节点物料{root_node}，自动以当前设备作为根节点")
                root_node.res_content.parent_uuid = self.uuid
        r.command = json.dumps({"data": {"data": tree_set.dump()}, "action": "update"})
        response: SerialCommand_Response = await self._resource_clients["c2s_update_resource_tree"].call_async(r)  # type: ignore
        try:
            uuid_maps = json.loads(response.response)
            self.resource_tracker.loop_update_uuid(resources, uuid_maps)
        except Exception as e:
            self.lab_logger().error(f"更新资源uuid失败: {e}")
            self.lab_logger().error(traceback.format_exc())
        self.lab_logger().trace(f"资源更新结果: {response}")

    async def get_resource(self, resources_uuid: List[str], with_children: bool = True) -> ResourceTreeSet:
        """
        根据资源UUID列表获取资源树

        Args:
            resources_uuid: 资源UUID列表
            with_children: 是否包含子节点，默认为True

        Returns:
            ResourceTreeSet: 资源树集合
        """
        response: SerialCommand.Response = await self._resource_clients["c2s_update_resource_tree"].call_async(
            SerialCommand.Request(
                command=json.dumps(
                    {
                        "data": {"data": resources_uuid, "with_children": with_children},
                        "action": "get",
                    }
                )
            )
        )  # type: ignore
        raw_nodes = json.loads(response.response)
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_nodes)
        self.lab_logger().debug(f"获取资源结果: {len(tree_set.trees)} 个资源树")
        return tree_set

    async def get_resource_with_dir(self, resource_id: str, with_children: bool = True) -> "ResourcePLR":
        """
        根据资源ID获取单个资源实例

        Args:
            resource_ids: 资源ID字符串
            with_children: 是否包含子节点，默认为True

        Returns:
            ResourcePLR: PLR资源实例
        """
        r = SerialCommand.Request()
        r.command = json.dumps(
            {
                "id": resource_id,
                "uuid": None,
                "with_children": with_children,
            }
        )
        # 发送请求并等待响应
        response: SerialCommand_Response = await self._resource_clients["resource_get"].call_async(r)
        raw_data = json.loads(response.response)

        # 转换为 PLR 资源
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        plr_resource = tree_set.to_plr_resources()[0]
        self.lab_logger().debug(f"获取资源 {resource_id} 成功")
        return plr_resource

    def transfer_to_new_resource(
        self, plr_resource: "ResourcePLR", tree: ResourceTreeInstance, additional_add_params: Dict[str, Any]
    ):
        parent_uuid = tree.root_node.res_content.parent_uuid
        if parent_uuid:
            parent_resource: ResourcePLR = self.resource_tracker.uuid_to_resources.get(parent_uuid)
            if parent_resource is None:
                self.lab_logger().warning(
                    f"物料{plr_resource}请求挂载{tree.root_node.res_content.name}的父节点{parent_uuid}不存在"
                )
            else:
                try:
                    # 特殊兼容所有plr的物料的assign方法，和create_resource append_resource后期同步
                    additional_params = {}
                    extra = getattr(plr_resource, "unilabos_extra", {})
                    if len(extra):
                        self.lab_logger().info(f"发现物料{plr_resource}额外参数: " + str(extra))
                    if "update_resource_site" in extra:
                        additional_add_params["site"] = extra["update_resource_site"]
                    site = additional_add_params.get("site", None)
                    spec = inspect.signature(parent_resource.assign_child_resource)
                    if "spot" in spec.parameters:
                        ordering_dict: Dict[str, Any] = getattr(parent_resource, "_ordering")
                        if ordering_dict:
                            site = list(ordering_dict.keys()).index(site)
                        additional_params["spot"] = site
                    old_parent = plr_resource.parent
                    if old_parent is not None:
                        # plr并不支持同一个deck的加载和卸载
                        self.lab_logger().warning(f"物料{plr_resource}请求从{old_parent}卸载")
                        old_parent.unassign_child_resource(plr_resource)
                    self.lab_logger().warning(
                        f"物料{plr_resource}请求挂载到{parent_resource}，额外参数：{additional_params}"
                    )

                    # ⭐ assign 之前，需要从 resources 列表中移除
                    # 因为资源将不再是顶级资源，而是成为 parent_resource 的子资源
                    # 如果不移除，figure_resource 会找到两次：一次在 resources，一次在 parent 的 children
                    resource_id = id(plr_resource)
                    for i, r in enumerate(self.resource_tracker.resources):
                        if id(r) == resource_id:
                            self.resource_tracker.resources.pop(i)
                            self.lab_logger().debug(
                                f"从顶级资源列表中移除 {plr_resource.name}（即将成为 {parent_resource.name} 的子资源）"
                            )
                            break

                    parent_resource.assign_child_resource(plr_resource, location=None, **additional_params)

                    func = getattr(self.driver_instance, "resource_tree_transfer", None)
                    if callable(func):
                        # 分别是 物料的原来父节点，当前物料的状态，物料的新父节点（此时物料已经重新assign了）
                        func(old_parent, plr_resource, parent_resource)
                except Exception as e:
                    self.lab_logger().warning(
                        f"物料{plr_resource}请求挂载{tree.root_node.res_content.name}的父节点{parent_resource}[{parent_uuid}]失败！\n{traceback.format_exc()}"
                    )

    async def s2c_resource_tree(self, req: SerialCommand_Request, res: SerialCommand_Response):
        """
        处理资源树更新请求

        支持三种操作：
        - add: 添加新资源到资源树
        - update: 更新现有资源
        - remove: 从资源树中移除资源
        """
        from pylabrobot.resources.resource import Resource as ResourcePLR

        def _handle_add(
            plr_resources: List[ResourcePLR], tree_set: ResourceTreeSet, additional_add_params: Dict[str, Any]
        ) -> Dict[str, Any]:
            """
            处理资源添加操作的内部函数

            Args:
                plr_resources: PLR资源列表
                tree_set: 资源树集合
                additional_add_params: 额外的添加参数

            Returns:
                操作结果字典
            """
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                self.resource_tracker.add_resource(plr_resource)
                self.transfer_to_new_resource(plr_resource, tree, additional_add_params)

            func = getattr(self.driver_instance, "resource_tree_add", None)
            if callable(func):
                func(plr_resources)

            return {"success": True, "action": "add"}

        def _handle_remove(resources_uuid: List[str]) -> Dict[str, Any]:
            """
            处理资源移除操作的内部函数

            Args:
                resources_uuid: 要移除的资源UUID列表

            Returns:
                操作结果字典，包含移除的资源列表
            """
            found_resources: List[List[Union[ResourcePLR, dict]]] = self.resource_tracker.figure_resource(
                [{"uuid": uid} for uid in resources_uuid], try_mode=True
            )
            found_plr_resources = []
            other_plr_resources = []

            for found_resource in found_resources:
                for resource in found_resource:
                    if issubclass(resource.__class__, ResourcePLR):
                        found_plr_resources.append(resource)
                    else:
                        other_plr_resources.append(resource)

            # 调用driver的remove回调
            func = getattr(self.driver_instance, "resource_tree_remove", None)
            if callable(func):
                func(found_plr_resources)

            # 从parent卸载并从tracker移除
            for plr_resource in found_plr_resources:
                if plr_resource.parent is not None:
                    plr_resource.parent.unassign_child_resource(plr_resource)
                self.resource_tracker.remove_resource(plr_resource)
                self.lab_logger().info(f"移除物料 {plr_resource} 及其子节点")

            for other_plr_resource in other_plr_resources:
                self.resource_tracker.remove_resource(other_plr_resource)
                self.lab_logger().info(f"移除物料 {other_plr_resource} 及其子节点")

            return {
                "success": True,
                "action": "remove",
                # "removed_plr": found_plr_resources,
                # "removed_other": other_plr_resources,
            }

        def _handle_update(
            plr_resources: List[Union[ResourcePLR, ResourceDictInstance]], tree_set: ResourceTreeSet, additional_add_params: Dict[str, Any]
        ) -> Dict[str, Any]:
            """
            处理资源更新操作的内部函数

            Args:
                plr_resources: PLR资源列表（包含新状态）
                tree_set: 资源树集合
                additional_add_params: 额外的参数

            Returns:
                操作结果字典
            """
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                if isinstance(plr_resource, ResourceDictInstance):
                    self._lab_logger.info(f"跳过 非资源{plr_resource.res_content.name} 的更新")
                    continue
                states = plr_resource.serialize_all_state()
                original_instance: ResourcePLR = self.resource_tracker.figure_resource(
                    {"uuid": tree.root_node.res_content.uuid}, try_mode=False
                )

                # Update操作中包含改名：需要先remove再add
                if original_instance.name != plr_resource.name:
                    old_name = original_instance.name
                    new_name = plr_resource.name
                    self.lab_logger().info(f"物料改名操作：{old_name} -> {new_name}")

                    # 收集所有相关的uuid（包括子节点）
                    _handle_remove([original_instance.unilabos_uuid])
                    original_instance.name = new_name
                    _handle_add([original_instance], tree_set, additional_add_params)

                    self.lab_logger().info(f"物料改名完成：{old_name} -> {new_name}")

                # 常规更新：不涉及改名
                original_parent_resource = original_instance.parent
                original_parent_resource_uuid = getattr(original_parent_resource, "unilabos_uuid", None)
                target_parent_resource_uuid = tree.root_node.res_content.uuid_parent

                self.lab_logger().info(
                    f"物料{original_instance} 原始父节点{original_parent_resource_uuid} "
                    f"目标父节点{target_parent_resource_uuid} 更新"
                )

                # 更新extra
                if getattr(plr_resource, "unilabos_extra", None) is not None:
                    original_instance.unilabos_extra = getattr(plr_resource, "unilabos_extra")  # type: ignore  # noqa: E501

                # 如果父节点变化，需要重新挂载
                if (
                    original_parent_resource_uuid != target_parent_resource_uuid
                    and original_parent_resource is not None
                ):
                    self.transfer_to_new_resource(original_instance, tree, additional_add_params)
                else:
                    # 判断是否变更了resource_site
                    target_site = original_instance.unilabos_extra.get("update_resource_site")
                    sites = original_instance.parent.sites if original_instance.parent is not None and hasattr(original_instance.parent, "sites") else None
                    site_names = list(original_instance.parent._ordering.keys()) if original_instance.parent is not None and hasattr(original_instance.parent, "sites") else []
                    if target_site is not None and sites is not None and site_names is not None:
                        site_index = sites.index(original_instance)
                        site_name = site_names[site_index]
                        if site_name != target_site:
                            self.transfer_to_new_resource(original_instance, tree, additional_add_params)

                # 加载状态
                original_instance.load_all_state(states)
                child_count = len(original_instance.get_all_children())
                self.lab_logger().info(
                    f"更新了资源属性 {plr_resource}[{tree.root_node.res_content.uuid}] " f"及其子节点 {child_count} 个"
                )

            # 调用driver的update回调
            func = getattr(self.driver_instance, "resource_tree_update", None)
            if callable(func):
                func(plr_resources)

            return {"success": True, "action": "update"}

        try:
            data = json.loads(req.command)
            results = []

            for i in data:
                action = i.get("action")  # remove, add, update
                resources_uuid: List[str] = i.get("data")  # 资源数据
                additional_add_params = i.get("additional_add_params", {})  # 额外参数
                self.lab_logger().info(
                    f"[Resource Tree Update] Processing {action} operation, " f"resources count: {len(resources_uuid)}"
                )
                tree_set = None
                if action in ["add", "update"]:
                    tree_set = await self.get_resource(
                        resources_uuid=resources_uuid, with_children=True if action == "add" else False
                    )
                try:
                    if action == "add":
                        if tree_set is None:
                            raise ValueError("tree_set不能为None")
                        plr_resources = tree_set.to_plr_resources()
                        result = _handle_add(plr_resources, tree_set, additional_add_params)
                        new_tree_set = ResourceTreeSet.from_plr_resources(plr_resources)
                        r = SerialCommand.Request()
                        r.command = json.dumps(
                            {"data": {"data": new_tree_set.dump()}, "action": "update"})  # 和Update Resource一致
                        response: SerialCommand_Response = await self._resource_clients[
                            "c2s_update_resource_tree"].call_async(r)  # type: ignore
                        self.lab_logger().info(f"确认资源云端 Add 结果: {response.response}")
                        results.append(result)
                    elif action == "update":
                        if tree_set is None:
                            raise ValueError("tree_set不能为None")
                        plr_resources = []
                        for tree in tree_set.trees:
                            if tree.root_node.res_content.type == "device":
                                plr_resources.append(tree.root_node)
                            else:
                                plr_resources.append(ResourceTreeSet([tree]).to_plr_resources()[0])
                        new_tree_set = ResourceTreeSet.from_plr_resources(plr_resources)
                        result = _handle_update(plr_resources, tree_set, additional_add_params)
                        r = SerialCommand.Request()
                        r.command = json.dumps(
                            {"data": {"data": new_tree_set.dump()}, "action": "update"})  # 和Update Resource一致
                        response: SerialCommand_Response = await self._resource_clients[
                            "c2s_update_resource_tree"].call_async(r)  # type: ignore
                        self.lab_logger().info(f"确认资源云端 Update 结果: {response.response}")
                        results.append(result)
                    elif action == "remove":
                        result = _handle_remove(resources_uuid)
                        results.append(result)
                except Exception as e:
                    error_msg = f"Error processing {action} operation: {str(e)}"
                    self.lab_logger().error(f"[Resource Tree Update] {error_msg}")
                    self.lab_logger().error(traceback.format_exc())
                    results.append({"success": False, "action": action, "error": error_msg})

            # 返回处理结果
            result_json = {"results": results, "total": len(data)}
            res.response = json.dumps(result_json, ensure_ascii=False, cls=TypeEncoder)
            self.lab_logger().info(f"[Resource Tree Update] Completed processing {len(data)} operations")

        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format: {str(e)}"
            self.lab_logger().error(f"[Resource Tree Update] {error_msg}")
            res.response = json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.lab_logger().error(f"[Resource Tree Update] {error_msg}")
            self.lab_logger().error(traceback.format_exc())
            res.response = json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)

        return res

    async def transfer_resource_to_another(
        self,
        plr_resources: List["ResourcePLR"],
        target_device_id: str,
        target_resources: List["ResourcePLR"],
        sites: List[str],
    ):
        # 准备工作
        uids = []
        target_uids = []
        for plr_resource in plr_resources:
            uid = getattr(plr_resource, "unilabos_uuid", None)
            if uid is None:
                raise ValueError(f"来源物料{plr_resource}没有unilabos_uuid属性，无法转运")
            uids.append(uid)
        for target_resource in target_resources:
            uid = getattr(target_resource, "unilabos_uuid", None)
            if uid is None:
                raise ValueError(f"目标物料{target_resource}没有unilabos_uuid属性，无法转运")
            target_uids.append(uid)
        srv_address = f"/srv{target_device_id}/s2c_resource_tree"
        sclient = self.create_client(SerialCommand, srv_address)
        # 等待服务可用（设置超时）
        if not sclient.wait_for_service(timeout_sec=5.0):
            self.lab_logger().error(f"[{self.device_id} Node-Resource] Service {srv_address} not available")
            raise ValueError(f"[{self.device_id} Node-Resource] Service {srv_address} not available")

        # 先从当前节点移除资源
        await self.s2c_resource_tree(
            SerialCommand_Request(
                command=json.dumps([{"action": "remove", "data": uids}], ensure_ascii=False)  # 只移除父节点
            ),
            SerialCommand_Response(),
        )

        # 通知云端转运资源
        for plr_resource, target_uid, site in zip(plr_resources, target_uids, sites):
            tree_set = ResourceTreeSet.from_plr_resources([plr_resource])
            for root_node in tree_set.root_nodes:
                root_node.res_content.parent = None
                root_node.res_content.parent_uuid = target_uid
            r = SerialCommand.Request()
            r.command = json.dumps({"data": {"data": tree_set.dump()}, "action": "update"})  # 和Update Resource一致
            response: SerialCommand_Response = await self._resource_clients["c2s_update_resource_tree"].call_async(r)  # type: ignore
            self.lab_logger().info(f"资源云端转运到{target_device_id}结果: {response.response}")

            # 创建请求
            request = SerialCommand.Request()
            request.command = json.dumps(
                [
                    {
                        "action": "add",
                        "data": tree_set.all_nodes_uuid,  # 只添加父节点，子节点会自动添加
                        "additional_add_params": {"site": site},
                    }
                ],
                ensure_ascii=False,
            )

            future = sclient.call_async(request)
            timeout = 30.0
            start_time = time.time()
            while not future.done():
                if time.time() - start_time > timeout:
                    self.lab_logger().error(
                        f"[{self.device_id} Node-Resource] Timeout waiting for response from {target_device_id}"
                    )
                    return False
                time.sleep(0.05)
            self.lab_logger().info(f"资源本地增加到{target_device_id}结果: {response.response}")
        return None

    def register_device(self):
        """向注册表中注册设备信息"""
        topics_info = self._property_publishers.copy()
        actions_info = self._action_servers.copy()
        # 创建设备信息
        device_info = DeviceInfoType(
            id=self.device_id,
            uuid=self.uuid,
            node_name=self.node_name,
            namespace=self.namespace,
            driver_instance=self.driver_instance,
            status_publishers=topics_info,
            actions=actions_info,
            hardware_interface=self._hardware_interface,
            base_node_instance=self,
        )
        # 加入全局注册表
        registered_devices[self.device_id] = device_info
        from unilabos.config.config import BasicConfig
        from unilabos.ros.nodes.presets.host_node import HostNode

        if not BasicConfig.is_host_mode:
            sclient = self.create_client(SerialCommand, "/node_info_update")
            # 启动线程执行发送任务
            threading.Thread(
                target=self.send_slave_node_info,
                args=(sclient,),
                daemon=True,
                name=f"ROSDevice{self.device_id}_send_slave_node_info",
            ).start()
        else:
            host_node = HostNode.get_instance(0)
            if host_node is not None:
                host_node.device_machine_names[self.device_id] = "本地"

    def send_slave_node_info(self, sclient):
        sclient.wait_for_service()
        request = SerialCommand.Request()
        from unilabos.config.config import BasicConfig

        request.command = json.dumps(
            {
                "SYNC_SLAVE_NODE_INFO": {
                    "machine_name": BasicConfig.machine_name,
                    "type": "slave",
                    "edge_device_id": self.device_id,
                }
            },
            ensure_ascii=False,
            cls=TypeEncoder,
        )

        # 发送异步请求并等待结果
        future = sclient.call_async(request)
        response = future.result()

    def lab_logger(self):
        """
        获取实验室自定义日志记录器

        这个日志记录器会同时向ROS2日志和自定义日志发送消息，
        并使用node_name和namespace作为标识。

        Returns:
            日志记录器实例
        """
        return self._lab_logger

    def create_ros_publisher(self, attr_name, msg_type, initial_period=5.0):
        """创建ROS发布者"""
        # 检测装饰器配置（支持 get_{attr_name} 方法和 @property）
        topic_config = {}

        # 优先检测 get_{attr_name} 方法
        if hasattr(self.driver_instance, f"get_{attr_name}"):
            getter_method = getattr(self.driver_instance, f"get_{attr_name}")
            topic_config = get_topic_config(getter_method)

        # 如果没有配置，检测 @property 装饰的属性
        if not topic_config:
            driver_class = type(self.driver_instance)
            if hasattr(driver_class, attr_name):
                class_attr = getattr(driver_class, attr_name)
                if isinstance(class_attr, property) and class_attr.fget is not None:
                    topic_config = get_topic_config(class_attr.fget)

        # 使用装饰器配置或默认值
        cfg_period = topic_config.get("period")
        cfg_print = topic_config.get("print_publish")
        cfg_qos = topic_config.get("qos")
        period: float = cfg_period if cfg_period is not None else initial_period
        print_publish: bool = cfg_print if cfg_print is not None else self._print_publish
        qos: int = cfg_qos if cfg_qos is not None else 10

        # 获取属性值的方法
        def get_device_attr():
            try:
                if hasattr(self.driver_instance, f"get_{attr_name}"):
                    return getattr(self.driver_instance, f"get_{attr_name}")()
                else:
                    return getattr(self.driver_instance, attr_name)
            except AttributeError as ex:
                if ex.args[0].startswith(f"AttributeError: '{self.driver_instance.__class__.__name__}' object"):
                    self.lab_logger().error(
                        f"publish error, {str(type(self.driver_instance))[8:-2]} has no attribute '{attr_name}'"
                    )
                else:
                    self.lab_logger().error(
                        f"publish error, when {str(type(self.driver_instance))[8:-2]} getting attribute '{attr_name}'"
                    )
                    self.lab_logger().error(traceback.format_exc())

        self._property_publishers[attr_name] = PropertyPublisher(
            self, attr_name, get_device_attr, msg_type, period, print_publish, qos
        )

    def create_ros_action_server(self, action_name, action_value_mapping):
        """创建ROS动作服务器"""
        action_type = action_value_mapping["type"]
        str_action_type = str(action_type)[8:-2]

        self._action_servers[action_name] = ActionServer(
            self,
            action_type,
            action_name,
            execute_callback=self._create_execute_callback(action_name, action_value_mapping),
            callback_group=self.callback_group,
        )

        self.lab_logger().trace(f"发布动作: {action_name}, 类型: {str_action_type}")

    def _setup_decorated_subscribers(self):
        """扫描 driver_instance 中带有 @subscribe 装饰器的方法并创建订阅者"""
        subscriptions = get_all_subscriptions(self.driver_instance)

        for method_name, method, config in subscriptions:
            topic_template = config.get("topic")
            msg_type = config.get("msg_type")
            qos = config.get("qos", 10)

            if not topic_template:
                self.lab_logger().warning(f"订阅方法 {method_name} 缺少 topic 配置，跳过")
                continue

            # 如果没有指定 msg_type，尝试从类型注解推断
            if msg_type is None:
                try:
                    hints = get_type_hints(method)
                    # 第一个参数是 self，第二个是 msg
                    param_names = list(hints.keys())
                    if param_names:
                        msg_type = hints[param_names[0]]
                except Exception:
                    pass

            if msg_type is None:
                self.lab_logger().warning(f"订阅方法 {method_name} 缺少 msg_type 配置且无法从类型注解推断，跳过")
                continue

            # 替换 topic 模板中的占位符
            topic = self._resolve_topic_template(topic_template)

            self.create_ros_subscriber(topic, msg_type, method, qos)

    def _resolve_topic_template(self, topic_template: str) -> str:
        """
        解析 topic 模板，替换占位符

        支持的占位符:
            - {device_id}: 设备ID
            - {namespace}: 完整命名空间
        """
        return topic_template.format(
            device_id=self.device_id,
            namespace=self.namespace,
        )

    def create_ros_subscriber(self, topic: str, msg_type, callback, qos: int = 10):
        """
        创建ROS订阅者

        Args:
            topic: Topic 名称
            msg_type: ROS 消息类型
            callback: 回调方法（会自动绑定到 driver_instance）
            qos: QoS 深度配置
        """
        try:
            subscription = self.create_subscription(
                msg_type,
                topic,
                callback,
                qos,
                callback_group=self.callback_group,
            )
            self._topic_subscribers[topic] = subscription
            str_msg_type = str(msg_type)[8:-2] if str(msg_type).startswith("<class") else str(msg_type)
            self.lab_logger().trace(f"订阅Topic: {topic}, 类型: {str_msg_type}, QoS: {qos}")
        except Exception as ex:
            self.lab_logger().error(f"创建订阅者 {topic} 失败，类型: {msg_type}，错误: {ex}\n{traceback.format_exc()}")

    def get_real_function(self, instance, attr_name):
        if hasattr(instance.__class__, attr_name):
            obj = getattr(instance.__class__, attr_name)
            if isinstance(obj, property):
                return lambda *args, **kwargs: obj.fset(instance, *args, **kwargs), get_type_hints(obj.fset)
            obj = getattr(instance, attr_name)
            return obj, get_type_hints(obj)
        else:
            obj = getattr(instance, attr_name)
            return obj, get_type_hints(obj)

    def _create_execute_callback(self, action_name, action_value_mapping):
        """创建动作执行回调函数"""

        async def execute_callback(goal_handle: ServerGoalHandle):
            # 初始化结果信息变量
            execution_error = ""
            execution_success = False
            action_return_value = None

            #####    self.lab_logger().info(f"执行动作: {action_name}")
            goal = goal_handle.request

            # 从目标消息中提取参数, 并调用对应的方法
            if "sequence" in action_value_mapping:
                # 如果一个指令对应函数的连续调用，如启动和等待结果，默认参数应该属于第一个函数调用
                def ACTION(**kwargs):
                    for i, action in enumerate(action_value_mapping["sequence"]):
                        if i == 0:
                            self.lab_logger().info(f"执行序列动作第一步: {action}")
                            self.get_real_function(self.driver_instance, action)[0](**kwargs)
                        else:
                            self.lab_logger().info(f"执行序列动作后续步骤: {action}")
                            self.get_real_function(self.driver_instance, action)[0]()

                action_paramtypes = self.get_real_function(self.driver_instance, action_value_mapping["sequence"][0])[
                    1
                ]
            else:
                ACTION, action_paramtypes = self.get_real_function(self.driver_instance, action_name)

            action_kwargs = convert_from_ros_msg_with_mapping(goal, action_value_mapping["goal"])
            self.lab_logger().debug(f"任务 {ACTION.__name__} 接收到原始目标: {action_kwargs}")
            error_skip = False
            # 向Host查询物料当前状态，如果是host本身的增加物料的请求，则直接跳过
            if action_name not in ["create_resource_detailed", "create_resource"]:
                for k, v in goal.get_fields_and_field_types().items():
                    if v in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]:
                        self.lab_logger().info(f"{action_name} 查询资源状态: Key: {k} Type: {v}")

                        try:
                            # 统一处理单个或多个资源
                            is_sequence = v != "unilabos_msgs/Resource"
                            resource_inputs = action_kwargs[k] if is_sequence else [action_kwargs[k]]

                            # 批量查询资源
                            queried_resources = []
                            for resource_data in resource_inputs:
                                plr_resource = await self.get_resource_with_dir(
                                    resource_id=resource_data["id"], with_children=True
                                )
                                if "sample_id" in resource_data:
                                    plr_resource.unilabos_extra["sample_uuid"] = resource_data["sample_id"]
                                queried_resources.append(plr_resource)

                            self.lab_logger().debug(f"资源查询结果: 共 {len(queried_resources)} 个资源")

                            # 通过资源跟踪器获取本地实例
                            final_resources = queried_resources if is_sequence else queried_resources[0]
                            if not is_sequence:
                                plr = self.resource_tracker.figure_resource({"name": final_resources.name}, try_mode=False)
                                # 保留unilabos_extra
                                if hasattr(final_resources, "unilabos_extra") and hasattr(plr, "unilabos_extra"):
                                    plr.unilabos_extra = getattr(final_resources, "unilabos_extra", {}).copy()
                                final_resources = plr
                            else:
                                new_resources = []
                                for res in queried_resources:
                                    plr = self.resource_tracker.figure_resource({"name": res.name}, try_mode=False)
                                    if hasattr(res, "unilabos_extra") and hasattr(plr, "unilabos_extra"):
                                        plr.unilabos_extra = getattr(res, "unilabos_extra", {}).copy()
                                    new_resources.append(plr)
                                final_resources = new_resources
                            action_kwargs[k] = final_resources

                        except Exception as e:
                            self.lab_logger().error(f"{action_name} 物料实例获取失败: {e}\n{traceback.format_exc()}")
                            error_skip = True
                            execution_error = traceback.format_exc()
                            break

            time_start = time.time()
            time_overall = 100
            future = None
            if not error_skip:
                # 将阻塞操作放入线程池执行
                if asyncio.iscoroutinefunction(ACTION):
                    try:
                        self.lab_logger().trace(f"异步执行动作 {ACTION}")

                        def _handle_future_exception(fut: Future):
                            nonlocal execution_error, execution_success, action_return_value
                            try:
                                action_return_value = fut.result()
                                if isinstance(action_return_value, BaseException):
                                    raise action_return_value
                                execution_success = True
                            except Exception as _:
                                execution_error = traceback.format_exc()
                                error(
                                    f"异步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{action_kwargs}"
                                )

                        future = ROS2DeviceNode.run_async_func(ACTION, trace_error=False, **action_kwargs)
                        future.add_done_callback(_handle_future_exception)
                    except Exception as e:
                        execution_error = traceback.format_exc()
                        execution_success = False
                        self.lab_logger().error(f"创建异步任务失败: {traceback.format_exc()}")
                else:
                    self.lab_logger().trace(f"同步执行动作 {ACTION}")
                    future = self._executor.submit(ACTION, **action_kwargs)

                    def _handle_future_exception(fut: Future):
                        nonlocal execution_error, execution_success, action_return_value
                        try:
                            action_return_value = fut.result()
                            execution_success = True
                        except Exception as _:
                            execution_error = traceback.format_exc()
                            error(
                                f"同步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{action_kwargs}"
                            )

                    future.add_done_callback(_handle_future_exception)

            action_type = action_value_mapping["type"]
            feedback_msg_types = action_type.Feedback.get_fields_and_field_types()
            result_msg_types = action_type.Result.get_fields_and_field_types()

            while future is not None and not future.done():
                if goal_handle.is_cancel_requested:
                    self.lab_logger().info(f"取消动作: {action_name}")
                    future.cancel()  # 尝试取消线程池中的任务
                    goal_handle.canceled()
                    return action_type.Result()

                self._time_spent = time.time() - time_start
                self._time_remaining = time_overall - self._time_spent

                # 发布反馈
                feedback_values = {}
                for msg_name, attr_name in action_value_mapping["feedback"].items():
                    if hasattr(self.driver_instance, f"get_{attr_name}"):
                        method = getattr(self.driver_instance, f"get_{attr_name}")
                        if not asyncio.iscoroutinefunction(method):
                            feedback_values[msg_name] = method()
                    elif hasattr(self.driver_instance, attr_name):
                        feedback_values[msg_name] = getattr(self.driver_instance, attr_name)

                if self._print_publish:
                    self.lab_logger().info(f"反馈: {feedback_values}")

                feedback_msg = convert_to_ros_msg_with_mapping(
                    ros_msg_type=action_type.Feedback(),
                    obj=feedback_values,
                    value_mapping=action_value_mapping["feedback"],
                )

                goal_handle.publish_feedback(feedback_msg)
                time.sleep(0.5)

            if future is not None and future.cancelled():
                self.lab_logger().info(f"动作 {action_name} 已取消")
                return action_type.Result()

            # self.lab_logger().info(f"动作执行完成: {action_name}")
            del future

            # 向Host更新物料当前状态
            if action_name not in ["create_resource_detailed", "create_resource"]:
                for k, v in goal.get_fields_and_field_types().items():
                    if v not in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]:
                        continue
                    self.lab_logger().info(f"更新资源状态: {k}")
                    # 仅当action_kwargs[k]不为None时尝试转换
                    akv = action_kwargs[k]  # 已经是完成转换的物料了
                    apv = action_paramtypes[k]
                    final_type = get_type_class(apv)
                    if final_type is None:
                        continue
                    try:
                        # 去重：使用 seen 集合获取唯一的资源对象
                        seen = set()
                        unique_resources = []
                        for rs in akv:  # todo: 这里目前只支持plr的类型
                            if isinstance(rs, list):
                                for r in rs:
                                    res = self.resource_tracker.parent_resource(r)  # 获取 resource 对象
                            else:
                                res = self.resource_tracker.parent_resource(r)
                            if id(res) not in seen:
                                seen.add(id(res))
                                unique_resources.append(res)

                        # 使用新的资源树接口
                        if unique_resources:
                            await self.update_resource(unique_resources)
                    except Exception as e:
                        self.lab_logger().error(f"资源更新失败: {e}")
                        self.lab_logger().error(traceback.format_exc())

            # 发布结果
            goal_handle.succeed()
            ##### self.lab_logger().info(f"设置动作成功: {action_name}")

            result_values = {}
            for msg_name, attr_name in action_value_mapping["result"].items():
                if hasattr(self.driver_instance, f"get_{attr_name}"):
                    result_values[msg_name] = getattr(self.driver_instance, f"get_{attr_name}")()
                elif hasattr(self.driver_instance, attr_name):
                    result_values[msg_name] = getattr(self.driver_instance, attr_name)

            result_msg = convert_to_ros_msg_with_mapping(
                ros_msg_type=action_type.Result(), obj=result_values, value_mapping=action_value_mapping["result"]
            )

            for attr_name in result_msg_types.keys():
                if attr_name in ["success", "reached_goal"]:
                    setattr(result_msg, attr_name, True)
                elif attr_name == "return_info":
                    setattr(
                        result_msg,
                        attr_name,
                        get_result_info_str(execution_error, execution_success, action_return_value),
                    )

            self.lab_logger().trace(f"动作 {action_name} 完成并返回结果")
            return result_msg

        return execute_callback

    def _execute_driver_command(self, string: str):
        try:
            target = json.loads(string)
        except Exception as ex:
            try:
                target = yaml.safe_load(io.StringIO(string))
            except Exception as ex2:
                raise JsonCommandInitError(
                    f"执行动作时JSON/YAML解析失败: \n{ex}\n{ex2}\n原内容: {string}\n{traceback.format_exc()}"
                )
        try:
            function_name = target["function_name"]
            function_args = target["function_args"]
            assert isinstance(function_args, dict), "执行动作时JSON必须为dict类型\n原JSON: {string}"
            function = getattr(self.driver_instance, function_name)
            assert callable(
                function
            ), f"执行动作时JSON中的function_name对应的函数不可调用: {function_name}\n原JSON: {string}"

            # 处理 ResourceSlot 类型参数
            args_list = default_manager._analyze_method_signature(function)["args"]
            for arg in args_list:
                arg_name = arg["name"]
                arg_type = arg["type"]

                # 跳过不在 function_args 中的参数
                if arg_name not in function_args:
                    continue

                # 处理单个 ResourceSlot
                if arg_type == "unilabos.registry.placeholder_type:ResourceSlot":
                    resource_data = function_args[arg_name]
                    if isinstance(resource_data, dict) and "id" in resource_data:
                        try:
                            converted_resource = self._convert_resource_sync(resource_data)
                            function_args[arg_name] = converted_resource
                        except Exception as e:
                            self.lab_logger().error(
                                f"转换ResourceSlot参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                            )
                            raise JsonCommandInitError(f"ResourceSlot参数转换失败: {arg_name}")

                # 处理 ResourceSlot 列表
                elif isinstance(arg_type, tuple) and len(arg_type) == 2:
                    resource_slot_type = "unilabos.registry.placeholder_type:ResourceSlot"
                    if arg_type[0] == "list" and arg_type[1] == resource_slot_type:
                        resource_list = function_args[arg_name]
                        if isinstance(resource_list, list):
                            try:
                                converted_resources = []
                                for resource_data in resource_list:
                                    if isinstance(resource_data, dict) and "id" in resource_data:
                                        converted_resource = self._convert_resource_sync(resource_data)
                                        converted_resources.append(converted_resource)
                                function_args[arg_name] = converted_resources
                            except Exception as e:
                                self.lab_logger().error(
                                    f"转换ResourceSlot列表参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                                )
                                raise JsonCommandInitError(f"ResourceSlot列表参数转换失败: {arg_name}")

            return function(**function_args)
        except KeyError as ex:
            raise JsonCommandInitError(
                f"执行动作时JSON缺少function_name或function_args: {ex}\n原JSON: {string}\n{traceback.format_exc()}"
            )

    def _convert_resource_sync(self, resource_data: Dict[str, Any]):
        """同步转换资源数据为实例"""
        # 创建资源查询请求
        r = SerialCommand.Request()
        r.command = json.dumps(
            {
                "id": resource_data.get("id", None),
                "uuid": resource_data.get("uuid", None),
                "with_children": True,
            }
        )

        # 同步调用资源查询服务
        future = self._resource_clients["resource_get"].call_async(r)

        # 等待结果（使用while循环，每次sleep 0.05秒，最多等待30秒）
        timeout = 30.0
        elapsed = 0.0
        while not future.done() and elapsed < timeout:
            time.sleep(0.05)
            elapsed += 0.05

        if not future.done():
            raise Exception(f"资源查询超时: {resource_data}")

        response = future.result()
        if response is None:
            raise Exception(f"资源查询返回空结果: {resource_data}")

        raw_data = json.loads(response.response)

        # 转换为 PLR 资源
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        plr_resource = tree_set.to_plr_resources()[0]

        # 通过资源跟踪器获取本地实例
        res = self.resource_tracker.figure_resource(plr_resource, try_mode=True)
        if len(res) == 0:
            self.lab_logger().warning(f"资源转换未能索引到实例: {resource_data}，返回新建实例")
            return plr_resource
        elif len(res) == 1:
            return res[0]
        else:
            raise ValueError(f"资源转换得到多个实例: {res}")

    async def _execute_driver_command_async(self, string: str):
        try:
            target = json.loads(string)
        except Exception as ex:
            try:
                target = yaml.safe_load(io.StringIO(string))
            except Exception as ex2:
                raise JsonCommandInitError(
                    f"执行动作时JSON/YAML解析失败: \n{ex}\n{ex2}\n原内容: {string}\n{traceback.format_exc()}"
                )
        try:
            function_name = target["function_name"]
            function_args = target["function_args"]
            assert isinstance(function_args, dict), "执行动作时JSON必须为dict类型\n原JSON: {string}"
            function = getattr(self.driver_instance, function_name)
            assert callable(
                function
            ), f"执行动作时JSON中的function_name对应的函数不可调用: {function_name}\n原JSON: {string}"
            assert asyncio.iscoroutinefunction(
                function
            ), f"执行动作时JSON中的function并非异步: {function_name}\n原JSON: {string}"

            # 处理 ResourceSlot 类型参数
            args_list = default_manager._analyze_method_signature(function)["args"]
            for arg in args_list:
                arg_name = arg["name"]
                arg_type = arg["type"]

                # 跳过不在 function_args 中的参数
                if arg_name not in function_args:
                    continue

                # 处理单个 ResourceSlot
                if arg_type == "unilabos.registry.placeholder_type:ResourceSlot":
                    resource_data = function_args[arg_name]
                    if isinstance(resource_data, dict) and "id" in resource_data:
                        try:
                            converted_resource = await self._convert_resource_async(resource_data)
                            function_args[arg_name] = converted_resource
                        except Exception as e:
                            self.lab_logger().error(
                                f"转换ResourceSlot参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                            )
                            raise JsonCommandInitError(f"ResourceSlot参数转换失败: {arg_name}")

                # 处理 ResourceSlot 列表
                elif isinstance(arg_type, tuple) and len(arg_type) == 2:
                    resource_slot_type = "unilabos.registry.placeholder_type:ResourceSlot"
                    if arg_type[0] == "list" and arg_type[1] == resource_slot_type:
                        resource_list = function_args[arg_name]
                        if isinstance(resource_list, list):
                            try:
                                converted_resources = []
                                for resource_data in resource_list:
                                    if isinstance(resource_data, dict) and "id" in resource_data:
                                        converted_resource = await self._convert_resource_async(resource_data)
                                        converted_resources.append(converted_resource)
                                function_args[arg_name] = converted_resources
                            except Exception as e:
                                self.lab_logger().error(
                                    f"转换ResourceSlot列表参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                                )
                                raise JsonCommandInitError(f"ResourceSlot列表参数转换失败: {arg_name}")

            return await function(**function_args)
        except KeyError as ex:
            raise JsonCommandInitError(
                f"执行动作时JSON缺少function_name或function_args: {ex}\n原JSON: {string}\n{traceback.format_exc()}"
            )

    async def _convert_resource_async(self, resource_data: Dict[str, Any]):
        """异步转换资源数据为实例"""
        # 使用封装的get_resource_with_dir方法获取PLR资源
        plr_resource = await self.get_resource_with_dir(resource_ids=resource_data["id"], with_children=True)

        # 通过资源跟踪器获取本地实例
        res = self.resource_tracker.figure_resource(plr_resource, try_mode=True)
        if len(res) == 0:
            # todo: 后续通过decoration来区分，减少warning
            self.lab_logger().warning(f"资源转换未能索引到实例: {resource_data}，返回新建实例")
            return plr_resource
        elif len(res) == 1:
            return res[0]
        else:
            raise ValueError(f"资源转换得到多个实例: {res}")

    # 异步上下文管理方法
    async def __aenter__(self):
        """进入异步上下文"""
        self.lab_logger().info(f"进入异步上下文: {self.device_id}")
        if hasattr(self.driver_instance, "__aenter__"):
            await self.driver_instance.__aenter__()  # type: ignore
        self.lab_logger().info(f"异步上下文初始化完成: {self.device_id}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出异步上下文"""
        self.lab_logger().info(f"退出异步上下文: {self.device_id}")
        if hasattr(self.driver_instance, "__aexit__"):
            await self.driver_instance.__aexit__(exc_type, exc_val, exc_tb)  # type: ignore
        self.lab_logger().info(f"异步上下文清理完成: {self.device_id}")


class DeviceInitError(Exception):
    pass


class JsonCommandInitError(Exception):
    pass


class ROS2DeviceNode:
    """
    ROS2设备节点类

    这个类封装了设备类实例和ROS2节点的功能，提供ROS2接口。
    它不继承设备类，而是通过代理模式访问设备类的属性和方法。
    """

    @staticmethod
    async def safe_task_wrapper(trace_callback, func, **kwargs):
        try:
            if callable(trace_callback):
                trace_callback(await func(**kwargs))
            return await func(**kwargs)
        except Exception as e:
            if callable(trace_callback):
                trace_callback(e)
            return e

    @classmethod
    def run_async_func(cls, func, trace_error=True, inner_trace_callback=None, **kwargs) -> Task:
        def _handle_future_exception(fut: Future):
            try:
                ret = fut.result()
                if isinstance(ret, BaseException):
                    raise ret
            except Exception as e:
                error(f"异步任务 {func.__name__} 获取结果失败")
                error(traceback.format_exc())

        future = rclpy.get_global_executor().create_task(
            ROS2DeviceNode.safe_task_wrapper(inner_trace_callback, func, **kwargs)
        )
        if trace_error:
            future.add_done_callback(_handle_future_exception)
        return future

    @classmethod
    async def async_wait_for(cls, node: Node, wait_time: float, callback_group=None):
        future = Future()
        timer = node.create_timer(
            wait_time, lambda: future.set_result(None), callback_group=callback_group, clock=node.get_clock()
        )
        await future
        timer.cancel()
        node.destroy_timer(timer)

    @property
    def driver_instance(self):
        return self._driver_instance

    @property
    def ros_node_instance(self):
        return self._ros_node

    def __init__(
        self,
        device_id: str,
        device_uuid: str,
        driver_class: Type[T],
        device_config: ResourceDictInstance,
        driver_params: Dict[str, Any],
        status_types: Dict[str, Any],
        action_value_mappings: Dict[str, Any],
        hardware_interface: Dict[str, Any],
        print_publish: bool = True,
        driver_is_ros: bool = False,
    ):
        """
        初始化ROS2设备节点

        Args:
            device_id: 设备标识符
            device_uuid: 设备uuid
            driver_class: 设备类
            device_config: 原始初始化的ResourceDictInstance
            driver_params: driver初始化的参数
            status_types: 状态类型映射
            action_value_mappings: 动作值映射
            hardware_interface: 硬件接口配置
            children:
            print_publish: 是否打印发布信息
            driver_is_ros:
        """

        # 保存设备类是否支持异步上下文
        self._has_async_context = hasattr(driver_class, "__aenter__") and hasattr(driver_class, "__aexit__")
        self._driver_class = driver_class
        self.device_config = device_config
        children: List[ResourceDictInstance] = device_config.children
        self.driver_is_ros = driver_is_ros
        self.driver_is_workstation = False
        self.resource_tracker = DeviceNodeResourceTracker()

        # use_pylabrobot_creator 使用 cls的包路径检测
        use_pylabrobot_creator = (
            driver_class.__module__.startswith("pylabrobot")
            or driver_class.__name__ == "LiquidHandlerAbstract"
            or driver_class.__name__ == "LiquidHandlerBiomek"
            or driver_class.__name__ == "PRCXI9300Handler"
            or driver_class.__name__ == "TransformXYZHandler"
            or driver_class.__name__ == "OpcUaClient"
        )

        # 创建设备类实例
        if use_pylabrobot_creator:
            # 先对pylabrobot的子资源进行加载，不然subclass无法认出
            # 在下方对于加载Deck等Resource要手动import
            register()
            self._driver_creator = PyLabRobotCreator(
                driver_class, children=children, resource_tracker=self.resource_tracker
            )
        else:
            from unilabos.devices.workstation.workstation_base import WorkstationBase

            if issubclass(
                self._driver_class, WorkstationBase
            ):  # 是WorkstationNode的子节点，就要调用WorkstationNodeCreator
                self.driver_is_workstation = True
                self._driver_creator = WorkstationNodeCreator(
                    driver_class, children=children, resource_tracker=self.resource_tracker
                )
            else:
                self._driver_creator = DeviceClassCreator(
                    driver_class, children=children, resource_tracker=self.resource_tracker
                )

        if driver_is_ros:
            driver_params["device_id"] = device_id
            driver_params["resource_tracker"] = self.resource_tracker
        self._driver_instance = self._driver_creator.create_instance(driver_params)
        if self._driver_instance is None:
            logger.critical(f"设备实例创建失败 {driver_class}, params: {driver_params}")
            raise DeviceInitError("错误: 设备实例创建失败")

        # 创建ROS2节点
        if driver_is_ros:
            self._ros_node = self._driver_instance  # type: ignore
        elif self.driver_is_workstation:
            from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode

            self._ros_node = ROS2WorkstationNode(
                protocol_type=driver_params["protocol_type"],
                children=children,
                driver_instance=self._driver_instance,  # type: ignore
                device_id=device_id,
                device_uuid=device_uuid,
                status_types=status_types,
                action_value_mappings=action_value_mappings,
                hardware_interface=hardware_interface,
                print_publish=print_publish,
                resource_tracker=self.resource_tracker,
            )
        else:
            self._ros_node = BaseROS2DeviceNode(
                driver_instance=self._driver_instance,
                device_id=device_id,
                device_uuid=device_uuid,
                status_types=status_types,
                action_value_mappings=action_value_mappings,
                hardware_interface=hardware_interface,
                print_publish=print_publish,
                resource_tracker=self.resource_tracker,
            )
        self._ros_node: BaseROS2DeviceNode
        self._ros_node.lab_logger().info(f"初始化完成 {self._ros_node.uuid} {self.driver_is_ros}")
        self.driver_instance._ros_node = self._ros_node  # type: ignore
        self.driver_instance._execute_driver_command = self._ros_node._execute_driver_command  # type: ignore
        self.driver_instance._execute_driver_command_async = self._ros_node._execute_driver_command_async  # type: ignore
        if hasattr(self.driver_instance, "post_init"):
            try:
                self.driver_instance.post_init(self._ros_node)  # type: ignore
            except Exception as e:
                self._ros_node.lab_logger().error(f"设备后初始化失败: {e}")


class DeviceInfoType(TypedDict):
    id: str
    uuid: str
    node_name: str
    namespace: str
    driver_instance: Any
    status_publishers: Dict[str, PropertyPublisher]
    actions: Dict[str, ActionServer]
    hardware_interface: Dict[str, Any]
    base_node_instance: BaseROS2DeviceNode
