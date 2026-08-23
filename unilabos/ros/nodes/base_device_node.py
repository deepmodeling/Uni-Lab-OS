import inspect
import io
import json
import threading
import time
import traceback

from unilabos.utils.tools import fast_dumps_str as _fast_dumps_str, fast_loads as _fast_loads
from typing import (
    get_type_hints,
    TypeVar,
    Generic,
    Dict,
    Any,
    Type,
    TypedDict,
    Optional,
    List,
    TYPE_CHECKING,
    Union,
    Tuple,
)

from concurrent.futures import ThreadPoolExecutor
import asyncio

import rclpy
import yaml
from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient, get_action_server_names_and_types_by_node
from rclpy.action.server import ServerGoalHandle
from rclpy.client import Client
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.service import Service
from unilabos_msgs.action import SendCmd, StrSingleInput
from unilabos_msgs.srv._serial_command import SerialCommand_Request, SerialCommand_Response

from unilabos.config.config import BasicConfig
from unilabos.device_runtime.node import DeviceNode
from unilabos.device_runtime.async_utils import schedule_async_func
from unilabos.registry.decorators import get_topic_config
from unilabos.registry.action_policy import (
    SUCCESS_TYPE_NORMAL,
)
from unilabos.registry.placeholder_type import ResourceSlotRawInput
from unilabos.utils.decorator import get_all_subscriptions

from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.materials import apply_substances, resolve_site_spot
from unilabos.resources.graphio import (
    initialize_resources,
)
from unilabos.resources.objects.resource import EXTRA_SAMPLE_UUID, ResourceDictType
from unilabos.ros.msgs.message_converter import (
    String,
    convert_to_ros_msg,
    convert_from_ros_msg,
    convert_from_ros_msg_with_mapping,
    convert_to_ros_msg_with_mapping,
    get_ros_type_by_msgname,
)
from unilabos_msgs.srv import SerialCommand  # type: ignore

from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceTreeSet,
    ResourceTreeInstance,
    ResourceDictInstance,
    PARAM_SAMPLE_UUIDS,
    JSON_UNILABOS_PARAM,
)
from unilabos.device_runtime.driver_creator import (
    select_driver_creator,
)
from rclpy.task import Task, Future
from unilabos.utils.import_manager import default_manager
from unilabos.utils.log import info, debug, warning, error, critical, logger, trace
from unilabos.utils.type_check import TypeEncoder, serialize_result_info
from unilabos.utils.exception import ActionResultError, DeviceActionError

if TYPE_CHECKING:
    from pylabrobot.resources import Resource as ResourcePLR

T = TypeVar("T")


def _native_driver_result_failed(
    action_name: str, action_type: Any, value: Any
) -> bool:
    """原生 ROS Action 的 bool/dict success 是业务成功位；JSON Command 可返回 bool 数据。"""

    type_name = str(getattr(action_type, "__name__", ""))
    if action_name.startswith("_execute_driver_command") or type_name.startswith(
        "UniLabJsonCommand"
    ):
        return False
    if value is False:
        return True
    return isinstance(value, dict) and value.get("success") is False


def _coerce_device_error_info(
    action_name: str,
    value: Any,
    error_text: str,
) -> Dict[str, Any]:
    """把原生 Action 的失败返回归一化为 Host 可匹配的结构化错误。"""

    source: Dict[str, Any] = {}
    if isinstance(value, dict):
        provided = value.get("error_info")
        source = (
            {**value, **provided}
            if isinstance(provided, dict)
            else dict(value)
        )

    exception_type = str(source.get("exception_type") or "ActionResultError")
    raw_mro = source.get("exception_mro")
    if isinstance(raw_mro, list) and raw_mro:
        exception_mro = [str(name) for name in raw_mro]
    elif exception_type == "ActionResultError":
        exception_mro = [
            error_class.__name__ for error_class in ActionResultError.__mro__
        ]
    else:
        exception_mro = [
            exception_type,
            "Exception",
            "BaseException",
            "object",
        ]

    error_message = str(
        source.get("error_message")
        or source.get("error")
        or source.get("message")
        or source.get("reason")
        or error_text
        or "device action reported an unsuccessful result"
    )
    error_info: Dict[str, Any] = {
        "action_name": str(source.get("action_name") or action_name),
        "exception_type": exception_type,
        "exception_mro": exception_mro,
        "error_message": error_message,
        "traceback": str(source.get("traceback") or error_text or error_message),
    }
    for key in ("category", "severity"):
        if source.get(key) is not None:
            error_info[key] = str(source[key])
    return error_info


class RclpyAsyncMutex:
    """rclpy executor 兼容的异步互斥锁

    通过 executor.create_task 唤醒等待者，避免 timer 的 InvalidHandle 问题。
    """

    def __init__(self, name: str = ""):
        self._lock = threading.Lock()
        self._acquired = False
        self._queue: List[Future] = []
        self._name = name
        self._holder: Optional[str] = None

    async def acquire(self, node: "BaseROS2DeviceNode", tag: str = ""):
        """获取锁。如果已被占用，则异步等待直到锁释放。"""
        t0 = time.time()
        with self._lock:
            qlen = len(self._queue)
            if not self._acquired:
                self._acquired = True
                self._holder = tag
                # node.lab_logger().debug(
                #     f"[Mutex:{self._name}] 获取锁 tag={tag} (无等待, queue=0)"
                # )
                return
            holder = self._holder
            waiter = Future()
            self._queue.append(waiter)
            node.lab_logger().trace(
                f"[Mutex:{self._name}] 进入锁等待队列 tag={tag} holder={holder} queue={qlen + 1}"
            )
        try:
            await waiter
        except BaseException:
            with self._lock:
                if waiter in self._queue:
                    self._queue.remove(waiter)
                    node.lab_logger().debug(
                        f"[Mutex:{self._name}] 取消锁等待 tag={tag} queue={len(self._queue)}"
                    )
            raise
        wait_ms = (time.time() - t0) * 1000
        self._holder = tag
        node.lab_logger().trace(f"[Mutex:{self._name}] 队列继续执行 tag={tag} waited={wait_ms:.0f}ms")

    def release(self, node: "BaseROS2DeviceNode"):
        """释放锁，通过 executor task 唤醒下一个等待者。"""
        with self._lock:
            old_holder = self._holder
            next_waiter = None
            while self._queue:
                next_waiter = self._queue.pop(0)
                if not next_waiter.done():
                    break
                next_waiter = None
            if next_waiter is not None:
                node.lab_logger().trace(
                    f"[Mutex:{self._name}] 释放锁 holder={old_holder} 唤醒队列下一个 queue={len(self._queue)}"
                )

                async def _wake():
                    if not next_waiter.done():
                        next_waiter.set_result(None)

                rclpy.get_global_executor().create_task(_wake())
            else:
                self._acquired = False
                self._holder = None
                # node.lab_logger().debug(
                #     f"[Mutex:{self._name}] 释放锁 holder={old_holder} → 空闲"
                # )


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
    device_config: ResourceDictInstance,
    status_types: Dict[str, Any],
    action_value_mappings: Dict[str, Any],
    hardware_interface: Dict[str, Any],
    print_publish: bool,
    driver_params: Dict[str, Any],
    driver_is_ros: bool = False,
    *args,
    **kwargs,
):
    """初始化设备节点的包装函数，和ROS2DeviceNode初始化保持一致"""
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
        self.msg_type = self._normalize_msg_type(msg_type)
        self.original_msg_type = msg_type
        self.get_method = get_method
        self.timer_period = initial_period
        self.print_publish = print_publish
        self.qos = qos

        self._value = None
        try:
            self.publisher_ = node.create_publisher(self.msg_type, f"{name}", qos)
        except Exception as e:
            self.node.lab_logger().error(
                f"StatusError, DeviceId: {self.node.device_id} 创建发布者 {name} 失败，"
                f"可能由于注册表有误，类型: {msg_type}，错误: {e}"
            )
            self.msg_type = String
            try:
                self.publisher_ = node.create_publisher(self.msg_type, f"{name}", qos)
                self.node.lab_logger().warning(
                    f"属性 {name} 的发布类型已降级为 String，原始类型: {msg_type}"
                )
            except Exception:
                self.publisher_ = None
        self.timer = node.create_timer(self.timer_period, self.publish_property)
        self.__loop = ROS2DeviceNode.get_asyncio_loop()
        str_msg_type = str(self.msg_type)[8:-2]
        self.node.lab_logger().trace(f"发布属性: {name}, 类型: {str_msg_type}, 周期: {initial_period}秒, QoS: {qos}")

    @staticmethod
    def _normalize_msg_type(msg_type):
        if msg_type in (dict, list, tuple, set) or msg_type in ("dict", "list", "tuple", "set"):
            return String
        return msg_type

    def _normalize_value(self, value):
        if self.msg_type is String and isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, ensure_ascii=False, cls=TypeEncoder)
        return value

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
            # self.node.lab_logger().trace(f"【.publish_property】开始发布属性: {self.name}")
            value = self.get_property()
            self.node.emit_status(self.name, value)
            if self.print_publish:
                pass
                # self.node.lab_logger().trace(f"【.publish_property】发布 {self.msg_type}: {value}")
            if value is not None:
                if self.publisher_ is None:
                    return
                value = self._normalize_value(value)
                msg = convert_to_ros_msg(self.msg_type, value)
                self.publisher_.publish(msg)
                # self.node.lab_logger().trace(f"【.publish_property】属性 {self.name} 发布成功")
        except Exception as e:
            topic = getattr(self.publisher_, "topic", self.name)
            self.node.lab_logger().error(
                f"【.publish_property】发布属性 {topic} 出错: {str(e)}\n{traceback.format_exc()}"
            )

    def change_frequency(self, period):
        # 动态改变定时器频率
        self.timer_period = period
        self.node.get_logger().info(f"【.change_frequency】修改 {self.name} 定时器周期为: {self.timer_period} 秒")

        # 重置定时器
        self.timer.cancel()
        self.timer = self.node.create_timer(self.timer_period, self.publish_property)


