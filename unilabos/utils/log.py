import logging
import os
import platform
from datetime import datetime
import ctypes
import atexit
import inspect
from typing import Tuple, cast

# 添加TRACE级别到logging模块
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")


class CustomRecord:
    custom_stack_info: Tuple[str, int, str, str]


# Windows颜色支持
if platform.system() == "Windows":
    # 尝试启用Windows终端的ANSI支持
    kernel32 = ctypes.windll.kernel32
    # 获取STD_OUTPUT_HANDLE
    STD_OUTPUT_HANDLE = -11
    # 启用ENABLE_VIRTUAL_TERMINAL_PROCESSING
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    # 获取当前控制台模式
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    # 启用ANSI处理
    kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    # 程序退出时恢复控制台设置
    @atexit.register
    def reset_console():
        kernel32.SetConsoleMode(handle, mode.value)


# 定义不同日志级别的颜色
class ColoredFormatter(logging.Formatter):
    """自定义日志格式化器，支持颜色输出"""

    # ANSI 颜色代码
    COLORS = {
        "RESET": "\033[0m",  # 重置
        "BOLD": "\033[1m",  # 加粗
        "GRAY": "\033[37m",  # 灰色
        "WHITE": "\033[97m",  # 白色
        "BLACK": "\033[30m",  # 黑色
        "TRACE_LEVEL": "\033[1;90m",  # 加粗深灰色
        "DEBUG_LEVEL": "\033[1;36m",  # 加粗青色
        "INFO_LEVEL": "\033[1;32m",  # 加粗绿色
        "WARNING_LEVEL": "\033[1;33m",  # 加粗黄色
        "ERROR_LEVEL": "\033[1;31m",  # 加粗红色
        "CRITICAL_LEVEL": "\033[1;35m",  # 加粗紫色
        "TRACE_TEXT": "\033[90m",  # 深灰色
        "DEBUG_TEXT": "\033[37m",  # 灰色
        "INFO_TEXT": "\033[97m",  # 白色
        "WARNING_TEXT": "\033[33m",  # 黄色
        "ERROR_TEXT": "\033[31m",  # 红色
        "CRITICAL_TEXT": "\033[35m",  # 紫色
        "DATE": "\033[37m",  # 日期始终使用灰色
    }

    def __init__(self, use_colors=True, microseconds=False, show_thread=False):
        super().__init__()
        # 强制启用颜色
        self.use_colors = use_colors
        # microseconds: 保留微秒级时间戳（默认毫秒），便于精确排查时序
        self.microseconds = microseconds
        # show_thread: 输出线程名，便于区分 queue/收发等并发逻辑
        self.show_thread = show_thread

    def _format_datetime(self, record) -> str:
        """构建时间戳字符串，可选微秒级精度"""
        datetime_str = datetime.fromtimestamp(record.created).strftime("%y-%m-%d [%H:%M:%S,%f")
        if not self.microseconds:
            datetime_str = datetime_str[:-3]  # 截断到毫秒
        return datetime_str + "]"

    def _format_right_info(self, record) -> str:
        """构建右侧的线程/函数/模块定位信息"""
        filename = record.filename.replace(".py", "").split("\\")[-1]  # 提取文件名（不含路径和扩展名）
        if "/" in filename:
            filename = filename.split("/")[-1]
        module_path = f"{record.name}.{filename}"
        func_line = f"{record.funcName}:{record.lineno}"
        thread_part = f" [{record.threadName}]" if self.show_thread else ""
        return f"{thread_part} [{func_line}] [{module_path}]"

    def format(self, record):
        # 检查是否有自定义堆栈信息
        if hasattr(record, "custom_stack_info") and record.custom_stack_info:  # type: ignore
            r = cast(CustomRecord, record)
            frame_info = r.custom_stack_info
            record.filename = frame_info[0]
            record.lineno = frame_info[1]
            record.funcName = frame_info[2]
            if len(frame_info) > 3:
                record.name = frame_info[3]
        if not self.use_colors:
            return self._format_basic(record)

        level_color = self.COLORS.get(f"{record.levelname}_LEVEL", self.COLORS["WHITE"])
        text_color = self.COLORS.get(f"{record.levelname}_TEXT", self.COLORS["WHITE"])
        date_color = self.COLORS["DATE"]
        reset = self.COLORS["RESET"]

        # 日期格式
        datetime_str = self._format_datetime(record)

        # 线程、模块和函数信息
        right_info = self._format_right_info(record)

        # 主要消息
        main_msg = record.getMessage()

        # 构建基本消息格式
        formatted_message = (
            f"{date_color}{datetime_str}{reset} "
            f"{level_color}[{record.levelname}]{reset} "
            f"{text_color}{main_msg}"
            f"{date_color}{right_info}{reset}"
        )

        # 处理异常信息
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            if formatted_message[-1:] != "\n":
                formatted_message = formatted_message + "\n"
            formatted_message = formatted_message + text_color + exc_text + reset
        elif record.stack_info:
            if formatted_message[-1:] != "\n":
                formatted_message = formatted_message + "\n"
            formatted_message = formatted_message + text_color + self.formatStack(record.stack_info) + reset

        return formatted_message

    def _format_basic(self, record):
        """基本格式化，不包含颜色"""
        datetime_str = self._format_datetime(record)
        right_info = self._format_right_info(record)

        formatted_message = f"{datetime_str} [{record.levelname}] {record.getMessage()}{right_info}"

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            if formatted_message[-1:] != "\n":
                formatted_message = formatted_message + "\n"
            formatted_message = formatted_message + exc_text
        elif record.stack_info:
            if formatted_message[-1:] != "\n":
                formatted_message = formatted_message + "\n"
            formatted_message = formatted_message + self.formatStack(record.stack_info)

        return formatted_message

    def formatException(self, exc_info):
        """重写异常格式化，确保异常信息保持正确的格式和颜色"""
        # 获取标准的异常格式化文本
        formatted_exc = super().formatException(exc_info)
        return formatted_exc


