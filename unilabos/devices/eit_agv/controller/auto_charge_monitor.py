# coding: utf-8
"""
功能:
    AGV自动充电监控独立入口脚本
    持续监控AGV所在位置, 当AGV在充电站CP6时自动执行电量检查与充电
    支持命令行参数配置检查间隔和重试间隔

用法:
    python -m eit_agv.controller.auto_charge_monitor
    python -m eit_agv.controller.auto_charge_monitor --interval-hours 2 --retry-minutes 10
"""

import argparse
import logging
import os
import sys

from .agv_controller import AGVController


def _setup_logging(log_dir: str) -> None:
    """
    功能:
        配置日志系统, 同时输出到控制台和日志文件
    参数:
        log_dir: 日志文件所在目录
    返回:
        无
    """
    log_file = os.path.join(log_dir, "auto_charge_monitor.log")

    # 统一格式: 时间 级别 模块 消息
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 文件处理器, 追加模式, UTF-8编码
    file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    # 控制台处理器
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
        解析命令行参数
    返回:
        argparse.Namespace, 包含interval_hours和retry_minutes
    """
    parser = argparse.ArgumentParser(
        description="AGV自动充电监控 - 当AGV在CP6充电站时自动检查并执行充电"
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=1.0,
        metavar="HOURS",
        help="AGV在CP6完成充电检查后的等待时间(小时), 默认1.0"
    )
    parser.add_argument(
        "--retry-minutes",
        type=float,
        default=5.0,
        metavar="MINUTES",
        help="AGV不在CP6时的重试查询间隔(分钟), 默认5.0"
    )
    return parser.parse_args()


def main() -> None:
    """
    功能:
        主入口, 初始化日志和控制器后启动充电监控循环
    返回:
        无
    """
    # 日志文件存放在 eit_agv/data 目录
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    _setup_logging(log_dir)

    logger = logging.getLogger(__name__)

    args = _parse_args()
    logger.info(
        f"自动充电监控启动 | 检查间隔: {args.interval_hours}小时 | "
        f"不在CP6时重试间隔: {args.retry_minutes}分钟"
    )

    try:
        controller = AGVController()
        controller.auto_charge_loop(
            interval_hours=args.interval_hours,
            retry_wait_minutes=args.retry_minutes
        )
    except KeyboardInterrupt:
        logger.info("收到中断信号, 自动充电监控已退出")
    except Exception as e:
        logger.exception(f"自动充电监控发生未处理异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
