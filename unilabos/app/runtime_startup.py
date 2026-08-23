"""设备 backend、管理端 Web 与可视化的统一运行入口。"""

from __future__ import annotations

import threading
from typing import Any

from unilabos.config.config import BasicConfig
from unilabos.utils.banner_print import print_status


def _run_management_or_wait(backend_thread: threading.Thread) -> None:
    if not BasicConfig.is_host_mode:
        backend_thread.join()
        return

    from unilabos.server.api.app import start_server

    start_server(
        open_browser=not BasicConfig.disable_browser,
        port=BasicConfig.port,
    )


def run_runtime(args: dict[str, Any]) -> None:
    """启动设备 runtime 和 Host 微后端管理 API。"""

    from unilabos.app.backend import start_backend

    if args["visual"] == "disable":
        _run_management_or_wait(start_backend(**args))
        return

    from unilabos.resources.graphio import dict_from_graph

    devices_and_resources = dict_from_graph(args["graph"])
    if devices_and_resources is None:
        _run_management_or_wait(start_backend(**args))
        return

    from unilabos.device_mesh.resource_visalization import ResourceVisualization

    visualization = ResourceVisualization(
        devices_and_resources,
        [node.res_content for node in args["resources_config"].all_nodes],
        enable_rviz=args["visual"] == "rviz",
    )
    args["resources_mesh_config"] = visualization.resource_model
    backend_thread = start_backend(**args)

    if BasicConfig.is_host_mode:
        from unilabos.server.api.app import start_server

        threading.Thread(
            target=start_server,
            kwargs={
                "open_browser": not BasicConfig.disable_browser,
                "port": BasicConfig.port,
            },
            daemon=True,
            name="UniLabManagementAPI",
        ).start()

    try:
        visualization.start()
    except OSError as exc:
        if "AMENT_PREFIX_PATH" not in str(exc):
            raise
        print_status(
            f"ROS 2环境未正确设置，跳过3D可视化启动。错误详情: {exc}",
            "warning",
        )
        print_status(
            "建议激活 ROS 2 环境，或使用 --backend hostlink / --visual disable",
            "info",
        )

    backend_thread.join()


__all__ = ["run_runtime"]
