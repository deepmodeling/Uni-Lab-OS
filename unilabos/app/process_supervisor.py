"""产品进程监督器：只管理子进程重启，不装配任何领域运行时。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from unilabos.utils.banner_print import print_status

RESTART_EXIT_CODE = 42


def build_child_argv() -> list[str]:
    """构造移除监督器专用参数的产品子进程命令行。

    参数：无；读取当前 ``sys.argv``。
    返回：保留普通产品参数、删除 ``restart_mode`` 与最大重启次数的字符串列表。
    异常：无；未知参数保持原样，由产品子进程自己的解析器处理。
    """

    # ``child_arguments`` 是最终交给产品子进程的完整参数，不包含监督器自身开关。
    child_arguments: list[str] = []
    # ``skip_next_value`` 表示前一个参数是需要连同值一起删除的最大重启次数选项。
    skip_next_value = False
    for argument in sys.argv:
        if skip_next_value:
            skip_next_value = False
            continue
        if argument in ("--restart_mode", "--restart-mode"):
            continue
        if argument in ("--auto_restart_count", "--auto-restart-count"):
            skip_next_value = True
            continue
        if argument.startswith(
            ("--auto_restart_count=", "--auto-restart-count=")
        ):
            continue
        child_arguments.append(argument)
    return child_arguments


def run_as_supervisor(max_restarts: int) -> None:
    """运行只负责启动和重启产品子进程的监督器。

    参数：``max_restarts`` 是收到专用重启退出码后允许的最大重启次数。
    返回：正常路径不返回；子进程普通退出、超过次数或人工中断时以对应状态结束
    监督器进程。
    异常：子进程创建失败时传播 ``OSError``；人工中断会先终止已经创建的子进程，
    若中断发生在创建完成前则直接安全退出，不访问未绑定进程。
    不变量：监督器自身不加载设备、工作流（Workflow）或物料（Material）运行时。
    """

    # ``child_command`` 固定当前 Python 解释器和已经清理的产品参数。
    child_command = [sys.executable, *build_child_argv()]
    # ``completed_restart_count`` 只统计子进程明确请求的安全重启。
    completed_restart_count = 0

    print_status(
        f"[监督器] 已启用重启模式（最多重启 {max_restarts} 次），"
        f"子进程命令：{' '.join(child_command)}",
        "info",
    )

    while True:
        print_status(
            "[监督器] 正在启动产品子进程"
            f"（{completed_restart_count}/{max_restarts}）",
            "info",
        )
        # ``child_process`` 在创建完成前保持空值，保证人工中断不会访问未绑定变量。
        child_process: subprocess.Popen[bytes] | None = None
        try:
            # ``child_environment`` 明确告诉子进程存在监督器；子进程仍只能在确认
            # 执行安全后请求重启，不能自行建立第二个监督循环。
            child_environment = os.environ.copy()
            child_environment["UNILABOS_RESTART_SUPERVISED"] = "1"
            child_process = subprocess.Popen(
                child_command,
                env=child_environment,
            )
            # ``child_exit_code`` 是本轮子进程唯一结算结果。
            child_exit_code = child_process.wait()
        except KeyboardInterrupt:
            print_status("[监督器] 收到人工中断，正在终止产品子进程", "info")
            if child_process is not None:
                child_process.terminate()
                try:
                    child_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child_process.kill()
                    child_process.wait()
            raise SystemExit(1) from None

        if child_exit_code == RESTART_EXIT_CODE:
            completed_restart_count += 1
            if completed_restart_count > max_restarts:
                print_status(
                    f"[监督器] 已达到最大重启次数 {max_restarts}，停止运行",
                    "warning",
                )
                raise SystemExit(1)
            print_status(
                "[监督器] 产品子进程请求安全重启"
                f"（{completed_restart_count}/{max_restarts}），2 秒后重启",
                "info",
            )
            time.sleep(2)
            continue

        if child_exit_code != 0:
            print_status(
                f"[监督器] 产品子进程以状态码 {child_exit_code} 退出",
                "warning",
            )
        else:
            print_status("[监督器] 产品子进程正常退出", "info")
        raise SystemExit(child_exit_code)


__all__ = ["RESTART_EXIT_CODE", "build_child_argv", "run_as_supervisor"]
