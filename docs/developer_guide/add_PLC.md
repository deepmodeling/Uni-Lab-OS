# PLC 通信标准与设备驱动编写指南（基于 AI4M 工站）

> 本文档以 `unilabos/devices/workstation/AI4M`（水凝胶检测工站）为参考实现，
> 介绍如何将 PLC 控制的实验设备接入 Uni-Lab-OS：包含通信协议选型、节点表标准、
> 通信基类、设备驱动、Registry 配置以及调试方法。
>
> 阅读对象：负责现场调试与设备接入的同学。

---

## 0. 总览：一台 PLC 设备从硬件到云端的链路

```
       PLC（西门子 / 倍福 / 三菱 / 汇川 / 国产 PLC ...）
                   ▲
                   │  各家 PLC 私有协议（S7 / Modbus / EtherCAT ...）
                   │
        ┌──────────┴──────────┐
        │  OPC UA Server      │  ← 统一在 PLC 侧或独立网关上配置
        │  （内置或 KEPServer）│
        └──────────┬──────────┘
                   │  OPC UA over TCP（标准协议）
                   │
        ┌──────────┴──────────┐
        │  Uni-Lab 设备驱动    │  ← 本教程主体
        │  AI4MDevice         │
        │  ├─ base_opcua_client.py  通信基类
        │  ├─ opcua_nodes_*.csv     节点表（标准）
        │  └─ AI4M.py               动作函数
        └──────────┬──────────┘
                   │  ROS2 Action / 云端 HTTP
                   ▼
              实验记录本 / 云端调度
```

**统一约定**：所有 PLC 设备**只暴露 OPC UA 接口**给 Uni-Lab，PC 端不直接处理 S7 / Modbus 等底层协议。
这是 Uni-Lab 在工站类设备上的 PLC 通信标准。

---

## 1. 为什么选 OPC UA 作为标准？

| 维度 | 自研 TCP/串口协议 | Modbus | **OPC UA** |
|---|---|---|---|
| 厂家无关 | ✗ | 部分 | **✓** |
| 自带类型系统 | ✗ | ✗（裸寄存器） | **✓（Boolean/Int16/Float...）** |
| 命名空间 / 节点树 | ✗ | ✗（地址=魔数） | **✓（带名字、可分组）** |
| 订阅推送 | ✗ | ✗ | **✓（DataChange Notification）** |
| 鉴权 / 加密 | 自己造 | ✗ | **✓** |
| 与 PLC 工程师沟通成本 | 高 | 中 | **低（按变量名沟通）** |

实际接入时，PLC 工程师只需要在 PLC 侧把约定的"上位通讯变量"暴露到 OPC UA Server，
我们在 PC 侧就能用 `节点名 + 数据类型` 直接读写，不用管底层是 S7 还是 Modbus。

---

## 2. 节点表标准：`opcua_nodes_xxx.csv`

PLC 侧暴露的所有变量统一**用一张 CSV 表**描述，这是 PC 端和 PLC 端**唯一的接口契约**。
位置示例：`unilabos/devices/workstation/AI4M/opcua_nodes_AI4M.csv`。

### 2.1 列定义

| 列名 | 是否必填 | 说明 |
|---|---|---|
| `Name` | ✅ | 节点名（PLC 工程师在 PLC 项目中真实使用的变量名，通常是中文/原始名） |
| `EnglishName` | 推荐 | 英文别名，**PC 端代码全部用这个名字**调用 |
| `NodeType` | ✅ | `VARIABLE`（变量）或 `METHOD`（方法），AI4M 全部用变量 |
| `DataType` | ✅ | `BOOLEAN` / `INT16` / `INT32` / `FLOAT` / `DOUBLE` / `STRING` ... |
| `NodeLanguage` | 推荐 | `Chinese` / `English`，配合 `EnglishName` 做映射 |
| `NodeId` | ✅ | OPC UA 标准 NodeId，格式 `ns=<namespace>;s=<string>` 或 `ns=<n>;i=<int>` |

