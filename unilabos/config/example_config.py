# unilabos的配置文件

class BasicConfig:
    ak = ""  # 实验室网页给您提供的ak代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --ak 传入，优先按照传入参数解析
    sk = ""  # 实验室网页给您提供的sk代码，您可以在配置文件中指定，也可以通过运行unilabos时以 --sk 传入，优先按照传入参数解析
    port = 8002  # 管理端 HTTP/Web API 与主微前端端口
    disable_browser = False  # 只禁止自动打开浏览器，不停止管理端服务


# WebSocket配置，一般无需调整
class WSConfig:
    reconnect_interval = 5  # 重连间隔（秒）
    max_reconnect_attempts = 999  # 最大重连次数
    ws_ping_interval = 5  # ping间隔（秒），对齐服务端 PingPeriod
    ws_ping_timeout = 7  # pong等待超时（秒），对齐服务端 PongWait


# Host/Slave ROS2 组网；Slave 推荐通过 --host-node-ip 指定 Host，不必写死在文件中。
class HostLinkConfig:
    enable = True
    port = 7302
    bind = "0.0.0.0"
    advertise_ip = ""  # Host 多网卡时填写 Slave 可达 IP
    heartbeat_interval = 5.0
    heartbeat_timeout = 15.0
    connect_timeout = 5.0
    request_timeout = 10.0
    ros_assist_apply = True
    ros_domain_id = ""  # Host 可在此统一配置，也可用 --ros-domain-id
    ros_discovery_range = ""
    ros_static_peers = ""
    ros_discovery_server = ""  # 空=微后端托管；外部 host:port；off=禁用
    ros_discovery_port = 0  # 0=复用 HostLink 数字端口（TCP/UDP）
