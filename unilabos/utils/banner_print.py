"""
横幅和UI打印工具

提供用于显示彩色横幅、状态信息和其他UI元素的工具函数。
"""

import os
import platform
from datetime import datetime
import importlib.metadata
from typing import Dict, Any


# ANSI颜色代码
class Colors:
    """ANSI颜色代码集合"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    COLOR_HEAD = "\033[38;5;214m"
    COLOR_DECO = "\033[38;5;242m"
    COLOR_TAIL = "\033[38;5;220m"

    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"


def get_version() -> str:
    """
    获取ilabos的版本号

    通过importlib.metadata尝试获取包版本。
    如果失败，返回开发版本号。

    Returns:
        版本号字符串
    """
    try:
        return importlib.metadata.version("unilabos")
    except importlib.metadata.PackageNotFoundError:
        return "dev-0.1.0"  # 开发版本


def print_unilab_banner(args_dict: Dict[str, Any], show_config: bool = True) -> None:
    """
    打印UNI LAB启动横幅

    Args:
        args_dict: 命令行参数字典
        show_config: 是否显示配置信息
    """
    # 检测终端是否支持ANSI颜色
    if platform.system() == "Windows":
        os.system("")  # 启用Windows终端中的ANSI支持

    # 获取当前时间
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 获取版本号
    version = get_version()

    # 构建UNI LAB字符艺术
    banner = f"""{Colors.COLOR_HEAD}
  ██╗   ██╗ ███╗   ██╗ ██╗     ██╗      █████╗  ██████╗ {Colors.COLOR_TAIL}
  ██║   ██║ ████╗  ██║ ██║     ██║     ██╔══██╗ ██╔══██╗
  ██║   ██║ ██╔██╗ ██║ ██║     ██║     ███████║ ██████╔╝
  ██║   ██║ ██║╚██╗██║ ██║     ██║     ██╔══██║ ██╔══██╗
  ╚██████╔╝ ██║ ╚████║ ██║     ██████╗ ██║  ██║ ██████╔╝{Colors.COLOR_DECO}
    ╚════╝  ╚═╝  ╚═══╝ ╚═╝     ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ {Colors.RESET}"""

    # 显示版本信息
    system_info = f"""
{Colors.YELLOW}Version:{Colors.RESET} {Colors.BRIGHT_GREEN}{version}{Colors.RESET}
{Colors.YELLOW}System:{Colors.RESET} {Colors.WHITE}{platform.system()} {platform.release()}{Colors.RESET}
{Colors.YELLOW}Python:{Colors.RESET} {Colors.WHITE}{platform.python_version()}{Colors.RESET}
{Colors.YELLOW}Time:{Colors.RESET} {Colors.WHITE}{current_time}{Colors.RESET}
{Colors.BRIGHT_WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}"""

    # 打印横幅和系统信息
    print(banner + system_info)

    # 如果需要，显示配置信息
    if show_config and args_dict:
        print_config(args_dict)


def print_config(args_dict: Dict[str, Any]) -> None:
    """
    打印配置信息

    Args:
        args_dict: 命令行参数字典
    """
    config_info = f"{Colors.BRIGHT_BLUE}{Colors.BOLD}Configuration:{Colors.RESET}\n"

    # 后端信息
    if "backend" in args_dict:
        config_info += f"{Colors.CYAN}• Backend:{Colors.RESET} "
        config_info += f"{Colors.WHITE}{args_dict['backend']}{Colors.RESET}\n"

    # 主机模式
    if "without_host" in args_dict:
        mode = "Slave" if args_dict["without_host"] else "Master"
        config_info += f"{Colors.CYAN}• Host Mode:{Colors.RESET} {Colors.WHITE}{mode}{Colors.RESET}\n"

    # 如果有图或设备信息，显示它们
    if "graph" in args_dict and args_dict["graph"] is not None:
        config_info += f"{Colors.CYAN}• Graph:{Colors.RESET} "
        config_info += f"{Colors.WHITE}{args_dict['graph']}{Colors.RESET}\n"
    elif "devices" in args_dict and args_dict["devices"] is not None:
        config_info += f"{Colors.CYAN}• Devices:{Colors.RESET} "
        config_info += f"{Colors.WHITE}{args_dict['devices']}{Colors.RESET}\n"
        if "resources" in args_dict and args_dict["resources"] is not None:
            config_info += f"{Colors.CYAN}• Resources:{Colors.RESET} "
            config_info += f"{Colors.WHITE}{args_dict['resources']}{Colors.RESET}\n"

    # 控制器配置
    if "controllers" in args_dict and args_dict["controllers"] is not None:
        config_info += f"{Colors.CYAN}• Controllers:{Colors.RESET} "
        config_info += f"{Colors.WHITE}{args_dict['controllers']}{Colors.RESET}\n"

    # 打印结束分隔线
    config_info += f"{Colors.BRIGHT_WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}\n"

    print(config_info)


def print_status(message: str, status_type: str = "info") -> None:
    """
    打印带颜色的状态消息

    Args:
        message: 要打印的消息
        status_type: 状态类型（'info', 'success', 'warning', 'error'）
    """
    color = Colors.WHITE
    prefix = ""

    if status_type == "info":
        color = Colors.BLUE
        prefix = "INFO"
    elif status_type == "success":
        color = Colors.GREEN
        prefix = "SUCCESS"
    elif status_type == "warning":
        color = Colors.YELLOW
        prefix = "WARNING"
    elif status_type == "error":
        color = Colors.RED
        prefix = "ERROR"

    print(f"{color}[{prefix}]{Colors.RESET} {message}")