### 2.2 真实样例（节选自 `opcua_nodes_AI4M.csv`）

| Name | EnglishName | NodeType | DataType | NodeLanguage | NodeId |
|---|---|---|---|---|---|
| 机器人空闲 | `robot_ready` | VARIABLE | BOOLEAN | Chinese | `ns=4;s=上位通讯变量\|机器人空闲` |
| 机器人取烧杯编号 | `robot_pick_beaker_id` | VARIABLE | INT16 | Chinese | `ns=4;s=上位通讯变量\|机器人取烧杯编号` |
| 检测1请求参数 | `station_1_request_params` | VARIABLE | BOOLEAN | Chinese | `ns=4;s=上位通讯变量\|检测1请求参数` |
| 检测1工艺完成 | `station_1_process_complete` | VARIABLE | BOOLEAN | Chinese | `ns=4;s=上位通讯变量\|检测1工艺完成` |
| 磁力搅拌参数设置_C[0].搅拌速度 | `mag_stirrer_c0_stir_speed` | VARIABLE | INT16 | Chinese | `ns=4;s=上位通讯变量\|磁力搅拌参数设置_C[0].搅拌速度` |
| 报警复位 | `alarm_reset` | VARIABLE | BOOLEAN | Chinese | `ns=4;s=上位通讯变量\|报警复位` |

### 2.3 设计规范（必读）

1. **命名按"角色-编号-属性"分层**，便于代码批量寻址：
   - `mag_stirrer_c{0..4}_stir_speed`（搅拌仪 0~4 的搅拌速度）
   - `station_{1..3}_process_complete`（检测站 1~3 的完成信号）
   - `robot_rack_pick_beaker_{1..5}_complete`（取烧杯 1~5 的完成信号）

   这样在驱动里可以直接 `f"mag_stirrer_c{idx}_stir_speed"` 拼出节点名。

2. **数据类型与 PLC 侧严格一致**：
   - `BOOL` → `BOOLEAN`，`INT/WORD` → `INT16/UINT16`，`DINT` → `INT32`，`REAL` → `FLOAT`。
   - 类型不一致会触发 `BadTypeMismatch`，写入失败。

3. **NodeId 必须从 PLC 工程或 OPC UA Server 中导出**，不要自己拼。
   常见格式：
   - 西门子 1500：`ns=4;s=上位通讯变量|<变量名>`
   - 倍福 TwinCAT：`ns=4;s=PLC1.MAIN.<变量名>`
   - KEPServerEX：`ns=2;s=Channel1.Device1.<Tag>`

4. **每个工站一个独立 CSV**，不要共用。
   AI4M 中真机用 `opcua_nodes_AI4M.csv`，仿真用 `opcua_nodes_AI4M_sim.csv`。

---

## 3. 通信基类架构

文件：`unilabos/devices/workstation/AI4M/base_opcua_client.py`

整个通信层分两层：

```
BaseOpcUaClient                       # 最小可用：连接 + 节点注册 + 读写 + 方法调用
        ▲
        │ 继承
        │
OpcUaClientWithSubscription           # 生产可用：+ 订阅推送 + 缓存 + 自动重连
        ▲
        │ 继承
        │
AI4MDevice                            # 业务驱动：在它之上写设备动作函数
```

### 3.1 `BaseOpcUaClient` 核心能力

```python
class BaseOpcUaClient(UniversalDriver):
    client: Optional[Client] = None
    _node_registry: Dict[str, OpcUaNodeBase] = {}     # name -> Variable/Method
    _name_mapping: Dict[str, str] = {}                # 英文名 -> 中文名
    _reverse_mapping: Dict[str, str] = {}             # 中文名 -> 英文名
    _found_node_objects: Dict[str, Any] = {}          # 缓存 ua.Node 用于订阅

    @classmethod
    def load_csv(cls, file_path) -> Tuple[List[OpcUaNode], dict, dict]: ...
    def register_node_list(self, node_list) -> "BaseOpcUaClient": ...
    def use_node(self, name) -> OpcUaNodeBase: ...
    def read_node(self, node_name: str) -> str: ...   # 返回 JSON
    def write_node(self, json_input: str) -> str: ...
    def call_method(self, node_name, *args) -> Tuple[Any, bool]: ...
```

