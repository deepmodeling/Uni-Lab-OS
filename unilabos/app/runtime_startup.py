"""设备 backend、管理端 Web 与可视化的统一运行入口。"""

from __future__ import annotations

import errno
import threading
import time
from typing import Any

from unilabos.config.config import BasicConfig
from unilabos.utils.banner_print import print_status

#: 设备 runtime 就绪等待上限：真实驱动初始化（串口 / 网络设备握手）可能要几十秒
BACKEND_READY_TIMEOUT_S = 120.0


def _wait_backend_ready(backend_thread: threading.Thread, timeout: float) -> bool:
    """等设备执行适配器注册（HostLink 设备与 host 服务已就绪 / ROS HostNode 已建）或 backend 线程退出。"""

    from unilabos.backend.hostlink.adapter_registry import get_execution_adapter

    deadline = time.monotonic() + timeout
    while backend_thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if get_execution_adapter(timeout=min(0.5, remaining)) is not None:
            return True
    return get_execution_adapter() is not None


def _announce_ready_and_start_managed(backend_thread: threading.Thread) -> None:
    """设备 runtime 真正就绪后才打就绪横幅、拉起受管 Slave（在后台线程运行）。

    ``start_backend`` 只是起了线程：HostLink 端口虽已在进程启动之初打开（Slave 连上后会
    等 Host 就绪），设备与 host 服务还没起来，这时打"已就绪"横幅为时过早；runtime 起不来时
    更不该拉起任何东西。
    """

    ready = _wait_backend_ready(backend_thread, BACKEND_READY_TIMEOUT_S)
    if not ready and not backend_thread.is_alive():
        return  # runtime 起不来：主线程按 backend_fatal_failure 退出
    if not ready:
        print_status(
            f"设备 runtime {BACKEND_READY_TIMEOUT_S:.0f}s 内仍未就绪；"
            "受管设备进程照常拉起，Slave 会自行重连",
            "warning",
        )
    print_slave_launch_hint()
    _start_managed_device_processes()


def _start_managed_device_processes() -> None:
    """Host 就绪后拉起台账里 auto_start 的受管 Slave 子进程（崩溃由服务看护重启）。"""
    try:
        from unilabos.server.services.device_processes import get_device_process_service

        started = get_device_process_service().start_auto()
        if started:
            print_status(f"已拉起 {len(started)} 个受管设备进程", "info")
    except Exception as exc:  # noqa: BLE001 - 受管进程失败不影响 Host 本体
        print_status(f"受管设备进程启动失败: {exc}", "warning")


def build_slave_launch_command(
    *,
    backend: str,
    host_ip: str,
    hostlink_port: int,
    hostlink_enabled: bool,
) -> str:
    """生成在另一台机器上把设备接入本 Host 的 Slave 启动命令。"""

    parts = ["unilab", "--backend", backend, "--is_slave"]
    if hostlink_enabled:
        parts += ["--host_node_ip", host_ip, "--hostlink_port", str(hostlink_port)]
    else:
        parts.append("--disable_hostlink")
    parts += ["-g", "<图文件.json>"]
    return " ".join(parts)


def print_slave_launch_hint() -> None:
    """Host 就绪后告诉用户如何拉起 Slave；Slave 进程和 --role backend 不打印。"""

    from unilabos.config.config import HostLinkConfig

    backend = str(BasicConfig.backend or "hostlink")
    hostlink_enabled = bool(HostLinkConfig.enable)
    host_ip = str(HostLinkConfig.advertise_ip or "").strip()
    hostlink_port = int(HostLinkConfig.port)
    if hostlink_enabled:
        from unilabos.backend.hostlink.ros_assist import detect_local_ip
        from unilabos.backend.hostlink.server import get_hostlink_server

        server = get_hostlink_server()
        if server is not None:
            hostlink_port = int(server.port)
        if not host_ip:
            host_ip = detect_local_ip() or "127.0.0.1"

    command = build_slave_launch_command(
        backend=backend,
        host_ip=host_ip,
        hostlink_port=hostlink_port,
        hostlink_enabled=hostlink_enabled,
    )
    transport = "HostLink TCP" if hostlink_enabled else "ROS 2 DDS 发现"
    print_status(f"Host 已就绪（backend={backend}，Slave 经 {transport} 接入）", "success")
    print_status(f"在其他机器上启动 Slave 接入本 Host：{command}", "info")
    if hostlink_enabled:
        print_status(
            "同一台机器上可把 --host_node_ip 改为 127.0.0.1，并用 --server_database_root 指定另一个"
            "数据库目录（一个数据库根目录只能由一个实例使用）；需要加载驱动包时追加 --devices <目录>。",
            "info",
        )
    else:
        print_status(
            "已关闭 HostLink，Slave 需与 Host 处于同一 ROS_DOMAIN_ID 且网络可互相发现。",
            "info",
        )