def _to_numeric_level(loglevel, default=logging.DEBUG) -> int:
    """将日志级别(字符串/常量)统一转换为数字级别。

    Args:
        loglevel: 'TRACE'/'DEBUG'/'INFO'/... 字符串，或 logging 常量，或 None
        default: 解析失败或为 None 时使用的默认级别
    """
    if loglevel is None:
        return default
    if isinstance(loglevel, str):
        if loglevel.upper() == "TRACE":
            return TRACE_LEVEL
        numeric_level = getattr(logging, loglevel.upper(), None)
        if not isinstance(numeric_level, int):
            print(f"警告: 无效的日志级别 '{loglevel}'，使用默认级别 DEBUG")
            return default
        return numeric_level
    return loglevel


# 配置日志处理器
def configure_logger(loglevel=None, working_dir=None):
    """配置日志记录器

    Args:
        loglevel: 日志级别，可以是字符串（'TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'）
                 或logging模块的常量（如logging.DEBUG）或TRACE_LEVEL
    """
    # 获取根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(TRACE_LEVEL)
    # 设置日志级别
    numeric_level = _to_numeric_level(loglevel)

    # 移除已存在的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)  # 使用与根记录器相同的级别

    # 使用自定义的颜色格式化器
    color_formatter = ColoredFormatter()
    console_handler.setFormatter(color_formatter)

    # 添加处理器到根日志记录器
    root_logger.addHandler(console_handler)

    # 如果指定了工作目录，添加文件处理器
    log_filepath = None
    if working_dir is not None:
        logs_dir = os.path.join(working_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        # 生成日志文件名：日期 时间.log
        log_filename = datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".log"
        log_filepath = os.path.join(logs_dir, log_filename)

        # 创建文件处理器
        file_handler = logging.FileHandler(log_filepath, encoding="utf-8")
        file_handler.setLevel(TRACE_LEVEL)

        # 使用不带颜色的格式化器
        file_formatter = ColoredFormatter(use_colors=False)
        file_handler.setFormatter(file_formatter)

        root_logger.addHandler(file_handler)

    logging.getLogger("asyncio").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)
    return log_filepath


