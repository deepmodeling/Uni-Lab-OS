import json
import time
import traceback
from pprint import pformat
from typing import List, Dict, Any, Optional, TYPE_CHECKING

import rclpy
from rosidl_runtime_py import message_to_ordereddict

from unilabos.messages import *  # type: ignore  # protocol names
from rclpy.action import ActionServer, ActionClient
from rclpy.action.server import ServerGoalHandle
from unilabos_msgs.srv._serial_command import SerialCommand_Request, SerialCommand_Response

from unilabos.compile import action_protocol_generators
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.ros.msgs.message_converter import (
    get_action_type,
    convert_to_ros_msg,
    convert_from_ros_msg_with_mapping,
)
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, DeviceNodeResourceTracker, ROS2DeviceNode
from unilabos.resources.resource_tracker import ResourceDictType, ResourceTreeSet, ResourceDictInstance
from unilabos.utils.type_check import get_result_info_str

if TYPE_CHECKING:
    from unilabos.devices.workstation.workstation_base import WorkstationBase


class ROS2WorkstationNodeTempError(Exception):
    pass


class ROS2WorkstationNode(BaseROS2DeviceNode):
    """
    ROS2WorkstationNode代表管理ROS2环境中设备通信和动作的协议节点。
    它初始化设备节点，处理动作客户端，并基于指定的协议执行工作流。
    它还物理上代表一组协同工作的设备，如带夹持器的机械臂，带传送带的CNC机器等。
    """

    driver_instance: "WorkstationBase"

    def __init__(
        self,
        protocol_type: List[str],
        children: List[ResourceDictInstance],
        *,
        driver_instance: "WorkstationBase",
        device_id: str,
        registry_name: str,
        device_uuid: str,
        status_types: Dict[str, Any],
        action_value_mappings: Dict[str, Any],
        hardware_interface: Dict[str, Any],
        print_publish=True,
        resource_tracker: Optional["DeviceNodeResourceTracker"] = None,
    ):
        self._setup_protocol_names(protocol_type)

        # 初始化非BaseROSNode的属性
        self.children = children
        # 初始化基类，让基类处理常规动作
        super().__init__(
            driver_instance=driver_instance,
            device_id=device_id,
            registry_name=registry_name,
            device_uuid=device_uuid,
            status_types=status_types,
            action_value_mappings={**action_value_mappings, **self.protocol_action_mappings},
            hardware_interface=hardware_interface,
            print_publish=print_publish,
            resource_tracker=resource_tracker,
        )

        self._busy = False
        self.sub_devices = {}
        self._action_clients = {}

        # 初始化子设备
        self.communication_node_id_to_instance = {}

        for device_config in self.children:
            device_id = device_config.res_content.id
            if device_config.res_content.type != "device":
                self.lab_logger().debug(
                    f"[Protocol Node] Skipping type {device_config.res_content.type} {device_id} already existed, skipping."
                )
                continue
            try:
                d = self.initialize_device(device_id, device_config)
            except Exception as ex:
                self.lab_logger().error(
                    f"[Protocol Node] Failed to initialize device {device_id}: {ex}\n{traceback.format_exc()}"
                )
                d = None
            if d is None:
                continue

            if "serial_" in device_id or "io_" in device_id:
                self.communication_node_id_to_instance[device_id] = d
                continue

        for device_config in self.children:
            device_id = device_config.res_content.id
            if device_config.res_content.type != "device":
                continue
            # 设置硬件接口代理
            if device_id not in self.sub_devices:
                self.lab_logger().error(f"[Protocol Node] {device_id} 还没有正确初始化，跳过...")
                continue
            d = self.sub_devices[device_id]
            if d:
                hardware_interface = d.ros_node_instance._hardware_interface
                if (
                    hasattr(d.driver_instance, hardware_interface["name"])
                    and hasattr(d.driver_instance, hardware_interface["write"])
                    and (hardware_interface["read"] is None or hasattr(d.driver_instance, hardware_interface["read"]))
                ):

                    name = getattr(d.driver_instance, hardware_interface["name"])
                    read = hardware_interface.get("read", None)
                    write = hardware_interface.get("write", None)

                    # 如果硬件接口是字符串，通过通信设备提供
                    if isinstance(name, str) and name in self.sub_devices:
                        communicate_device = self.sub_devices[name]
                        communicate_hardware_info = communicate_device.ros_node_instance._hardware_interface
                        self._setup_hardware_proxy(d, self.sub_devices[name], read, write)
                        self.lab_logger().info(
                            f"\n通信代理：为子设备{device_id}\n    "
                            f"添加了{read}方法(来源：{name} {communicate_hardware_info['write']}) \n    "
                            f"添加了{write}方法(来源：{name} {communicate_hardware_info['read']})"
                        )

        self.lab_logger().info(f"ROS2WorkstationNode {device_id} initialized with protocols: {self.protocol_names}")

    def _setup_protocol_names(self, protocol_type):
        # 处理协议类型
        if isinstance(protocol_type, str):
            if "," not in protocol_type:
                self.protocol_names = [protocol_type]
            else:
                self.protocol_names = [protocol.strip() for protocol in protocol_type.split(",")]
        else:
            self.protocol_names = protocol_type
        # 准备协议相关的动作值映射
        self.protocol_action_mappings = {}
        for protocol_name in self.protocol_names:
            protocol_type = globals()[protocol_name]
            self.protocol_action_mappings[protocol_name] = get_action_type(protocol_type)

    def initialize_device(self, device_id, device_config):
        """初始化设备并创建相应的动作客户端"""
        # device_id_abs = f"{self.device_id}/{device_id}"
        device_id_abs = f"{device_id}"
        self.lab_logger().info(f"初始化子设备: {device_id_abs}")
        d = self.sub_devices[device_id] = initialize_device_from_dict(device_id_abs, device_config)

        # 为子设备的每个动作创建动作客户端
        if d is not None and hasattr(d, "ros_node_instance"):
            node = d.ros_node_instance
            node.resource_tracker = self.resource_tracker  # 站内应当共享资源跟踪器
            for action_name, action_mapping in node._action_value_mappings.items():
                if action_name.startswith("auto-") or str(action_mapping.get("type", "")).startswith(
                    "UniLabJsonCommand"
                ):
                    continue
                action_id = f"/devices/{device_id_abs}/{action_name}"
                if action_id not in self._action_clients:
                    try:
                        self._action_clients[action_id] = ActionClient(
                            self, action_mapping["type"], action_id, callback_group=self.callback_group
                        )
                    except Exception as ex:
                        self.lab_logger().error(f"创建动作客户端失败: {action_id}, 错误: {ex}")
                        continue
                    self.lab_logger().trace(f"为子设备 {device_id} 创建动作客户端: {action_name}")
        return d

    def create_device(self, device_id: str, config: ResourceDictType) -> dict:
        """Dynamically add a sub-device to this workstation."""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id in self.sub_devices:
            return {"success": False, "error": f"Sub-device {device_id} already exists"}

        try:
            from unilabos.config.config import BasicConfig
            config.setdefault("id", device_id)
            config.setdefault("type", "device")
            config.setdefault("machine_name", BasicConfig.machine_name or "本地")
            res_dict = ResourceDictInstance.get_resource_instance_from_dict(config)

            d = self.initialize_device(device_id, res_dict)
            if d is None:
                return {"success": False, "error": f"initialize_device returned None for {device_id}"}

            # Add to children config list
            self.children.append(res_dict)

            # Add to resource tracker
            try:
                from unilabos.resources.resource_tracker import ResourceTreeInstance
                tree = ResourceTreeInstance(res_dict)
                for plr_resource in ResourceTreeSet([tree]).to_plr_resources():
                    self.resource_tracker.add_resource(plr_resource)
            except Exception as ex:
                self.lab_logger().warning(f"[Workstation-DeviceMgr] PLR resource registration skipped: {ex}")

            self.lab_logger().info(f"[Workstation-DeviceMgr] Sub-device {device_id} created")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            self.lab_logger().error(f"[Workstation-DeviceMgr] Failed to create {device_id}: {e}")
            self.lab_logger().error(traceback.format_exc())
            return {"success": False, "error": str(e)}

    def destroy_device(self, device_id: str) -> dict:
        """Dynamically remove a sub-device from this workstation."""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id not in self.sub_devices:
            return {"success": False, "error": f"Sub-device {device_id} not found"}

        try:
            # Remove from children config list
            self.children = [
                c for c in self.children
                if c.res_content.id != device_id
            ]

            # Remove from resource tracker
            try:
                tracked = self.resource_tracker.uuid_to_resources.copy()
                for uid, res in tracked.items():
                    res_id = res.get("id") if isinstance(res, dict) else getattr(res, "name", None)
                    if res_id == device_id:
                        self.resource_tracker.remove_resource(res)
            except Exception as ex:
                self.lab_logger().warning(f"[Workstation-DeviceMgr] Resource tracker cleanup: {ex}")

            # Remove action clients for this sub-device
            action_prefix = f"/devices/{device_id}/"
            to_remove = [k for k in self._action_clients if k.startswith(action_prefix)]
            for k in to_remove:
                try:
                    self._action_clients[k].destroy()
                except Exception:
                    pass
                del self._action_clients[k]

            # Destroy the ROS2 node
            instance = self.sub_devices.pop(device_id, None)
            if instance is not None:
                ros_node = getattr(instance, "ros_node_instance", None)
                if ros_node is not None:
                    try:
                        ros_node.destroy_node()
                    except Exception as e:
                        self.lab_logger().warning(
                            f"[Workstation-DeviceMgr] Error destroying ROS node for {device_id}: {e}"
                        )

            # Remove from communication map if present
            self.communication_node_id_to_instance.pop(device_id, None)

            self.lab_logger().info(f"[Workstation-DeviceMgr] Sub-device {device_id} destroyed")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            self.lab_logger().error(f"[Workstation-DeviceMgr] Failed to destroy {device_id}: {e}")
            self.lab_logger().error(traceback.format_exc())
            return {"success": False, "error": str(e)}

    def create_ros_action_server(self, action_name, action_value_mapping):
        """创建ROS动作服务器"""
        if action_name not in self.protocol_names:
            # 非protocol方法调用父类注册
            return super().create_ros_action_server(action_name, action_value_mapping)
        # 和Base创建的路径是一致的
        protocol_name = action_name
        action_type = action_value_mapping["type"]
        str_action_type = str(action_type)[8:-2]
        protocol_type = globals()[protocol_name]
        protocol_steps_generator = action_protocol_generators[protocol_type]

        self._action_servers[action_name] = ActionServer(
            self,
            action_type,
            action_name,
            execute_callback=self._create_protocol_execute_callback(action_name, protocol_steps_generator),
            callback_group=self.callback_group,
        )
        self.lab_logger().trace(f"发布动作: {action_name}, 类型: {str_action_type}")
        return

    def _create_protocol_execute_callback(self, protocol_name, protocol_steps_generator):
        async def execute_protocol(goal_handle: ServerGoalHandle):
            """执行完整的工作流"""
            # 初始化结果信息变量
            execution_error = ""
            execution_success = False
            protocol_return_value = None
            self.lab_logger().info(f"Executing {protocol_name} action...")
            action_value_mapping = self._action_value_mappings[protocol_name]
            step_results = []
            try:
                self.lab_logger().warning("+" * 30)
                self.lab_logger().info(protocol_steps_generator)
                # 从目标消息中提取参数, 并调用Protocol生成器(根据设备连接图)生成action步骤
                goal = goal_handle.request
                protocol_kwargs = convert_from_ros_msg_with_mapping(goal, action_value_mapping["goal"])

                # # 🔧 添加调试信息
                # print(f"🔍 转换后的 protocol_kwargs: {protocol_kwargs}")
                # print(f"🔍 vessel 在转换后: {protocol_kwargs.get('vessel', 'NOT_FOUND')}")

                # # 🔧 完全禁用Host查询，直接使用转换后的数据
                # print(f"🔧 跳过Host查询，直接使用转换后的数据")
                # 向Host查询物料当前状态
                for k, v in goal.get_fields_and_field_types().items():
                    if v in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]:
                        self.lab_logger().info(f"{protocol_name} 查询资源状态: Key: {k} Type: {v}")

                        try:
                            # 统一处理单个或多个资源
                            resource_id = (
                                protocol_kwargs[k]["id"]
                                if v == "unilabos_msgs/Resource"
                                else protocol_kwargs[k][0]["id"]
                            )
                            resource_uuid = protocol_kwargs[k].get("uuid", None)
                            r = SerialCommand_Request()
                            r.command = json.dumps({"id": resource_id, "uuid": resource_uuid, "with_children": True})
                            # 发送请求并等待响应
                            response: SerialCommand_Response = await self._resource_clients["resource_get"].call_async(
                                r
                            )  # type: ignore
                            raw_data = json.loads(response.response)
                            tree_set = ResourceTreeSet.from_raw_dict_list(raw_data)
                            target = tree_set.dump()
                            protocol_kwargs[k] = target[0][0] if v == "unilabos_msgs/Resource" else target
                        except Exception as ex:
                            self.lab_logger().error(f"查询资源失败: {k}, 错误: {ex}\n{traceback.format_exc()}")
                            raise

                self.lab_logger().info(f"🔍 最终的 vessel: {protocol_kwargs.get('vessel', 'NOT_FOUND')}")

                from unilabos.resources.graphio import physical_setup_graph

                self.lab_logger().info(f"Working on physical setup: {physical_setup_graph}")
                protocol_steps = protocol_steps_generator(G=physical_setup_graph, **protocol_kwargs)
                logs = []
                for step in protocol_steps:
                    if isinstance(step, dict) and "log_message" in step.get("action_kwargs", {}):
                        logs.append(step)
                    elif isinstance(step, list):
                        logs.append(step)
                self.lab_logger().info(
                    f"Goal received: {protocol_kwargs}, running steps: "
                    f"{json.dumps(logs, indent=4, ensure_ascii=False)}"
                )

                time_start = time.time()
                time_overall = 100
                self._busy = True

                # 逐步执行工作流
                for i, action in enumerate(protocol_steps):
                    # self.get_logger().info(f"Running step {i + 1}: {action}")
                    if isinstance(action, dict):
                        # 如果是单个动作，直接执行
                        if action["action_name"] == "wait":
                            time.sleep(action["action_kwargs"]["time"])
                            step_results.append({"step": i + 1, "action": "wait", "result": "completed"})
                        else:
                            try:
                                result = await self.execute_single_action(**action)
                                step_results.append({"step": i + 1, "action": action["action_name"], "result": result})
                                ret_info = json.loads(getattr(result, "return_info", "{}"))
                                if not ret_info.get("suc", False):
                                    raise RuntimeError(f"Step {i + 1} failed.")
                            except ROS2WorkstationNodeTempError as ex:
                                step_results.append(
                                    {"step": i + 1, "action": action["action_name"], "result": ex.args[0]}
                                )
                    elif isinstance(action, list):
                        # 如果是并行动作，同时执行
                        actions = action
                        futures = [
                            rclpy.get_global_executor().create_task(self.execute_single_action(**a)) for a in actions
                        ]
                        results = [await f for f in futures]
                        step_results.append(
                            {
                                "step": i + 1,
                                "parallel_actions": [a["action_name"] for a in actions],
                                "results": results,
                            }
                        )

                # 向Host更新物料当前状态
                for k, v in goal.get_fields_and_field_types().items():
                    if v not in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]:
                        continue
                    self.lab_logger().info(f"更新资源状态: {k}")
                    try:
                        # 去重：使用 seen 集合获取唯一的资源对象
                        seen = set()
                        unique_resources = []

                        # 获取资源数据，统一转换为列表
                        resource_data = protocol_kwargs[k]
                        is_sequence = v != "unilabos_msgs/Resource"
                        if not is_sequence:
                            resource_list = [resource_data] if isinstance(resource_data, dict) else resource_data
                        else:
                            # 处理序列类型，可能是嵌套列表
                            resource_list = []
                            if isinstance(resource_data, list):
                                for item in resource_data:
                                    if isinstance(item, list):
                                        resource_list.extend(item)
                                    else:
                                        resource_list.append(item)
                            else:
                                resource_list = [resource_data]

                        for res_data in resource_list:
                            if not isinstance(res_data, dict):
                                continue
                            res_name = res_data.get("id") or res_data.get("name")
                            if not res_name:
                                continue

                            # 使用 resource_tracker 获取本地 PLR 实例
                            plr = self.resource_tracker.figure_resource({"name": res_name}, try_mode=False)
                            # 获取父资源
                            res = self.resource_tracker.parent_resource(plr)
                            if res is None:
                                res = plr
                            if id(res) not in seen:
                                seen.add(id(res))
                                unique_resources.append(res)

                        # 使用新的资源树接口更新
                        if unique_resources:
                            await self.update_resource(unique_resources)
                    except Exception as e:
                        self.lab_logger().error(f"资源更新失败: {e}")
                        self.lab_logger().error(traceback.format_exc())

                # 设置成功状态和返回值
                execution_success = True
                protocol_return_value = {
                    "protocol_name": protocol_name,
                    "steps_executed": len(protocol_steps),
                    "step_results": step_results,
                    "total_time": time.time() - time_start,
                }

                goal_handle.succeed()

            except Exception as e:
                # 捕获并记录错误信息
                str_step_results = [
                    {
                        k: dict(message_to_ordereddict(v)) if k == "result" and hasattr(v, "SLOT_TYPES") else v
                        for k, v in i.items()
                    }
                    for i in step_results
                ]
                execution_error = f"{traceback.format_exc()}\n\nStep Result: {pformat(str_step_results)}"
                execution_success = False
                self.lab_logger().error(f"协议 {protocol_name} 执行出错: {str(e)} \n{traceback.format_exc()}")

                # 设置动作失败
                goal_handle.abort()

            finally:
                self._busy = False

            # 创建结果消息
            result = action_value_mapping["type"].Result()
            result.success = execution_success

            # 获取结果消息类型信息，检查是否有return_info字段
            result_msg_types = action_value_mapping["type"].Result.get_fields_and_field_types()

            # 设置return_info字段（如果存在）
            for attr_name in result_msg_types.keys():
                if attr_name in ["success", "reached_goal"]:
                    setattr(result, attr_name, execution_success)
                elif attr_name == "return_info":
                    setattr(
                        result,
                        attr_name,
                        get_result_info_str(execution_error, execution_success, protocol_return_value),
                    )

            self.lab_logger().info(f"协议 {protocol_name} 完成并返回结果")
            return result

        return execute_protocol

    async def execute_single_action(self, device_id, action_name, action_kwargs):
        """执行单个动作"""
        # 构建动作ID
        if action_name == "log_message":
            self.lab_logger().info(f"[Protocol Log] {action_kwargs}")
            raise ROS2WorkstationNodeTempError(f"[Protocol Log] {action_kwargs}")
        if device_id in ["", None, "self"]:
            action_id = f"/devices/{self.device_id}/{action_name}"
        else:
            action_id = f"/devices/{device_id}/{action_name}"  # 执行时取消了主节点信息 /{self.device_id}

        # 检查动作客户端是否存在
        if action_id not in self._action_clients:
            self.lab_logger().error(f"找不到动作客户端: {action_id}")
            return None

        # 发送动作请求
        action_client = self._action_clients[action_id]
        goal_msg = convert_to_ros_msg(action_client._action_type.Goal(), action_kwargs)

        ##### self.lab_logger().info(f"发送动作请求到: {action_id}")
        action_client.wait_for_server()

        # 等待动作完成
        request_future = action_client.send_goal_async(goal_msg)
        handle = await request_future

        if not handle.accepted:
            self.lab_logger().error(f"动作请求被拒绝: {action_name}")
            return None

        result_future = await handle.get_result_async()
        ##### self.lab_logger().info(f"动作完成: {action_name}")

        return result_future.result

    """还没有改过的部分"""

    def _setup_hardware_proxy(
        self, device: ROS2DeviceNode, communication_device: ROS2DeviceNode, read_method, write_method
    ):
        """为设备设置硬件接口代理"""
        # extra_info = [getattr(device.driver_instance, info) for info in communication_device.ros_node_instance._hardware_interface.get("extra_info", [])]
        write_func = getattr(
            communication_device.driver_instance, communication_device.ros_node_instance._hardware_interface["write"]
        )
        read_func = getattr(
            communication_device.driver_instance, communication_device.ros_node_instance._hardware_interface["read"]
        )

        def _read(*args, **kwargs):
            return read_func(*args, **kwargs)

        def _write(*args, **kwargs):
            return write_func(*args, **kwargs)

        if read_method:
            # bound_read = MethodType(_read, device.driver_instance)
            setattr(device.driver_instance, read_method, _read)

        if write_method:
            # bound_write = MethodType(_write, device.driver_instance)
            setattr(device.driver_instance, write_method, _write)
