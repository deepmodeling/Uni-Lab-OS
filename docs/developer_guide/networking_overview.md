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
│           uni-lab.bohrium.com                │
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
unilab --ak your_ak --sk your_sk -g slave_devices.json --is_slave
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
unilab --ak your_ak --sk your_sk -g host.json
```

**从节点**:

```bash
unilab --ak your_ak --sk your_sk -g slave1.json --is_slave
unilab --ak your_ak --sk your_sk -g slave2.json --is_slave --port 8003
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
unilab --ak your_ak --sk your_sk --upload_registry --use_remote_resource

# 实验室B
unilab --ak your_ak --sk your_sk --upload_registry --use_remote_resource
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
unilab --ak your_ak --sk your_sk -g host.json --port 8002
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
unilab --ak your_ak --sk your_sk -g slave1.json --is_slave --port 8003

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

### 防火墙配置

**建议做法**：

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
curl https://uni-lab.bohrium.com/api/v1/health

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
unilab --ak your_ak --sk your_sk -g host.json --port 8002
```

### 房间 B - 从节点 1

```bash
# liquid_handler.json
unilab --ak your_ak --sk your_sk -g liquid_handler.json --is_slave --port 8003
```

### 房间 C - 从节点 2

```bash
# analytical.json
unilab --ak your_ak --sk your_sk -g analytical.json --is_slave --port 8004
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
- [工作站架构](workstation_architecture.md) - 复杂工作站搭建

---

## 参考资料

- [ROS2 网络配置](https://docs.ros.org/en/humble/Tutorials/Advanced/Networking.html)
- [DDS 配置](https://fast-dds.docs.eprosima.com/)
- Uni-Lab 云平台文档
