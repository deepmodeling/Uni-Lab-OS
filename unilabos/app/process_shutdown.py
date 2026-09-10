"""OS Host 进程正常信号退出时的显式服务清理。"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterable
from types import FrameType
from typing import Any

from unilabos.utils import logger

ShutdownHandler = Callable[[int, FrameType | None], None]


def install_host_shutdown_handlers(
    communication_clients: Iterable[Any],
) -> ShutdownHandler:
    """为 Host 进程安装会显式关闭网络所有权的正常退出处理器。

    参数说明：``communication_clients`` 是本进程已经启动的远端通信客户端；局部
    ``clients`` 冻结安装时的所有权集合，``failures`` 汇总清理错误。返回：同时
    注册到 ``SIGINT`` 和 ``SIGTERM`` 的处理器，便于进程级测试验证。异常：安装
    不是在主线程执行时由 ``signal.signal`` 抛出；处理器总以 ``SystemExit(0)``
    结束正常退出，即使单个客户端停止失败也继续关闭 Edge 服务和 ROS2 定向发现。
    """

    clients = tuple(communication_clients)

    def _shutdown(signum: int, frame: FrameType | None) -> None:
        """停止进程拥有的通信、调度和 ROS2 网络服务后退出。

        参数说明：``signum`` 是收到的正常退出信号，``frame`` 是 Python 提供的
        可选当前栈帧；局部 ``failures`` 保存不能阻止后续清理的异常。返回：永不
        正常返回，最终抛出 ``SystemExit(0)``；重复信号先被忽略，避免清理重入。
        """

        # 清理期间忽略第二个正常退出信号，避免发现服务刚收到 TERM 又被重入关闭。
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        failures: list[BaseException] = []
        for communication_client in clients:
            try:
                communication_client.stop()
            except BaseException as error:  # noqa: BLE001 - 必须继续关闭网络所有权
                failures.append(error)
        try:
            from unilabos.package_manager import close_workspace_product_lifecycle

            close_workspace_product_lifecycle()
        except BaseException as error:  # noqa: BLE001 - 必须继续关闭设备与网络
            failures.append(error)
        try:
            from unilabos.workflow.composition import shutdown_workflow_runtime

            shutdown_workflow_runtime()
        except BaseException as error:  # noqa: BLE001 - 必须继续关闭 Edge 存储
            failures.append(error)
        try:
            from unilabos.app.scheduler.integration import shutdown_edge_services

            shutdown_edge_services()
        except BaseException as error:  # noqa: BLE001 - 正常退出仍需完成其余报告
            failures.append(error)
        try:
            from unilabos.app.runtime_storage import close_runtime_storage_session

            close_runtime_storage_session()
        except BaseException as error:  # noqa: BLE001 - 退出前仍需汇总全部错误
            failures.append(error)
        for error in failures:
            logger.error(
                "Host process shutdown cleanup failed after signal %s: %s",
                signum,
                error,
            )
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    return _shutdown


__all__ = ["ShutdownHandler", "install_host_shutdown_handlers"]