def _fail_on_port_in_use(exc: OSError) -> None:
    """管理端端口被占用属于用户可修复的启动错误：只打印修改建议，不打印堆栈。"""

    from unilabos.app.supervisor import PORT_IN_USE_EXIT_CODE

    print_status(exc.strerror or str(exc), "error")
    raise SystemExit(PORT_IN_USE_EXIT_CODE) from exc


def _exit_on_backend_failure() -> None:
    """设备 runtime 线程异常退出后，服务循环已被它叫停：按原因给出退出码，别当正常退出。

    HostLink 端口被占（多半是上次没退干净的 Host）与管理端口被占同一退出码，监督进程
    会退避重试，占用者被结束后即可自动恢复；其它 runtime 崩溃按普通失败退出。
    """

    from unilabos.backend import backend_fatal_failure

    failure = backend_fatal_failure()
    if failure is None:
        return
    if isinstance(failure, OSError) and failure.errno == errno.EADDRINUSE:
        _fail_on_port_in_use(failure)
    print_status(f"设备 runtime 启动失败，进程退出：{failure}", "error")
    raise SystemExit(1) from failure


def _serves_over_control_plane() -> bool:
    """调度权威拉起的 Host 子进程不监听端口：管理 API 由权威经控制 WS 下发、在进程内执行。"""

    from unilabos.app.supervisor import is_host_child

    return is_host_child()


def _ensure_management_port_available() -> None:
    """Host 模式下在拉起设备 runtime 之前先确认管理端端口可用，避免设备起来后再失败。"""

    if not BasicConfig.is_host_mode or _serves_over_control_plane():
        return
    from unilabos.server.api.app import ManagementPortInUseError, ensure_port_available

    try:
        ensure_port_available("0.0.0.0", BasicConfig.port)
    except ManagementPortInUseError as exc:
        _fail_on_port_in_use(exc)


def _start_management_server() -> None:
    from unilabos.server.api.app import ManagementPortInUseError, serve_over_control_plane, start_server

    if _serves_over_control_plane():
        print_status("Host 子进程不监听端口：管理 API 由调度权威经控制面 WS 下发、在本进程内执行", "info")
        serve_over_control_plane()
        return
    try:
        start_server(
            open_browser=not BasicConfig.disable_browser,
            port=BasicConfig.port,
        )
    except ManagementPortInUseError as exc:
        _fail_on_port_in_use(exc)


def _run_management_or_wait(backend_thread: threading.Thread) -> None:
    if not BasicConfig.is_host_mode:
        backend_thread.join()
        _exit_on_backend_failure()
        return

    threading.Thread(
        target=_announce_ready_and_start_managed,
        args=(backend_thread,),
        name="UniLabBackendReady",
        daemon=True,
    ).start()
    _start_management_server()
    _exit_on_backend_failure()


def run_runtime(args: dict[str, Any]) -> None:
    """启动设备 runtime 和 Host 微后端管理 API。"""

    from unilabos.backend import start_backend

    _ensure_management_port_available()

    if BasicConfig.is_host_mode:
        from unilabos.server.services.driver_packages import get_driver_package_service
        state = get_driver_package_service().mutation_gate.status()
        if state['unresolved_operations'] or state['error']:
            print_status('包操作恢复尚未人工确认：仅启动管理服务，设备 runtime 与托管 slave 保持停止；确认后手动重启 Host。', 'warning')
            # 设备 runtime 不会启动：关掉进程启动时提前打开的 HostLink 端口，别让 Slave 空等
            from unilabos.backend.hostlink.startup import release_prebound_host_server

            release_prebound_host_server()
            _start_management_server()
            return

    if args["visual"] == "disable":
        _run_management_or_wait(start_backend(**args))
        return

    from unilabos.device_mesh.resource_visalization import ResourceVisualization

    resource_tree_set = args["resources_config"]
    # 可视化按节点 id 查设备 / 物料模型；节点 dict 即 ResourceTreeSet 的 dump 形态。
    devices_and_resources = {
        node["id"]: node for tree in resource_tree_set.dump() for node in tree
    }
    visualization = ResourceVisualization(
        devices_and_resources,
        [node.res_content for node in resource_tree_set.all_nodes],
        enable_rviz=args["visual"] == "rviz",
    )
    args["resources_mesh_config"] = visualization.resource_model
    backend_thread = start_backend(**args)

    if BasicConfig.is_host_mode:
        threading.Thread(
            target=_announce_ready_and_start_managed,
            args=(backend_thread,),
            name="UniLabBackendReady",
            daemon=True,
        ).start()
        threading.Thread(
            target=_start_management_server,
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
    _exit_on_backend_failure()


__all__ = ["build_slave_launch_command", "print_slave_launch_hint", "run_runtime"]
