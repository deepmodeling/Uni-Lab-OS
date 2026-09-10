# unilabos的配置文件


class BasicConfig:
    ak = ""  # 实验室网页给您提供的ak代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --ak 传入，优先按照传入参数解析
    sk = ""  # 实验室网页给您提供的sk代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --sk 传入，优先按照传入参数解析
    # 当前进程明确授权的工作流源码（Workflow Source）选择目录；空 tuple 禁止隐式扫描。
    workflow_editable_package_roots = ()


# WebSocket配置，一般无需调整
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ws_ping_interval = 5  # ping间隔（秒），对齐服务端 PingPeriod
    ws_ping_timeout = 7  # pong等待超时（秒），对齐服务端 PongWait


# OS 物料查询固定使用当前主进程内嵌的库存权威；正式后端由前端直接选择。
class HTTPConfig:
    material_query_timeout = 10


# HostLink 由 Edge 微后端拥有：Host 监听所有 Slave、下发 ROS 网络策略并代理物料查询。
class HostLinkConfig:
    enable = True
    host = ""  # Slave 填 Host 微后端 IP；Host 留空
    port = 7302
    bind = "0.0.0.0"
    advertise_ip = ""  # 空 = 自动探测 Host 对外 IP
    ros_assist_apply = True
    ros_domain_id = ""
    ros_discovery_range = ""
    ros_static_peers = ""
    ros_discovery_server = ""  # 空=Host 自动启动；off=禁用；ip:port=外部服务
    ros_discovery_port = 0  # 0=复用 HostLink 数字端口（TCP/UDP 各自监听）


# OpenTelemetry/SigNoz 默认关闭。生产环境建议用环境变量注入 endpoint/headers，
# 不要把 token 或认证 header 写进配置文件。
class OTelConfig:
    enabled = False
    endpoint = ""  # OTLP/gRPC，例如 http://signoz-otel-collector:4317
    protocol = "grpc"
    logs_enabled = True
    logs_endpoint = ""  # 空值复用 endpoint
    logs_protocol = ""  # 空值复用 protocol
    insecure = True
    service_name = "uni-lab-edge"
    deployment_environment = ""
