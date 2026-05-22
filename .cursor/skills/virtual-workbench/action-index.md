# Action Index — virtual_workbench

6 个动作，按功能分类。每个动作的完整 JSON Schema 在 `actions/<name>.json`。

---

## 物料准备

### `auto-prepare_materials`

批量准备物料（虚拟起始节点），生成 A1-A5 物料编号，输出 5 个 handle 供后续节点使用

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/prepare_materials.json`](actions/prepare_materials.json)
- **可选参数**: `count`（物料数量，默认 5）

---

## 机械臂 & 加热台操作

### `auto-move_to_heating_station`

将物料从 An 位置移动到空闲加热台（竞争机械臂，自动查找空闲加热台）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_heating_station.json`](actions/move_to_heating_station.json)
- **核心参数**: `material_number`（物料编号，integer）

### `auto-start_heating`

启动指定加热台的加热程序（可并行，3 个加热台同时工作）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/start_heating.json`](actions/start_heating.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）

### `auto-move_to_output`

将加热完成的物料从加热台移动到输出位置 Cn

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/move_to_output.json`](actions/move_to_output.json)
- **核心参数**: `station_id`（加热台 ID），`material_number`（物料编号）

---

## 物料转移

### `transfer`

异步转移物料到目标设备（通过 ROS 资源转移）

- **action_type**: `UniLabJsonCommandAsync`
- **Schema**: [`actions/transfer.json`](actions/transfer.json)
- **核心参数**: `resource`, `target_device`, `mount_resource`
- **占位符字段**:
  - `resource` — **ResourceSlot**，待转移的物料数组 `[{id, name, uuid}, ...]`
  - `target_device` — **DeviceSlot**，目标设备路径字符串
  - `mount_resource` — **ResourceSlot**，目标孔位数组 `[{id, name, uuid}, ...]`

---

## 人工确认

### `manual_confirm`

创建人工确认节点，等待用户手动确认后继续（含物料转移上下文）

- **action_type**: `UniLabJsonCommand`
- **Schema**: [`actions/manual_confirm.json`](actions/manual_confirm.json)
- **核心参数**: `resource`, `target_device`, `mount_resource`, `timeout_seconds`, `assignee_user_ids`
- **占位符字段**:
  - `resource` — **ResourceSlot**，物料数组
  - `target_device` — **DeviceSlot**，目标设备路径
  - `mount_resource` — **ResourceSlot**，目标孔位数组
  - `assignee_user_ids` — `unilabos_manual_confirm` 类型