它做的事情可以归纳为四步：

1. **`load_csv`**：读取节点表，建立 `Name ↔ EnglishName` 双向映射。
2. **`register_node_list`**：把节点登记进 `_variables_to_find` 待查找列表。
3. **`_connect` → `_find_nodes`**：连上 OPC UA 后，按 `NodeId` 把每个节点解析成 `Variable` / `Method` 对象，放进 `_node_registry`。
4. **`use_node(name)`**：业务代码取节点的唯一入口，**支持中英文混用**，找不到会自动重试一次。

### 3.2 `OpcUaClientWithSubscription` 增强能力

在 `BaseOpcUaClient` 基础上提供三个生产环境必备的能力：

#### a) 订阅缓存（高频读零开销）

```python
def _setup_subscriptions(self):
    self._subscription = self.client.create_subscription(
        self._subscription_interval,    # 默认 500ms
        SubscriptionHandler(self),
    )
    for node_name, node in self._node_registry.items():
        if node.type == NodeType.VARIABLE and node.node_id:
            handle = self._subscription.subscribe_data_change(ua_node)
            self._subscription_handles[node_name] = handle
```

当 PLC 侧变量变化时，`datachange_notification` 回调会把新值写进 `self._node_values[name]`，
后续 `get_node_value` 优先读缓存——**业务代码可以放心地写 `while not self.get_node_value(...): time.sleep(1)` 而不用担心 OPC UA 频繁请求**。

#### b) 智能缓存的 `get_node_value`

```python
def get_node_value(self, name, use_cache=True, force_read=False):
    # 1. 中英文名归一化
    chinese_name = self._name_mapping.get(name, name)

    # 2. force_read=True 强制透传到 OPC UA Server
    if force_read: ...

    # 3. 命中订阅推送 → 直接返回缓存
    # 4. 命中按需读 + 未过期（cache_timeout=5s）→ 返回缓存
    # 5. 否则发起 read 并更新缓存
```

#### c) 连接监控 + 自动重连

后台线程每 30s 调一次 `client.get_namespace_array()` 探活，断线则自动 `disconnect → connect → 重新订阅`，最多重试 5 次。

### 3.3 数据类型 / 节点类型

`unilabos/device_comms/opcua_client/node/uniopcua.py`：

```python
class DataType(Enum):
    BOOLEAN = VariantType.Boolean
    INT16   = VariantType.Int16
    INT32   = VariantType.Int32
    FLOAT   = VariantType.Float
    STRING  = VariantType.String
    # ...

class NodeType(Enum):
    VARIABLE = NodeClass.Variable
    METHOD   = NodeClass.Method
    OBJECT   = NodeClass.Object
```

`Variable.write()` 内部会按 `DataType` 做强制类型转换，
所以 CSV 里的 `DataType` 列就是"PC 端转换写入值的类型说明书"。

---

## 4. 编写设备驱动：以 `AI4MDevice` 为例

文件：`unilabos/devices/workstation/AI4M/AI4M.py`

### 4.1 继承通信基类，最小骨架

```python
from typing import Optional
from unilabos.devices.workstation.AI4M.base_opcua_client import OpcUaClientWithSubscription

class AI4MDevice(OpcUaClientWithSubscription):
    def __init__(
        self,
        url: str,                              # opc.tcp://192.168.1.10:4840
        deck: Optional[AI4M_deck] = None,      # 物料台面（资源树）
        csv_path: str = None,                  # 节点表 CSV
        username: str = None,
        password: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0,
        subscription_interval: int = 500,
        *args, **kwargs,
    ):
        super().__init__(
            url=url, username=username, password=password,
            use_subscription=use_subscription,
            cache_timeout=cache_timeout,
            subscription_interval=subscription_interval,
            *args, **kwargs,
        )

        # 物料台面初始化（见教程 4. 物料系统）
        self.deck = deck or AI4M_deck(setup=True)
        self._robot_lock = threading.Lock()

        # 关键：加载节点表
        if csv_path:
            self.load_nodes_from_csv(csv_path)
```

