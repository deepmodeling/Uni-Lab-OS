"""调度权威进程：Scheduler / Workflow Authority / Registry Authority / Materials / 管理端口。

两种形态共用本模块：

``unilab``（默认）
    ``unilab`` 进程本身就是权威：持有 ``--port`` 管理端口（浏览器连这里），四库落
    ``<root>``，并直接把 Host 作为子进程拉起、看护。Host **不监听任何端口**：它只主动
    发 HTTP（``--address`` 指回本进程：物料 / 注册表 / 工作流上报、请求结果回送）并维持一条
    控制 WS；浏览器需要的 Host 专有路由由 ``edge_proxy`` 经 WS 交给 Host 在进程内执行。
    安静点重启只重启 Host 子进程，调度状态与前端连接都不受影响；权威自己常驻。

``unilab --role backend``
    只起权威，不带 Host；Edge 进程用 ``--address`` 接入（可以在另一台机器），同样只需
    出向连接。四库落 ``<root>/backend``，与同机 Edge 的库完全隔离，避免 SQLite 跨进程并发写。
"""

from __future__ import annotations

import os
import signal
import socket
import sys
from typing import Any, Dict, Optional

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.utils.banner_print import print_status


def _authority_listening(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _stop_on_signal(signum: int, frame: Any) -> None:
    """SIGTERM / SIGHUP：与 Ctrl+C 走同一条有序停机路径。

    uvicorn 服务期间这两个信号由它自己接住；它优雅停机后会恢复原处理器并**重新抛出**收到的
    信号——原处理器若是默认动作，进程就在那一刻被信号直接杀掉，下面 ``finally`` 里停 Host
    子进程、关库都不会执行，Host 子进程成了孤儿。所以权威给这两个信号装上本处理器：uvicorn
    还在服务就请它停机，否则（启动阶段、uvicorn 停完后重新抛出）抛 KeyboardInterrupt 让主线程
    沿 ``finally`` 收尾。
    """

    from unilabos.server.api.app import request_server_shutdown

    # 信号处理器里不打印：主线程可能正持有 stdout 的缓冲锁，重入写会抛 RuntimeError
    if request_server_shutdown():
        return
    raise KeyboardInterrupt


def _install_stop_signals() -> None:
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _stop_on_signal)
    # 控制台 Ctrl+Break（含关闭控制台窗口）也走 Ctrl+C 的优雅停机路径（uvicorn 停机后会重新抛出信号）
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.default_int_handler)


