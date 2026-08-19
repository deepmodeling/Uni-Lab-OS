# 组网部署与主从模式配置

本文档介绍 Uni-Lab-OS 的组网架构、部署方式和主从模式的详细配置。

## 目录

- [架构概览](#架构概览)
- [节点类型](#节点类型)
- [通信机制](#通信机制)
- [典型拓扑](#典型拓扑)
- [主从模式配置](#主从模式配置)
- [网络配置](#网络配置)
- [示例：多房间部署](#示例多房间部署)
- [故障处理](#故障处理)
- [监控和维护](#监控和维护)

---

## 架构概览

Uni-Lab-OS 支持多种部署模式：

```
┌──────────────────────────────────────────────┐
│      Cloud Platform/Self-hosted Platform     │
│           leap-lab.bohrium.com                │
│  (Resource Management, Task Scheduling,      │
│              Monitoring)                     │
└────────────────────┬─────────────────────────┘
                     │ WebSocket / HTTP
                     │
          ┌──────────┴──────────┐
          │                     │
     ┌────▼─────┐         ┌────▼─────┐
     │  Master  │◄──ROS2──►│  Slave   │
     │   Node   │         │   Node   │
     │  (Host)  │         │ (Slave)  │
     └────┬─────┘         └────┬─────┘
          │                    │
     ┌────┴────┐          ┌────┴────┐
     │ Device A│          │ Device B│
     │ Device C│          │ Device D│
     └─────────┘          └─────────┘
```

---

## 节点类型

### 主节点（Host Node）

**功能**:

- 创建和管理全局资源
- 提供 host_node 服务
- 连接云端平台
- 协调多个从节点
- 提供 Web 管理界面

**启动命令**:

```bash
unilab --ak your_ak --sk your_sk -g host_devices.json
```

### 从节点（Slave Node）

**功能**:

- 管理本地设备
- 不连接云端（可选）
- 向主节点注册
- 执行分配的任务

**启动命令**:

```bash
unilab --ak your_ak --sk your_sk -g slave_devices.json \
  --is_slave --host-node-ip 192.168.1.10
```

---

## 通信机制

### ROS2 通信

**用途**: 节点间实时通信

**通信方式**:

- **Topic**: 状态广播（设备状态、传感器数据）
- **Service**: 同步请求（资源查询、配置获取）
- **Action**: 异步任务（设备操作、长时间运行）

**示例**:

```bash
# 查看ROS2节点
ros2 node list

# 查看topic
ros2 topic list

# 查看action
ros2 action list
```

### HostLink 组网控制通道与无 ROS backend

Host 运行 ROS2 或 HostLink backend 时会在 TCP `7302` 监听 HostLink。Slave 通过
`--host-node-ip <host-ip>[:port]` 建立控制连接并完成：

- 上报启动图中的设备 ID，供 Host 发现 Slave 及其设备归属；
- ROS2 模式在 `rclpy.init` 前接收并应用 Host 的 `ROS_DOMAIN_ID`、发现范围、
  静态对端和外部 Fast DDS Discovery Server 地址。

在 `--backend ros2` 下，HostLink 只辅助组网，设备 Action、节点注册和资源同步仍
走 ROS2。`--backend hostlink` 则完全不导入 ROS：Host 与 Slave 都使用 BasicRuntime
加载本地纯 Python 驱动。Slave 在 HELLO 中发布设备动作、状态字段和设备 UUID；驱动
通过通用节点发布的状态通知会立即发送，心跳还会定期补发完整状态。Host 与 Slave 可以双向调用设备动作，
动作带独立 ID，支持反馈和协作取消。通用节点还提供与 ROS 相同形状的
`create_publisher(...).publish(...)` 和 `create_subscription(...)`：Basic 在本进程分发，
HostLink 由 Host 按绝对 Topic 名称转发，消息会转换为 JSON 可传输的 Python 值。
Slave 启动时会把本地设备物料树同步给 Host，
后续 `update_resource` 和 `get_resource` 也由 Host 保存和查询，不要求启动 ROS service
或 Web API。

```bash
# 无 ROS Host
unilab -g host.json --backend hostlink --hostlink-port 7302

# 无 ROS Slave
unilab -g slave.json --backend hostlink --is-slave \
  --host-node-ip 192.168.1.10 --hostlink-port 7302
```

驱动通过 `post_init(node)` 获得通用 `DeviceNode`，可使用日志、异步等待、任务调度、
状态通知、Topic 发布/订阅、物料更新/查询和跨设备动作调用。相对 Topic 名称会按
`/devices/<device_id>/<topic>` 解析；设备状态也会发布到这个路径。注册表可用
`class.supported_backends: [hostlink, ros2]` 明确声明可运行的通信 backend；
`class.type: ros2` 默认只允许 ROS2。注册表设备动作可以在 HostLink 上传递目标、反馈、取消和结果；
驱动调用时携带的 `action_type` 只作为兼容信息，实际按动作名和字典参数执行。
直接操作外部 ROS 图的 MoveIt ActionClient、规划场景/图像等 ROS 专用 Topic，
以及工作站跨设备物料搬运仍使用 ROS2。这些驱动已标记为 `[ros2]`，HostLink 启动时会
直接提示该驱动不支持，而不是在导入过程中报缺少 `rclpy`。

设备动作在每台设备内串行执行；不同 Slave/设备可以并行。取消是协作式的：驱动需
接收 `ActionContext` 并在长操作中检查取消状态，已经进入的阻塞硬件调用不会被强制
终止。连接断开时设备在 `heartbeat_timeout` 后离线，客户端会指数退避重连，但不会
自动重放动作。HostLink 的物料树保存在 Host 进程内，目前不会自动上传云端。

当前 HostLink 是面向可信实验室局域网的明文 TCP 协议，尚未提供 TLS 或双方身份认证。
部署时应通过防火墙限制 `7302` 的来源；跨不可信网络使用时应先接入 VPN/安全隧道。

#### 端口与前端归属

| 服务 | 默认地址 | 协议 | 使用者 |
|---|---|---|---|
| 主 Web/API | `0.0.0.0:8002` | HTTP/WebSocket over TCP | 状态页、主微前端、API 客户端；由 `--port-management` 配置 |
| HostLink | `0.0.0.0:7302` | NDJSON over raw TCP | Host/Slave 进程，不供浏览器访问 |
| F003 Local Bridge API | `127.0.0.1:8014` | HTTP | 仅完整集成分支中的本地工作流微前端 |

因此微前端不访问 `7302`。接入主 OS API 的微前端跟随
`--port-management`（`--port` 为兼容缩写），默认访问 `8002`；F003 本地桥接
微前端仍使用其独立的 `8014`。`--disable-browser` 只禁止自动打开页面，不会停止
`8002` 的 HTTP/Web 服务。两个独立 TCP 服务不能绑定同一个 IP/端口。

#### HostLink 与 ROS2 参数

| 参数 | 作用域 | 默认值 | 说明 |
|---|---|---:|---|
| `--host-node-ip` | Slave | 空 | Host IP/主机名；兼容 `ip:port` |
| `--hostlink-port` | Host + Slave | `7302` | HostLink TCP 监听/连接端口；优先于 `--host-node-ip` 中的端口 |
| `--hostlink-bind` | Host | `0.0.0.0` | HostLink 监听网卡 |
| `--hostlink-advertise-ip` | Host | 自动探测 | 多网卡时发布给 Slave 的可达 IP |
| `--disable-hostlink` | Host + Slave | 否 | 仅 ROS2 可用：禁用 HostLink 并回退原 ROS2 发现；不能和 `--backend hostlink` 同用 |
| `--hostlink-heartbeat-interval` | Slave | `5` 秒 | 心跳发送间隔 |
| `--hostlink-heartbeat-timeout` | Host | `15` 秒 | Slave 离线判定时间 |
| `--hostlink-connect-timeout` | Slave | `5` 秒 | 单次 TCP 连接和握手超时 |
| `--hostlink-request-timeout` | Host + Slave | `10` 秒 | 控制请求/设备 RPC 超时 |
| `--ros-domain-id` | Host + Slave | 环境值 | Host 下发给 Slave；Slave 本地值仅作连接前兜底 |
| `--ros-discovery-range` | Host | 环境值 | `SYSTEM_DEFAULT/SUBNET/LOCALHOST/OFF` |
| `--ros-static-peers` | Host | 自动加入 Host IP | 分号分隔的静态发现对端 |
| `--ros-discovery-server` | Host | 空 | 外部 Fast DDS `host:port`；`off` 禁用；空值由微后端托管 |
| `--ros-discovery-port` | Host | `0` | 托管 Discovery Server 的 UDP 端口；`0` 复用 HostLink 数字端口 |
| `--no-ros-assist` | ROS2 Slave | 否 | 保留 HostLink 心跳/设备发现，但不应用 Host ROS 参数 |

ROS2 Host 的微后端会在 `rclpy.init` 前启动托管的 Fast DDS Discovery
Server；若当前环境找不到 `fast-discovery-server`/`fastdds` 可执行文件，会记录错误并
回退到现有 `ROS_AUTOMATIC_DISCOVERY_RANGE`/`ROS_STATIC_PEERS` 策略。direct
`hostlink` backend 不启动 DDS 进程。

### WebSocket 通信

**用途**: 主节点与云端通信

**特点**:

- 实时双向通信
- 自动重连
- 心跳保持

**配置**:

```python
# local_config.py
BasicConfig.ak = "your_ak"
BasicConfig.sk = "your_sk"
```

---

## 典型拓扑

### 单节点模式

**适用场景**: 小型实验室、开发测试

```
┌──────────────────┐
│  Uni-Lab Node    │
│  ┌────────────┐  │
│  │  Device A  │  │
│  │  Device B  │  │
│  │  Device C  │  │
│  └────────────┘  │
└──────────────────┘
```

**优点**:

- 配置简单
- 无网络延迟
- 适合快速原型

**启动**:

```bash
unilab --ak your_ak --sk your_sk -g all_devices.json
```

### 主从模式

**适用场景**: 多房间、分布式设备

```
┌─────────────┐      ┌──────────────┐
│ Master Node │◄────►│ Slave Node 1 │
│ Coordinator │      │   Liquid     │
│ Web UI      │      │  Handling    │
└──────┬──────┘      └──────────────┘
       │
       │             ┌──────────────┐
       └────────────►│ Slave Node 2 │
                     │  Analytical  │
                     │  (NMR/GC)    │
                     └──────────────┘
```

**优点**:

- 物理分隔
- 独立故障域
- 易于扩展

**适用场景**:

- 设备物理位置分散
- 不同房间的设备
- 需要独立故障域
- 分阶段扩展系统

**主节点**:

```bash
unilab --ak your_ak --sk your_sk -g host.json --ros-domain-id 42
```

**从节点**:

```bash
unilab --ak your_ak --sk your_sk -g slave1.json \
  --is-slave --host-node-ip 192.168.1.10 --hostlink-port 7302
unilab --ak your_ak --sk your_sk -g slave2.json \
  --is-slave --host-node-ip 192.168.1.10 --hostlink-port 7302 --port-management 8003
```

### 云端集成模式

**适用场景**: 远程监控、多实验室协作

```
      Cloud Platform
            │
    ┌───────┴────────┐
    │                │
Laboratory A    Laboratory B
(Master Node)   (Master Node)
```

**优点**:

- 远程访问
- 数据同步
- 任务调度

**启动**:

```bash
# 实验室A
unilab --ak your_ak --sk your_sk --upload_registry

# 实验室B
unilab --ak your_ak --sk your_sk --upload_registry
```

---

## 主从模式配置

### 主节点配置

#### 1. 创建主节点设备图

`host.json`:

```json
{
  "nodes": [],
  "links": []
}
```

#### 2. 启动主节点

```bash
# 基本启动
unilab --ak your_ak --sk your_sk -g host.json

# 带云端集成
unilab --ak your_ak --sk your_sk -g host.json --upload_registry

# 指定端口
unilab --ak your_ak --sk your_sk -g host.json --port-management 8002
```

#### 3. 验证主节点

```bash
# 检查ROS2节点
ros2 node list
# 应该看到 /host_node

# 检查服务
ros2 service list | grep host_node

# Web界面
# 访问 http://localhost:8002
```

### 从节点配置

#### 1. 创建从节点设备图

`slave1.json`:

```json
{
  "nodes": [
    {
      "id": "liquid_handler_1",
      "name": "液体处理工作站",
      "type": "device",
      "class": "liquid_handler",
      "config": {
        "simulation": false
      }
    }
  ],
  "links": []
}
```

#### 2. 启动从节点

```bash
# 基本从节点启动
unilab --ak your_ak --sk your_sk -g slave1.json --is_slave

# 指定不同端口（如果多个从节点在同一台机器）
unilab --ak your_ak --sk your_sk -g slave1.json --is_slave --port-management 8003

# 跳过等待主节点（独立测试）
unilab --ak your_ak --sk your_sk -g slave1.json --is_slave --slave_no_host
```

#### 3. 验证从节点

```bash
# 检查节点连接
ros2 node list

# 检查设备状态
ros2 topic echo /liquid_handler_1/status
```

### 跨节点通信

#### 资源访问

主节点可以访问从节点的资源：

```bash
# 在主节点或其他节点调用从节点设备
ros2 action send_goal /liquid_handler_1/transfer_liquid \
  unilabos_msgs/action/TransferLiquid \
  "{source: {...}, target: {...}, volume: 100.0}"
```

#### 状态监控

主节点监控所有从节点状态：

```bash
# 订阅从节点状态
ros2 topic echo /liquid_handler_1/status

# 查看所有设备状态
ros2 topic list | grep status
```

---

## 网络配置

### ROS2 DDS 配置

确保主从节点在同一网络：

```bash
# 检查网络可达性
ping <slave_node_ip>

# 设置ROS_DOMAIN_ID（可选，用于隔离）
export ROS_DOMAIN_ID=42
```

推荐由 Host 启动参数统一 domain，Slave 不再重复维护：

```bash
# Host：发布 domain 42
unilab -g host.json --ros-domain-id 42

# Slave：通过 HostLink 获取 domain 42 和发现配置
unilab -g slave.json --is-slave --host-node-ip 192.168.1.10
```

如实验室使用已有 Fast DDS Discovery Server，可在 Host 指定并下发：

```bash
unilab -g host.json --ros-domain-id 42 \
  --ros-discovery-server 192.168.1.10:11811
```

此功能切片不会自动启动 Discovery Server 进程；未指定时仍沿用 ROS2/DDS
原有发现机制，并把 Host IP 加入 `ROS_STATIC_PEERS`。

### 防火墙配置

**建议做法**：

HostLink 需要 Slave 能访问 Host 的 TCP `7302`（若在 `--host-node-ip` 中指定
其他端口，则开放对应端口）。ROS2 backend 下该端口只承载组网控制；HostLink
backend 下还承载设备描述、状态、JSON Topic、动作 RPC 和物料树同步，
但不承载浏览器流量。

为了确保 ROS2 DDS 通信正常，建议直接关闭防火墙，而不是配置特定端口。ROS2 使用动态端口范围，配置特定端口可能导致通信问题。

**Linux**:

```bash
# 关闭防火墙
sudo ufw disable

# 或者临时停止防火墙
sudo systemctl stop ufw
```

**Windows**:

```powershell
# 在Windows安全中心关闭防火墙
# 控制面板 -> 系统和安全 -> Windows Defender 防火墙 -> 启用或关闭Windows Defender防火墙
```

### 验证网络连通性

在配置完成后，使用 ROS2 自带的 demo 节点来验证跨节点通信是否正常：

**在主节点机器上**（激活 unilab 环境后）：

```bash
# 启动talker
ros2 run demo_nodes_cpp talker

# 同时在另一个终端启动listener
ros2 run demo_nodes_cpp listener
```

**在从节点机器上**（激活 unilab 环境后）：

```bash
# 启动talker
ros2 run demo_nodes_cpp talker

# 同时在另一个终端启动listener
ros2 run demo_nodes_cpp listener
```

**注意**：必须在两台机器上**互相启动** talker 和 listener，否则可能出现只能收不能发的单向通信问题。

**预期结果**：

- 每台机器的 listener 应该能同时接收到本地和远程 talker 发送的消息
- 如果只能看到本地消息，说明网络配置有问题
- 如果两台机器都能互相收发消息，则组网配置正确

### 本地网络要求

**ROS2 通信**:

- 同一局域网或 VPN
- 端口：默认 DDS 端口（7400-7500）
- 组播支持（或配置 unicast）

**检查连通性**:

```bash
# Ping测试
ping <target_ip>

# ROS2节点发现
ros2 node list
ros2 daemon stop && ros2 daemon start
```

### 云端连接

**要求**:

- HTTPS (443)
- WebSocket 支持
- 稳定的互联网连接

**测试连接**:

```bash
# 测试云端连接
curl https://leap-lab.bohrium.com/api/v1/health

# 测试WebSocket
# 启动Uni-Lab后查看日志
```

---

## 示例：多房间部署

### 场景描述

- **房间 A**: 主控室，有 Web 界面
- **房间 B**: 液体处理室
- **房间 C**: 分析仪器室

### 房间 A - 主节点

```bash
# host.json
unilab --ak your_ak --sk your_sk -g host.json --port-management 8002
```

### 房间 B - 从节点 1

```bash
# liquid_handler.json
unilab --ak your_ak --sk your_sk -g liquid_handler.json --is_slave --port-management 8003
```

### 房间 C - 从节点 2

```bash
# analytical.json
unilab --ak your_ak --sk your_sk -g analytical.json --is_slave --port-management 8004
```

---

## 故障处理

### 节点离线

**检测**:

```bash
ros2 node list  # 查看在线节点
```

**处理**:

1. 检查网络连接
2. 重启节点
3. 检查日志

### 从节点无法连接主节点

1. 检查网络：

   ```bash
   ping <host_ip>
   ```

2. 检查 ROS_DOMAIN_ID：

   ```bash
   echo $ROS_DOMAIN_ID
   ```

3. 使用`--slave_no_host`测试：
   ```bash
   unilab --ak your_ak --sk your_sk -g slave.json --is_slave --slave_no_host
   ```

### 通信延迟

**排查**:

```bash
# 网络延迟
ping <node_ip>

# ROS2话题延迟
ros2 topic hz /device_status
ros2 topic bw /device_status
```

**优化**:

- 减少发布频率
- 使用 QoS 配置
- 优化网络带宽

### 数据同步失败

**检查**:

```bash
# 查看日志
tail -f unilabos_data/logs/unilab.log | grep sync
```

**解决**:

- 检查云端连接
- 验证 AK/SK
- 手动触发同步

### 资源不可见

检查资源注册：

```bash
ros2 service call /host_node/resource_list \
  unilabos_msgs/srv/ResourceList
```

---

## 监控和维护

### 节点状态监控

```bash
# 查看所有节点
ros2 node list

# 查看话题
ros2 topic list
```

---

## 相关文档

- [最佳实践指南](../user_guide/best_practice.md) - 完整的实验室搭建流程
- [安装指南](../user_guide/installation.md) - 环境安装步骤
- [启动参数详解](../user_guide/launch.md) - 启动参数说明
- [添加设备驱动](add_device.md) - 自定义设备开发
- [工作站架构](examples/workstation_architecture.md) - 复杂工作站搭建

---

## 参考资料

- [ROS2 网络配置](https://docs.ros.org/en/jazzy/Tutorials/Advanced/Networking.html)
- [DDS 配置](https://fast-dds.docs.eprosima.com/)
- Uni-Lab 云平台文档