`load_nodes_from_csv` 会一次性完成：解析 CSV → 注册节点 → 解析 NodeId → 建立订阅，
**之后整个驱动都通过 `self.get_node_value(name)` / `self.set_node_value(name, value)` 操作 PLC**。

### 4.2 PLC 通信的核心模式：握手协议（Handshake）

PLC 编程的本质是"扫描周期 + 状态机"，PC 端**绝对不能用 fire-and-forget 的方式发指令**。
和 PLC 配合的标准模式是 **"PC 写指令 → PC 等待 PLC 回执 → PC 复位指令"**。

AI4M 中所有 `trigger_*` 函数都遵循以下三种握手范式之一：

#### 范式 A：脉冲触发 + 完成信号（最常用）

```python
def trigger_init(self) -> dict:
    # ① 复位上一轮残留
    self.set_node_value("alarm_reset", True); time.sleep(1.0)
    self.set_node_value("alarm_reset", False)
    self.set_node_value("manual_auto_switch", False)

    # ② 等待 PLC 退出自动模式
    while self.get_node_value("auto_mode"):
        time.sleep(1.0)

    # ③ 发起初始化脉冲（True → False）
    self.set_node_value("initialize", True); time.sleep(1.0)
    self.set_node_value("initialize", False)

    # ④ 等待 PLC 给出完成信号
    while not self.get_node_value("init finished"):
        time.sleep(1.0)

    return {"message": "设备初始化完成"}
```

要点：
- **"PC 写一个 BOOL 拉高再拉低"** 模拟脉冲，PLC 用上升沿触发动作。
- **`get_node_value` 要在 while 循环里轮询**，配合订阅缓存基本无压力。
- **每个动作必须有"开始"和"完成"两个独立的 BOOL 节点**，不能复用。

#### 范式 B：参数下发 + 请求/已执行/完成 三步握手（带数据的工艺）

```python
def trigger_station_process(self, station_id: int, mag_stir_speed: int, ...):
    request_node        = f"station_{station_id}_request_params"
    params_received_node = f"station_{station_id}_params_received"
    start_node          = f"station_{station_id}_start"
    complete_node       = f"station_{station_id}_process_complete"

    # ① PC 复位三个状态位（避免上一轮影响）
    self._reset_station_process_flags(station_id)

    # ② 等 PLC 主动请求参数（PLC 准备好了才接收）
    while not self.get_node_value(request_node):
        time.sleep(1.0)

    # ③ PC 下发参数（注意：PLC 内部数组是 0-based，PC 暴露给用户是 1-based）
    station_idx = station_id - 1
    self.set_node_value(f"mag_stirrer_c{station_idx}_stir_speed", mag_stir_speed)
    self.set_node_value(f"mag_stirrer_c{station_idx}_heat_temp",  mag_stir_heat_temp)
    self.set_node_value(f"mag_stirrer_c{station_idx}_time_set",   mag_stir_time_set)
    self.set_node_value(f"syringe_pump_{station_idx}_abs_position_set", syringe_pump_abs_pos)

    # ④ PC 通知 PLC "参数已就绪"，等 PLC 回复"已执行"
    self.set_node_value(start_node, True)
    while not self.get_node_value(params_received_node):
        time.sleep(1.0)

    # ⑤ 等 PLC 完成整个工艺
    while not self.get_node_value(complete_node):
        time.sleep(5.0)

    self.set_node_value(start_node, False)   # 复位，方便下一轮
    return {"station_id": station_id, "message": "..."}
```

四个状态位的语义：

