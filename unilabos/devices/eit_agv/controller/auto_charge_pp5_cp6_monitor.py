# coding: utf-8
"""
功能:
    PP5/CP6自动充电监控独立入口脚本.
    持续监控AGV所在位置, 当AGV位于PP5或CP6时按待命充电规则执行检查.
    支持命令行参数配置检查间隔和重试间隔.

用法:
    python -m eit_agv.controller.auto_charge_pp5_cp6_monitor
    python -m eit_agv.controller.auto_charge_pp5_cp6_monitor --interval-hours 2 --retry-minutes 10
"""

import argparse
import logging
import os
import sys

from .agv_controller import AGVController


def _setup_logging(log_dir: str) -> None:
    """
    功能:
        配置日志系统, 同时输出到控制台和日志文件.
    参数:
        log_dir: 日志文件所在目录.
    返回:
        无.
    """
    log_file = os.path.join(log_dir, "auto_charge_pp5_cp6_monitor.log")

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.info(f"日志文件路径: {log_file}")


def _parse_args() -> argparse.Namespace:
    """
    功能:
        解析命令行参数.
    返回:
        argparse.Namespace, 包含interval_hours和retry_minutes.
    """
    parser = argparse.ArgumentParser(
        description="AGV PP5/CP6自动充电监控, 根据待命点和电量阈值切换PP5与CP6"
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=1.0,
        metavar="HOURS",
        help="检查成功后的等待时间(小时), 默认1.0",
    )
    parser.add_argument(
        "--retry-minutes",
        type=float,
        default=5.0,
        metavar="MINUTES",
        help="检查跳过或出错后的重试间隔(分钟), 默认5.0",
    )
    return parser.parse_args()


def main() -> None:
    """
    功能:
        主入口, 初始化日志和控制器后启动PP5/CP6充电监控循环.
    参数:
        无.
    返回:
        无.
    """
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    _setup_logging(log_dir)

    logger = logging.getLogger(__name__)
    args = _parse_args()
    logger.info(
        f"PP5/CP6自动充电监控启动 | 检查间隔: {args.interval_hours}小时 | "
        f"重试间隔: {args.retry_minutes}分钟"
    )

    try:
        controller = AGVController()
        controller.auto_charge_pp5_cp6_loop(
            interval_hours=args.interval_hours,
            retry_wait_minutes=args.retry_minutes,
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号, PP5/CP6自动充电监控已退出")
    except Exception as e:
        logger.exception(f"PP5/CP6自动充电监控发生未处理异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
