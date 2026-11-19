---
name: yibin-electrolyte-submit
description: >-
  通过 Uni-Lab Notebook API 向宜宾电解液工站提交实验，覆盖配液分液（Bioyond LIMS）、
  扣电组装（CoinCellAssembly）、扣电测试全流程。
  包含 Excel 解析、formulation 构建、工作流节点参数填写、notebook 提交与状态轮询。
  Use when the user wants to submit electrolyte experiments, assemble or test coin cells,
  parse experiment Excel files, build notebook payloads, or mentions
  宜宾/配液/分液/扣电/电解液实验/notebook提交/CoinCell/BioyondLIMS.
---

# 宜宾电解液产线 API 操作指南

本 skill 覆盖两个设备的完整操作流程：
1. **配液分液工站** (`bioyond_cell_workstation`) — Bioyond LIMS 配液/分液/转运
2. **扣电组装站** (`BatteryStation`) — Modbus PLC 扣电组装/数据采集

## 设备信息

| 属性 | 配液分液工站 | 扣电组装站 |
|------|------------|-----------|
| device_id | `bioyond_cell_workstation` | `BatteryStation` |
| 显示名 | 配液分液工站 | 扣电工作站 |
| 源码 | `unilabos/devices/workstation/bioyond_studio/bioyond_cell/bioyond_cell_workstation.py` | `unilabos/devices/workstation/coin_cell_assembly/coin_cell_assembly.py` |
| 类名 | `BioyondCellWorkstation` | `CoinCellAssemblyWorkstation` |
| 通讯 | HTTP REST (Bioyond LIMS API) | Modbus TCP (PLC 寄存器) |

## 前置条件

### 认证信息

> AK / SK 不要写进文件：从环境变量 `UNILAB_AK` / `UNILAB_SK` 读取，认证头是 `Authorization: Lab <base64(AK:SK)>`。

```
AUTH="Authorization: Lab <base64(UNILAB_AK:UNILAB_SK)>"
BASE="https://uni-lab.test.bohrium.com"
```

来源：`--ak <UNILAB_AK> --sk <UNILAB_SK> --addr test`

### 启动 unilab（云端模式）

> **重要**：提交实验前必须确保 unilab 正在运行且已连接云端 WebSocket。

```powershell
$env:PYTHONIOENCODING="utf-8"
conda activate newunilab2603
cd D:\UniLabdev\Uni-Lab-OS\unilabos\devices\workstation
unilab -g D:\UniLabdev\Uni-Lab-OS\yibin_electrolyte_config.json --ak <UNILAB_AK> --sk <UNILAB_SK> --upload_registry --addr test --disable_browser --skip_env_check
```

**启动要点**：
1. 必须先激活虚拟环境 `newunilab2603`
2. 工作目录切到 `unilabos/devices/workstation`（设备驱动所在目录）
3. `--upload_registry` 将 64 个设备 + 142 个资源注册到云端
4. `--skip_env_check` + `PYTHONIOENCODING=utf-8` 避免 Windows GBK 编码崩溃
5. 启动后后台运行，等待日志出现 `Application startup complete` 和 `Host node ready signal published with 3 devices`

**验证连接成功的标志**：
- 日志出现 `[MessageProcessor] ... wss://uni-lab.test.bohrium.com/api/v1/ws/schedule`
- 日志出现 `[WebSocketClient] Host node ready signal published with 3 devices`
- 日志出现 `Resource tree add completed`（资源树同步完成）

### 云端物料上架与入库（启动后必做）

> **在提交实验之前，必须提醒用户完成以下云端操作，否则实验会因物料缺失而失败。**

1. **拖拽上料**：在云端 UI（`$BASE/laboratory/<lab_uuid>`）的资源树视图中，将物料拖拽到对应的仓库/库位上。unilab 启动后资源树会自动同步到云端，但物料的**上架位置**需要用户在 UI 上手动确认或调整。

2. **确认配液物料入库**：确保所有配液实验需要的试剂（如 LiPF6、EC、DMC、EMC 等）已在 LIMS 系统中完成入库。可通过以下方式验证：
   - 云端 UI 资源树中对应仓库（如"粉末加样头堆栈"、"配液站内试剂仓库"）下有物料节点
   - 或通过 API #8 获取资源树后检查物料节点是否存在

3. **告知 AI 可以提交**：用户完成上述操作后，告知 AI "物料已上架，可以提交实验"，AI 再执行 notebook 提交流程。