class BaseROS2DeviceNode(Node, DeviceNode, Generic[T]):
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
    backend_name = "ros2"

    def __init__(
        self,
        driver_instance: T,
        device_id: str,
        registry_name: str,
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
        self.registry_name = registry_name
        self.uuid = device_uuid
        self.resource_uuid = device_uuid
        self.publish_high_frequency = False
        self.callback_group = ReentrantCallbackGroup()
        self.resource_tracker = resource_tracker
        from unilabos.device_runtime.resource import AuthorityResourceService

        self.set_resource_service(AuthorityResourceService())

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
        self._topic_subscriber_types: Dict[str, Any] = {}  # 每个 topic 当前订阅所用的消息类型
        self._subscriber_monitors: Dict[str, Any] = {}  # retry_interval 模式下"重试建立订阅"的定时器（建立成功即取消）
        self._setup_decorated_subscribers()

        # 跨设备调用动作时复用的 ActionClient 缓存（key: action_id）
        self._cross_device_action_clients: Dict[str, ActionClient] = {}
        # 跨设备动作类型探测缓存（key: "<clean_device_id>/<function_name>"，value: 原生 Action 类型或 None）
        self._remote_action_type_cache: Dict[str, Any] = {}
        # 创建线程池执行器
        self._executor = ThreadPoolExecutor(
            max_workers=max(len(action_value_mappings), 1), thread_name_prefix=f"ROSDevice{self.device_id}"
        )

        self._append_resource_lock = RclpyAsyncMutex(name=f"AR:{device_id}")
        self._resource_tree_uuid_locks: Dict[str, RclpyAsyncMutex] = {}
        self._resource_tree_uuid_locks_guard = threading.Lock()

        # 创建资源管理客户端
        self._resource_clients: Dict[str, Client] = {
            "resource_get": self.create_client(SerialCommand, "/resources/get", callback_group=self.callback_group),
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
            _cmd = _fast_loads(req.command)
            _res_name = _cmd.get("resource", [{}])
            _res_name = (_res_name[0].get("id", "?") if isinstance(_res_name, list) and _res_name
                         else _res_name.get("id", "?") if isinstance(_res_name, dict) else "?")
            _ar_tag = f"{_res_name}"
            # _t_enter = time.time()
            # self.lab_logger().info(f"[AR:{_ar_tag}] 进入 append_resource")
            await self._append_resource_lock.acquire(self, tag=_ar_tag)
            # _t_locked = time.time()
            try:
                return await _append_resource_inner(req, res, _ar_tag)
                # _t_done = time.time()
                # self.lab_logger().info(
                #     f"[AR:{_ar_tag}] 完成 "
                #     f"等锁={(_t_locked - _t_enter) * 1000:.0f}ms "
                #     f"执行={(_t_done - _t_locked) * 1000:.0f}ms "
                #     f"总计={(_t_done - _t_enter) * 1000:.0f}ms"
                # )
            except Exception as _ex:
                self.lab_logger().error(f"[AR:{_ar_tag}] 异常: {_ex}")
                raise
            finally:
                self._append_resource_lock.release(self)

        async def _append_resource_inner(req: SerialCommand_Request, res: SerialCommand_Response, _ar_tag: str = ""):
            from pylabrobot.resources.deck import Deck
            from pylabrobot.resources import Coordinate

            # _t0 = time.time()
            client = self._resource_clients["c2s_update_resource_tree"]
            request = SerialCommand.Request()
            request2 = SerialCommand.Request()
            command_json = _fast_loads(req.command)
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
                parent_resource = self.resource_tracker.figure_resource({"name": bind_parent_id})
                for r in rts.root_nodes:
                    # noinspection PyUnresolvedReferences
                    r.res_content.parent_uuid = parent_resource.unilabos_uuid
            else:
                for r in rts.root_nodes:
                    r.res_content.parent_uuid = self.uuid
            rts_plr_instances = rts.to_plr_resources()
            if len(rts.root_nodes) == 1 and isinstance(rts_plr_instances[0], RegularContainer):
                # noinspection PyTypeChecker
                container_instance: RegularContainer = rts_plr_instances[0]
                found_resources = self.resource_tracker.figure_resource(
                    {"name": container_instance.name}, try_mode=True
                )
                if not len(found_resources):
                    self.resource_tracker.add_resource(container_instance)
                    logger.info(f"添加物料{container_instance.name}到资源跟踪器")
                else:
                    assert len(found_resources) == 1, f"找到多个同名物料: {container_instance.name}, 请检查物料系统"
                    found_resource = found_resources[0]
                    if isinstance(found_resource, RegularContainer):
                        logger.info(f"更新物料{container_instance.name}的数据{found_resource.state}")
                        found_resource.state.update(container_instance.state)
                    elif isinstance(found_resource, dict):
                        raise ValueError("已不支持 字典 版本的RegularContainer")
                    else:
                        logger.info(
                            f"更新物料{container_instance.name}出现不支持的数据类型{type(found_resource)} {found_resource}"
                        )
            # noinspection PyUnresolvedReferences
            # _t1 = time.time()
            # self.lab_logger().debug(
            #     f"[AR:{_ar_tag}] 准备完成 PLR转换+序列化 {((_t1 - _t0) * 1000):.0f}ms, 发送首次上传..."
            # )
            request.command = _fast_dumps_str(
                {
                    "action": "add",
                    "data": {
                        "data": rts.dump(),
                        "mount_uuid": parent_resource.unilabos_uuid if parent_resource is not None else self.uuid,
                        "first_add": False,
                    },
                }
            )
            tree_response: SerialCommand.Response = await client.call_async(request)
            # _t2 = time.time()
            # self.lab_logger().debug(
            #     f"[AR:{_ar_tag}] 首次上传完成 {((_t2 - _t1) * 1000):.0f}ms"
            # )
            uuid_maps = _fast_loads(tree_response.response)
            plr_instances = rts.to_plr_resources()
            for plr_instance in plr_instances:
                self.resource_tracker.loop_update_uuid(plr_instance, uuid_maps)
            # driver 自带 create_resource 时由 driver 负责增加液体；否则走通用 apply_substances
            _delegates_to_driver = hasattr(self.driver_instance, "create_resource") and self.node_name != "host_node"
            # 容器自身液体 (LIQUID_INPUT_SLOT == [-1])：在快照前写入，使其归属 created_resource_tree
            if (
                not _delegates_to_driver
                and len(plr_instances) == 1
                and ADD_LIQUID_TYPE
                and list(LIQUID_INPUT_SLOT) == [-1]
            ):
                apply_substances(plr_instances[0], ADD_LIQUID_TYPE, LIQUID_VOLUME, broadcast=True)
            rts: ResourceTreeSet = ResourceTreeSet.from_plr_resources(plr_instances)
            self.lab_logger().info(f"Resource tree added. UUID mapping: {len(uuid_maps)} nodes")
            final_response = {
                "created_resource_tree": rts.dump(),
                "liquid_input_resource_tree": [],
            }
            res.response = json.dumps(final_response)
            # 如果driver自己就有assign的方法，那就使用driver自己的assign方法
            if _delegates_to_driver:
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
                    res.response = json.dumps(serialize_result_info("", True, ret), ensure_ascii=False)
                except Exception as e:
                    self.lab_logger().error(
                        f"运行设备的create_resource出错：{create_resource_func}\n{traceback.format_exc()}"
                    )
                    res.response = json.dumps(
                        serialize_result_info(traceback.format_exc(), False, {}),
                        ensure_ascii=False,
                    )
                return res
            try:
                if len(rts.root_nodes) == 1 and parent_resource is not None:
                    plr_instance = plr_instances[0]
                    # 子孔位液体 (LIQUID_INPUT_SLOT != [-1])：写入子孔位，归属 liquid_input_resource_tree。
                    # 统一走 apply_substances：孔位解析(int→_ordering key/名称/children) + 1→N 广播 + set_liquids。
                    if ADD_LIQUID_TYPE and list(LIQUID_INPUT_SLOT) != [-1]:
                        input_wells = apply_substances(
                            plr_instance, ADD_LIQUID_TYPE, LIQUID_VOLUME, slots=LIQUID_INPUT_SLOT, broadcast=True
                        )
                        final_response["liquid_input_resource_tree"] = ResourceTreeSet.from_plr_resources(
                            input_wells
                        ).dump()
                        res.response = json.dumps(final_response)
                    if (
                        issubclass(parent_resource.__class__, Deck)
                        and hasattr(parent_resource, "assign_child_at_slot")
                        and "slot" in other_calling_param
                    ):
                        other_calling_param["slot"] = int(other_calling_param["slot"])
                        parent_resource.assign_child_at_slot(plr_instance, **other_calling_param)
                    else:
                        _discard_slot = other_calling_param.pop("slot", -1)
                        parent_resource.assign_child_resource(
                            plr_instance,
                            Coordinate(location["x"], location["y"], location["z"]),
                            **other_calling_param,
                        )
                    # noinspection PyUnresolvedReferences
                    # _t3 = time.time()
                    rts_with_parent = ResourceTreeSet.from_plr_resources([parent_resource])
                    # _n_parent = len(rts_with_parent.all_nodes)
                    if rts_with_parent.root_nodes[0].res_content.uuid_parent is None:
                        rts_with_parent.root_nodes[0].res_content.parent_uuid = self.uuid
                    request.command = _fast_dumps_str(
                        {
                            "action": "add",
                            "data": {
                                "data": rts_with_parent.dump(),
                                "mount_uuid": rts_with_parent.root_nodes[0].res_content.uuid_parent,
                                "first_add": False,
                            },
                        }
                    )
                    # _t4 = time.time()
                    # self.lab_logger().debug(
                    #     f"[AR:{_ar_tag}] 二次上传序列化 {_n_parent}节点 {((_t4 - _t3) * 1000):.0f}ms, 发送中..."
                    # )
                    tree_response: SerialCommand.Response = await client.call_async(request)
                    # _t5 = time.time()
                    uuid_maps = _fast_loads(tree_response.response)
                    self.resource_tracker.loop_update_uuid(input_resources, uuid_maps)
                    # self._lab_logger.info(
                    #     f"[AR:{_ar_tag}] 二次上传完成 HTTP={(_t5 - _t4) * 1000:.0f}ms "
                    #     f"UUID映射={len(uuid_maps)}节点 总执行={(_t5 - _t0) * 1000:.0f}ms"
                    # )
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
                res.response = json.dumps(
                    serialize_result_info(traceback.format_exc(), False, {}),
                    ensure_ascii=False,
                )
            except Exception as e:
                self.lab_logger().error("Host请求添加物料时出错")
                self.lab_logger().error(traceback.format_exc())
                res.response = json.dumps(
                    serialize_result_info(traceback.format_exc(), False, {}),
                    ensure_ascii=False,
                )
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
            "material_sync": self.setup_material_sync_service(),
            "s2c_device_manage": self.create_service(
                SerialCommand,
                f"/srv{self.namespace}/s2c_device_manage",
                self.s2c_device_manage,  # type: ignore
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

    def create_task(self, coroutine, trace_error=True, **kwargs) -> Task:
        """Schedule a coroutine while accepting the legacy async-function form."""

        if callable(coroutine):
            return self.run_async_func(
                coroutine,
                trace_error,
                **kwargs,
            )
        if kwargs:
            raise TypeError("协程对象不能再接收额外关键字参数")

        return rclpy.get_global_executor().create_task(coroutine)

    async def update_resource(self, resources: List["ResourcePLR"]):
        return await DeviceNode.update_resource(self, resources)

    async def get_resource(self, resources_uuid: List[str], with_children: bool = True) -> ResourceTreeSet:
        """
        根据资源UUID列表获取资源树

        Args:
            resources_uuid: 资源UUID列表
            with_children: 是否包含子节点，默认为True

        Returns:
            ResourceTreeSet: 资源树集合
        """
        tree_set = await DeviceNode.get_resource(
            self,
            resources_uuid,
            with_children,
        )
        self.lab_logger().trace(f"获取资源结果: {len(tree_set.trees)} 个资源树 {tree_set.root_nodes}")
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
        if not response.response:
            raise ValueError(f"查询资源 {resource_id} 失败：服务端返回空响应")
        raw_data = json.loads(response.response)
        if not raw_data:
            raise ValueError(f"查询资源 {resource_id} 失败：返回数据为空")

        # 转换为 PLR 资源
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        plr_resource = tree_set.to_plr_resources()[0]
        self.lab_logger().debug(f"获取资源 {resource_id} 成功")
        return plr_resource

    def transfer_to_new_resource(
        self, plr_resource: "ResourcePLR", tree: ResourceTreeInstance, additional_add_params: Dict[str, Any]
    ) -> Optional["ResourcePLR"]:
        parent_uuid = tree.root_node.res_content.parent_uuid
        if not parent_uuid:
            self.lab_logger().warning(
                f"物料{plr_resource} parent未知，挂载到当前节点下，额外参数：{additional_add_params}"
            )
            return None
        if parent_uuid == self.uuid:
            self.lab_logger().warning(
                f"物料{plr_resource}请求挂载到{self.identifier}，额外参数：{additional_add_params}"
            )
            return None
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
                    # 复用 set_substance 的同一套 slot/site 解析（int 索引 / "A1" 标签 / 名称匹配）；
                    # 解析不出（含 site 为空）则回退原始 site，由父级默认排布。
                    spot = resolve_site_spot(parent_resource, site)
                    additional_params["spot"] = spot if spot is not None else site
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
                return parent_resource
            except Exception as e:
                self.lab_logger().warning(
                    f"物料{plr_resource}请求挂载{tree.root_node.res_content.name}的父节点{parent_resource}[{parent_uuid}]失败！\n{traceback.format_exc()}"
                )

    async def _acquire_resource_tree_uuid_locks(self, uuids: List[str], tag: str = "") -> List[RclpyAsyncMutex]:
        """按资源 UUID 获取本节点实例内的资源树锁。"""
        lock_keys = sorted({str(uid) for uid in uuids if uid})
        with self._resource_tree_uuid_locks_guard:
            locks = [
                self._resource_tree_uuid_locks.setdefault(uid, RclpyAsyncMutex(name=f"RT:{self.device_id}:{uid}"))
                for uid in lock_keys
            ]

        acquired_locks = []
        try:
            for lock in locks:
                await lock.acquire(self, tag=tag)
                acquired_locks.append(lock)
        except BaseException:
            self._release_resource_tree_uuid_locks(acquired_locks)
            raise
        return acquired_locks

    def _release_resource_tree_uuid_locks(self, locks: List[RclpyAsyncMutex]):
        for lock in reversed(locks):
            lock.release(self)

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
        ) -> Tuple[Dict[str, Any], List[ResourcePLR]]:
            """
            处理资源添加操作的内部函数

            Args:
                plr_resources: PLR资源列表
                tree_set: 资源树集合
                additional_add_params: 额外的添加参数

            Returns:
                操作结果字典
            """
            parents = []  # 放的是被变更的物料 / 被变更的物料父级
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                self.resource_tracker.add_resource(plr_resource)
                parent = self.transfer_to_new_resource(plr_resource, tree, additional_add_params)
                if parent is not None:
                    parents.append(parent)
                else:
                    parents.append(plr_resource)

            func = getattr(self.driver_instance, "resource_tree_add", None)
            if callable(func):
                func(plr_resources)

            return {"success": True, "action": "add"}, parents

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
                self.lab_logger().info(f"[资源同步] 移除物料 {plr_resource} 及其子节点")

            for other_plr_resource in other_plr_resources:
                self.resource_tracker.remove_resource(other_plr_resource)
                self.lab_logger().info(f"[资源同步] 移除物料 {other_plr_resource} 及其子节点")

            return {
                "success": True,
                "action": "remove",
                # "removed_plr": found_plr_resources,
                # "removed_other": other_plr_resources,
            }

        def _handle_update(
            plr_resources: List[Union[ResourcePLR, ResourceDictInstance]],
            tree_set: ResourceTreeSet,
            additional_add_params: Dict[str, Any],
        ) -> Tuple[Dict[str, Any], List[ResourcePLR]]:
            """
            处理资源更新操作的内部函数

            Args:
                plr_resources: PLR资源列表（包含新状态）
                tree_set: 资源树集合
                additional_add_params: 额外的参数

            Returns:
                操作结果字典
            """
            original_instances = []
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                if isinstance(plr_resource, ResourceDictInstance):
                    self._lab_logger.info(f"跳过 非资源{plr_resource.res_content.name} 的更新")
                    continue
                states = plr_resource.serialize_all_state()
                original_instance: ResourcePLR = self.resource_tracker.figure_resource(
                    {"uuid": tree.root_node.res_content.uuid}, try_mode=False
                )
                original_parent_resource = original_instance.parent
                original_parent_resource_uuid = getattr(original_parent_resource, "unilabos_uuid", None)
                target_parent_resource_uuid = tree.root_node.res_content.uuid_parent
                if target_parent_resource_uuid == self.uuid:
                    not_same_parent = False
                    original_parent_resource = None
                    original_parent_resource_uuid = self.uuid
                else:
                    not_same_parent = (
                        original_parent_resource_uuid != target_parent_resource_uuid
                        and original_parent_resource is not None
                    )
                old_name = original_instance.name
                new_name = plr_resource.name
                parent_appended = False

                # Update操作中包含改名：需要先remove再add，这里更新父节点即可
                if not not_same_parent and old_name != new_name:
                    self.lab_logger().info(f"物料改名操作：{old_name} -> {new_name}")

                    # 收集所有相关的uuid（包括子节点）
                    _handle_remove([original_instance.unilabos_uuid])
                    original_instance.name = new_name
                    _handle_add([original_instance], tree_set, additional_add_params)

                    self.lab_logger().info(f"物料改名完成：{old_name} -> {new_name}")
                    original_instances.append(original_parent_resource)
                    parent_appended = True

                # 常规更新：不涉及改名
                self.lab_logger().info(
                    f"物料{original_instance} 原始父节点{original_parent_resource_uuid} "
                    f"目标父节点{target_parent_resource_uuid} 更新"
                )

                # 更新extra
                if getattr(plr_resource, "unilabos_extra", None) is not None:
                    original_instance.unilabos_extra = getattr(plr_resource, "unilabos_extra")  # type: ignore  # noqa: E501

                # 如果父节点变化，需要重新挂载
                if not_same_parent:
                    parent = self.transfer_to_new_resource(original_instance, tree, additional_add_params)
                    original_instances.append(parent)
                    parent_appended = True
                else:
                    # 判断是否变更了resource_site，重新登记
                    target_site = original_instance.unilabos_extra.get("update_resource_site")
                    sites = (
                        original_parent_resource.sites
                        if original_parent_resource is not None and hasattr(original_parent_resource, "sites")
                        else None
                    )
                    site_names = (
                        list(original_parent_resource._ordering.keys())
                        if original_parent_resource is not None and hasattr(original_parent_resource, "sites")
                        else []
                    )
                    if target_site is not None and sites is not None and site_names is not None:
                        site_index = None
                        try:
                            # sites 可能是 ItemizedCarrier 的 Resource 列表，或 PRCXI 的 ResourceSite 列表。
                            # 只有itemized_carrier在使用，准备弃用
                            site_index = sites.index(original_instance)
                        except ValueError:
                            # canonical Site 只按占用物料 UUID 匹配。
                            for idx, site in enumerate(sites):
                                if isinstance(site, dict):
                                    occupied_uuid = site.get("occupied_material_uuid")
                                else:
                                    occupied_uuid = getattr(site, "occupied_material_uuid", None)
                                if occupied_uuid and occupied_uuid == getattr(
                                    original_instance, "unilabos_uuid", None
                                ):
                                    site_index = idx
                                    break
                                # 默认资源会放到000，后续必须按 occupied_material_uuid 精确匹配。
                                # elif (original_instance.location.x == site["position"]["x"] and original_instance.location.y == site["position"]["y"] and original_instance.location.z == site["position"]["z"]):
                                #     site_index = idx
                                #     break
                        if site_index is None:
                            site_name = None
                        else:
                            site_name = site_names[site_index]
                        if site_name != target_site:
                            parent = self.transfer_to_new_resource(original_instance, tree, additional_add_params)
                            if parent is not None:
                                original_instances.append(parent)
                                parent_appended = True

                # 加载状态
                # noinspection PyProtectedMember
                original_instance._size_x = plr_resource._size_x
                # noinspection PyProtectedMember
                original_instance._size_y = plr_resource._size_y
                # noinspection PyProtectedMember
                original_instance._size_z = plr_resource._size_z
                # noinspection PyProtectedMember
                original_instance._local_size_z = plr_resource._local_size_z
                original_instance.location = plr_resource.location
                original_instance.rotation = plr_resource.rotation
                original_instance.barcode = plr_resource.barcode
                original_instance.load_all_state(states)
                child_count = len(original_instance.get_all_children())
                self.lab_logger().info(
                    f"更新了资源属性 {plr_resource}[{tree.root_node.res_content.uuid}] " f"及其子节点 {child_count} 个"
                )
                if not parent_appended:
                    original_instances.append(original_instance)

            # 调用driver的update回调
            func = getattr(self.driver_instance, "resource_tree_update", None)
            if callable(func):
                func(original_instances)

            return {"success": True, "action": "update"}, original_instances

        try:
            data = json.loads(req.command)
            results = []

            for i in data:
                action = i.get("action")  # remove, add, update
                resources_uuid: List[str] = i.get("data") or []  # 资源数据
                if not isinstance(resources_uuid, list):
                    resources_uuid = [resources_uuid]
                additional_add_params = i.get("additional_add_params", {})  # 额外参数
                self.lab_logger().trace(f"[资源同步] 处理 {action}, " f"resources count: {len(resources_uuid)}")
                # NOTE: 当前仅按请求中显式传入的 uuid 加锁。
                # 如果后续操作会修改未出现在请求里的父节点或子节点，需要把这些关联 uuid 一并纳入锁集合。
                resource_locks = await self._acquire_resource_tree_uuid_locks(
                    resources_uuid, tag=f"{action}:{','.join(map(str, resources_uuid))}"
                )
                try:
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
                            result, parents = _handle_add(plr_resources, tree_set, additional_add_params)
                            parents: List[Optional["ResourcePLR"]] = [i for i in parents if i is not None]
                            # de_dupe_parents = list(set(parents))
                            # Fix unhashable type error for WareHouse
                            de_dupe_parents = []
                            _seen_ids = set()
                            for p in parents:
                                if id(p) not in _seen_ids:
                                    _seen_ids.add(id(p))
                                    de_dupe_parents.append(p)
                            new_tree_set = ResourceTreeSet.from_plr_resources(de_dupe_parents)  # 去重
                            for tree in new_tree_set.trees:
                                if tree.root_node.res_content.uuid_parent is None and self.node_name != "host_node":
                                    tree.root_node.res_content.parent_uuid = self.uuid
                            r = SerialCommand.Request()
                            r.command = json.dumps(
                                {"data": {"data": new_tree_set.dump()}, "action": "update"}
                            )  # 和Update Resource一致
                            response: SerialCommand_Response = await self._resource_clients[
                                "c2s_update_resource_tree"
                            ].call_async(
                                r
                            )  # type: ignore
                            self.lab_logger().trace(f"确认资源云端 Add 结果: {response.response}")
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
                            result, original_instances = _handle_update(plr_resources, tree_set, additional_add_params)
                            if not BasicConfig.no_update_feedback:
                                new_tree_set = ResourceTreeSet.from_plr_resources(original_instances)  # 去重
                                for tree in new_tree_set.trees:
                                    if tree.root_node.res_content.uuid_parent is None and self.node_name != "host_node":
                                        tree.root_node.res_content.parent_uuid = self.uuid
                                r = SerialCommand.Request()
                                r.command = json.dumps(
                                    {"data": {"data": new_tree_set.dump()}, "action": "update"}
                                )  # 和Update Resource一致
                                response: SerialCommand_Response = await self._resource_clients[
                                    "c2s_update_resource_tree"
                                ].call_async(
                                    r
                                )  # type: ignore
                                self.lab_logger().trace(f"确认资源云端 Update 结果: {response.response}")
                            results.append(result)
                        elif action == "remove":
                            result = _handle_remove(resources_uuid)
                            results.append(result)
                    except Exception as e:
                        error_msg = f"Error processing {action} operation: {str(e)}"
                        self.lab_logger().error(f"[Resource Tree Update] {error_msg}")
                        self.lab_logger().error(traceback.format_exc())
                        results.append({"success": False, "action": action, "error": error_msg})
                finally:
                    self._release_resource_tree_uuid_locks(resource_locks)

            # 返回处理结果
            result_json = {"results": results, "total": len(data)}
            res.response = json.dumps(result_json, ensure_ascii=False, cls=TypeEncoder)
            # self.lab_logger().info(f"[Resource Tree Update] Completed processing {len(data)} operations")

        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format: {str(e)}"
            self.lab_logger().error(f"[资源同步] {error_msg}")
            res.response = json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.lab_logger().error(f"[资源同步] {error_msg}")
            self.lab_logger().error(traceback.format_exc())
            res.response = json.dumps({"success": False, "error": error_msg}, ensure_ascii=False)

        return res

    async def s2c_device_manage(self, req: SerialCommand_Request, res: SerialCommand_Response):
        """Handle add/remove device requests from HostNode via SerialCommand."""
        try:
            cmd = json.loads(req.command)
            action = cmd.get("action", "")
            data = cmd.get("data", {})
            device_id = data.get("device_id", "")

            if not device_id:
                res.response = json.dumps({"success": False, "error": "device_id required"})
                return res

            if action == "add":
                result = self.create_device(device_id, data)
            elif action == "remove":
                result = self.destroy_device(device_id)
            else:
                result = {"success": False, "error": f"Unknown action: {action}"}

            res.response = json.dumps(result, ensure_ascii=False)

        except NotImplementedError as e:
            self.lab_logger().warning(f"[DeviceManage] {e}")
            res.response = json.dumps({"success": False, "error": str(e)})
        except Exception as e:
            self.lab_logger().error(f"[DeviceManage] Error: {e}")
            res.response = json.dumps({"success": False, "error": str(e)})

        return res

    def create_device(self, device_id: str, config: "ResourceDictType") -> dict:
        """Create a sub-device dynamically. Override in HostNode / WorkstationNode."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support dynamic device creation"
        )

    def destroy_device(self, device_id: str) -> dict:
        """Destroy a sub-device dynamically. Override in HostNode / WorkstationNode."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support dynamic device removal"
        )

    async def transfer_resource_to_another(
        self,
        plr_resources: List["ResourcePLR"],
        target_device_id: str,
        target_resources: List["ResourcePLR"],
        sites: List[str],
    ):
        return await DeviceNode.transfer_resource_to_another(
            self,
            plr_resources,
            target_device_id,
            target_resources,
            sites,
        )

    # ==================================================================
    # 跨设备调用动作（便捷封装）
    #
    # 调用方只需提供 dict 入参，本类负责"选通道 + 转换"，逻辑与 host_node.send_goal 对齐：
    #   - device_id     目标设备 ID（带不带 /devices/ 前缀都行）
    #   - action_name   目标设备的函数名 / 动作名
    #   - action_kwargs 入参 dict（调用方不要自己 dump，序列化由内部完成）
    # 通道判定（自动）：目标若为该 action 暴露了**专用原生 action server**（即非
    # UniLabJsonCommand / 非 auto- 动作）→ 走原生通道、按 Goal 字段"对应发"；否则
    # 走 serial JSON 指令通道（_execute_driver_command）、把入参 json dumps 成 string。
    # 结果解析两通道统一：先 convert_from_ros_msg 转成 dict，再按 return_info / success
    # 提取真正的 return_value（与 host_node.get_result_callback 一致）。
    # 远端执行失败时会把错误转成 DeviceActionError 在本函数调用处 raise。
    # ==================================================================

    def _resolve_remote_action_type(self, clean_device_id: str, function_name: str) -> Optional[Any]:
        """从 ROS 图探测目标设备是否为 ``function_name`` 暴露了专用(原生) action server。

        - 有：返回其 ROS Action 类型（走原生通道、字段映射，``unilabos command`` 之外的"其他"）。
        - 无：返回 ``None``（说明它是 UniLabJsonCommand / auto 动作，走 ``_execute_driver_command``
          串行 JSON 通道，str dumps）。

        结果按 ``<clean_device_id>/<function_name>`` 缓存，避免每次调用都查图。
        """
        cache_key = f"{clean_device_id}/{function_name}"
        if cache_key in self._remote_action_type_cache:
            return self._remote_action_type_cache[cache_key]

        node_name = clean_device_id.split("/")[-1]
        namespace = f"/devices/{clean_device_id}"
        target_action_id = f"{namespace}/{function_name}"
        resolved: Optional[Any] = None
        try:
            for action_id, action_types in get_action_server_names_and_types_by_node(self, node_name, namespace):
                if action_id == target_action_id and action_types:
                    resolved = get_ros_type_by_msgname(action_types[0])
                    break
        except Exception as ex:  # 图查询失败不致命，回退到 serial 通道
            self.lab_logger().trace(f"[call_device_action] 探测远端动作类型失败 {target_action_id}: {ex}")
            return None

        # 探测到原生类型才缓存（None 不缓存：目标可能晚于本次调用才上线，留待下次重探）
        if resolved is not None:
            self._remote_action_type_cache[cache_key] = resolved
        return resolved

    def _build_action_call(
        self,
        device_id: str,
        action_name: str,
        action_kwargs: Optional[Dict[str, Any]],
        is_async_command: bool,
        sample_uuids: Optional[Dict[str, str]],
        action_type: Optional[Any] = None,
    ) -> Tuple[str, ActionClient, Any]:
        """构造跨设备动作调用的 ``(action_id, action_client, goal_msg)``。

        ``action_kwargs`` 必须是 **dict**（``None`` 视为 ``{}``）；序列化由本函数内部完成，
        调用方不要自己 ``json.dumps``。

        通道判定与 ``host_node.send_goal`` 对齐：

        - ``action_type`` 显式传入一个 ROS Action 类型：直接走**原生通道**（手动指定，跳过探测）。
        - ``action_type is None``：自动探测目标动作类型——
          * 探测到专用原生 action server → 原生通道 ``/devices/<id>/<action_name>``，
            ``convert_to_ros_msg`` 按 Goal 字段"对应发"。
          * 未探测到（UniLabJsonCommand / auto 动作）→ serial JSON 指令通道
            ``/devices/<id>/_execute_driver_command``，把入参包成 json_command 后 ``json.dumps``
            到 ``string`` 字段。
        """
        action_kwargs = action_kwargs or {}
        if not isinstance(action_kwargs, dict):
            raise ValueError(f"action_kwargs 必须是 dict，实际为 {type(action_kwargs).__name__}")

        clean_device_id = (
            device_id[len("/devices/"):] if device_id.startswith("/devices/") else device_id.lstrip("/")
        )
        namespace = f"/devices/{clean_device_id}"
        function_name = action_name[5:] if action_name.startswith("auto-") else action_name

        # 未显式指定类型时，从 ROS 图自动探测目标动作是否为原生 action
        if action_type is None:
            action_type = self._resolve_remote_action_type(clean_device_id, function_name)

        if action_type is not None:
            # 原生 ROS action 通道：按 Goal 字段"对应发"
            action_id = f"{namespace}/{function_name}"
            goal = convert_to_ros_msg(action_type.Goal(), action_kwargs)
            client = self._cross_device_action_clients.get(action_id)
            if client is None:
                client = ActionClient(self, action_type, action_id, callback_group=self.callback_group)
                self._cross_device_action_clients[action_id] = client
            return action_id, client, goal

        # serial JSON 指令通道（unilabos command）：入参 json dumps 到 string 字段
        suffix = "_execute_driver_command_async" if is_async_command else "_execute_driver_command"
        action_id = f"{namespace}/{suffix}"

        json_command: Dict[str, Any] = {
            "function_name": function_name,
            "function_args": action_kwargs,
            JSON_UNILABOS_PARAM: {
                PARAM_SAMPLE_UUIDS: sample_uuids or {},
            },
        }
        goal = convert_to_ros_msg(
            StrSingleInput.Goal(),
            {"string": json.dumps(json_command, ensure_ascii=False, cls=TypeEncoder)},
        )

        client = self._cross_device_action_clients.get(action_id)
        if client is None:
            client = ActionClient(self, StrSingleInput, action_id, callback_group=self.callback_group)
            self._cross_device_action_clients[action_id] = client
        return action_id, client, goal

    def _parse_action_result(self, device_id: str, action_name: str, result_msg) -> Any:
        """把动作结果消息解析成 python 值/字典（两通道统一，与 host_node.get_result_callback 一致）。

        流程：

        1. 先用 ``convert_from_ros_msg`` 把 ROS 结果消息**转成 dict**（与 callback 解析消息同一套）。
        2. 若结果带 ``return_info``（serial 指令 / UniLab @action）：解析其中 JSON 的
           ``suc / error / return_value``——失败抛 :class:`DeviceActionError`、成功返回 ``return_value``。
        3. 若无 ``return_info``（纯原生 ROS action）：按 ``success`` 字段判定，返回整份结果 dict。

        失败统一抛 :class:`DeviceActionError`，成功统一返回 dict / python 值。
        """
        result_data = convert_from_ros_msg(result_msg)
        # 极少数纯标量结果（非 dict）直接返回
        if not isinstance(result_data, dict):
            return result_data

        return_info_str = result_data.get("return_info")
        if return_info_str is not None:
            try:
                info_dict = json.loads(return_info_str) if isinstance(return_info_str, str) else return_info_str
            except (json.JSONDecodeError, TypeError) as ex:
                raise DeviceActionError(
                    device_id, action_name, f"无法解析远端 return_info: {ex}; 原始内容: {return_info_str}"
                )
            if not isinstance(info_dict, dict):
                raise DeviceActionError(device_id, action_name, f"return_info 不是对象(dict): {info_dict}")
            suc = info_dict.get("suc", result_data.get("success", True))
            return_value = info_dict.get("return_value")
            if not suc:
                raise DeviceActionError(
                    device_id,
                    action_name,
                    info_dict.get("error") or "远端返回失败 (suc=False)",
                    return_value=return_value,
                )
            return return_value

        # 无 return_info：纯原生 action，按 success 字段判定，返回整份结果 dict
        if result_data.get("success") is False:
            raise DeviceActionError(
                device_id, action_name, "远端返回 success=False", return_value=result_data
            )
        return result_data

    def call_device_action(
        self,
        device_id: str,
        action_name: str,
        action_kwargs: Optional[Dict[str, Any]] = None,
        *,
        action_type: Optional[Any] = None,
        is_async_command: bool = False,
        sample_uuids: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = 600.0,
        server_wait_timeout: float = 10.0,
        poll_interval: float = 0.05,
    ) -> Any:
        """同步调用另一台设备的动作并返回其结果（阻塞当前线程）。

        适合在同步 action（线程池中执行）里调用其它设备。
        依赖全局 MultiThreadedExecutor 在其它线程处理 ActionClient 回调，故阻塞安全。

        通道**自动判定**（与 host_node.send_goal 对齐），调用方只管传 dict 入参：

        - 目标动作有专用原生 action server（非 UniLabJsonCommand / 非 auto- 动作）→ 走**原生通道**
          ``/devices/<id>/<action_name>``，``convert_to_ros_msg`` 按 Goal 字段填充。
        - 否则 → 走 serial JSON 指令通道 ``_execute_driver_command``，入参 json dump 过去、
          结果 json dump 回来。

        可显式传 ``action_type``（某 ROS Action 类型）强制走原生通道、跳过自动探测。

        结果解析两通道统一（``_parse_action_result``）：先 ``convert_from_ros_msg`` 转成 dict，
        带 ``return_info`` 的（serial / UniLab @action）返回其中的 ``return_value``，
        纯原生 action 返回整份结果 dict。

        Args:
            device_id: 目标设备 ID（可带或不带 /devices/ 前缀）。
            action_name: 目标设备的函数名 / 动作名（auto- 前缀会自动去除）。
            action_kwargs: 入参 **dict**（``None`` 视为 ``{}``）——序列化由本函数内部完成，
                调用方不要自己先 ``json.dumps``（否则会被二次编码）。serial 通道需可 json 序列化；
                原生通道按 Goal 字段填充。
            action_type: 可选，原生 ROS Action 类型；传入则强制走原生通道并跳过自动探测。
            is_async_command: 仅 serial 通道有效，是否走 _execute_driver_command_async。
            sample_uuids: 可选的 unilabos 物料样本映射，一般留空。
            timeout: 等待结果的总超时（秒），None 表示不超时。
            server_wait_timeout: 等待目标动作服务就绪的超时（秒）。
            poll_interval: 轮询间隔（秒）。

        Returns:
            python 值 / dict：带 return_info 时为远端 ``return_value``，纯原生 action 为结果转成的 dict。

        Raises:
            DeviceActionError: 服务不可用 / 目标被拒绝 / 超时 / 远端执行失败 / 结果解析失败。
        """
        action_id, client, goal = self._build_action_call(
            device_id, action_name, action_kwargs, is_async_command, sample_uuids, action_type
        )
        if not client.wait_for_server(timeout_sec=server_wait_timeout):
            raise DeviceActionError(
                device_id, action_name, f"动作服务 {action_id} 不可用（等待 {server_wait_timeout}s 超时）"
            )

        self.lab_logger().debug(f"[call_device_action] -> {action_id}, args={str(action_kwargs)[:500]}")
        send_future = client.send_goal_async(goal)
        self._wait_future_blocking(send_future, timeout, device_id, action_name, "发送目标", poll_interval)
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            raise DeviceActionError(device_id, action_name, "目标被拒绝", rejected=True)

        result_future = goal_handle.get_result_async()
        self._wait_future_blocking(result_future, timeout, device_id, action_name, "等待结果", poll_interval)
        result_wrap = result_future.result()
        return self._parse_action_result(device_id, action_name, result_wrap.result)

    async def call_device_action_async(
        self,
        device_id: str,
        action_name: str,
        action_kwargs: Optional[Dict[str, Any]] = None,
        *,
        action_type: Optional[Any] = None,
        is_async_command: bool = False,
        sample_uuids: Optional[Dict[str, str]] = None,
        server_wait_timeout: float = 10.0,
    ) -> Any:
        """异步调用另一台设备的动作并返回其结果。

        适合在 async action（运行在 rclpy executor 上）里调用其它设备，不阻塞执行线程。
        参数语义与 :meth:`call_device_action` 一致（``action_kwargs`` 必须是 dict、通道自动判定、
        可用 ``action_type`` 强制走原生通道）。

        Raises:
            DeviceActionError: 服务不可用 / 目标被拒绝 / 远端执行失败 / 结果解析失败。
        """
        action_id, client, goal = self._build_action_call(
            device_id, action_name, action_kwargs, is_async_command, sample_uuids, action_type
        )

        waited = 0.0
        while not client.server_is_ready():
            if waited >= server_wait_timeout:
                raise DeviceActionError(
                    device_id, action_name, f"动作服务 {action_id} 不可用（等待 {server_wait_timeout}s 超时）"
                )
            await self.sleep(0.1)
            waited += 0.1

        self.lab_logger().debug(f"[call_device_action_async] -> {action_id}, args={str(action_kwargs)[:500]}")
        goal_handle = await client.send_goal_async(goal)
        if goal_handle is None or not goal_handle.accepted:
            raise DeviceActionError(device_id, action_name, "目标被拒绝", rejected=True)

        result_wrap = await goal_handle.get_result_async()
        return self._parse_action_result(device_id, action_name, result_wrap.result)

    def _wait_future_blocking(
        self, future, timeout: Optional[float], device_id: str, action_name: str, stage: str, poll_interval: float
    ) -> None:
        """阻塞轮询 rclpy Future 直到完成或超时（用于同步上下文）。"""
        start = time.time()
        while not future.done():
            if timeout is not None and (time.time() - start) > timeout:
                raise DeviceActionError(device_id, action_name, f"{stage}超时（{timeout}s）")
            time.sleep(poll_interval)

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
                    "registry_name": self.registry_name,
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
        """创建ROS发布者。已在 status_types 中声明的属性直接创建；@topic_config 用于覆盖默认参数。"""
        topic_cfg = {}
        driver_class = type(self.driver_instance)

        # 区分 @property 和普通方法两种情况
        is_prop = hasattr(driver_class, attr_name) and isinstance(
            getattr(driver_class, attr_name), property
        )

        if is_prop:
            class_attr = getattr(driver_class, attr_name)
            if class_attr.fget is not None:
                topic_cfg = get_topic_config(class_attr.fget)
        else:
            if hasattr(self.driver_instance, attr_name):
                method = getattr(self.driver_instance, attr_name)
                if callable(method):
                    topic_cfg = get_topic_config(method)

        # 发布名称优先级: @topic_config(name=...) > get_ 前缀去除 > attr_name
        cfg_name = topic_cfg.get("name")
        if cfg_name:
            publish_name = cfg_name
        elif attr_name.startswith("get_"):
            publish_name = attr_name[4:]
        else:
            publish_name = attr_name

        # @topic_config 参数覆盖默认值
        cfg_period = topic_cfg.get("period")
        cfg_print = topic_cfg.get("print_publish")
        cfg_qos = topic_cfg.get("qos")
        period: float = cfg_period if cfg_period is not None else initial_period
        print_publish: bool = cfg_print if cfg_print is not None else self._print_publish
        qos: int = cfg_qos if cfg_qos is not None else 10

        # 获取属性值的方法
        def get_device_attr():
            try:
                if is_prop:
                    return getattr(self.driver_instance, attr_name)
                else:
                    return getattr(self.driver_instance, attr_name)()
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

        self._property_publishers[publish_name] = PropertyPublisher(
            self, publish_name, get_device_attr, msg_type, period, print_publish, qos
        )

    def create_ros_action_server(self, action_name, action_value_mapping):
        """创建ROS动作服务器"""
        action_type = action_value_mapping["type"]
        str_action_type = str(action_type)[8:-2]

        try:
            self._action_servers[action_name] = ActionServer(
                self,
                action_type,
                action_name,
                execute_callback=self._create_execute_callback(action_name, action_value_mapping),
                callback_group=self.callback_group,
            )
        except Exception as e:
            self.lab_logger().error(f"创建ActionServer失败，Device: {self.device_id}, Action Name: {action_name}, Action Type: {action_type}, Error: {e}")
            return
        self.lab_logger().trace(f"发布动作: {action_name}, 类型: {str_action_type}")

    # 跨设备订阅时发布者可能尚未就绪，按该周期(秒)循环延迟重试解析消息类型，直到订上（不设上限）
    _SUBSCRIBE_RETRY_PERIOD = 10.0

    def _setup_decorated_subscribers(self):
        """扫描 driver_instance 中带有 @subscribe 装饰器的方法并创建订阅者"""
        for method_name, method, config in get_all_subscriptions(self.driver_instance):
            self._setup_one_subscriber(method_name, method, config)

    def _setup_one_subscriber(self, method_name: str, method, config: dict):
        """解析 @subscribe 目标并建立订阅；msg_type 未知时起定时器循环重试，建立成功即停。

        重试周期取 ``retry_interval``（未设置时用 ``_SUBSCRIBE_RETRY_PERIOD``，默认 10s），
        不设上限、一直重试直到订上。订上之后不再判活/轮询——断线重连交给 DDS，真出问题等报错暴露。
        """
        try:
            topic = self._resolve_subscription_target(config)
        except Exception as ex:
            self.lab_logger().warning(f"订阅方法 {method_name} 解析 topic 失败: {ex}，跳过")
            return

        # 先同步试一次，成功直接结束（无需定时器）
        if self._ensure_subscription(method, config, topic):
            return
        if method_name in self._subscriber_monitors:
            return

        retry_interval = config.get("retry_interval")
        period = float(retry_interval) if retry_interval is not None else self._SUBSCRIBE_RETRY_PERIOD

        def _retry(_mn=method_name, _m=method, _cfg=config, _t=topic):
            if self._ensure_subscription(_m, _cfg, _t):
                self._cancel_subscriber_monitor(_mn)

        self._subscriber_monitors[method_name] = self.create_timer(period, _retry)

    def _cancel_subscriber_monitor(self, method_name: str):
        """取消并移除某订阅的重试定时器（建立成功或放弃时调用）。"""
        timer = self._subscriber_monitors.pop(method_name, None)
        if timer is not None:
            timer.cancel()

    def _ensure_subscription(self, method, config, topic) -> bool:
        """解析 msg_type 并在尚未建立时创建订阅；返回订阅是否已建立。

        - 已建立 / 解析到类型并建好：返回 True。
        - 当前查不到类型（发布者尚未就绪/已下线）：返回 False，等待下次重试。
        """
        if topic in self._topic_subscriber_types:
            return True

        msg_type = self._resolve_subscription_msg_type(config, topic, method)
        if msg_type is None:
            self.lab_logger().trace(f"订阅 {topic} 暂未识别消息类型，等待下个周期重试")
            return False

        sub = self.create_ros_subscriber(
            topic,
            msg_type,
            method,
            config.get("qos", 10),
            trigger_when_change=config.get("trigger_when_change", False),
        )
        if sub is None:
            return False
        self._topic_subscriber_types[topic] = msg_type
        return True

    def _namespace_prefix(self) -> str:
        """当前设备命名空间的前缀（如 /devices/pump_1 -> /devices）。"""
        prefix = self.namespace.rsplit("/", 1)[0]
        return prefix if prefix else ""

    def _resolve_subscription_target(self, config: dict) -> str:
        """解析跨设备订阅的完整 topic 路径。

        仅支持两种写法（均为跨设备；订阅本设备自己的状态没有意义，直接用 getter 即可）：

        - 绝对路径：``@subscribe("/devices/other/status")``
        - 拆分写法：``@subscribe(device_id="other", status_name="status")``
        """
        topic = config.get("topic")
        dev = config.get("device_id")
        status_name = config.get("status_name")

        # 拆分写法：device_id + status_name（都必填）
        if dev or status_name:
            if not dev or not status_name:
                raise ValueError("拆分写法需要同时提供 device_id 和 status_name")
            return f"{self._namespace_prefix()}/{dev}/{status_name}"

        # 绝对路径
        if not topic:
            raise ValueError("缺少 topic（绝对路径）或 device_id + status_name")
        if not topic.startswith("/"):
            raise ValueError(f"topic 必须是绝对路径（以 / 开头），收到: {topic!r}")
        return topic

    def _resolve_subscription_msg_type(self, config: dict, topic: str, method):
        """按优先级自动识别订阅消息类型：显式 > ROS 图 > 回调注解。"""
        # 1) 显式指定
        msg_type = config.get("msg_type")
        if msg_type is not None:
            return msg_type

        # 2) ROS 图：读取该 topic 上已有发布者声明的类型
        try:
            for info in self.get_publishers_info_by_topic(topic):
                type_str = getattr(info, "topic_type", "") or ""
                if not type_str:
                    continue
                parts = type_str.split("/")
                if len(parts) == 2:  # 兼容 'std_msgs/Int32' 旧写法
                    type_str = f"{parts[0]}/msg/{parts[1]}"
                return get_ros_type_by_msgname(type_str)
        except Exception:
            pass

        # 3) 回调首个参数的类型注解（排除 self / return）
        try:
            hints = get_type_hints(method)
            for key, hint in hints.items():
                if key != "return":
                    return hint
        except Exception:
            pass

        return None

    def create_ros_subscriber(
        self,
        topic: str,
        msg_type,
        callback,
        qos: int = 10,
        trigger_when_change: bool = False,
    ):
        """
        创建ROS订阅者

        Args:
            topic: Topic 名称
            msg_type: ROS 消息类型
            callback: 回调方法（会自动绑定到 driver_instance）
            qos: QoS 深度配置
            trigger_when_change: 为 True 时，仅当值相对上一次发生变化才触发回调

        回调收到的值统一经 ``convert_from_ros_msg`` 转换：std_msgs 这类基础消息直接得到原生值
        （如 ``Int32 -> int``），复合消息得到递归转换后的 dict，与全局其它通道（topic 发布、
        call_device_action 结果解析）保持一致。
        """
        try:
            # 用闭包保存上一次的值：既用于 trigger_when_change 边沿触发，也用于"仅变更时打印日志"
            last_holder = {"has": False, "val": None}

            def actual_cb(msg, _cb=callback):
                value = convert_from_ros_msg(msg)
                changed = (not last_holder["has"]) or (last_holder["val"] != value)
                prev = last_holder["val"]
                last_holder["has"] = True
                last_holder["val"] = value
                # 日志只在值发生变更时打印（与 trigger_when_change 无关），避免重复值刷屏、减少日志体积
                if changed:
                    self.lab_logger().trace(f"订阅触发 {topic}: 值变更 {prev} -> {value}")
                # trigger_when_change=True 时，未变更则不触发用户回调（回调触发与日志解耦）
                if trigger_when_change and not changed:
                    return
                return _cb(value)

            subscription = self.create_subscription(
                msg_type,
                topic,
                actual_cb,
                qos,
                callback_group=self.callback_group,
            )
            self._topic_subscribers[topic] = subscription
            str_msg_type = str(msg_type)[8:-2] if str(msg_type).startswith("<class") else str(msg_type)
            self.lab_logger().trace(
                f"订阅Topic: {topic}, 类型: {str_msg_type}, QoS: {qos}, "
                f"trigger_when_change: {trigger_when_change}"
            )
            return subscription
        except Exception as ex:
            self.lab_logger().error(f"创建订阅者 {topic} 失败，类型: {msg_type}，错误: {ex}\n{traceback.format_exc()}")
            return None

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

    def _resolve_report_action_name(
        self,
        action_name: str,
        action_kwargs: Dict[str, Any],
    ) -> str:
        """解析 JSON command 实际调用的业务动作名，供 Host 匹配注册表。"""

        report_action_name = action_name
        if action_name in {"_execute_driver_command", "_execute_driver_command_async"}:
            try:
                command = json.loads(action_kwargs.get("string", ""))
                report_action_name = str(command["function_name"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return report_action_name

    def _create_execute_callback(self, action_name, action_value_mapping):
        """创建动作执行回调函数"""

        async def execute_callback(goal_handle: ServerGoalHandle):
            # 初始化结果信息变量
            execution_error = ""
            execution_success = False
            action_return_value = None
            execution_suc_type = SUCCESS_TYPE_NORMAL
            execution_error_info = None

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
            self.lab_logger().debug(f"任务 {ACTION.__name__} 接收到原始目标: {str(action_kwargs)[:1000]}")
            self.lab_logger().trace(f"任务 {ACTION.__name__} 接收到原始目标: {action_kwargs}")
            report_action_name = self._resolve_report_action_name(
                action_name,
                action_kwargs,
            )

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
                            queried_resources: list = [None] * len(resource_inputs)
                            uuid_indices: list[tuple[int, str, dict]] = []  # (index, uuid, resource_data)

                            # 第一遍：处理没有uuid的资源，收集有uuid的资源信息
                            for idx, resource_data in enumerate(resource_inputs):
                                unilabos_uuid = resource_data.get("data", {}).get("unilabos_uuid")
                                if unilabos_uuid is None:
                                    plr_resource = await self.get_resource_with_dir(
                                        resource_id=resource_data["id"], with_children=True
                                    )
                                    if "sample_id" in resource_data:
                                        plr_resource.unilabos_extra[EXTRA_SAMPLE_UUID] = resource_data["sample_id"]
                                    queried_resources[idx] = plr_resource
                                else:
                                    uuid_indices.append((idx, unilabos_uuid, resource_data))

                            # 第二遍：批量查询有uuid的资源
                            if uuid_indices:
                                uuids = [item[1] for item in uuid_indices]
                                resource_tree = await self.get_resource(uuids)
                                plr_resources = resource_tree.to_plr_resources()
                                for i, (idx, _, resource_data) in enumerate(uuid_indices):
                                    plr_resource = plr_resources[i]
                                    if "sample_id" in resource_data:
                                        plr_resource.unilabos_extra[EXTRA_SAMPLE_UUID] = resource_data["sample_id"]
                                    queried_resources[idx] = plr_resource

                            self.lab_logger().debug(f"资源查询结果: 共 {len(queried_resources)} 个资源")

                            # 通过资源跟踪器获取本地实例
                            final_resources = queried_resources if is_sequence else queried_resources[0]
                            if not is_sequence:
                                plr = self.resource_tracker.figure_resource(
                                    {"name": final_resources.name}, try_mode=False
                                )
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
                                    f"异步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{str(action_kwargs)[:1000]}"
                                )
                                trace(
                                    f"异步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{action_kwargs}"
                                )

                        future = self.run_async_func(ACTION, trace_error=False, **action_kwargs)
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
                                f"同步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{str(action_kwargs)[:1000]}"
                            )
                            trace(
                                f"同步任务 {ACTION.__name__} 报错了\n{traceback.format_exc()}\n原始输入：{action_kwargs}"
                            )

                    future.add_done_callback(_handle_future_exception)

            action_type = action_value_mapping["type"]
            feedback_msg_types = action_type.Feedback.get_fields_and_field_types()
            result_msg_types = action_type.Result.get_fields_and_field_types()

            # 低频 feedback timer（10s），不阻塞完成检测
            _feedback_timer = None

            def _publish_feedback():
                if future is not None and not future.done():
                    self._time_spent = time.time() - time_start
                    self._time_remaining = time_overall - self._time_spent
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

            if action_value_mapping.get("feedback"):
                _fb_interval = action_value_mapping.get("feedback_interval", 0.5)
                _feedback_timer = self.create_timer(
                    _fb_interval, _publish_feedback, callback_group=self.callback_group
                )

            # 等待 action 完成
            if future is not None:
                if isinstance(future, Task):
                    # rclpy Task：直接 await，完成瞬间唤醒
                    try:
                        _raw_result = await future
                    except Exception as e:
                        _raw_result = e
                else:
                    # concurrent.futures.Future（同步 action）：用 rclpy 兼容的轮询
                    _poll_future = Future()

                    def _on_sync_done(fut):
                        async def _wake():
                            if not _poll_future.done():
                                _poll_future.set_result(None)

                        # ThreadPoolExecutor callbacks run outside the rclpy executor.
                        # Wake the awaiting action coroutine from the executor thread;
                        # otherwise it may only resume when the executor naturally wakes up.
                        rclpy.get_global_executor().create_task(_wake())

                    future.add_done_callback(_on_sync_done)
                    await _poll_future
                    try:
                        _raw_result = future.result()
                    except Exception as e:
                        _raw_result = e

                # 确保 execution_error/success 被正确设置（不依赖 done callback 时序）
                if isinstance(_raw_result, BaseException):
                    if not execution_error:
                        execution_error = traceback.format_exception(
                            type(_raw_result), _raw_result, _raw_result.__traceback__
                        )
                        execution_error = "".join(execution_error)
                    execution_success = False
                    action_return_value = _raw_result
                elif not execution_error:
                    action_return_value = _raw_result
                    execution_success = not _native_driver_result_failed(
                        action_name, action_type, _raw_result
                    )
                    if not execution_success:
                        execution_error = (
                            "driver returned an unsuccessful native action result: "
                            f"{_raw_result!r}"
                        )

                if isinstance(_raw_result, BaseException):
                    execution_error_info = {
                        "action_name": report_action_name,
                        "exception_type": type(_raw_result).__name__,
                        "exception_mro": [
                            error_class.__name__
                            for error_class in type(_raw_result).__mro__
                        ],
                        "error_message": str(_raw_result),
                        "traceback": execution_error,
                    }
                    category = getattr(_raw_result, "category", None)
                    severity = getattr(_raw_result, "severity", None)
                    if category is not None:
                        execution_error_info["category"] = str(
                            getattr(category, "value", category)
                        )
                    if severity is not None:
                        execution_error_info["severity"] = str(
                            getattr(severity, "value", severity)
                        )
                elif not execution_success:
                    execution_error_info = _coerce_device_error_info(
                        report_action_name,
                        _raw_result,
                        execution_error,
                    )

            # 清理 feedback timer
            if _feedback_timer is not None:
                _feedback_timer.cancel()

            if future is not None and future.cancelled():
                self.lab_logger().info(f"动作 {action_name} 已取消")
                return action_type.Result()

            # self.lab_logger().info(f"动作执行完成: {action_name}")
            del future

            # 执行失败时跳过物料状态更新
            if execution_error:
                execution_success = False

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
                    setattr(result_msg, attr_name, execution_success)
                elif attr_name == "return_info":
                    setattr(
                        result_msg,
                        attr_name,
                        json.dumps(
                            serialize_result_info(
                                execution_error,
                                execution_success,
                                action_return_value,
                                suc_type=execution_suc_type,
                                error_info=execution_error_info,
                            ),
                            ensure_ascii=False,
                        ),
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
            # 获取 unilabos 系统参数
            unilabos_param: Dict[str, Any] = target[JSON_UNILABOS_PARAM]

            assert isinstance(function_args, dict), "执行动作时JSON必须为dict类型\n原JSON: {string}"
            function = getattr(self.driver_instance, function_name)
            assert callable(
                function
            ), f"执行动作时JSON中的function_name对应的函数不可调用: {function_name}\n原JSON: {string}"

            # 处理参数（包含 unilabos 系统参数如 sample_uuids）
            args_list = default_manager._analyze_method_signature(function, skip_unilabos_params=False)["args"]
            for arg in args_list:
                arg_name = arg["name"]
                arg_type = arg["type"]

                # 跳过不在 function_args 中的参数
                if arg_name not in function_args:
                    # 处理 sample_uuids 参数注入
                    if arg_name == PARAM_SAMPLE_UUIDS:
                        raw_sample_uuids = unilabos_param.get(PARAM_SAMPLE_UUIDS, {})
                        # 将 material uuid 转换为 resource 实例
                        # key: sample_uuid, value: material_uuid -> resource 实例
                        resolved_sample_uuids: Dict[str, Any] = {}
                        for sample_uuid, material_uuid in raw_sample_uuids.items():
                            if material_uuid and self.resource_tracker:
                                resource = self.resource_tracker.uuid_to_resources.get(material_uuid)
                                resolved_sample_uuids[sample_uuid] = resource if resource else material_uuid
                            else:
                                resolved_sample_uuids[sample_uuid] = material_uuid
                        function_args[PARAM_SAMPLE_UUIDS] = resolved_sample_uuids
                        self.lab_logger().debug(f"[JsonCommand] 注入 {PARAM_SAMPLE_UUIDS}: {resolved_sample_uuids}")
                    continue

                # 处理单个 ResourceSlot（单物料两种入参形态：
                #   dict = 资源引用，按 uuid 重新 with_children 拉取；
                #   list = 一棵树的扁平节点组（上游 handle 的 @flatten），就地装配成一个物料）
                if arg_type == "unilabos.registry.placeholder_type:ResourceSlot":
                    # 内部解析层：raw 值可能是 list（@flatten 节点组）或 dict（资源引用）
                    resource_data: ResourceSlotRawInput = function_args[arg_name]
                    try:
                        if isinstance(resource_data, list):
                            function_args[arg_name] = self._assemble_single_resource(resource_data)
                        elif isinstance(resource_data, dict) and "id" in resource_data:
                            function_args[arg_name] = self._convert_resources_sync(resource_data["uuid"])[0]
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
                                uuids = [r["uuid"] for r in resource_list if isinstance(r, dict) and "id" in r]
                                function_args[arg_name] = self._convert_resources_sync(*uuids) if uuids else []
                            except Exception as e:
                                self.lab_logger().error(
                                    f"转换ResourceSlot列表参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                                )
                                raise JsonCommandInitError(f"ResourceSlot列表参数转换失败: {arg_name}")

            # todo: 默认反报送
            return function(**function_args)
        except KeyError as ex:
            raise JsonCommandInitError(
                f"执行动作时JSON缺少function_name或function_args: {ex}\n原JSON: {string}\n{traceback.format_exc()}"
            )

    def _convert_resources_sync(self, *uuids: str) -> List["ResourcePLR"]:
        """同步转换资源 UUID 为实例

        Args:
            *uuids: 一个或多个资源 UUID

        Returns:
            单个 UUID 时返回单个资源实例，多个 UUID 时返回资源实例列表
        """
        if not uuids:
            raise ValueError("至少需要提供一个 UUID")

        uuids_list = list(uuids)
        future: Future = self._resource_clients["c2s_update_resource_tree"].call_async(
            SerialCommand.Request(
                command=json.dumps(
                    {
                        "data": {"data": uuids_list, "with_children": True},
                        "action": "get",
                    }
                )
            )
        )

        # 等待结果（使用while循环，每次sleep 0.05秒，最多等待30秒）
        timeout = 30.0
        elapsed = 0.0
        while not future.done() and elapsed < timeout:
            time.sleep(0.02)
            elapsed += 0.02

        if not future.done():
            raise Exception(f"资源查询超时: {uuids_list}")

        response = future.result()
        if response is None:
            raise Exception(f"资源查询返回空结果: {uuids_list}")

        raw_data = json.loads(response.response)
        if not raw_data:
            raise Exception(f"资源原始查询返回空结果: {raw_data}")

        # 转换为 PLR 资源
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
        if not len(tree_set.trees):
            raise Exception(f"资源查询返回空树: {raw_data}")
        plr_resources = tree_set.to_plr_resources()

        # 通过资源跟踪器获取本地实例
        figured_resources: List[ResourcePLR] = []
        for plr_resource, tree in zip(plr_resources, tree_set.trees):
            res = self.resource_tracker.figure_resource(plr_resource, try_mode=True)
            if len(res) == 0:
                self.lab_logger().warning(f"资源转换未能索引到实例: {tree.root_node.res_content}，返回新建实例")
                figured_resources.append(plr_resource)
            elif len(res) == 1:
                figured_resources.append(res[0])
            else:
                raise ValueError(f"资源转换得到多个实例: {res}")

        mapped_plr_resources = []
        for uuid in uuids_list:
            found = None
            for plr_resource in figured_resources:
                r = self.resource_tracker.loop_find_with_uuid(plr_resource, uuid)
                if r is not None:
                    found = r
                    break
            if found is None:
                raise Exception(f"未能在已解析的资源树中找到 uuid={uuid} 对应的资源")
            mapped_plr_resources.append(found)

        return mapped_plr_resources

    def _assemble_single_resource(self, raw_nodes: List[Dict[str, Any]]) -> "ResourcePLR":
        """把一组扁平节点 dict 装配成「单个物料」（单 ResourceSlot 的 list 输入形态）。

        list 输入形态：通常来自上游 handle 的 `xxx.@flatten`（一棵树的扁平节点列表，root + children），
        必须**恰好一个根** → 装配成一个物料；多根视为非法（一组必须变成一个物料）。
        与 dict 形态（按 uuid 重新 with_children 拉取）相对：此处直接就地装配，不回服务端拉取。
        """
        if not raw_nodes:
            raise ValueError("单物料 list 输入为空")
        tree_set = ResourceTreeSet.from_raw_dict_list(raw_nodes)
        if len(tree_set.trees) != 1:
            names = [t.root_node.res_content.name for t in tree_set.trees]
            raise ValueError(f"单物料输入要求恰好一个根物料，实际得到 {len(tree_set.trees)} 个根：{names}")
        plr = tree_set.to_plr_resources()[0]
        res = self.resource_tracker.figure_resource(plr, try_mode=True)
        if len(res) == 1:
            return res[0]
        if len(res) > 1:
            raise ValueError(f"单物料输入索引到多个本地实例：{res}")
        self.lab_logger().warning(f"单物料 list 输入未索引到本地实例，使用装配实例：{getattr(plr, 'name', plr)}")
        return plr

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
            # 获取 unilabos 系统参数
            unilabos_param: Dict[str, Any] = target.get(JSON_UNILABOS_PARAM, {})

            assert isinstance(function_args, dict), "执行动作时JSON必须为dict类型\n原JSON: {string}"
            function = getattr(self.driver_instance, function_name)
            assert callable(
                function
            ), f"执行动作时JSON中的function_name对应的函数不可调用: {function_name}\n原JSON: {string}"
            assert asyncio.iscoroutinefunction(
                function
            ), f"执行动作时JSON中的function并非异步: {function_name}\n原JSON: {string}"

            # 处理参数（包含 unilabos 系统参数如 sample_uuids）
            args_list = default_manager._analyze_method_signature(function, skip_unilabos_params=False)["args"]
            for arg in args_list:
                arg_name = arg["name"]
                arg_type = arg["type"]

                # 跳过不在 function_args 中的参数
                if arg_name not in function_args:
                    # 处理 sample_uuids 参数注入
                    if arg_name == PARAM_SAMPLE_UUIDS:
                        raw_sample_uuids = unilabos_param.get(PARAM_SAMPLE_UUIDS, {})
                        # 将 material uuid 转换为 resource 实例
                        # key: sample_uuid, value: material_uuid -> resource 实例
                        resolved_sample_uuids: Dict[str, Any] = {}
                        for sample_uuid, material_uuid in raw_sample_uuids.items():
                            if material_uuid and self.resource_tracker:
                                resource = self.resource_tracker.uuid_to_resources.get(material_uuid)
                                resolved_sample_uuids[sample_uuid] = resource if resource else material_uuid
                            else:
                                resolved_sample_uuids[sample_uuid] = material_uuid
                        function_args[PARAM_SAMPLE_UUIDS] = resolved_sample_uuids
                        self.lab_logger().debug(
                            f"[JsonCommandAsync] 注入 {PARAM_SAMPLE_UUIDS}: {resolved_sample_uuids}"
                        )
                    continue

                # 处理单个 ResourceSlot（单物料两种入参形态：
                #   dict = 资源引用，按 uuid 重新 with_children 拉取；
                #   list = 一棵树的扁平节点组（上游 handle 的 @flatten），就地装配成一个物料）
                _is_resource_slot = isinstance(arg_type, str) and arg_type.endswith(":ResourceSlot")
                if _is_resource_slot:
                    # 内部解析层：raw 值可能是 list（@flatten 节点组）或 dict（资源引用）
                    resource_data: ResourceSlotRawInput = function_args[arg_name]
                    try:
                        if isinstance(resource_data, list):
                            function_args[arg_name] = self._assemble_single_resource(resource_data)
                        elif isinstance(resource_data, dict) and "id" in resource_data:
                            function_args[arg_name] = await self._convert_resource_async(resource_data)
                    except Exception as e:
                        self.lab_logger().error(
                            f"转换ResourceSlot参数 {arg_name} 失败: {e}\n{traceback.format_exc()}"
                        )
                        raise JsonCommandInitError(f"ResourceSlot参数转换失败: {arg_name}")

                # 处理 ResourceSlot 列表
                elif isinstance(arg_type, tuple) and len(arg_type) == 2:
                    if arg_type[0] == "list" and isinstance(arg_type[1], str) and arg_type[1].endswith(":ResourceSlot"):
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

    async def _convert_resource_async(self, resource_data: "ResourceDictType"):
        """异步转换 ResourceDictType 为 PLR 实例，优先用 uuid 查询"""
        unilabos_uuid = resource_data.get("uuid")

        if unilabos_uuid:
            resource_tree = await self.get_resource([unilabos_uuid], with_children=True)
            plr_resources = resource_tree.to_plr_resources()
            if plr_resources:
                plr_resource = plr_resources[0]
            else:
                raise ValueError(f"通过 uuid={unilabos_uuid} 查询资源为空")
        else:
            res_id = resource_data.get("id") or resource_data.get("name", "")
            if not res_id:
                raise ValueError(f"资源数据缺少 uuid 和 id: {list(resource_data.keys())}")
            plr_resource = await self.get_resource_with_dir(resource_id=res_id, with_children=True)

        # 通过资源跟踪器获取本地实例
        res = self.resource_tracker.figure_resource(plr_resource, try_mode=True)
        if len(res) == 0:
            self.lab_logger().warning(f"资源转换未能索引到实例: {resource_data.get('id', '?')}，返回新建实例")
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

    # 类变量，用于循环管理
    _asyncio_loop = None
    _asyncio_loop_running = False
    _asyncio_loop_thread = None

    @classmethod
    def get_asyncio_loop(cls):
        return cls._asyncio_loop

    @classmethod
    def run_async_func(cls, func, trace_error=True, inner_trace_callback=None, **kwargs) -> Task:
        """兼容旧调用；新驱动应使用当前 DeviceNode 实例的同名方法。"""

        return schedule_async_func(
            rclpy.get_global_executor().create_task,
            func,
            trace_error=trace_error,
            inner_trace_callback=inner_trace_callback,
            error_callback=error,
            **kwargs,
        )

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
        # 在初始化时检查循环状态
        if ROS2DeviceNode._asyncio_loop_running and ROS2DeviceNode._asyncio_loop_thread is not None:
            pass
        elif ROS2DeviceNode._asyncio_loop_thread is None:
            self._start_loop()

        # 保存设备类是否支持异步上下文
        self._has_async_context = hasattr(driver_class, "__aenter__") and hasattr(driver_class, "__aexit__")
        self._driver_class = driver_class
        self.device_config = device_config
        children: List[ResourceDictInstance] = device_config.children
        self.driver_is_ros = driver_is_ros
        self.driver_is_workstation = False
        self.resource_tracker = DeviceNodeResourceTracker()

        creator_selection = select_driver_creator(
            driver_class,
            children=children,
            resource_tracker=self.resource_tracker,
            task_scheduler=rclpy.get_global_executor().create_task,
        )
        self.driver_is_workstation = creator_selection.is_workstation
        self._driver_creator = creator_selection.creator

        if driver_is_ros:
            driver_params["device_id"] = device_id
            driver_params["registry_name"] = device_config.res_content.klass
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
                registry_name=device_config.res_content.klass,
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
                registry_name=device_config.res_content.klass,
                device_uuid=device_uuid,
                status_types=status_types,
                action_value_mappings=action_value_mappings,
                hardware_interface=hardware_interface,
                print_publish=print_publish,
                resource_tracker=self.resource_tracker,
            )
        self._ros_node: BaseROS2DeviceNode
        # 将注册表类型名传递给BaseROS2DeviceNode,用于slave上报
        self._ros_node.lab_logger().info(f"初始化完成 {self._ros_node.uuid} {self.driver_is_ros}")
        self.driver_instance._ros_node = self._ros_node  # type: ignore
        self.driver_instance._execute_driver_command = self._ros_node._execute_driver_command  # type: ignore
        self.driver_instance._execute_driver_command_async = self._ros_node._execute_driver_command_async  # type: ignore
        if hasattr(self.driver_instance, "post_init"):
            try:
                self.driver_instance.post_init(self._ros_node)  # type: ignore
            except Exception as e:
                self._ros_node.lab_logger().error(f"设备后初始化失败: {e}")

    def _start_loop(self):
        def run_event_loop():
            loop = asyncio.new_event_loop()
            ROS2DeviceNode._asyncio_loop = loop
            asyncio.set_event_loop(loop)
            loop.run_forever()

        ROS2DeviceNode._asyncio_loop_thread = threading.Thread(
            target=run_event_loop, daemon=True, name="ROS2DeviceNode"
        )
        ROS2DeviceNode._asyncio_loop_thread.start()
        logger.info(f"循环线程已启动")


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
