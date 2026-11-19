"""ROS2 形态的工作站节点。

workstation 按「驱动 / 编排」分层：

1. **工作站驱动** —— :class:`unilabos.devices.workstation.workstation_base.WorkstationBase`
   及其子类是注册表的扫描源（本编排类不绑定 @device，需要被具体工作站
   节点承载，不进注册表）；
2. **工作站编排（本类）** —— 继承 :class:`BaseROS2DeviceNode` 作为设备节点壳，
   负责子设备初始化、硬件接口代理、XDL protocol ActionServer 与子设备
   ActionClient。HostLink 对应物是
   :class:`unilabos.backend.hostlink.workstation.WorkstationNode`。

protocol 步骤生成、协议名/模型解析与资源展开/回写是 backend 无关共享逻辑
（:mod:`unilabos.backend.runtime.workstation_protocol` 与
:mod:`unilabos.experiments.compile`），双 backend 同一份。
"""

from __future__ import annotations

import json
import time
import traceback
from pprint import pformat
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import rclpy
from rclpy.action import ActionClient, ActionServer
from rclpy.action.server import ServerGoalHandle
from rosidl_runtime_py import message_to_ordereddict

from unilabos.backend.ros2.base_device_node import (
    BaseROS2DeviceNode,
    DeviceNodeResourceTracker,
    ROS2DeviceNode,
)
from unilabos.backend.ros2.initialize_device import initialize_device_from_dict
from unilabos.backend.ros2.msgs.message_converter import (
    convert_from_ros_msg_with_mapping,
    convert_to_ros_msg,
    get_action_type,
)
from unilabos.backend.runtime.workstation_protocol import (
    WorkstationNodeTempError,
    expand_resource_value,
    physical_setup_graph,
    protocol_model,
    setup_protocol_names,
    update_protocol_resources,
)
from unilabos.config.config import BasicConfig
from unilabos.experiments.compile import action_protocol_generators
from unilabos.utils.serialization import serialize_result_info

if TYPE_CHECKING:
    from unilabos.devices.workstation.workstation_base import WorkstationBase
    from unilabos.resources.resource_tracker import ResourceDictInstance