| 信号 | 方向 | 含义 |
|---|---|---|
| `station_X_request_params` | **PLC → PC** | "我准备好了，把参数给我" |
| `station_X_start` | **PC → PLC** | "参数我已经写好了，开干" |
| `station_X_params_received` | **PLC → PC** | "参数我已经吃下了" |
| `station_X_process_complete` | **PLC → PC** | "工艺已经做完" |

**这是 PLC 通信教科书级别的标准范式**，所有带数据下发的动作都建议照抄。

#### 范式 C：编号下发 + 编号对应的完成信号（多目标互锁）

```python
def trigger_robot_pick_beaker(self, pick_beaker_id: int, place_station_id: int = None, ...):
    # ① 等机器人空闲（互锁）
    while not self.get_node_value("robot_ready"):
        time.sleep(1.0)

    # ② 阶段一：下发"取哪一杯"编号 + 等"取这一杯完成"
    pick_complete_node  = f"robot_rack_pick_beaker_{pick_beaker_id}_complete"
    self.set_node_value("robot_pick_beaker_id", pick_beaker_id)
    while not self.get_node_value(pick_complete_node):
        time.sleep(1.0)

    # ③ 阶段二：下发"放到哪个工站"编号 + 等"放完成"
    place_complete_node = f"robot_place_station_{place_station_id}_complete"
    self._reset_station_process_flags(place_station_id)
    self.set_node_value("robot_place_station_id", place_station_id)
    while not self.get_node_value(place_complete_node):
        time.sleep(1.0)
```

要点：
- **同一个动作的多个目标用"编号变量 + 编号对应的完成信号"实现**，不要每个目标都开一个开始位。
- **配合 Python 端 `threading.Lock()` 做软互锁**，避免多个线程争抢机器人。
- **每个阶段有独立的完成信号**，串行等待，不能合并。

### 4.3 一些容易踩坑的细节

1. **节点名映射**
   `set_node_value("alarm_reset", True)` 实际写入的是 CSV 中文名 `报警复位`，
   `get_node_value` 同理。**业务代码全部用 EnglishName**，不要直接用中文。

2. **PLC 数组索引和 PC 不一致**
   AI4M 里 PC 暴露 `station_id ∈ {1, 2, 3}`，但 PLC 内部数组是 `C[0..2]`，
   驱动里要做 `station_idx = station_id - 1`，**这种映射只在驱动层做一次**，
   不要让上层（registry / 实验记录本）感知。

3. **订阅模式下 BOOL 节点的边沿同步**
   订阅有 ~500ms 延迟。如果你刚 `set_node_value(x, True)` 就立刻 `get_node_value(x)`，
   读到的可能还是 `False`（订阅还没推回来）。
   解决方案：**写完后用 `force_read=True` 透传一次** 或加一段 `time.sleep`。

4. **永远不要忘记复位**
   `start` 拉 True 后必须有地方拉回 False，否则下一轮 PLC 上升沿不触发。
   AI4M 在 `_reset_station_process_flags` 中统一做：

   ```python
   def _reset_station_process_flags(self, station_id: int) -> None:
       self.set_node_value(f"station_{station_id}_process_complete", False)
       self.set_node_value(f"station_{station_id}_start", False)
       self.set_node_value(f"station_{station_id}_params_received", False)
   ```

5. **耗时长的等待 sleep 加大**
   工艺等待用 `time.sleep(5.0)`，机器人等待用 `time.sleep(1.0)`，初始化等待 `time.sleep(1.0)`，
   不要全部用 0.1s 轮询，会把日志刷爆。

---

## 5. 把驱动接到 Uni-Lab：Registry + Graph

### 5.1 Registry YAML（动作 schema）

文件：`unilabos/registry/devices/AI4M_station.yaml`

