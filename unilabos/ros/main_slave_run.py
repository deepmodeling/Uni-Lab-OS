import json
# from nt import device_encoding
import threading
import time
from typing import Optional, Dict, Any, List
import uuid

import rclpy
from unilabos_msgs.srv._serial_command import SerialCommand_Response

from unilabos.app.register import register_devices_and_resources
from unilabos.ros.nodes.presets.resource_mesh_manager import ResourceMeshManager
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker, ResourceTreeSet
from unilabos.devices.ros_dev.liquid_handler_joint_publisher import LiquidHandlerJointPublisher
from unilabos_msgs.srv import SerialCommand  # type: ignore
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.timer import Timer

from unilabos.registry.registry import lab_registry
from unilabos.ros.initialize_device import initialize_device_from_dict
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.utils import logger
from unilabos.config.config import BasicConfig
from unilabos.utils.type_check import TypeEncoder


def exit() -> None:
    """关闭ROS节点和资源"""
    host_instance = HostNode.get_instance()
    if host_instance is not None:
        # 停止发现定时器
        # noinspection PyProtectedMember
        if hasattr(host_instance, "_discovery_timer") and isinstance(host_instance._discovery_timer, Timer):
            # noinspection PyProtectedMember
            host_instance._discovery_timer.cancel()
        for _, device_node in host_instance.devices_instances.items():
            if hasattr(device_node, "destroy_node"):
                device_node.ros_node_instance.destroy_node()
        host_instance.destroy_node()
    rclpy.shutdown()


def main(
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list[dict] = [],
    graph: Optional[Dict[str, Any]] = None,
    controllers_config: Dict[str, Any] = {},
    bridges: List[Any] = [],
    visual: str = "disable",
    resources_mesh_config: dict = {},
    rclpy_init_args: List[str] = ["--log-level", "debug"],
    discovery_interval: float = 15.0,
) -> None:
    """主函数"""

    rclpy.init(args=rclpy_init_args)
    executor = rclpy.__executor = MultiThreadedExecutor()
    # 创建主机节点
    host_node = HostNode(
        "host_node",
        devices_config,
        resources_config,
        resources_edge_config,
        graph,
        controllers_config,
        bridges,
        discovery_interval,
    )

    if visual != "disable":
        from unilabos.ros.nodes.presets.joint_republisher import JointRepublisher

        # 将 ResourceTreeSet 转换为 list 用于 visual 组件
        resources_list = (
            [node.res_content.model_dump(by_alias=True) for node in resources_config.all_nodes]
            if resources_config
            else []
        )
        resource_mesh_manager = ResourceMeshManager(
            resources_mesh_config,
            resources_list,
            resource_tracker=host_node.resource_tracker,
            device_id="resource_mesh_manager",
            device_uuid=str(uuid.uuid4()),
        )
        joint_republisher = JointRepublisher("joint_republisher", host_node.resource_tracker)
        # lh_joint_pub = LiquidHandlerJointPublisher(
        #     resources_config=resources_list, resource_tracker=host_node.resource_tracker
        # ) 
        executor.add_node(resource_mesh_manager)
        executor.add_node(joint_republisher)
        # executor.add_node(lh_joint_pub)

    thread = threading.Thread(target=executor.spin, daemon=True, name="host_executor_thread")
    thread.start()

    while True:
        time.sleep(1)