**提醒话术模板**（AI 应在启动成功后发送给用户）：
```
unilab 已成功启动并连接云端。提交实验前请完成以下操作：
1. 在云端 UI 上确认资源树中的物料位置，必要时拖拽调整上料位
2. 确保配液所需的试剂（粉末、液体）已在 LIMS 中完成入库
3. 完成后告诉我，我将为您提交实验
```

### 生成 Action Schema（首次使用）

启动 unilab 后，在 `unilabos_data/` 目录下会生成 `req_device_registry_upload.json`。运行以下命令提取两个设备的 action JSON：

```bash
python .cursor/skills/create-device-skill/scripts/extract_device_actions.py --registry unilabos_data/req_device_registry_upload.json bioyond_cell_workstation .cursor/skills/yibin-electrolyte-submit/actions/
python .cursor/skills/create-device-skill/scripts/extract_device_actions.py --registry unilabos_data/req_device_registry_upload.json BatteryStation .cursor/skills/yibin-electrolyte-submit/actions/
```

## 请求约定

- Windows 平台**必须用 `curl.exe`**（非 PowerShell 的 curl 别名）
- 所有请求带 `$AUTH` 头
- URL 格式：`$BASE/api/v1/<endpoint>`
- POST/PATCH 请求体写入临时 JSON 文件后用 `-d '@tmp.json'` 传参（避免 PowerShell 转义问题）
- 本地 API 基址：`http://127.0.0.1:8002/api/v1/`

## Session State

每次会话开始时，依次获取以下信息：

```bash
# 1. lab_uuid
curl.exe -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
# → data.uuid → $lab_uuid

# 2. project_uuid
curl.exe -s -X GET "$BASE/api/v1/lab/project/list?lab_uuid=$lab_uuid" -H "$AUTH"
# → data.items[].uuid/name → 让用户选择或取唯一项 → $project_uuid
```

## 工作流模板（重要）

> **必须向用户索要已有的工作流模板 UUID 或 URL，不要自行创建。**
>
> 原因：通过 `edge/workflow/node` API 创建节点会报 `resource_node_template not found`——
> 云端的工作流节点模板系统和设备注册表是独立的，需要用户在云端 UI 上预先配置好工作流模板。

**获取方式**：
- 用户提供工作流页面 URL，如 `$BASE/laboratory/<lab_uuid>/workflow/<workflow_uuid>`
- 从 URL 中提取 `workflow_uuid`
- 用 API 获取模板详情：

```
GET /api/v1/lab/workflow/template/detail/<workflow_uuid>
```

返回 `data.nodes[]`：每个节点的 uuid、name、param、device_name、handles、disabled。

**示例**：
```
工作流 URL: https://uni-lab.test.bohrium.com/laboratory/e9ed9102-d709-4741-b7a0-d1e8578e2065/workflow/b49f80d9-58d6-4456-a521-56f4dd39cda0
→ workflow_uuid = b49f80d9-58d6-4456-a521-56f4dd39cda0
```

从模板详情中提取**未 disabled** 的节点的 `uuid` 和 `name`，后续提交 notebook 时使用。

## API Endpoints

### #1 获取 lab_uuid

```
GET /api/v1/edge/lab/info
```

### #2 列出项目

```
GET /api/v1/lab/project/list?lab_uuid=$lab_uuid
```

返回 `data.items[]`，取 `uuid` 和 `name`。

### #3 获取工作流模板详情

```
GET /api/v1/lab/workflow/template/detail/<workflow_uuid>
```

返回 `data.nodes[]`：每个节点的 uuid、name、param、device_name、handles。
提取活跃节点（`disabled != true`）的 `uuid` 用于构建 notebook 请求。

### #4 提交实验（创建 notebook）— 核心 API

```
POST /api/v1/lab/notebook
Body: {
  "lab_uuid": "<lab_uuid>",
  "project_uuid": "<project_uuid>",
  "workflow_uuid": "<workflow_uuid>",
  "name": "<实验名称>",
  "node_params": [
    {
      "sample_uuids": [],
      "datas": [
        {
          "node_uuid": "<模板中的节点UUID>",
          "param": { <参数键值对> },
          "sample_params": []
        }
      ]
    }
  ]
}
```

**关键注意事项**：
- `node_params` 是数组，每个元素代表一轮实验
- `datas` 中每个节点对应模板中的一个活跃节点
- `param` 中的字段名**必须使用 Python 函数参数名**，不能用模板中存储的 LIMS 字段名（见下方映射表）

### #5 查询 notebook 状态

```
GET /api/v1/lab/notebook/status?uuid=<notebook_uuid>
```