```yaml
AI4M_station:
  category: [AI4M_station]
  class:
    module: unilabos.devices.workstation.AI4M.AI4M:AI4MDevice    # ← 入口类
    type: python
    action_value_mappings:
      auto-trigger_init:
        schema:
          description: 设备初始化...
          properties:
            goal:    { properties: {}, required: [], type: object }
            result:
              properties: { message: { type: string } }
              required: [message]
              type: object
          type: object
        type: UniLabJsonCommand

      auto-trigger_station_process:
        always_free: true
        schema:
          description: 执行检测工艺流程
          properties:
            goal:
              properties:
                station_id:                   { type: integer, description: 检测编号 1-3 }
                mag_stir_stir_speed:          { type: integer }
                mag_stir_heat_temp:           { type: integer }
                mag_stir_time_set:            { type: integer }
                syringe_pump_abs_position_set:{ type: integer }
              required: [station_id, mag_stir_stir_speed, mag_stir_heat_temp,
                         mag_stir_time_set, syringe_pump_abs_position_set]
              type: object
            result: { ... }
        type: UniLabJsonCommand

  init_param_schema:
    config:
      type: object
      required: [url]
      properties:
        url:                   { type: string,  description: OPC UA 服务器地址 }
        csv_path:              { type: string,  description: 节点配置 CSV 路径 }
        deck:                  { type: string,  description: 资源树配置 }
        username:              { type: string }
        password:              { type: string }
        use_subscription:      { type: boolean, default: true }
        cache_timeout:         { type: number,  default: 5.0 }
        subscription_interval: { type: integer, default: 500 }
```

规则总结：
- `class.module` 指向驱动类（`module:ClassName`）。
- `action_value_mappings` 中的 key 形如 `auto-<方法名>`，对应驱动里的同名 Python 方法。
- `schema.goal` 自动转成 ROS2 Action 的 goal 消息，`schema.result` 转 result。
- `init_param_schema.config` 对应 `__init__` 的入参，**所有需要现场改的参数都要列出来**（最重要的就是 `url` 和 `csv_path`）。
- `always_free: true` 表示该动作不占用工站独占锁（多检测站可并发执行）。

### 5.2 Graph JSON（实例化）

文件：`unilabos/devices/workstation/AI4M/AI4M.json`

```json
{
  "nodes": [
    {
      "id": "AI4M_station",
      "name": "AI4M_station",
      "type": "device",
      "class": "AI4M_station",
      "children": ["AI4M_deck"],
      "parent": null,
      "config": {
        "url": "opc.tcp://192.168.1.10:4840",
        "csv_path": "opcua_nodes_AI4M.csv",
        "deck": {
          "data": {
            "_resource_child_name": "AI4M_deck",
            "_resource_type": "unilabos.devices.workstation.AI4M.decks:AI4M_deck"
          }
        }
      }
    },
    {
      "id": "AI4M_deck",
      "type": "deck",
      "class": "AI4M_deck",
      "parent": "AI4M_station",
      "config": { "type": "AI4M_deck" }
    }
  ]
}
```

要点：
- `class` 必须和 Registry YAML 的顶层 key 完全一致（`AI4M_station`）。
- `config` 字段**逐字传给驱动 `__init__`**，所以 Graph JSON = "现场参数表"。
- 多套相同设备时拷贝一份，把 `id` / `url` 改掉即可（参考 `AI4M002_station`）。

### 5.3 启动命令（来自 `start.md`）

```cmd
# 真机
python unilabos/app/main.py -g unilabos/devices/workstation/AI4M/AI4M.json `
  --ak <ak> --sk <sk> --address <api_url> --disable_browser

# 仿真（KEPServerEX 跑在本机 49320 端口）
python unilabos/app/main.py -g unilabos/devices/workstation/AI4M/AI4Msim.json `
  --ak <ak> --sk <sk> --disable_browser
```

启动时会把 Registry schema 同步到本地微后端；Backend 和前端通过当前注册表 API
读取 `auto-*` 动作，不再由 Host 直接上传旧 Backend 注册表。

---

## 6. 调试方法

### 6.1 用 KEPServerEX 仿真 PLC

不带 PLC 的开发机上，可以用 KEPServerEX（或 `python-opcua` 自建 server）模拟。
AI4M 提供了一份仿真节点表 `opcua_nodes_AI4M_sim.csv`，**只改 NodeId 不改语义**，
所以驱动代码无需任何改动即可在本机调试。

