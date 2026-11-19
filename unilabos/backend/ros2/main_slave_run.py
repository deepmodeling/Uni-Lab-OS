import json
import os

# from nt import device_encoding
import threading
import time
import traceback
from typing import Optional, Dict, Any, List
import uuid

import rclpy

from unilabos.app.register import collect_devices_and_resources
from unilabos.backend.ros2.presets.resource_mesh_manager import ResourceMeshManager
from unilabos.resources.resource_tracker import DeviceNodeResourceTracker, ResourceTreeSet
from unilabos.devices.ros_dev.liquid_handler_joint_publisher import LiquidHandlerJointPublisher
from unilabos_msgs.srv import SerialCommand  # type: ignore
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.timer import Timer

from unilabos.registry.registry import lab_registry
from unilabos.backend.ros2.initialize_device import initialize_device_from_dict
from unilabos.backend.ros2.presets.host_node import HostNode
from unilabos.utils import logger
from unilabos.config.config import BasicConfig, resolve_host_node_name
from unilabos.utils.serialization import TypeEncoder


def _spin_forever(executor) -> None:
    """守护式 spin：任何回调/协程异常都不允许终结 executor 线程。

    rclpy 的 spin 会把 Task 异常重新抛给调用线程（rclpy 设计如此）；executor
    线程一旦退出，本进程所有 ROS 回调停摆、进行中的 action 永远不会完成。
    异常在此记录后继续 spin，错误传播交由各动作自身的结果通道完成。
    """
    while rclpy.ok():
        try:
            executor.spin()
            return
        except Exception:
            logger.error(
                f"[ROS executor] 回调异常已捕获，executor 继续运行\n{traceback.format_exc()}"
            )


def _init_rclpy(args: List[str], domain_id: Optional[int]) -> None:
    """Initialize ROS with an explicit domain when supported by rclpy."""

    if rclpy.ok():
        logger.info("[ROS] rclpy already initialized, reusing context")
        return
    if domain_id is not None:
        # Also populate the environment for child processes and older rclpy
        # versions that do not accept the domain_id keyword.
        os.environ["ROS_DOMAIN_ID"] = str(domain_id)
    try:
        rclpy.init(args=args, domain_id=domain_id)
    except TypeError:
        # Older rclpy builds read ROS_DOMAIN_ID from the environment only.
        rclpy.init(args=args)


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
    from unilabos.backend.hostlink.network import shutdown_network_services

    shutdown_network_services()
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

    # ROS2 模式下 HostLink 只负责组网控制面，并由微后端持有生命周期。
    # 必须先发布/应用 Host ROS 策略，再初始化 DDS。
    from unilabos.backend.hostlink.network import setup_host_network_service

    setup_host_network_service()
    raw_domain_id = os.environ.get("ROS_DOMAIN_ID", "").strip()
    domain_id = int(raw_domain_id) if raw_domain_id else None
    _init_rclpy(rclpy_init_args, domain_id)
    executor = rclpy.__executor = MultiThreadedExecutor(num_threads=max(os.cpu_count() * 4, 48))
    # 创建主机节点；实例名支持 --host_node_id 重命名（注册表类型固定 host_node）
    host_node = HostNode(
        resolve_host_node_name(),
        devices_config,
        resources_config,
        graph,
        controllers_config,
        bridges,
        discovery_interval,
    )

    if visual != "disable":
        from unilabos.backend.ros2.presets.joint_republisher import JointRepublisher

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
            resource_uuid=str(uuid.uuid4()),
        )
        joint_republisher = JointRepublisher("joint_republisher", host_node.resource_tracker)
        # lh_joint_pub = LiquidHandlerJointPublisher(
        #     resources_config=resources_list, resource_tracker=host_node.resource_tracker
        # )
        executor.add_node(resource_mesh_manager)
        executor.add_node(joint_republisher)
        # executor.add_node(lh_joint_pub)

    thread = threading.Thread(target=_spin_forever, args=(executor,), daemon=True, name="host_executor_thread")
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
    # 1. Slave 先由微后端通过 HostLink 获取 domain/discovery 信息，再初始化
    # DDS；设备动作、装饰器、Topic、Service 与注册流程仍全部走 ROS2。
    from unilabos.backend.hostlink.network import (
        require_slave_startup_device_ids,
        setup_slave_network_client,
    )

    _hostlink_client, domain_id = setup_slave_network_client(
        device_ids=require_slave_startup_device_ids(devices_config)
    )
    _init_rclpy(rclpy_init_args, domain_id)
    executor = rclpy.__executor
    if not executor:
        executor = rclpy.__executor = MultiThreadedExecutor(num_threads=max(os.cpu_count() * 4, 48))

    # 1.5 启动 executor 线程
    thread = threading.Thread(target=_spin_forever, args=(executor,), daemon=True, name="slave_executor_thread")
    thread.start()

    # 2. 创建 Slave Machine Node
    n = Node(f"slaveMachine_{BasicConfig.machine_name}", parameter_overrides=[])
    executor.add_node(n)

    # 3. 向 Host 报送节点信息，并与物料权威对齐
    if not BasicConfig.slave_no_host:
        # 3.1 报送节点信息
        sclient = n.create_client(SerialCommand, "/node_info_update")
        sclient.wait_for_service()

        registry_config = {}
        devices_to_register, resources_to_register = collect_devices_and_resources(
            lab_registry
        )
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
        logger.info("Slave node info updated.")

        # 3.2 物料权威对齐：图中的 UUID 作为实例身份。materials.ensure
        # 复用已有权威记录，缺失时经 HostLink 以该 UUID 创建。
        # --disable_hostlink 是显式的纯 ROS2 降级模式：无链路可达物料权威，
        # 跳过对齐（资源仅存在于本地图），不阻断设备启动。
        if resources_config and _hostlink_client is None:
            logger.warning(
                f"HostLink 未启用，跳过 Slave 物料权威对齐: {len(resources_config.trees)} 棵树仅存在于本地图"
            )
        elif resources_config:
            from unilabos.protocol.materials import ACTOR_GRAPH
            from unilabos.resources import materials

            ensured = materials.ensure(
                resources_config,
                actor_type=ACTOR_GRAPH,
                actor_uuid=BasicConfig.machine_name or None,
            )
            logger.info(f"Slave 物料权威对齐完成: {len(ensured.trees)} 棵树（uuid 与图一致）")
        else:
            logger.info("No resources to add.")

    # 4. 初始化所有设备实例（resources_config 的 uuid 与权威一致）
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

    # 4.5 物料下行链路（append_resource / 资源树同步）走 HostLink，不建 ROS service：
    # 把下行 handler 挂到 HostLink client，Host 的分发经此调用本进程设备节点实例。
    if _hostlink_client is not None:
        from unilabos.backend.hostlink.downlink import register_hostlink_resource_handlers

        register_hostlink_resource_handlers(_hostlink_client)
        logger.info("HostLink 物料下行 handler 已注册（资源树同步 / 物料挂载）。")

    # 5. 如果启用可视化，创建可视化相关节点
    if visual != "disable":
        from unilabos.backend.ros2.presets.joint_republisher import JointRepublisher

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