# ============================================================================
# 服务端通信(WebSocket)独立日志
# 单独成文件、全量保留到本地、微秒级时间戳 + 线程名，便于排查通信/queue 时序问题
# ============================================================================
COMM_LOGGER_NAME = "unilabos.comm"
_comm_file_handler: "logging.Handler | None" = None  # 便于重启时清理 websockets 库 handler


def _attach_trace_method(target_logger: logging.Logger) -> logging.Logger:
    """为指定 logger 附加 .trace 方法，行为与模块级 trace 一致。

    通过 stacklevel=2 跳过本包装函数，使日志定位到真实调用处而非此处。
    """
    if not hasattr(target_logger, "trace"):
        def _trace(msg, *args, _lg=target_logger, **kwargs):
            kwargs.setdefault("stacklevel", 2)
            _lg.log(TRACE_LEVEL, msg, *args, **kwargs)

        target_logger.trace = _trace  # type: ignore[attr-defined]
    return target_logger


def get_comm_logger() -> logging.Logger:
    """获取通信专用 logger。

    未调用 ``configure_comm_logger`` 之前，该 logger 没有独立 handler 且
    ``propagate=True``，会回退到根 logger，行为与现状一致（安全降级）。
    """
    return _attach_trace_method(logging.getLogger(COMM_LOGGER_NAME))


