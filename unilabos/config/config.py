import base64
import traceback
import os
import importlib.util
from typing import Optional, Literal
from unilabos.utils import logger


class BasicConfig:
    ak = ""
    sk = ""
    working_dir = ""
    config_path = ""
    is_host_mode = True
    slave_no_host = False  # 是否跳过rclient.wait_for_service()
    upload_registry = False
    machine_name = "undefined"
    vis_2d_enable = False
    enable_resource_load = True
    communication_protocol = "websocket"
    startup_json_path = None  # 填写绝对路径
    disable_browser = False  # 禁止浏览器自动打开
    port = 8002  # 本地HTTP服务
    log_level: Literal['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = "DEBUG"  # 'TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'

    @classmethod
    def auth_secret(cls):
        if not cls.ak or not cls.sk:
            return ""
        target = f"{cls.ak}:{cls.sk}"
        base64_target = base64.b64encode(target.encode("utf-8")).decode("utf-8")
        return base64_target


# WebSocket配置
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ping_interval = 30  # ping间隔（秒）


# HTTP配置
class HTTPConfig:
    remote_addr = "http://127.0.0.1:48197/api/v1"


# ROS配置
class ROSConfig:
    modules = [
        "std_msgs.msg",
        "geometry_msgs.msg",
        "control_msgs.msg",
        "control_msgs.action",
        "nav2_msgs.action",
        "unilabos_msgs.msg",
        "unilabos_msgs.action",
    ]


def _update_config_from_module(module):
    for name, obj in globals().items():
        if isinstance(obj, type) and name.endswith("Config"):
            if hasattr(module, name) and isinstance(getattr(module, name), type):
                for attr in dir(getattr(module, name)):
                    if not attr.startswith("_"):
                        setattr(obj, attr, getattr(getattr(module, name), attr))

def _update_config_from_env():
    prefix = "UNILABOS_"
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        try:
            key_path = env_key[len(prefix):]  # Remove UNILAB_ prefix
            class_field = key_path.upper().split("_", 1)
            if len(class_field) != 2:
                logger.warning(f"[ENV] 环境变量格式不正确：{env_key}")
                continue

            class_key, field_key = class_field
            # 遍历 globals 找匹配类（不区分大小写）
            matched_cls = None
            for name, obj in globals().items():
                if name.upper() == class_key and isinstance(obj, type):
                    matched_cls = obj
                    break

            if matched_cls is None:
                logger.warning(f"[ENV] 未找到类：{class_key}")
                continue

            # 查找类属性（不区分大小写）
            matched_field = None
            for attr in dir(matched_cls):
                if attr.upper() == field_key:
                    matched_field = attr
                    break

            if matched_field is None:
                logger.warning(f"[ENV] 类 {matched_cls.__name__} 中未找到字段：{field_key}")
                continue

            current_value = getattr(matched_cls, matched_field)
            attr_type = type(current_value)
            if attr_type == bool:
                value = env_value.lower() in ("true", "1", "yes")
            elif attr_type == int:
                value = int(env_value)
            elif attr_type == float:
                value = float(env_value)
            else:
                value = env_value
            setattr(matched_cls, matched_field, value)
            logger.info(f"[ENV] 设置 {matched_cls.__name__}.{matched_field} = {value}")
        except Exception as e:
            logger.warning(f"[ENV] 解析环境变量 {env_key} 失败: {e}")


def load_config(config_path=None):
    # 如果提供了配置文件路径，从该文件导入配置
    if config_path:
        env_config_path = os.environ.get("UNILABOS_BASICCONFIG_CONFIG_PATH")
        config_path = env_config_path if env_config_path else config_path
        BasicConfig.config_path = os.path.abspath(os.path.dirname(config_path))
        if not os.path.exists(config_path):
            logger.error(f"[ENV] 配置文件 {config_path} 不存在")
            exit(1)
        try:
            module_name = "lab_" + os.path.basename(config_path).replace(".py", "")
            spec = importlib.util.spec_from_file_location(module_name, config_path)
            if spec is None:
                logger.error(f"[ENV] 配置文件 {config_path} 错误")
                return
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore
            _update_config_from_module(module)
            logger.info(f"[ENV] 配置文件 {config_path} 加载成功")
            _update_config_from_env()
        except Exception as e:
            logger.error(f"[ENV] 加载配置文件 {config_path} 失败")
            traceback.print_exc()
            exit(1)
    else:
        config_path = os.path.join(os.path.dirname(__file__), "local_config.py")
        load_config(config_path)
