import base64
import traceback
import os
import importlib.util
import re
from typing import Literal, Optional
from unilabos.utils import logger


HOST_NODE_REGISTRY_NAME = "host_node"
DEFAULT_HOST_NODE_NAME = HOST_NODE_REGISTRY_NAME
_ROS_NODE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOVED_CONFIG_FIELDS = {
    "BasicConfig": frozenset({"app_bridges", "communication_protocol"}),
}
# 与配置覆盖共用 UNILABOS_ 前缀、但由进程编排 / 可观测性模块自行读取的环境变量，
# 不是 ``UNILABOS_<Config类>_<字段>`` 覆盖，解析时直接跳过（不告警）。
_NON_CONFIG_ENV_KEYS = frozenset(
    {
        "UNILABOS_SUPERVISOR_INNER",  # app.supervisor：当前进程处于监督循环内
        "UNILABOS_HOST_CHILD",  # app.supervisor：由调度权威拉起的 Host 子进程
        "UNILABOS_OTEL_ENABLED",  # utils.tracing：是否创建 OTLP exporter
    }
)


# 值不进日志的配置字段（小写）。
_SECRET_CONFIG_FIELDS = frozenset({"ak", "sk", "control_token", "station_api_key"})


class BasicConfig:
    # 运行时 backend 名称由 unilabos.backend 统一规范化。
    backend: Literal["hostlink", "ros2"] = "hostlink"
    ak = ""
    sk = ""
    working_dir = ""
    # 调度权威与 Host 之间控制面（控制 WebSocket、命令正文、backend_http 回送）的共享 token，
    # 见 unilabos/config/control_token.py。权威留空时首次启动生成并保存在 <working_dir>/control_token；
    # Host 子进程由权威经环境变量传入；远端 Host（--address 指向另一台机器）在这里填权威那份 token。
    control_token = ""
    # V1 工站调用（``POST /api/v1/station/workflow-invocations``）的协议密钥，对应 V1 的
    # ``EdgeControlConfig.api_key``：上游 Backend 以 ``Authorization: Bearer <key>`` 调用。
    # 留空时读 <working_dir>/station_api_key（不自动生成）；都没有则工站调用一律 401。
    # 见 unilabos/config/station_key.py。
    station_api_key = ""
    # 允许跨域访问管理 API 的来源，逗号分隔（如 "https://xuwznln.github.io,http://localhost:5173"）；
    # "*" 为全部允许（默认）。
    cors_allow_origins = "*"
    # 四个数据库（runtime / materials / telemetry / history）所在目录；命令行
    # --server_database_root 优先。留空时使用工作目录下的 database/（每个实例各自一份）。
    server_database_root = ""
    # 库结构不一致且没有迁移路径时，策略为 refuse 的库拒绝启动；设为 True 则改为把旧库改名为
    # .bak-<时间戳> 后重建空库。开发期四个库都是 rebuild，本开关在发布切换为 refuse 后才起作用。
    database_rebuild_on_mismatch = False
    # 调度锁授予策略：auto（有原生库用原生，否则 Python FIFO）/ python / native（找不到原生库报错）。
    scheduler_grant_policy = "auto"
    # 原生策略库路径；留空时依次查环境变量 UNILABOS_SCHEDULER_GRANT_LIBRARY、<working_dir>/native/。
    scheduler_grant_library = ""
    # 由微后端组合根一次解析；四个数据库 writer 不得自行推导路径。
    server_database_paths = None
    config_path = ""
    is_host_mode = True
    slave_no_host = False  # 是否跳过rclient.wait_for_service()
    # 可重命名的 HostNode 运行时实例；注册表类型仍固定为 host_node。
    host_node_name = "host_node"
    machine_name = "undefined"
    vis_2d_enable = False
    no_update_feedback = False
    enable_resource_load = True
    startup_json_path = None  # 填写绝对路径
    disable_browser = False  # 只禁止浏览器自动打开，不停止管理端服务
    port = 8002  # 管理端 HTTP/Web API 与主微前端服务
    # 前端失效通知（frontend_event）的保留目标：定期清理，不是每次插入后的硬上限。
    frontend_event_retention_seconds = 86_400
    frontend_event_max_count = 10_000
    frontend_event_cleanup_interval_seconds = 60
    frontend_event_cleanup_batch_size = 1_000
    check_mode = False  # CI 检查模式，用于验证 registry 导入和文件一致性
    test_mode = False  # 测试模式，所有动作不实际执行，返回模拟结果
    extra_resource = False  # 是否加载lab_开头的额外资源
    # 'TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    log_level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "DEBUG"

    @classmethod
    def auth_secret(cls):
        if not cls.ak or not cls.sk:
            return ""
        target = f"{cls.ak}:{cls.sk}"
        base64_target = base64.b64encode(target.encode("utf-8")).decode("utf-8")
        return base64_target


def resolve_host_node_name(value: Optional[str] = None) -> str:
    """返回可用于 ROS/HostLink 的 HostNode 运行时名称。"""

    name = str(BasicConfig.host_node_name if value is None else value).strip()
    if not name:
        name = DEFAULT_HOST_NODE_NAME
    if not _ROS_NODE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "HostNode name must start with a letter or underscore and contain "
            "only ASCII letters, digits, and underscores"
        )
    return name