### 6.2 单独跑驱动（不开 ROS）

在驱动文件末尾的 `if __name__ == '__main__':` 段：

```python
if __name__ == '__main__':
    A4 = AI4MDevice(
        url="opc.tcp://192.168.1.10:4840",
        csv_path="opcua_nodes_AI4M.csv",
    )
    A4.trigger_init()
    print("初始化完成")
    A4.trigger_robot_pick_beaker(1, 1)
```

**新动作上线前一定要在这里裸跑一遍**，确认握手时序正确，再往上接 ROS。

### 6.3 看日志判断卡在哪

`base_opcua_client.py` 的日志已经覆盖了所有关键节点：

```
✓ 客户端已连接!
✓ 找到变量节点: 'robot_ready', NodeId: ns=4;s=...
✓ 已订阅节点: robot_ready
✓ 节点查找完成：所有 142 个节点均已找到
```

如果看到 `⚠ 以下 N 个节点未找到`，**99% 是 CSV 里的 NodeId 写错了**，回去对一下 PLC 工程导出的 NodeId。

### 6.4 检查节点是否能直接读写

```python
# 透传读，绕过订阅缓存
A4.get_node_value("robot_ready", force_read=True)

# 直接读 JSON 形式（适合从 HTTP/调试面板调）
A4.read_node("robot_ready")

# 写
A4.set_node_value("alarm_reset", True)
A4.write_node('{"node_name": "alarm_reset", "value": false}')
```

---

## 7. 接入新 PLC 设备的 Checklist

接到一台新工站时，按下面顺序做就能保证不漏：

- [ ] 1. 让 PLC 工程师把上位通讯变量整理到 OPC UA Server，导出 NodeId 清单。
- [ ] 2. 在 `unilabos/devices/workstation/<设备名>/` 下新建目录，复制 `AI4M/base_opcua_client.py` 不动。
- [ ] 3. 整理 `opcua_nodes_<设备名>.csv`，6 列填齐，并补上 `EnglishName`。
- [ ] 4. 在该目录写设备驱动 `<设备名>.py`，继承 `OpcUaClientWithSubscription`：
  - [ ] `__init__` 调用 `super().__init__` + `self.load_nodes_from_csv(csv_path)`。
  - [ ] 每个动作函数用范式 A/B/C 写握手协议。
  - [ ] 每个动作函数都返回 `dict`，至少含 `message` 字段。
- [ ] 5. 在 `unilabos/registry/devices/` 下新建 `<设备名>_station.yaml`，配置 `init_param_schema` 和 `action_value_mappings`。
- [ ] 6. 在该目录新建 `<设备名>.json`（Graph），填好 `url` 和 `csv_path`。
- [ ] 7. 用 `if __name__ == '__main__':` 单独跑驱动确认握手 OK。
- [ ] 8. 用 `python unilabos/app/main.py -g <Graph> ...` 上线，通过微后端注册表 API 确认能力后下发动作回归。

---

## 8. 参考实现速查

| 关注点 | 在 AI4M 中看哪里 |
|---|---|
| OPC UA 通信基类 | `base_opcua_client.py` |
| 节点定义类型系统 | `unilabos/device_comms/opcua_client/node/uniopcua.py` |
| 节点表 CSV 标准 | `opcua_nodes_AI4M.csv` |
| 设备驱动入口类 | `AI4M.py: AI4MDevice` |
| 握手范式 A（脉冲+完成） | `AI4M.py: trigger_init` |
| 握手范式 B（请求/参数/完成） | `AI4M.py: trigger_station_process` |
| 握手范式 C（编号+完成） | `AI4M.py: trigger_robot_pick_beaker` |
| 自动模式批量参数下发 | `AI4M.py: download_auto_params` |
| Registry schema | `unilabos/registry/devices/AI4M_station.yaml` |
| Graph 实例化 | `AI4M.json` / `AI4Msim.json` |
| 启动命令 | `start.md` |
