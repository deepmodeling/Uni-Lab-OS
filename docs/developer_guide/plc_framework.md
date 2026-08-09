# PLC 设备接管框架

> 本文档面向初次接触 UniLab-OS 的开发者，介绍系统如何通过工业协议"接管"（连接并控制）PLC 设备。

## 什么是"PLC 接管"？

**PLC**（可编程逻辑控制器）是工厂设备的控制核心，驱动机械臂、泵、阀门等硬件。UniLab-OS 通过网络协议直接读写 PLC 内部变量，从而控制设备运行：

```
UniLab-OS（Python）  ←通信协议→  PLC  ←电信号→  电机/气缸/传感器
```

UniLab-OS 提供两套接管框架，对应两种工业协议：

| 框架            | 协议             | 应用项目           | 核心文件                                                    |
| --------------- | ---------------- | ------------------ | ----------------------------------------------------------- |
| **Modbus 框架** | Modbus TCP / RTU | 扣式电池装配工站   | `unilabos/device_comms/modbus_plc/client.py`                |
| **OPC UA 框架** | OPC UA           | 后处理工站（怀柔） | `unilabos/devices/workstation/post_process/post_process.py` |

两套框架**设计思想完全一致**，底层通信协议不同。理解一个，另一个基本触类旁通。

---

## 核心概念

### 节点（Node）

节点是 PLC 内部一个具体的变量地址，可以理解为 PLC 的一个输入/输出端口。

| 属性 | 说明                                   | 示例                 |
| ---- | -------------------------------------- | -------------------- |
| 名称 | 人类可读标识                           | `COIL_SYS_START_CMD` |
| 地址 | PLC 内存地址                           | `0x0064`             |
| 类型 | Coil / HoldRegister / InputRegister 等 | `coil`               |

```
PLC 内存空间
├── Coil 区:    True / False  ← 控制开关量（启动/停止/复位）
├── Hold Reg:   120, 35.5 …  ← 存参数值（速度、位置）
└── Input Reg:  99.8, 42 …   ← 只读传感器数据
```

### 动作生命周期（Action Lifecycle）

每个设备动作被拆分为 4 个阶段，用 `try/finally` 保证安全性：

```python
try:
    init(...)    # 写入参数（速度、位置等）— 可选
    start(...)   # 发触发信号 + 轮询等待完成
    stop(...)    # 复位触发信号（成功时执行）
except:
    is_err = True
finally:
    cleanup(...) # 无论成败都执行，作为安全兜底
```

| 阶段      | 何时执行                | 典型内容                             |
| --------- | ----------------------- | ------------------------------------ |
| `init`    | 成功路径（可选）        | 写运动速度 = 20.0                    |
| `start`   | 成功路径                | 写启动位 = True，等待完成位 = True   |
| `stop`    | 成功路径                | 写启动位 = False（正常复位）         |
| `cleanup` | **无论成败**（finally） | 安全兜底复位，防止异常时设备持续运动 |

> **为什么 `cleanup` 无论成败都执行？**
> 若 `start` 阶段因传感器故障抛出异常，`stop` 会被跳过，PLC 触发位仍为 `True`——设备可能持续运动。`cleanup` 放在 `finally` 块中，作为最后的安全保障，确保 PLC 一定被复位到安全状态。实际上大多数动作将 `cleanup` 设为 `null`，由 `stop` 负责正常复位即可。

---

## Modbus 框架

**核心文件**：`unilabos/device_comms/modbus_plc/client.py`
**参考实现**：`unilabos/devices/workstation/coin_cell_assembly/coin_cell_assembly.py`

### 连接与节点注册

```python
from unilabos.device_comms.modbus_plc.client import TCPClient, BaseClient

# 1. 建立 TCP 连接
client = TCPClient(addr="172.16.28.102", port=502)
client.client.connect()

# 2. 从 CSV 加载节点定义
nodes = BaseClient.load_csv("coin_cell_assembly_b.csv")

# 3. 注册节点，之后可按名称访问
client.register_node_list(nodes)

# 访问节点
client.use_node('COIL_SYS_START_CMD').write(True)
value, err = client.use_node('COIL_SYS_START_STATUS').read(1)
```

**CSV 格式**（`Name` / `DeviceType` / `Address` / `DataType`）：

| Name               | DeviceType    | Address | DataType |
| ------------------ | ------------- | ------- | -------- |
| COIL_SYS_START_CMD | coil          | 100     | INT16    |
| REG_SPEED          | hold_register | 200     | FLOAT32  |

### 三段式接管流程（扣式电池工站）

PLC 设备通常需要按固定顺序切换模式，以扣式电池工站为例：

```
Python                             PLC
  │── 写 HAND_CMD = True ─────────>│ 切换到手动模式
  │<─ 读 HAND_STATUS = True ────────│ 确认进入手动
  │── 写 INIT_CMD = True ──────────>│ 执行初始化
  │<─ 读 INIT_STATUS = True ─────────│ 初始化完成
  │── 写 HAND_CMD = False ──────────>│ 复位（脉冲信号）
  │── 写 INIT_CMD = False ──────────>│ 复位
  │── 写 AUTO_CMD = True ───────────>│ 切换自动模式
  │<─ 读 AUTO_STATUS = True ─────────│ 自动模式就绪
  │── 写 AUTO_CMD = False ──────────>│ 复位
  │── 写 START_CMD = True ──────────>│ 开始运行
  │<─ 读 START_STATUS = True ────────│ 运行确认
  │── 写 START_CMD = False ──────────>│ 复位
```

> **脉冲信号模式**：命令写 `True` → 等待 PLC 状态位确认 → 命令写回 `False`，这是大多数 PLC 的标准触发方式，而不是保持高电平。

### JSON 配置方式

