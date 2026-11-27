import threading

from unilabos.app.runtime_thread_lifecycle import (
    start_runtime_generation,
    start_runtime_thread,
)
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils import logger


# 根据选择的 backend 启动相应的功能
def start_backend(
    backend: str,
    devices_config: ResourceTreeSet,
    resources_config: ResourceTreeSet,
    resources_edge_config: list[dict] = [],
    graph=None,
    controllers_config: dict = {},
    bridges=[],
    visual: str = "None",
    resources_mesh_config: dict = {},
    **kwargs,
) -> threading.Thread:
    if kwargs.get("is_slave"):
        raise ValueError("Slave 部署已移除，设备必须由 edge_runtime 加载")
    if backend == "ros":
        # 假设 ros_main, simple_main, automancer_main 是不同 backend 的启动函数
        from unilabos.ros.runtime import main  # 如果选择 'ros' 作为 backend
    elif backend == "simple":
        # 这里假设 simple_backend 和 automancer_backend 是你定义的其他两个后端
        # from simple_backend import main as simple_main
        pass
    elif backend == "automancer":
        # from automancer_backend import main as automancer_main
        pass
    else:
        raise ValueError(f"Unsupported backend: {backend}")

    start_runtime_generation()
    backend_thread = start_runtime_thread(
        target=main,
        args=(
            devices_config,
            resources_config,
            resources_edge_config,
            graph,
            controllers_config,
            bridges,
            visual,
            resources_mesh_config,
        ),
        name="backend_thread",
    )
    logger.info(f"Backend {backend} started.")
    return backend_thread