def slave(
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list = [],
    graph: Optional[Dict[str, Any]] = None,
    controllers_config: Dict[str, Any] = {},
    bridges: List[Any] = [],
    visual: str = "disable",
    resources_mesh_config: dict = {},
    rclpy_init_args: List[str] = ["--log-level", "debug"],
) -> None:
    """从节点函数"""
    # 1. 初始化 ROS2
    if not rclpy.ok():
        rclpy.init(args=rclpy_init_args)
    executor = rclpy.__executor
    if not executor:
        executor = rclpy.__executor = MultiThreadedExecutor()

    # 1.5 启动 executor 线程
    thread = threading.Thread(target=executor.spin, daemon=True, name="slave_executor_thread")
    thread.start()

    # 2. 创建 Slave Machine Node
    n = Node(f"slaveMachine_{BasicConfig.machine_name}", parameter_overrides=[])
    executor.add_node(n)

    # 3. 向 Host 报送节点信息和物料，获取 UUID 映射
    uuid_mapping = {}
    if not BasicConfig.slave_no_host:
        # 3.1 报送节点信息
        sclient = n.create_client(SerialCommand, "/node_info_update")
        sclient.wait_for_service()

        registry_config = {}
        devices_to_register, resources_to_register = register_devices_and_resources(lab_registry, True)
        registry_config.update(devices_to_register)
        registry_config.update(resources_to_register)
        request = SerialCommand.Request()
        request.command = json.dumps(
            {
                "machine_name": BasicConfig.machine_name,
                "type": "slave",
                "devices_config": devices_config.dump(),
                "registry_config": registry_config,
            },
            ensure_ascii=False,
            cls=TypeEncoder,
        )
        sclient.call_async(request).result()
        logger.info(f"Slave node info updated.")

        # 3.2 报送物料树，获取 UUID 映射
        if resources_config:
            rclient = n.create_client(SerialCommand, "/c2s_update_resource_tree")
            rclient.wait_for_service()

            request = SerialCommand.Request()
            request.command = json.dumps(
                {
                    "data": {
                        "data": resources_config.dump(),
                        "mount_uuid": "",
                        "first_add": True,
                    },
                    "action": "add",
                },
                ensure_ascii=False,
            )
            tree_response: SerialCommand_Response = rclient.call(request)
            uuid_mapping = json.loads(tree_response.response)
            logger.info(f"Slave resource tree added. UUID mapping: {len(uuid_mapping)} nodes")

            # 3.3 使用 UUID 映射更新 resources_config 的 UUID（参考 client.py 逻辑）
            old_uuids = {node.res_content.uuid: node for node in resources_config.all_nodes}
            for old_uuid, node in old_uuids.items():
                if old_uuid in uuid_mapping:
                    new_uuid = uuid_mapping[old_uuid]
                    node.res_content.uuid = new_uuid
                    # 更新所有子节点的 parent_uuid
                    for child in node.children:
                        child.res_content.parent_uuid = new_uuid
                else:
                    logger.warning(f"资源UUID未更新: {old_uuid}")
        else:
            logger.info("No resources to add.")

    # 4. 初始化所有设备实例（此时 resources_config 的 UUID 已更新）
    devices_instances = {}
    for device_config in devices_config.root_nodes:
        device_id = device_config.res_content.id
        if device_config.res_content.type == "device":
            d = initialize_device_from_dict(device_id, device_config)
            if d is not None:
                devices_instances[device_id] = d
                logger.info(f"Device {device_id} initialized.")
            else:
                logger.warning(f"Device {device_id} initialization failed.")

    # 5. 如果启用可视化，创建可视化相关节点
    if visual != "disable":
        from unilabos.ros.nodes.presets.joint_republisher import JointRepublisher

        # 将 ResourceTreeSet 转换为 list 用于 visual 组件
        resources_list = (
            [node.res_content.model_dump(by_alias=True) for node in resources_config.all_nodes]
            if resources_config
            else []
        )
        resource_mesh_manager = ResourceMeshManager(
            resources_mesh_config,
            resources_list,
            resource_tracker=DeviceNodeResourceTracker(),
            device_id="resource_mesh_manager",
        )
        joint_republisher = JointRepublisher("joint_republisher", DeviceNodeResourceTracker())
        lh_joint_pub = LiquidHandlerJointPublisher(
            resources_config=resources_list, resource_tracker=DeviceNodeResourceTracker()
        )
        executor.add_node(resource_mesh_manager)
        executor.add_node(joint_republisher)
        executor.add_node(lh_joint_pub)

    # 7. 保持运行
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