def configure_comm_logger(working_dir=None, loglevel=None):
    """为服务端通信(WebSocket)配置独立日志，复用 ``ColoredFormatter`` 逻辑。

    - 独立文件：``<working_dir>/logs/ws_comm_<日期 时间>.log``，TRACE 全量落本地
    - 微秒级时间戳 + 线程名，便于排查 queue 机制、收发时序与并发标识
    - ``propagate=False``，与主日志解耦，避免日志混在一起
    - 控制台仍保留实时输出（级别与主控制台一致），不丢失现有可见性
    - 同步把 ``websockets`` 库自身的协议日志(握手/ping/pong/关闭)落到同一文件

    Args:
        working_dir: 工作目录(``unilabos_data``)，None 时不写文件
        loglevel: 控制台日志级别，与主日志保持一致

    Returns:
        日志文件绝对路径(未配置文件时为 None)
    """
    global _comm_file_handler

    comm_logger = get_comm_logger()
    comm_logger.setLevel(TRACE_LEVEL)
    comm_logger.propagate = False  # 与根 logger 解耦，单独成文件

    # 移除旧 handler，支持重启重复调用
    for handler in comm_logger.handlers[:]:
        comm_logger.removeHandler(handler)
        handler.close()

    # 控制台 handler：保留实时可见性，带线程名便于现场观察
    console_handler = logging.StreamHandler()
    console_handler.setLevel(_to_numeric_level(loglevel))
    console_handler.setFormatter(ColoredFormatter(use_colors=True, show_thread=True))
    comm_logger.addHandler(console_handler)

    log_filepath = None
    if working_dir is not None:
        logs_dir = os.path.join(working_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        log_filename = "ws_comm_" + datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".log"
        log_filepath = os.path.join(logs_dir, log_filename)

        file_handler = logging.FileHandler(log_filepath, encoding="utf-8")
        file_handler.setLevel(TRACE_LEVEL)  # 全量保留到本地
        # 文件不带颜色，开启微秒精度 + 线程名
        file_handler.setFormatter(ColoredFormatter(use_colors=False, microseconds=True, show_thread=True))
        comm_logger.addHandler(file_handler)

        # websockets 库自身日志(协议层)也归集到同一文件，方便排查链路问题；
        # 保持其 propagate=True，不影响主日志原有行为。
        ws_lib_logger = logging.getLogger("websockets")
        if _comm_file_handler is not None and _comm_file_handler in ws_lib_logger.handlers:
            ws_lib_logger.removeHandler(_comm_file_handler)
        ws_lib_logger.addHandler(file_handler)
        _comm_file_handler = file_handler

    comm_logger.info(f"[CommLogger] 通信日志已初始化，文件: {log_filepath}")
    return log_filepath


# 配置日志系统
configure_logger()

# 获取日志记录器
logger = logging.getLogger(__name__)


# 获取调用栈信息的工具函数
def _get_caller_info(stack_level=0) -> Tuple[str, int, str, str]:
    """
    获取调用者的信息

    Args:
        stack_level: 堆栈回溯的级别，0表示当前函数，1表示调用者，依此类推

    Returns:
        (filename, line_number, function_name, module_name) 元组
    """
    # 堆栈级别需要加3:
    # +1 因为这个函数本身占一层
    # +1 因为日志函数(debug, info等)占一层
    # +1 因为下面调用 inspect.stack() 也占一层
    frame = inspect.currentframe()
    try:
        # 跳过适当的堆栈帧
        for _ in range(stack_level + 3):
            if frame and frame.f_back:
                frame = frame.f_back
            else:
                break

        if frame:
            filename = frame.f_code.co_filename if frame.f_code else "unknown"
            line_number = frame.f_lineno if hasattr(frame, "f_lineno") else 0
            function_name = frame.f_code.co_name if frame.f_code else "unknown"

            # 获取模块名称
            module_name = "unknown"
            if frame.f_globals and "__name__" in frame.f_globals:
                module_name = frame.f_globals["__name__"].rsplit(".", 1)[0]

            return (filename, line_number, function_name, module_name)
        return ("unknown", 0, "unknown", "unknown")
    finally:
        del frame  # 避免循环引用


# 便捷日志记录函数
def debug(msg, *args, stack_level=0, **kwargs):
    """
    记录DEBUG级别日志

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.debug的其他参数
    """
    # 获取调用者信息
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.debug(msg, *args, **kwargs)


def info(msg, *args, stack_level=0, **kwargs):
    """
    记录INFO级别日志

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.info的其他参数
    """
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.info(msg, *args, **kwargs)


def warning(msg, *args, stack_level=0, **kwargs):
    """
    记录WARNING级别日志

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.warning的其他参数
    """
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.warning(msg, *args, **kwargs)


def error(msg, *args, stack_level=0, **kwargs):
    """
    记录ERROR级别日志

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.error的其他参数
    """
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.error(msg, *args, **kwargs)


def critical(msg, *args, stack_level=0, **kwargs):
    """
    记录CRITICAL级别日志

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.critical的其他参数
    """
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.critical(msg, *args, **kwargs)


def trace(msg, *args, stack_level=0, **kwargs):
    """
    记录TRACE级别日志（比DEBUG级别更低）

    Args:
        msg: 日志消息
        stack_level: 堆栈回溯级别，用于定位日志的实际调用位置
        *args, **kwargs: 传递给logger.log的其他参数
    """
    if stack_level > 0:
        caller_info = _get_caller_info(stack_level)
        extra = kwargs.get("extra", {})
        extra["custom_stack_info"] = caller_info
        kwargs["extra"] = extra
    logger.log(TRACE_LEVEL, msg, *args, **kwargs)


logger.trace = trace

# 测试日志输出（如果直接运行此文件）
if __name__ == "__main__":
    print("测试不同日志级别的颜色输出:")
    trace("这是一条跟踪日志 (TRACE级别显示为深灰色，其他文本也为深灰色)")
    debug("这是一条调试日志 (DEBUG级别显示为蓝色，其他文本为灰色)")
    info("这是一条信息日志 (INFO级别显示为绿色，其他文本为白色)")
    warning("这是一条警告日志 (WARNING级别显示为黄色，其他文本也为黄色)")
    error("这是一条错误日志 (ERROR级别显示为红色，其他文本也为红色)")
    critical("这是一条严重错误日志 (CRITICAL级别显示为紫色，其他文本也为紫色)")
    # 测试异常输出
    try:
        1 / 0
    except Exception as e:
        error(f"发生错误: {e}", exc_info=True)