| status | 含义 |
|--------|------|
| `running` | 执行中 |
| `success` | 成功 |
| `fail` | 失败 |

### #6 运行设备单动作（本地 API）

```
POST http://127.0.0.1:8002/api/v1/job/add
Body: {
  "device_id": "<device_id>",
  "action": "<action_name>",
  "action_args": { <参数键值对> },
  "sample_material": {}
}
```

本地 API 可自动解析 `action_type`，无需手动指定。适用于快速调试或云端未连接时。

### #7 查询本地任务状态

```
GET http://127.0.0.1:8002/api/v1/job/<job_id>/status
```

| status | 含义 |
|--------|------|
| 0 | UNKNOWN |
| 1 | ACCEPTED |
| 2 | EXECUTING |
| 4 | SUCCEEDED |
| 5 | CANCELED |
| 6 | ABORTED |

### #8 获取资源树

```
GET /api/v1/lab/material/download/<lab_uuid>
```

返回所有节点（`id`, `name`, `uuid`, `type`, `parent`）。填写 Slot 字段时用此接口筛选节点。

## Placeholder Slot 填写规则

action JSON 中 `placeholder_keys` 标记了哪些字段需要填 Slot：

| placeholder 值 | Slot 类型 | 填写格式 |
|---------------|-----------|---------|
| `unilabos_resources` | ResourceSlot | `{"id": "/path/name", "name": "name", "uuid": "xxx"}` |
| `unilabos_devices` | DeviceSlot | `"/parent/device_name"` 路径字符串 |
| `unilabos_nodes` | NodeSlot | `"/parent/node_name"` 路径字符串 |
| `unilabos_class` | ClassSlot | `"class_name"` 字符串 |
| `unilabos_formulation` | FormulationSlot | `[{well_name, liquids: [{name, volume}]}]` |

### ResourceSlot 填写

从 API #8 资源树中筛选**物料**节点：

```json
{"id": "/bioyond_cell_workstation/YB_Bioyond_Deck/自动堆栈-左", "name": "自动堆栈-左", "uuid": "3a19debc-..."}
```

数组字段：`[{id, name, uuid}, ...]`
特例：`create_resource` 的 `res_id` 允许填不存在的路径。

### DeviceSlot 填写

从资源树筛选 `type=device` 的节点，填路径字符串：

```
"/BatteryStation"
"/bioyond_cell_workstation"
```

### FormulationSlot 填写

```json
[
  {
    "sample_uuid": "",
    "well_name": "YB_PrepBottle_15mL_Carrier_bottle_A1",
    "liquids": [
      { "name": "LiPF6", "mass": 12.5 },
      { "name": "EC", "mass": 50.0 }
    ]
  }
]
```

`well_name` 从资源树中取物料节点的 `name`。

## 参数名映射（重要的坑）

> 工作流模板中存储的参数名和 Python 函数实际接受的参数名**不一定相同**。
> 提交 notebook 时必须使用 **Python 函数参数名**。

### `create_orders_formulation` 参数映射

| 模板中的 param 键 | 实际 Python 参数名 | 说明 |
|-------------------|-------------------|------|
| `pouch_cell_info` | `pouch_cell_volume` | 软包组装分液体积 (mL) |
| `conductivity_info` | `conductivity_volume` | 电导测试分液体积 (mL) |
| `load_shedding_info` | `coin_cell_volume` | 扣电组装分液体积 (mL) |
| `formulation` | `formulation` | 配方数组（名称一致） |
| `batch_id` | `batch_id` | 批次号（名称一致） |
| `bottle_type` | `bottle_type` | 配液瓶类型（名称一致） |
| `mix_time` | `mix_time` | 混匀时间(秒)（名称一致） |
| `conductivity_bottle_count` | `conductivity_bottle_count` | 电导瓶数（名称一致） |

当从模板中读到 `param` 包含 `pouch_cell_info` 等 LIMS 字段名时，提交 notebook 时要用右列的 Python 函数参数名。否则会报 `TypeError: got an unexpected keyword argument`。

## 典型工作流

### 方式一：通过 Notebook API 批量提交（推荐）

**适用场景**：多组配方的批量实验，云端管理实验记录

```
1. 向用户索要工作流模板 URL（不要自行创建）
2. 获取 lab_uuid（API #1）和 project_uuid（API #2）
3. 获取工作流模板详情（API #3），提取活跃节点 UUID
4. 解析用户提供的 Excel 文件，构建 formulation 数组
5. 提交 notebook（API #4）
6. 轮询 notebook 状态（API #5）直到完成
```

