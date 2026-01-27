import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    """
    功能:
        统一存放 driver 的基础配置, 例如 URL, 账号密码, 默认超时时间.
    参数:
        base_url: 设备上位机服务地址, 例如 "http://127.0.0.1:4669".
        username: 登录用户名.
        password: 登录密码.
        timeout_s: requests 默认超时(秒).
        verify_ssl: https 场景是否校验证书, 内网调试可为 False.
        log_level: 日志级别字符串, 例如 "INFO".
    返回:
        Settings.
    """

    base_url: str = "http://10.40.13.51:4669"
    username: str = "admin"
    password: str = "admin"
    timeout_s: float = 30.0
    verify_ssl: bool = True
    log_level: str = "INFO"

    # 数据存储配置
    data_dir: Path = Path(__file__).parent.parent / "data"
    enable_data_logging: bool = True
    task_retention_days: int = 90

    @staticmethod
    def from_env() -> "Settings":
        """
        功能:
            从环境变量读取配置, 便于部署与 CI.
        参数:
            无.
        返回:
            Settings.
        环境变量:
            SYN_STATION_BASE_URL, SYN_STATION_USERNAME, SYN_STATION_PASSWORD,
            SYN_STATION_TIMEOUT_S, SYN_STATION_VERIFY_SSL, SYN_STATION_LOG_LEVEL,
            SYN_STATION_DATA_DIR, SYN_STATION_ENABLE_DATA_LOGGING, SYN_STATION_TASK_RETENTION_DAYS.
        """
        base_url = os.getenv("SYN_STATION_BASE_URL", Settings.base_url)
        username = os.getenv("SYN_STATION_USERNAME", Settings.username)
        password = os.getenv("SYN_STATION_PASSWORD", Settings.password)

        timeout_s_str = os.getenv("SYN_STATION_TIMEOUT_S", str(Settings.timeout_s))
        try:
            timeout_s = float(timeout_s_str)
        except ValueError:
            timeout_s = Settings.timeout_s

        verify_ssl_str = os.getenv("SYN_STATION_VERIFY_SSL", str(Settings.verify_ssl))
        verify_ssl = verify_ssl_str.strip().lower() in ("1", "true", "yes", "y", "on")

        log_level = os.getenv("SYN_STATION_LOG_LEVEL", Settings.log_level)

        # 数据存储配置
        data_dir_str = os.getenv("SYN_STATION_DATA_DIR")
        data_dir = Path(data_dir_str) if data_dir_str else Settings.data_dir

        enable_data_logging_str = os.getenv("SYN_STATION_ENABLE_DATA_LOGGING", str(Settings.enable_data_logging))
        enable_data_logging = enable_data_logging_str.strip().lower() in ("1", "true", "yes", "y", "on")

        task_retention_days_str = os.getenv("SYN_STATION_TASK_RETENTION_DAYS", str(Settings.task_retention_days))
        try:
            task_retention_days = int(task_retention_days_str)
        except ValueError:
            task_retention_days = Settings.task_retention_days

        return Settings(
            base_url=base_url,
            username=username,
            password=password,
            timeout_s=timeout_s,
            verify_ssl=verify_ssl,
            log_level=log_level,
            data_dir=data_dir,
            enable_data_logging=enable_data_logging,
            task_retention_days=task_retention_days,
        )


def configure_logging(level: str = "INFO") -> None:
    """
    功能:
        配置全局 logging, 统一输出格式.
    参数:
        level: 日志级别, 例如 "DEBUG", "INFO".
    返回:
        无.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