# WebSocket配置
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    # 心跳配置使用 ws_ 命名空间，避免被 local_config 中的通用 ping 字段覆盖。
    ws_ping_interval = 5  # ping间隔（秒），对齐服务端 PingPeriod
    ws_ping_timeout = 8  # pong等待超时（秒），对齐服务端 PongWait


# HTTP配置
class HTTPConfig:
    # Host 连接的 Backend（调度权威）地址：config 文件或 --address 给了用给的，为空表示
    # 本机自己的 Backend 端口（backend_port）。HTTP 数据面与 runtime.v1 控制 WebSocket
    # 都走这一个地址。不内置任何云端地址；环境快捷选项仅保留在 UniLabOS-Launcher。
    remote_addr = ""
    # 控制 WebSocket 的低层覆盖；CLI 只暴露统一 --address。为空时从 Backend 地址派生：
    # runtime.v1 的 WS 控制面与 HTTP API 同 host 同端口。
    schedule_addr = ""
    # 仅供 Backend 侧 legacy 适配层的探测器（legacy_adaptor.probe）读取：
    # "runtime.v1" 或 "legacy"（旧云端 Backend：job_start / host_node_ready /
    # /lab/resource 消息族）。Edge 的会话工厂固定 runtime.v1，不读取此项。
    backend_protocol = ""
    # Host 访问的物料权威地址；为空时与 Backend 地址相同。
    material_microbackend_addr = ""
    material_query_timeout = 10
    # Backend（--role backend）管理 API / 控制 WebSocket 的监听端口（Host 缺省连接的就是它）。
    # Backend 不再主动连 Host：Host 不监听端口，Backend 需要的 Host 数据经控制 WS 的
    # backend_http 请求由 Host 在进程内执行并回送。edge_data_addr 仅为兼容旧配置保留、不再使用。
    edge_data_addr = ""
    backend_port = 8081
    # 驱动包索引镜像（JSON，结构同 https://github.com/Xuwznln/awesome-lab-devices 的 index.json）。
    # 官方索引默认由 OpenLab 前端在浏览器里直接读取；只有浏览器出不了网、需要 Edge 侧
    # 内网镜像时才配这里。为空时 /driver-packages/catalog 只并入本地
    # <working_dir>/driver_package_catalog.json。
    driver_package_index_url = ""


# Host/Slave 控制通道。ROS2 backend 用它同步发现参数；hostlink backend 还会
# 通过同一条长连接发布设备状态并执行远程动作。
class HostLinkConfig:
    enable = True
    host = ""  # Slave 侧指定的 HostNode IP/主机名
    port = 7302
    bind = "0.0.0.0"
    advertise_ip = ""  # Host 下发给 Slave 的可达地址；空时自动探测
    heartbeat_interval = 5.0
    heartbeat_timeout = 15.0
    connect_timeout = 5.0
    request_timeout = 10.0
    ros_assist_apply = True
    ros_domain_id = ""  # 空时沿用 ROS_DOMAIN_ID
    ros_discovery_range = ""  # SYSTEM_DEFAULT / SUBNET / LOCALHOST / OFF
    ros_static_peers = ""  # 分号分隔
    # 外部 Fast DDS Discovery Server 的 host:port；off 表示明确清除继承值。
    # 空值由 ROS2 组网微后端托管；0 复用 HostLink 数字端口（TCP/UDP 可共存）。
    ros_discovery_server = ""
    ros_discovery_port = 0


class OTelConfig:
    """OpenTelemetry 配置；默认关闭且所有调用均 fail-open。"""

    enabled = False
    endpoint = ""
    insecure = True
    service_name = "uni-lab-edge"
    service_namespace = "unilab"
    service_version = "0.11.3"
    deployment_environment = ""
    headers = ""
    resource_attributes = ""
    trace_sampler = "parentbased_always_on"
    sample_ratio = 1.0
    max_queue_size = 2048
    max_export_batch_size = 512
    schedule_delay_ms = 5000
    export_timeout_ms = 5000
    shutdown_timeout_ms = 5000


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
                        if attr in _REMOVED_CONFIG_FIELDS.get(name, ()):
                            continue
                        setattr(obj, attr, getattr(getattr(module, name), attr))


def _update_config_from_env():
    prefix = "UNILABOS_"
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix) or env_key in _NON_CONFIG_ENV_KEYS:
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
            if attr_type is bool:
                value = env_value.lower() in ("true", "1", "yes")
            elif attr_type is int:
                value = int(env_value)
            elif attr_type is float:
                value = float(env_value)
            else:
                value = env_value
            setattr(matched_cls, matched_field, value)
            # 密钥类字段只记"已设置"：环境变量覆盖会进日志，日志可能被转发给前端或上传
            shown = "<已设置>" if matched_field.lower() in _SECRET_CONFIG_FIELDS else value
            logger.info(f"[ENV] 设置 {matched_cls.__name__}.{matched_field} = {shown}")
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
        except Exception:
            logger.error(f"[ENV] 加载配置文件 {config_path} 失败")
            traceback.print_exc()
            exit(1)
    else:
        config_path = os.path.join(os.path.dirname(__file__), "example_config.py")
        load_config(config_path)