class ROS2WorkstationNode(BaseROS2DeviceNode):
    """ROS2 的工作站编排节点。

    设备节点壳 + 子设备初始化/ActionClient + 硬件接口代理 +
    protocol ActionServer。
    """

    driver_instance: "WorkstationBase"

    def __init__(
        self,
        protocol_type: Any,
        children: Optional[List["ResourceDictInstance"]] = None,
        *,
        driver_instance: Optional["WorkstationBase"] = None,
        device_id: str = "",
        registry_name: str = "",
        resource_uuid: str = "",
        status_types: Optional[Dict[str, Any]] = None,
        action_value_mappings: Optional[Dict[str, Any]] = None,
        hardware_interface: Optional[Dict[str, Any]] = None,
        print_publish: bool = True,
        resource_tracker: Optional["DeviceNodeResourceTracker"] = None,
    ):
        self.protocol_names = setup_protocol_names(protocol_type)

        # protocol 的 ROS action 类型（ActionServer 建立用）
        self.protocol_action_mappings: Dict[str, Any] = {}
        for protocol_name in self.protocol_names:
            self.protocol_action_mappings[protocol_name] = get_action_type(
                protocol_model(protocol_name)
            )

        self.children = children or []
        # 初始化基类，让基类处理常规动作
        BaseROS2DeviceNode.__init__(
            self,
            driver_instance=driver_instance,
            device_id=device_id,
            registry_name=registry_name,
            resource_uuid=resource_uuid,
            status_types=status_types or {},
            action_value_mappings={**(action_value_mappings or {}), **self.protocol_action_mappings},
            hardware_interface=hardware_interface or {},
            print_publish=print_publish,
            resource_tracker=resource_tracker,
        )

        self._busy = False
        self.sub_devices: Dict[str, Any] = {}
        self._action_clients: Dict[str, Any] = {}

        # 初始化子设备
        self.communication_node_id_to_instance: Dict[str, Any] = {}

        for device_config in self.children:
            child_device_id = device_config.res_content.id
            if device_config.res_content.type != "device":
                self.lab_logger().debug(
                    f"[Workstation] Skipping type {device_config.res_content.type} {child_device_id}."
                )
                continue
            try:
                d = self.initialize_device(child_device_id, device_config)
            except Exception as ex:
                self.lab_logger().error(
                    f"[Workstation] Failed to initialize device {child_device_id}: {ex}\n{traceback.format_exc()}"
                )
                d = None
            if d is None:
                continue

            if "serial_" in child_device_id or "io_" in child_device_id:
                self.communication_node_id_to_instance[child_device_id] = d
                continue

        for device_config in self.children:
            child_device_id = device_config.res_content.id
            if device_config.res_content.type != "device":
                continue
            # 设置硬件接口代理
            if child_device_id not in self.sub_devices:
                self.lab_logger().error(f"[Workstation] {child_device_id} 还没有正确初始化，跳过...")
                continue
            d = self.sub_devices[child_device_id]
            if d:
                child_hardware_interface = d.ros_node_instance._hardware_interface
                if (
                    hasattr(d.driver_instance, child_hardware_interface["name"])
                    and hasattr(d.driver_instance, child_hardware_interface["write"])
                    and (
                        child_hardware_interface["read"] is None
                        or hasattr(d.driver_instance, child_hardware_interface["read"])
                    )
                ):

                    name = getattr(d.driver_instance, child_hardware_interface["name"])
                    read = child_hardware_interface.get("read", None)
                    write = child_hardware_interface.get("write", None)

                    # 如果硬件接口是字符串，通过通信设备提供
                    if isinstance(name, str) and name in self.sub_devices:
                        communicate_device = self.sub_devices[name]
                        communicate_hardware_info = communicate_device.ros_node_instance._hardware_interface
                        self._setup_hardware_proxy(d, self.sub_devices[name], read, write)
                        self.lab_logger().info(
                            f"\n通信代理：为子设备{child_device_id}\n    "
                            f"添加了{read}方法(来源：{name} {communicate_hardware_info['write']}) \n    "
                            f"添加了{write}方法(来源：{name} {communicate_hardware_info['read']})"
                        )

        self.lab_logger().info(
            f"ROS2WorkstationNode {device_id} initialized with protocols: {self.protocol_names}"
        )

    def initialize_device(self, device_id: str, device_config: "ResourceDictInstance"):
        """初始化子设备并创建相应的动作客户端。"""
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

    def create_device(self, device_id: str, config: Any) -> dict:
        """动态添加子设备。"""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id in self.sub_devices:
            return {"success": False, "error": f"Sub-device {device_id} already exists"}

        try:
            from unilabos.resources.resource_tracker import ResourceDictInstance, ResourceTreeSet

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
        """动态移除子设备。"""
        if not device_id:
            return {"success": False, "error": "device_id required"}

        if device_id not in self.sub_devices:
            return {"success": False, "error": f"Sub-device {device_id} not found"}

        try:
            # Remove from children config list
            self.children = [c for c in self.children if c.res_content.id != device_id]

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
        """创建ROS动作服务器；protocol 动作走协议编排回调。"""
        if action_name not in self.protocol_names:
            # 非protocol方法调用父类注册
            return super().create_ros_action_server(action_name, action_value_mapping)
        # 和Base创建的路径是一致的
        protocol_name = action_name
        action_type = action_value_mapping["type"]
        str_action_type = str(action_type)[8:-2]
        protocol_type = protocol_model(protocol_name)
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
                self.lab_logger().info(protocol_steps_generator)
                # 从目标消息中提取参数, 并调用Protocol生成器(根据设备连接图)生成action步骤
                goal = goal_handle.request
                protocol_kwargs = convert_from_ros_msg_with_mapping(goal, action_value_mapping["goal"])

                # 向权威查询物料当前状态（Resource 字段展开为完整资源树）
                for k, v in goal.get_fields_and_field_types().items():
                    if v in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]:
                        self.lab_logger().info(f"{protocol_name} 查询资源状态: Key: {k} Type: {v}")
                        try:
                            protocol_kwargs[k] = await expand_resource_value(
                                self, protocol_kwargs[k]
                            )
                        except Exception as ex:
                            self.lab_logger().error(f"查询资源失败: {k}, 错误: {ex}\n{traceback.format_exc()}")
                            raise

                setup_graph = physical_setup_graph()
                self.lab_logger().info(f"Working on physical setup: {setup_graph}")
                protocol_steps = protocol_steps_generator(G=setup_graph, **protocol_kwargs)
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
                self._busy = True

                # 逐步执行工作流
                for i, action in enumerate(protocol_steps):
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
                            except WorkstationNodeTempError as ex:
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

                # 向权威更新物料当前状态
                resource_values = [
                    protocol_kwargs[k]
                    for k, v in goal.get_fields_and_field_types().items()
                    if v in ["unilabos_msgs/Resource", "sequence<unilabos_msgs/Resource>"]
                ]
                try:
                    await update_protocol_resources(self, resource_values)
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
                        json.dumps(
                            serialize_result_info(
                                execution_error,
                                execution_success,
                                protocol_return_value,
                            ),
                            ensure_ascii=False,
                        ),
                    )

            self.lab_logger().info(f"协议 {protocol_name} 完成并返回结果")
            return result

        return execute_protocol

    async def execute_single_action(self, device_id, action_name, action_kwargs):
        """执行单个动作（经子设备 ActionClient）。"""
        # 构建动作ID
        if action_name == "log_message":
            self.lab_logger().info(f"[Protocol Log] {action_kwargs}")
            raise WorkstationNodeTempError(f"[Protocol Log] {action_kwargs}")
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

        action_client.wait_for_server()

        # 等待动作完成
        request_future = action_client.send_goal_async(goal_msg)
        handle = await request_future

        if not handle.accepted:
            self.lab_logger().error(f"动作请求被拒绝: {action_name}")
            return None

        result_future = await handle.get_result_async()

        return result_future.result

    def _setup_hardware_proxy(
        self, device: "ROS2DeviceNode", communication_device: "ROS2DeviceNode", read_method, write_method
    ):
        """为设备设置硬件接口代理。

        把 ``device`` 的读/写方法替换为转发到 ``communication_device`` 真实读写函数的闭包，
        从而让多个设备共享同一个通信端点。

        若 ``device`` 或通信端的 hardware_interface 声明了 ``extra_info``（一组属性名），
        转发时会从 ``device`` 实例上实时读取这些属性的值，并以 ``属性名=值`` 的形式作为
        关键字参数注入给通信设备的读写函数（典型用途：Modbus 从站 id、寄存器地址等每个
        设备固有、但需要交给共享通信端的参数）。调用方显式传入的同名关键字参数优先级更高。
        """
        comm_hw = communication_device.ros_node_instance._hardware_interface
        comm_instance = communication_device.driver_instance
        comm_id = getattr(comm_instance, "device_id", comm_hw.get("name"))
        write_name = comm_hw.get("write")
        read_name = comm_hw.get("read")
        # 用默认值 getattr 避免端点方法名配错时直接崩溃整站；缺失则给出清晰提示并跳过该方向
        write_func = getattr(comm_instance, write_name, None) if write_name else None
        read_func = getattr(comm_instance, read_name, None) if read_name else None
        if write_name and write_func is None:
            self.lab_logger().error(
                f"[硬件代理] 通信设备 {comm_id} 没有 write 方法 '{write_name}'，无法为使用方建立写代理；"
                f"请在该通信设备的 @device(hardware_interface=...) 中把 write 指向真实方法名"
            )
        if read_name and read_func is None:
            self.lab_logger().error(
                f"[硬件代理] 通信设备 {comm_id} 没有 read 方法 '{read_name}'，无法为使用方建立读代理；"
                f"请在该通信设备的 @device(hardware_interface=...) 中把 read 指向真实方法名"
            )

        # extra_info：需要随读写一起注入的额外参数名（使用方与通信端声明取并集），值从使用方实例读取
        device_hw = device.ros_node_instance._hardware_interface
        driver_instance = device.driver_instance
        display_id = getattr(driver_instance, "device_id", read_method or write_method)
        extra_names: List[str] = []
        for name in [*(device_hw.get("extra_info") or []), *(comm_hw.get("extra_info") or [])]:
            if name in extra_names:
                continue
            if hasattr(driver_instance, name):
                extra_names.append(name)
            else:
                self.lab_logger().warning(
                    f"[硬件代理] 子设备 {display_id} 的 extra_info 声明了属性 '{name}'，"
                    f"但其实例上不存在该属性，转发时将忽略该参数"
                )

        def _extra_kwargs() -> Dict[str, Any]:
            return {name: getattr(driver_instance, name) for name in extra_names}

        def _read(*args, **kwargs):
            return read_func(*args, **{**_extra_kwargs(), **kwargs})

        def _write(*args, **kwargs):
            return write_func(*args, **{**_extra_kwargs(), **kwargs})

        if read_method and read_func is not None:
            setattr(driver_instance, read_method, _read)

        if write_method and write_func is not None:
            setattr(driver_instance, write_method, _write)


__all__ = ["ROS2WorkstationNode", "WorkstationNodeTempError"]
