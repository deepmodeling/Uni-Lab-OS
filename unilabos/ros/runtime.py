import os

# from nt import device_encoding
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# rcutils 会缓存首次日志初始化时的输出流，必须先固定到受控的 stderr。
os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"

import rclpy
from rclpy.signals import SignalHandlerOptions
from rclpy.timer import Timer

from unilabos.app.runtime_thread_lifecycle import (
    begin_runtime_shutdown,
    publish_edge_runtime_ready_signal,
    start_runtime_thread,
)
from unilabos.config.config import BasicConfig, EdgeControlConfig
from unilabos.resources.resource_tracker import (
    ResourceTreeSet,
)
from unilabos.ros.executor import RuntimeExecutor, ensure_executor_thread_running
from unilabos.ros.logging import close_ros_logging, prepare_ros_logging
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.ros.nodes.presets.resource_mesh_manager import ResourceMeshManager
from unilabos.utils import logger


def exit() -> None:
    """关闭ROS节点和资源"""
    begin_runtime_shutdown()
    host_instance = HostNode.get_instance()
    if host_instance is not None:
        # 停止发现定时器
        # noinspection PyProtectedMember
        if hasattr(host_instance, "_discovery_timer") and isinstance(
            host_instance._discovery_timer, Timer
        ):
            # noinspection PyProtectedMember
            host_instance._discovery_timer.cancel()
        for _, device_node in host_instance.devices_instances.items():
            device_node.ros_node_instance.destroy_node()
        host_instance.destroy_node()
    try:
        rclpy.shutdown()
    finally:
        close_ros_logging()


def _wait_for_control_plane(bridges: List[Any], thread: threading.Thread) -> None:
    """在可配置预算内等待 Edge 控制桥完成首次连接。"""

    deadline = time.monotonic() + float(EdgeControlConfig.readiness_timeout)
    control_bridges = [
        bridge
        for bridge in bridges
        if callable(getattr(bridge, "is_connected", None))
    ]
    while control_bridges and time.monotonic() < deadline:
        ensure_executor_thread_running(thread)
        if all(bridge.is_connected() for bridge in control_bridges):
            break
        time.sleep(0.05)
    if control_bridges and not all(
        bridge.is_connected() for bridge in control_bridges
    ):
        raise RuntimeError(
            "Edge Runtime failed to connect its control plane before readiness"
        )


def main(
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list[dict] = [],
    graph: Optional[Dict[str, Any]] = None,
    controllers_config: Dict[str, Any] = {},
    bridges: List[Any] = [],
    visual: str = "disable",
    resources_mesh_config: dict = {},
    rclpy_init_args: Optional[List[str]] = None,
    discovery_interval: float = 15.0,
) -> None:
    """主函数"""

    rclpy_init_args = prepare_ros_logging(
        rclpy_init_args, context_initialized=rclpy.ok()
    )
    # Support restart - check if rclpy is already initialized
    if not rclpy.ok():
        # 应用先标记正常关闭再停止 ROS，禁止原生信号线程抢先关闭 context。
        rclpy.init(args=rclpy_init_args, signal_handler_options=SignalHandlerOptions.NO)
    else:
        logger.info("[ROS] rclpy already initialized, reusing context")
    executor = rclpy.__executor = RuntimeExecutor(
        num_threads=max(os.cpu_count() * 4, 48)
    )
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
            [
                node.res_content.model_dump(by_alias=True)
                for node in resources_config.all_nodes
            ]
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
        joint_republisher = JointRepublisher(
            "joint_republisher", host_node.resource_tracker
        )
        # lh_joint_pub = LiquidHandlerJointPublisher(
        #     resources_config=resources_list, resource_tracker=host_node.resource_tracker
        # )
        executor.add_node(resource_mesh_manager)
        executor.add_node(joint_republisher)
        # executor.add_node(lh_joint_pub)

    thread = start_runtime_thread(
        target=executor.spin, name="host_executor_thread"
    )

    if BasicConfig.process_role == "edge_runtime":
        _wait_for_control_plane(bridges, thread)
        publish_edge_runtime_ready_signal()

    while True:
        time.sleep(1)
        ensure_executor_thread_running(thread)
