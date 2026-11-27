"""
日志适配器模块

用于将各种框架的日志（如Uvicorn、FastAPI等）统一适配到ilabos的日志系统
"""

import logging
from urllib.parse import urlsplit

from unilabos.utils.log import critical, debug, error, info, is_detailed_logging_enabled, warning

# 仅列出已确认会周期调用的只读端点；精确匹配路径，不能扩展为前缀过滤。
POLLING_GET_PATHS = frozenset({
    "/api/v1/health", "/api/v1/readiness", "/api/v1/devices", "/api/v1/online-devices",
})


class PollingAccessFilter(logging.Filter):
    """普通模式只省略白名单成功 GET，未知格式和异常响应全部保留。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if is_detailed_logging_enabled() or record.name != "uvicorn.access" or record.levelno >= logging.WARNING:
            return True
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _client, method, target, _version, status = record.args
        if method != "GET" or not isinstance(target, str) or type(status) is not int:
            return True
        try:
            path = urlsplit(target).path
        except ValueError:
            return True
        return not (200 <= status < 300 and path in POLLING_GET_PATHS)


class UvicornLogAdapter:
    """Uvicorn日志适配器，将Uvicorn的日志重定向到我们的日志系统"""

    @staticmethod
    def configure():
        """配置Uvicorn的日志系统，使用我们自定义的日志格式"""
        # 获取uvicorn相关的日志记录器
        uvicorn_loggers = [
            logging.getLogger("uvicorn"),
            logging.getLogger("uvicorn.access"),
            logging.getLogger("uvicorn.error"),
            logging.getLogger("fastapi"),
        ]

        # 清除现有处理器
        old_handlers = set()
        for logger_instance in uvicorn_loggers:
            for handler in logger_instance.handlers[:]:
                if getattr(handler, "_unilabos_otel_handler", False):
                    continue
                logger_instance.removeHandler(handler)
                old_handlers.add(handler)
        for handler in old_handlers:
            handler.close()

        # 添加自定义处理器
        adapter_handler = UvicornToIlabosHandler()

        # 为所有uvicorn日志记录器添加处理器
        for logger_instance in uvicorn_loggers:
            logger_instance.addHandler(adapter_handler)
            # 设置日志级别
            logger_instance.setLevel(logging.INFO)
            # 禁止传播到根日志记录器，避免重复输出
            logger_instance.propagate = False


class UvicornToIlabosHandler(logging.Handler):
    """将Uvicorn日志处理为ilabos日志格式的处理器"""

    def __init__(self):
        super().__init__()
        self.addFilter(PollingAccessFilter())
        self.level_map = {
            logging.DEBUG: debug,
            logging.INFO: info,
            logging.WARNING: warning,
            logging.ERROR: error,
            logging.CRITICAL: critical,
        }

    def emit(self, record):
        """发送日志记录到ilabos日志系统"""
        try:
            msg = self.format(record)
            log_func = self.level_map.get(record.levelno, info)
            # 根据日志源添加前缀
            if record.name.startswith("uvicorn"):
                prefix = "[Uvicorn] "
                if record.name == "uvicorn.access":
                    prefix = "[Uvicorn.HTTP] "
                msg = f"{prefix}{msg}"
            elif record.name.startswith("fastapi"):
                msg = f"[FastAPI] {msg}"
            else:
                msg = f"{record.name} {msg}"
            log_func(msg, stack_level=5)
        except Exception:
            self.handleError(record)


def setup_fastapi_logging():
    """原地配置 Uvicorn，并禁止其 dictConfig 关闭应用文件和 OTel 出口。"""
    UvicornLogAdapter.configure()
    # Uvicorn 支持 log_config=None，直接复用上面的配置。非增量 dictConfig
    # 会调用 logging.shutdown()，误关闭仍挂在根 logger 上的文件会话锁。
    return None
