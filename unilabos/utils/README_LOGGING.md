# UniLabOS 日志配置说明

> **文件位置**: `unilabos/utils/log.py`  
> **最后更新**: 2026-01-11  
> **维护者**: Uni-Lab-OS 开发团队

本文档说明 UniLabOS 日志系统中对第三方库和内部模块的日志级别配置，避免控制台被过多的 DEBUG 日志淹没。

---

## 📋 已屏蔽的日志

以下库/模块的日志已被设置为 **WARNING** 或 **INFO** 级别，不再显示 DEBUG 日志：

### 1. pymodbus（Modbus 通信库）

**配置位置**: `log.py` 第196-200行

```python
# pymodbus 库的日志太详细，设置为 WARNING
logging.getLogger('pymodbus').setLevel(logging.WARNING)
logging.getLogger('pymodbus.logging').setLevel(logging.WARNING)
logging.getLogger('pymodbus.logging.base').setLevel(logging.WARNING)
logging.getLogger('pymodbus.logging.decoders').setLevel(logging.WARNING)
```

**屏蔽原因**:
- pymodbus 在 DEBUG 级别会输出每一次 Modbus 通信的详细信息
- 包括 `Processing: 0x5 0x1e 0x0 0x0...` 等原始数据
- 包括 `decoded PDU function_code(3 sub -1) -> ReadHoldingRegistersResponse(...)` 等解码信息
- 这些信息对日常使用价值不大，但会快速刷屏

**典型被屏蔽的日志**:
```
[DEBUG] Processing: 0x5 0x1e 0x0 0x0 0x0 0x7 0x1 0x3 0x4 0x0 0x0 0x0 0x0 [handleFrame:72] [pymodbus.logging.base]
[DEBUG] decoded PDU function_code(3 sub -1) -> ReadHoldingRegistersResponse(...) [decode:79] [pymodbus.logging.decoders]
```

---

### 2. websockets（WebSocket 库）

**配置位置**: `log.py` 第202-205行

```python
# websockets 库的日志输出较多，设置为 WARNING
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('websockets.client').setLevel(logging.WARNING)
logging.getLogger('websockets.server').setLevel(logging.WARNING)
```

**屏蔽原因**:
- WebSocket 连接、断开、心跳等信息在 DEBUG 级别会频繁输出
- 对于长时间运行的服务，这些日志意义不大

---

### 3. ROS Host Node（设备状态更新）

**配置位置**: `log.py` 第207-208行

```python
# ROS 节点的状态更新日志过于频繁，设置为 INFO
logging.getLogger('unilabos.ros.nodes.presets.host_node').setLevel(logging.INFO)
```

**屏蔽原因**:
- 设备状态更新（如手套箱压力）每隔几秒就会更新一次
- DEBUG 日志会记录每一次状态变化，导致日志刷屏
- 这些频繁的状态更新对调试价值不大

**典型被屏蔽的日志**:
```
[DEBUG] [/devices/host_node] Status updated: BatteryStation.data_glove_box_pressure = 4.229457855224609 [property_callback:666] [unilabos.ros.nodes.presets.host_node]
```

---

### 4. asyncio 和 urllib3

**配置位置**: `log.py` 第224-225行

```python
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)
```

**屏蔽原因**:
- asyncio: 异步 IO 的内部调试信息
- urllib3: HTTP 请求库的连接池、重试等详细信息

---

## 🔧 如何临时启用这些日志（调试用）

### 方法1: 修改 log.py（永久启用）

在 `log.py` 的 `configure_logger()` 函数中，将对应库的日志级别改为 `logging.DEBUG`:

```python
# 临时启用 pymodbus 的 DEBUG 日志
logging.getLogger('pymodbus').setLevel(logging.DEBUG)
logging.getLogger('pymodbus.logging').setLevel(logging.DEBUG)
logging.getLogger('pymodbus.logging.base').setLevel(logging.DEBUG)
logging.getLogger('pymodbus.logging.decoders').setLevel(logging.DEBUG)
```

### 方法2: 在代码中临时启用（单次调试）

在需要调试的代码文件中添加：

```python
import logging

# 临时启用 pymodbus DEBUG 日志
logging.getLogger('pymodbus').setLevel(logging.DEBUG)

# 你的 Modbus 调试代码
...

# 调试完成后恢复
logging.getLogger('pymodbus').setLevel(logging.WARNING)
```

### 方法3: 使用环境变量或配置文件（推荐）

未来可以考虑在启动参数中添加 `--debug-modbus` 等选项来动态控制。

---

## 📊 日志级别说明

| 级别 | 数值 | 用途 | 是否显示 |
|------|------|------|---------|
| TRACE | 5 | 最详细的跟踪信息 | ✅ |
| DEBUG | 10 | 调试信息 | ✅ |
| INFO | 20 | 一般信息 | ✅ |
| WARNING | 30 | 警告信息 | ✅ |
| ERROR | 40 | 错误信息 | ✅ |
| CRITICAL | 50 | 严重错误 | ✅ |

**当前配置**:
- UniLabOS 自身代码: DEBUG 及以上全部显示
- pymodbus/websockets: **WARNING** 及以上显示（屏蔽 DEBUG/INFO）
- ROS host_node: **INFO** 及以上显示（屏蔽 DEBUG）

---

## ⚠️ 重要提示

### 修改生效时间
- 修改 `log.py` 后需要 **重启 unilab 服务** 才能生效
- 不需要重新安装或重新编译

### 调试 Modbus 通信问题
如果需要调试 Modbus 通信故障，应该：
1. 临时启用 pymodbus DEBUG 日志（方法2）
2. 复现问题
3. 查看详细的通信日志
4. 调试完成后记得恢复 WARNING 级别

### 调试设备状态问题
如果需要调试设备状态更新问题：
```python
logging.getLogger('unilabos.ros.nodes.presets.host_node').setLevel(logging.DEBUG)
```

---

## 📝 维护记录

| 日期 | 修改内容 | 操作人 |
|------|---------|--------|
| 2026-01-11 | 初始创建，添加 pymodbus、websockets、ROS host_node 屏蔽 | - |
| 2026-01-07 | 添加 pymodbus 和 websockets 屏蔽（log-0107.py） | - |

---

## 🔗 相关文件

- `log.py` - 日志配置主文件
- `unilabos/devices/workstation/coin_cell_assembly/` - 使用 Modbus 的扣电工作站代码
- `unilabos/ros/nodes/presets/host_node.py` - ROS 主机节点代码

---

**维护提示**: 如果添加了新的第三方库或发现新的日志刷屏问题，请在此文档中记录并更新 `log.py` 配置。
