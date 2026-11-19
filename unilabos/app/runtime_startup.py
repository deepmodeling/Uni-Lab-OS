"""设备 backend、管理端 Web 与可视化的统一运行入口。"""

from __future__ import annotations

import errno
import sys
import threading
import time
from typing import Any

from unilabos.config.config import BasicConfig
from unilabos.utils.banner_print import print_status

#: 设备 runtime 就绪等待上限：真实驱动初始化（串口 / 网络设备握手）可能要几十秒
BACKEND_READY_TIMEOUT_S = 120.0
#: Slave 收到停机信号后等设备 runtime 有序收尾的上限（秒）。
SLAVE_SHUTDOWN_JOIN_S = 10.0
#: 建议的 Slave 命令里 Host 地址的占位符（本机没有可推荐的地址时使用）。
SLAVE_HINT_HOST_PLACEHOLDER = "<本机IP>"


def _wait_backend_ready(backend_thread: threading.Thread, timeout: float) -> bool:
    """等设备执行适配器注册（HostLink 设备与 host 服务已就绪 / ROS HostNode 已建）或 backend 线程退出。"""

    from unilabos.client.host.hostlink.adapter_registry import get_execution_adapter

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
    unsuggested_ip = ""
    if hostlink_enabled:
        from unilabos.client.host.hostlink.ros_assist import detect_local_ip, suggestable_host_ip
        from unilabos.client.host.hostlink.server import get_hostlink_server

        server = get_hostlink_server()
        if server is not None:
            hostlink_port = int(server.port)
        if not host_ip:
            # 默认出口地址只在像局域网 / 公网地址时才写进建议命令：出口落在代理 / VPN 虚拟网卡
            # 时 Slave 多半连不上它，宁可留占位符让用户填 Slave 实际能访问的本机地址。
            detected = detect_local_ip()
            if suggestable_host_ip(detected):
                host_ip = detected
            else:
                unsuggested_ip = detected
                host_ip = SLAVE_HINT_HOST_PLACEHOLDER

    command = build_slave_launch_command(
        backend=backend,
        host_ip=host_ip,
        hostlink_port=hostlink_port,
        hostlink_enabled=hostlink_enabled,
    )
    transport = "HostLink TCP" if hostlink_enabled else "ROS 2 DDS 发现"
    print_status(f"Host 已就绪（backend={backend}，Slave 经 {transport} 接入）", "success")
    print_status(f"在其他机器上启动 Slave 接入本 Host：{command}", "info")
    if unsuggested_ip:
        print_status(
            f"本机默认出口地址 {unsuggested_ip} 不像局域网地址（常见于代理 / VPN 虚拟网卡），"
            f"请把 {SLAVE_HINT_HOST_PLACEHOLDER} 换成 Slave 所在机器能访问到的本机地址。",
            "info",
        )
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

    from unilabos.client.host import backend_fatal_failure

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


def _join_backend(backend_thread: threading.Thread) -> None:
    # 分段等待：Windows 上无限期 join 收不到 Ctrl+C，也收不到父进程消失时对主线程的中断请求
    while backend_thread.is_alive():
        backend_thread.join(timeout=0.5)


def _stop_backend_runtime() -> None:
    """请设备 runtime 结束：ROS 2 下关闭 rclpy 上下文，runtime 线程随即有序收尾。"""

    rclpy = sys.modules.get("rclpy")
    if rclpy is None:
        return
    try:
        rclpy.try_shutdown()
    except Exception:  # noqa: BLE001 - 停机路径尽力而为
        pass


def _join_backend_until_stopped(backend_thread: threading.Thread) -> None:
    """等设备 runtime 线程结束；收到停机信号时先让它有序收尾（断开 HostLink、销毁节点、
    关 rclpy），限时等它结束后再把停机异常交给上层。"""

    try:
        _join_backend(backend_thread)
    except (SystemExit, KeyboardInterrupt):
        _stop_backend_runtime()
        backend_thread.join(timeout=SLAVE_SHUTDOWN_JOIN_S)
        raise


def _run_management_or_wait(backend_thread: threading.Thread) -> None:
    if not BasicConfig.is_host_mode:
        _join_backend_until_stopped(backend_thread)
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

    from unilabos.client.host import start_backend

    _ensure_management_port_available()

    if BasicConfig.is_host_mode:
        from unilabos.server.services.driver_packages import get_driver_package_service
        state = get_driver_package_service().mutation_gate.status()
        if state['unresolved_operations'] or state['error']:
            print_status('包操作恢复尚未人工确认：仅启动管理服务，设备 runtime 与托管 slave 保持停止；确认后手动重启 Host。', 'warning')
            # 设备 runtime 不会启动：关掉进程启动时提前打开的 HostLink 端口，别让 Slave 空等
            from unilabos.client.host.hostlink.startup import release_prebound_host_server

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

    _join_backend_until_stopped(backend_thread)
    _exit_on_backend_failure()


__all__ = ["build_slave_launch_command", "print_slave_launch_hint", "run_runtime"]