def run_backend_process(
    args_dict: Dict[str, Any],
    registry: Any,
    working_dir: str,
    *,
    launch_host: bool = False,
) -> None:
    """装配并阻塞运行调度权威进程；返回即进程退出。

    ``launch_host=True`` 时同时看护 Host 子进程（默认拓扑）。
    """

    from unilabos.backend.hostlink.downlink import (
        configure_remote_device_relay,
        remote_material_sync_dispatcher,
    )
    from unilabos.client.materials import LocalMaterialsClient
    from unilabos.resources.adapters.registry_materials import sync_registry_resources
    from unilabos.server.api.app import (
        ManagementPortInUseError,
        ensure_port_available,
        setup_server,
        start_server,
    )
    from unilabos.server.api.edge_proxy import configure_edge_proxy
    from unilabos.server.api.runtime import install_edge_control_api
    from unilabos.server.backend.composition import (
        set_materials_gateway,
        setup_local_scheduler,
        setup_materials_service,
        shutdown_backend_services,
    )
    from unilabos.server.backend.edge_control import (
        EdgeControlService,
        set_edge_control_service,
    )
    from unilabos.server.database import claim_database_root
    from unilabos.server.services.runtime.registry import get_registry_service
    from unilabos.server.startup import (
        BACKEND_DATABASE_DIRNAME,
        configured_database_root,
        resolve_database_paths,
    )

    # 最先装停机信号：启动阶段收到 SIGTERM 也要沿 finally 收尾（停已拉起的 Host、关库）
    _install_stop_signals()
    base_root = os.path.expanduser(
        configured_database_root(args_dict, working_dir=working_dir)
    )
    if launch_host:
        # 默认拓扑：权威沿用原来的库位置（老数据不搬家），Host 子进程用 <root>/edge。
        authority_root = base_root
        edge_root = os.path.join(base_root, "edge")
        port = int(args_dict.get("port_management") or BasicConfig.port)
        try:
            ensure_port_available("0.0.0.0", port)
        except ManagementPortInUseError as exc:
            print_status(exc.strerror or str(exc), "error")
            raise SystemExit(1) from exc
    else:
        authority_root = os.path.join(base_root, BACKEND_DATABASE_DIRNAME)
        edge_root = ""
        port = int(args_dict.get("port_management") or HTTPConfig.backend_port)
    args_dict["server_database_root"] = authority_root
    paths = resolve_database_paths(args_dict, working_dir=working_dir)
    # 先独占权威自己的根目录再开库（启动入口已取过时是同一把锁）。<根目录>/edge 归 Host 子进程，
    # 由子进程自己取锁，权威运行期间不碰它。
    claim_database_root(paths.root, role="调度权威" if launch_host else "调度权威（--role backend）")

    from unilabos.server.backend.reset import ResetController, ResetConflict, configure_reset
    if (paths.root / "reset-pending.json").exists():
        raise RuntimeError("上次全量重置未完成；请根据 reset-pending.json 恢复或完成归档后再启动")
    reset_controller = None

    host_child: Optional[Any] = None
    unsubscribe_shutdown: Optional[Any] = None
    unsubscribe_edge_waiters: Optional[Any] = None
    from unilabos.utils.log_notices import set_local_log_source

    previous_log_source = set_local_log_source(None)
    try:
        materials_service = setup_materials_service(database_paths=paths)
        materials_gateway = LocalMaterialsClient(materials_service)
        # 权威没有设备：Host 专有路由、transfer 的 unload/load 投影、前端物料变更通知
        # 都经控制面 WS 交给 Host 在进程内执行
        configure_edge_proxy(True)
        configure_remote_device_relay(True)
        materials_service.set_resource_sync_dispatcher(remote_material_sync_dispatcher)
        template_report = sync_registry_resources(registry, materials_gateway)
        set_materials_gateway(materials_gateway)
        print_status(
            f"物料权威已就绪: {paths.materials_db} "
            f"({template_report.resource_count} 个资源模板)",
            "info",
        )

        # 调度权威 + Registry Authority 同一步装配（与默认 Host 同一函数）；
        # Registry Authority 接收 Edge 的全量快照并按条目维护版本。
        edge_control = EdgeControlService()
        set_edge_control_service(edge_control)

        setup_local_scheduler(backend=edge_control)
        registry_service = get_registry_service()
        if registry_service is None:
            raise RuntimeError("registry authority must be ready after local scheduler setup")
        active_entries = len(registry_service.list_entries(status="active"))
        print_status(
            f"注册表权威已就绪: {paths.runtime_db}"
            + (
                f"（{active_entries} 个生效条目）"
                if active_entries
                else "（等待 Host 首次上报）"
            ),
            "info",
        )
        print_status(
            f"调度权威已就绪: {paths.runtime_db}（executor=runtime.v1 控制面远程下发）",
            "info",
        )

        from unilabos.config.control_token import TOKEN_ENV, ensure_authority_token

        control_token, token_file = ensure_authority_token(working_dir)
        print_status(
            "控制面 token："
            + (f"{token_file}" if token_file is not None else "来自配置 BasicConfig.control_token")
            + "（远端 Host 需配置同一 token：BasicConfig.control_token 或环境变量 "
            + f"{TOKEN_ENV}）",
            "info",
        )
        app = setup_server()
        install_edge_control_api(app)

        if launch_host:
            from unilabos.app.supervisor import HostChildSupervisor, host_child_command

            host_child = HostChildSupervisor(
                host_child_command(
                    sys.argv[1:],
                    authority_port=port,
                    database_root=edge_root,
                ),
                # Host 子进程经环境变量拿到控制面 token，无需另配
                env={**os.environ, TOKEN_ENV: control_token},
                ready_probe=lambda: _authority_listening(port),
            )
            host_child.start()
            def reset_preflight() -> None:
                from unilabos.server.backend.composition import get_scheduler
                from unilabos.server.backend.lifecycle import active_device_job_ids
                from unilabos.server.backend.restart import get_restart_coordinator

                restart = get_restart_coordinator().status()
                if restart["pending"] or restart["restarting"]:
                    raise ResetConflict("请等待当前重启完成")
                scheduler = get_scheduler()
                scheduler.pause_dispatch(reason="reset")
                try:
                    if active_device_job_ids(scheduler, None):
                        raise ResetConflict("仍有活跃作业，请先结束或裁决作业后重置")
                    from unilabos.client.utils.envelope import EnvelopeError
                    from unilabos.server.api.edge_proxy import edge_response_data

                    def host_data(path: str, unavailable: str) -> dict:
                        response = edge_control.http_request("GET", path)
                        if response is None:
                            raise ResetConflict(unavailable)
                        try:
                            data = edge_response_data(response)
                        except (EnvelopeError, ValueError) as exc:
                            raise ResetConflict(f"{unavailable}：{exc}") from exc
                        return data if isinstance(data, dict) else {}

                    peer_status = host_data("/api/v1/hostlink/peers", "无法核对 Host 状态，请等待 Host 上线")
                    if any(peer.get("online", False) for peer in peer_status.get("peers", [])):
                        raise ResetConflict("仍有在线 Slave，请先停止全部受管设备进程及外部 Slave 后重置")
                    inventory = host_data("/api/v1/driver-packages", "无法核对驱动包操作状态")
                    if any(op.get("status") == "running" for op in inventory.get("operations", [])):
                        raise ResetConflict("请等待驱动包安装或卸载完成")
                except Exception as exc:
                    scheduler.resume_dispatch(reason="reset")
                    if isinstance(exc, ResetConflict):
                        raise
                    raise ResetConflict(f"无法确认安全重置条件：{exc}") from exc

            reset_controller = ResetController(paths, working_dir, reset_preflight)
            configure_reset(reset_controller)
            # 权威一收到 Ctrl+C / SIGTERM 就请 Host 开始退出，两边并行收尾；
            # finally 里的 stop() 只负责等它结束（超时强制结束）。
            from unilabos.server import lifecycle

            unsubscribe_shutdown = lifecycle.on_shutdown(host_child.request_stop)
            print_status(
                f"调度权威进程就绪：管理端口 {port} 常驻；Host 作为子进程运行"
                f"（不监听端口，经控制面 WS 受控，四库 {edge_root}），"
                "安静点重启只重启 Host（--no-safe-restart 可退回单进程）",
                "info",
            )
        else:
            print_status(
                f"Backend 进程已启动: 管理 API/WS 0.0.0.0:{port}；"
                f"Edge 侧配置 --address http://127.0.0.1:{port} 接入（Edge 只需出向连接）",
                "info",
            )
        from unilabos.server.services.process_management import configure_process_management

        # V1 进程管理快照 / 操作的运行形态：本进程是监督者，Host 子进程由 host_child 看护
        configure_process_management(
            management_port=port,
            supervisor=host_child,
            database_root=str(paths.root),
            edge_database_root=edge_root,
        )
        # 停机一开始就让等 Host 回 backend_http 的请求失败返回：浏览器请求、调度线程不必等到超时，
        # uvicorn 的优雅停机与事件循环的线程池收尾也就不会被这些等待拖住
        from unilabos.server import lifecycle as server_lifecycle

        unsubscribe_edge_waiters = server_lifecycle.on_shutdown(edge_control.fail_pending_http_requests)
        try:
            start_server(
                host="0.0.0.0",
                port=port,
                open_browser=launch_host and not BasicConfig.disable_browser,
            )
        except ManagementPortInUseError as exc:
            print_status(exc.strerror or str(exc), "error")
            raise SystemExit(1) from exc
        except KeyboardInterrupt:
            print_status("收到中断，正在停止调度权威进程", "info")
    finally:
        from unilabos.server.services.process_management import configure_process_management

        configure_process_management()
        set_local_log_source(previous_log_source)
        if unsubscribe_edge_waiters is not None:
            unsubscribe_edge_waiters()
        if unsubscribe_shutdown is not None:
            unsubscribe_shutdown()
        if host_child is not None:
            print_status("正在停止 Host 子进程", "info")
            host_child.stop()
        set_edge_control_service(None)
        configure_edge_proxy(False)
        configure_remote_device_relay(False)
        # RegistryService 共享 RuntimeService 的连接，由组合根统一关闭。
        shutdown_backend_services()
        if reset_controller is not None and reset_controller.pending:
            if host_child is not None and host_child.alive():
                raise RuntimeError("Host 未退出，拒绝清空数据库")
            # Host 子进程已停且不再拉起：归档前独占 <根目录>/edge，确认没有遗留的 Host 进程还开着这些库
            edge_lock = claim_database_root(edge_root, role="调度权威（全量重置归档）", wait_s=5.0)
            try:
                reset_controller.finish()
            finally:
                edge_lock.release()
            print_status(f"全部数据已移至 {reset_controller.backup}；重置完成。请不带旧 -g 重新启动 unilab。", "info")
        configure_reset(None)

    from unilabos.server.backend.restart import exit_if_restart_requested

    exit_if_restart_requested()


__all__ = ["run_backend_process"]