**Excel 解析规则**：
- 全局参数在第一个数据行：`batch_id`、`bottle_type`、`mix_time`、`coin_cell_volume`、`pouch_cell_volume`、`conductivity_volume`、`conductivity_bottle_count`
- 配方列从"试剂名1"开始，交替排列：试剂名列 + 质量列（以 `(g)` 结尾）
- 每行一个配方，`order_name` = 配方ID列
- formulation 中每个配方的 materials 数组只包含 `mass > 0` 的试剂

**node_params 构建**：所有配方放入同一个 round 的同一个 datas 条目中，因为只有一个节点（`create_orders_formulation`）。

### 方式二：设备单步操作（本地 API）

**适用场景**：调试、快速测试

```
1. 确保 unilab 已在本地启动
2. 通过 POST http://127.0.0.1:8002/api/v1/job/add 提交任务
3. 通过 GET /api/v1/job/<job_id>/status 查询状态
```

### 设备操作流程：配液 → 转运 → 扣电

```
1. [配液站] scheduler_start_and_auto_feeding  → 启动调度 + 上料
2. [配液站] create_orders_formulation          → 创建配液实验（配方输入）
3. [配液站] transfer_3_to_2_to_1_auto         → 分液瓶板转运到扣电站
4. [扣电站] coin_cell_init                     → 初始化+自动+启动
5. [扣电站] coin_cell_start                    → 发送瓶数+批量组装
```

## 云端使用心得

### 环境准备
- Windows 必须设置 `$env:PYTHONIOENCODING="utf-8"` 防止编码崩溃
- 使用 `--skip_env_check` 跳过依赖检查，加快启动
- 工作目录建议在 `unilabos/devices/workstation` 下启动

### 连接与注册
- `--upload_registry` 会自动将设备和资源注册到云端
- WebSocket 连接建立后，本地和云端的资源树会自动同步
- 注册成功后用户需在云端 UI 完成**物料拖放上架**操作
- 如果 unilab 断开重连，资源树会重新同步

### 工作流模板
- **不要自行调用 API 创建工作流或节点**——云端工作流节点模板需要预配置
- 始终向用户索要已有的工作流模板 URL
- 从 URL 中提取 `workflow_uuid`，通过 API #3 获取详情
- 模板中 `disabled: true` 的节点跳过，只处理活跃节点

### Notebook 实验提交
- Notebook 是云端管理实验的标准方式
- 一个 notebook 可包含多轮（`node_params` 数组），每轮可包含多组参数
- 提交后通过 API #5 轮询状态，LIMS 配液流程通常需要较长时间（8 个配方约 30-60 分钟）
- 实验进度可在云端 UI 和本地 unilab 日志中同步查看

### 常见错误
| 错误 | 原因 | 解决 |
|------|------|------|
| `edge not started error` | unilab 未连接云端 WebSocket | 检查 unilab 是否在运行、重启 |
| `resource_node_template not found` | 云端没有该设备的工作流模板 | 向用户索要已有模板，不要自行创建 |
| `got an unexpected keyword argument` | 参数名用了模板字段名而非 Python 函数参数名 | 参照上方映射表转换 |
| `UnicodeEncodeError: 'gbk'` | Windows 默认编码不支持特殊字符 | 设置 `PYTHONIOENCODING=utf-8` |
| `parse parameter error` | 云端 API 字段名错误 | `device_id` (非 `device_name`)、`action` (非 `action_name`)、必须带 `action_type` |

## 渐进加载策略

1. 先读本文件了解 API 端点、参数映射和云端注意事项
2. 需要具体 action 参数时，读 [action-index.md](action-index.md) 查找 action 名称和核心参数
3. 需要完整 schema 时，读 `actions/<action_name>.json`（需先运行提取命令生成）
4. 需要理解参数含义时，读设备源码

## 完整 Notebook 提交 Checklist

```
- [ ] 确认 unilab 已在本地启动并连接云端 WebSocket
- [ ] 提醒用户在云端 UI 拖拽上料、确认物料位置
- [ ] 提醒用户确认配液所需试剂已在 LIMS 完成入库
- [ ] 等待用户确认物料就绪后再继续
- [ ] 向用户索要工作流模板 URL → 提取 workflow_uuid
- [ ] 获取 lab_uuid（API #1）
- [ ] 获取 project_uuid（API #2）
- [ ] 获取工作流模板详情（API #3），提取活跃节点 UUID
- [ ] 解析用户 Excel 文件 → 构建 formulation + 全局参数
- [ ] 注意参数名映射（模板字段名 → Python 函数参数名）
- [ ] 提交 notebook（API #4）
- [ ] 轮询 notebook 状态（API #5）直到完成
```

