# unilabos的配置文件


class BasicConfig:
    # 默认沿用现有 SQLite 路径。PostgreSQL 连接使用同一个数据库，无额外 schema。
    database_type = "sqlite"
    database_url = ""  # PostgreSQL 示例：postgresql://user:password@localhost:5432/unilab
    ak = ""  # 实验室网页给您提供的ak代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --ak 传入，优先按照传入参数解析
    sk = ""  # 实验室网页给您提供的sk代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --sk 传入，优先按照传入参数解析
    # 当前进程明确授权的工作流源码（Workflow Source）选择目录；空 tuple 禁止隐式扫描。
    workflow_editable_package_roots = ()
    # 诊断日志滚动及清理；http_reports 等业务原始报送不受此策略影响。
    log_level = "INFO"
    file_log_level = "INFO"
    log_detailed = False  # 排障时设为 True，文件记录 TRACE
    log_max_bytes = 50 * 1024 * 1024
    log_backup_count = 9
    log_retention_days = 7
    log_total_max_bytes = 2 * 1024 * 1024 * 1024
    log_cleanup_interval_seconds = 600


# WebSocket配置，一般无需调整
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ws_ping_interval = 5  # ping间隔（秒），对齐服务端 PingPeriod
    ws_ping_timeout = 7  # pong等待超时（秒），对齐服务端 PongWait


# OS 物料查询固定使用当前主进程内嵌的库存权威；正式后端由前端直接选择。
class HTTPConfig:
    material_query_timeout = 10


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