Modbus 框架支持纯 JSON 配置，无需手写 Python 流程：

```json
{
    "register_node_list_from_csv_path": {"path": "M01.csv"},
    "create_flow": [
        {
            "name": "归位",
            "action": [{
                "address_function_to_create": [
                    {"func_name": "pos_tip",      "node_name": "M01_idlepos_coil_w",    "mode": "write", "value": true},
                    {"func_name": "pos_tip_read", "node_name": "M01_idlepos_coil_r",    "mode": "read",  "value": 1},
                    {"func_name": "manual_stop",  "node_name": "M01_manual_stop_coil_r","mode": "read",  "value": 1}
                ],
                "create_init_function":  {"func_name": "idel_init", "node_name": "M01_idlepos_velocity_rw", "mode": "write", "value": 20.0},
                "create_start_function": {
                    "func_name": "idel_position",
                    "write_functions": ["pos_tip"],
                    "condition_functions": ["pos_tip_read", "manual_stop"],
                    "stop_condition_expression": "pos_tip_read[0] and manual_stop[0]"
                },
                "create_stop_function":  {"func_name": "idel_stop", "node_name": "M01_idlepos_coil_w", "mode": "write", "value": false},
                "create_cleanup_function": null
            }]
        }
    ],
    "execute_flow": ["归位"]
}
```

执行：

```python
client.execute_procedure_from_json(json_data)
```

---

## OPC UA 框架

**核心文件**：`unilabos/devices/workstation/post_process/post_process.py`
**参考实现**：`unilabos/devices/workstation/post_process/opcua_huairou.json`

### 与 Modbus 的主要区别

| 特性       | Modbus               | OPC UA                            |
| ---------- | -------------------- | --------------------------------- |
| 节点发现   | 手动填写 CSV 地址    | **自动遍历**服务器节点树          |
| 数据获取   | 轮询（主动问）       | **订阅推送**（有变化时通知）      |
| 节点标识   | 数字地址（如 `100`） | 字符串 NodeId（如 `ns=2;s=速度`） |
| 断线处理   | 无                   | **后台监控线程**自动重连          |
| 认证安全   | 无                   | 支持用户名/密码                   |
| 工作流调用 | 手动调用             | **自动注册为实例方法**            |

### 连接与节点发现

```python
from unilabos.devices.workstation.post_process.post_process import OpcUaClient

client = OpcUaClient(
    url="opc.tcp://192.168.1.100:4840",
    username="admin",              # 可选
    password="123456",             # 可选
    config_path="opcua_huairou.json",  # 自动加载工作流配置
    cache_timeout=5.0,             # 节点值缓存 5 秒
    subscription_interval=500,     # 每 500ms 接收推送
)

# 节点自动通过订阅保持最新值，读取直接查本地缓存
value = client.get_node_value("grab_complete")
```

### JSON 配置方式

```json
{
    "register_node_list_from_csv_path": {"path": "opcua_nodes_huairou.csv"},
    "create_flow": [
        {
            "name": "trigger_grab_action",
            "description": "触发反应罐及原料罐抓取动作",
            "parameters": ["reaction_tank_number", "raw_tank_number"],
            "action": [{
                "init_function": {
                    "func_name": "init_grab_params",
                    "write_nodes": ["reaction_tank_number", "raw_tank_number"]
                },
                "start_function": {
                    "func_name": "start_grab",
                    "write_nodes": {"grab_trigger": true},
                    "condition_nodes": ["grab_complete"],
                    "stop_condition_expression": "grab_complete == True",
                    "timeout_seconds": 999999.0
                },
                "stop_function": {
                    "func_name": "stop_grab",
                    "write_nodes": {"grab_trigger": false}
                }
            }]
        }
    ]
}
```

配置加载后，工作流自动注册为实例方法：

```python
# 直接调用，传入参数，框架自动写入对应节点
client.trigger_grab_action(reaction_tank_number=2, raw_tank_number=3)
```

---

## 新增设备快速上手

### 使用 Modbus 框架

```
1. 从 PLC 工程师处拿到地址表，按格式填写 CSV（Name/DeviceType/Address/DataType）
2. 继承 BaseClient，在 __init__ 中连接并注册节点
3. 参考 coin_cell_assembly.py 编写三段式接管函数（手动→初始化→自动→启动）
4. 或直接编写 JSON 配置，调用 execute_procedure_from_json()
```

### 使用 OPC UA 框架

```
1. 确认设备支持 OPC UA，拿到服务器 URL（格式：opc.tcp://IP:PORT）
2. 准备 CSV 节点定义文件（可选，也可让框架自动发现）
3. 编写 JSON 配置：定义 parameters、init/start/stop 函数
4. 实例化 OpcUaClient，传入 config_path，直接调用自动注册的工作流方法
```

---

## 常见问题

**Q: `node {name} is not registered` 报错？**
A: 节点名不在 CSV 或未调用 `register_node_list_from_csv_path()`。

**Q: 程序卡死在 `while not status(): sleep(1)`？**
A: PLC 未返回预期完成信号。检查：PLC 是否在正确运行模式、状态位地址是否正确、PLC 有无报警。

**Q: OPC UA 连接成功但读不到节点？**
A: 检查节点名称是否与服务器显示名一致（区分中英文）。可调用 `_find_nodes()` 打印服务器全部节点。

**Q: 应该选 Modbus 还是 OPC UA？**
A: 取决于设备支持的协议，由 PLC 工程师决定。OPC UA 功能更完整，条件允许优先选择。

---

## 下一步

- {doc}`add_device` - 将驱动集成进 UniLab-OS 设备节点
- {doc}`add_action` - 为设备添加可调度的动作指令
- {doc}`add_yaml` - 编写设备注册表 YAML 文件