---

## 真实场景：宜宾产线 Excel 提交提示词模板

> 以下为已验证可用的标准提示词，适用于配液-分液-扣电全流程。

### 场景说明

- unilab 运行在本地 Windows 机器（miniforge 环境），连接云端 WebSocket
- AI（Cursor / OpenClaw）在任意设备上，通过云端 API 操作，**不需要本地 127.0.0.1**
- 工作流为 5 节点串联：`create_orders_formulation` → `transfer_3_to_2_to_1_auto` → `coin_cell_init` → `coin_cell_start` → `transfer_1_to_2`

### 已知固定参数（宜宾产线）

```
BASE     = https://uni-lab.test.bohrium.com
lab_uuid = e9ed9102-d709-4741-b7a0-d1e8578e2065
project  = YiBinElectrolyte (bc5224b4-8120-4765-9961-9dfc1802a1f6)
workflow = 配液分液formulation全流程 (2bc59938-db79-4415-ac2d-9897ef125f2f)
```

#### 工作流节点 UUID（固定，无需重新查询）

| 顺序 | action | node_uuid |
|------|--------|-----------|
| Step1 | auto-create_orders_formulation | `ece6744a-81ac-4ae4-8cd1-1c8eeda1dab6` |
| Step2 | auto-transfer_3_to_2_to_1_auto | `1c37a8dd-5ba0-413d-81db-94b9c936a171` |
| Step3 | auto-coin_cell_init | `97a676a2-d257-4479-9096-073b40300970` |
| Step4 | auto-coin_cell_start | `cf69017a-d29c-4aad-a63b-309d63dac2e9` |
| Step5 | auto-transfer_1_to_2 | `80d1c1aa-dbc3-4601-86b7-5c22a992dd9e` |

> ⚠️ 扣电站动作已由 `func_pack_device_init_auto_start_combined` / `func_sendbottle_allpack_multi`
> 重命名为 `coin_cell_init` / `coin_cell_start`。上表 node_uuid 对应的云端工作流需用新动作名**重建**后才能正常派发；
> 若不确定云端是否已重建，请通过 API #3 重新获取工作流详情确认各节点的 action 名与最新 node_uuid。

### 标准提示词

```
请使用 yibin-electrolyte-submit skill，提交以下实验：

工作流模板 URL：https://uni-lab.test.bohrium.com/laboratory/e9ed9102-d709-4741-b7a0-d1e8578e2065/workflow/2bc59938-db79-4415-ac2d-9897ef125f2f
Excel 文件路径：<粘贴或上传 xlsx 路径>

注意事项：
- lab_uuid、project_uuid、workflow节点UUID均已固定，无需重新查询
- 直接解析 Excel → 构建 payload → 提交
- mix_time 传标量整数即可（已兼容）
- 试剂名以 Excel 为准，注意区分 LiDFOB / LiDOFB 等拼写
- csv_export_path 取 Excel 中 csv_export_path 列的值
- 提交后告知 notebook UUID，无需自动轮询（实验耗时较长）
```

### Excel 列结构说明（experment_template_0415sim-*.xlsx）

| 列范围 | 内容 |
|--------|------|
| C | batch_id |
| D | bottle_type |
| E-H | coin_cell_volume / conductivity_bottle_count / conductivity_volume / csv_export_path |
| I-T | 试剂名+质量 交替排列（最多6对）|
| U | mix_time |
| V | order_name（每行配方的订单号）|
| W | pouch_cell_volume |
| X-Y | target_device / target_location（Step2参数）|
| AA | material_search_enable（Step3参数）|
| AB-AS | 扣电站参数（Step4）|

### CSV 导出说明

每次 `create_orders_formulation` 完成后，在 `csv_export_path` 目录下生成：
```
electrolyte_orders_<YYYYMMDD_HHMMSS>.csv
```
列：`orderCode, orderName, 配液瓶类型, 配液瓶二维码, 分液瓶类型, 分液瓶二维码, 目标配液质量比, 真实配液质量比, 时间`

> **注意**：barCode 为 `null` 或 `"nullBarCode123456"` 是正常现象，表示 LIMS 中该物料尚未扫码。配液瓶缺失通常是因为物料未放在手动传递窗（`locationId` 前缀 `3a19deae-2c7a-`）。
