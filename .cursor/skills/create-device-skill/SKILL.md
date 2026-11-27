---
name: create-device-skill
description: Create a skill for any Uni-Lab device by extracting action schemas from the device registry. Use when the user wants to create a new device skill, add device API documentation, or set up action schemas for a device.
---

# 创建设备 Skill 指南

本 meta-skill 教你如何为任意 Uni-Lab-OS 设备创建完整的 API 操作技能（参考 `unilab-device-api` 的成功案例）。

## 数据源

- **设备注册表**: `unilabos_data/req_device_registry_upload.json`
- **结构**: `{ "resources": [{ "id": "<device_id>", "class": { "module": "<python_module:ClassName>", "action_value_mappings": { ... } } }] }`
- **生成时机**: `unilab` 启动并完成注册表上传后自动生成
- **module 字段**: 格式 `unilabos.devices.xxx.yyy:ClassName`，可转为源码路径 `unilabos/devices/xxx/yyy.py`，阅读源码可了解参数含义和设备行为

## 创建流程

### Step 0 — 收集必备信息（缺一不可，否则询问后终止）

开始前**必须**确认以下 4 项信息全部就绪。如果用户未提供任何一项，**立即询问并终止当前流程**，等用户补齐后再继续。

向用户提问：「请提供你的 unilab 启动参数，我需要以下信息：」

#### 必备项 ①：ak / sk（认证凭据）

来源：启动命令的 `--ak` `--sk` 参数，或 config.py 中的 `ak = "..."` `sk = "..."`。

获取后立即生成 AUTH token：

```bash
python ./scripts/gen_auth.py <ak> <sk>
# 或从 config.py 提取
python ./scripts/gen_auth.py --config <config.py>
```

认证算法：`base64(ak:sk)` → `Authorization: Lab <token>`

#### 必备项 ②：--addr（目标环境）

决定 API 请求发往哪个服务器。从启动命令的 `--addr` 参数获取：

| `--addr` 值    | BASE URL                            |
| -------------- | ----------------------------------- |
| `test`         | `https://leap-lab.test.bohrium.com` |
| `uat`          | `https://leap-lab.uat.bohrium.com`  |
| `local`        | `http://127.0.0.1:48197`            |
| 不传（默认）   | `https://leap-lab.bohrium.com`      |
| 其他自定义 URL | 直接使用该 URL                      |

#### 必备项 ③：req_device_registry_upload.json（设备注册表）

数据文件由 `unilab` 启动时自动生成，需要定位它：

**推断 working_dir**（即 `unilabos_data` 所在目录）：

| 条件                 | working_dir 取值                                         |
| -------------------- | -------------------------------------------------------- |
| 传了 `--working_dir` | `<working_dir>/unilabos_data/`（若子目录已存在则直接用） |
| 仅传了 `--config`    | `<config 文件所在目录>/unilabos_data/`                   |
| 都没传               | `<当前工作目录>/unilabos_data/`                          |

**按优先级搜索文件**：

```
<推断的 working_dir>/unilabos_data/req_device_registry_upload.json
<推断的 working_dir>/req_device_registry_upload.json
<workspace 根目录>/unilabos_data/req_device_registry_upload.json
```

也可以直接 Glob 搜索：`**/req_device_registry_upload.json`

找到后**必须检查文件修改时间**并告知用户：「找到注册表文件 `<路径>`，生成于 `<时间>`。请确认这是最近一次启动生成的。」超过 1 天提醒用户是否需要重新启动 `unilab`。

**如果文件不存在** → 告知用户先运行 `unilab` 启动命令，等日志出现 `注册表响应数据已保存` 后再执行本流程。**终止。**

#### 必备项 ④：目标设备

用户需要明确要为哪个设备创建 skill。可以是设备名称（如「PRCXI 移液站」）或 device_id（如 `liquid_handler.prcxi`）。

如果用户不确定，运行提取脚本列出所有设备供选择：

```bash
python ./scripts/extract_device_actions.py --registry <找到的文件路径>
```

**四项全部就绪后才进入 Step 1。**

### Step 1 — 列出可用设备

运行提取脚本，列出所有设备及 action 数量和 Python 源码路径，让用户选择：

```bash
# 自动搜索（默认在 unilabos_data/ 和当前目录查找）
python ./scripts/extract_device_actions.py

# 指定注册表文件路径
python ./scripts/extract_device_actions.py --registry <path/to/req_device_registry_upload.json>
```

脚本输出包含每个设备的 **Python 源码路径**（从 `class.module` 转换），可用于后续阅读源码理解参数含义。

### Step 2 — 提取 Action Schema

用户选择设备后，运行提取脚本：

```bash
python ./scripts/extract_device_actions.py [--registry <path>] <device_id> ./skills/<skill-name>/actions/
```

脚本会显示设备的 Python 源码路径和类名，方便阅读源码了解参数含义。

每个 action 生成一个 JSON 文件，包含：

- `type` — 作为 API 调用的 `action_type`
- `schema` — 完整 JSON Schema（含 `properties.goal.properties` 参数定义）
- `goal` — goal 字段映射（含占位符 `$placeholder`）
- `goal_default` — 默认值

### Step 3 — 写 action-index.md

按模板为每个 action 写条目（**必须包含 `action_type`**）：

```markdown
### `<action_name>`

<用途描述（一句话）>

- **action_type**: `<从 actions/<name>.json 的 type 字段获取>`
- **Schema**: [`actions/<filename>.json`](actions/<filename>.json)
- **核心参数**: `param1`, `param2`（从 schema.required 获取）
- **可选参数**: `param3`, `param4`
- **占位符字段**: `field`（需填入物料信息，值以 `$` 开头）
```

描述规则：

- **每个 action 必须标注 `action_type`**（从 JSON 的 `type` 字段读取），这是 API #9 调用时的必填参数，传错会导致任务永远卡住
- 从 `schema.properties` 读参数列表（schema 已提升为 goal 内容）
- 从 `schema.required` 区分核心/可选参数
- 按功能分类（移液、枪头、外设等）
- 标注 `placeholder_keys` 中的字段类型：
  - `unilabos_resources` → **ResourceSlot**，填入 `{id, name, uuid}`（id 是路径格式，从资源树取物料节点）
  - `unilabos_devices` → **DeviceSlot**，填入路径字符串如 `"/host_node"`（从资源树筛选 type=device）
  - `unilabos_nodes` → **NodeSlot**，填入路径字符串如 `"/PRCXI/PRCXI_Deck"`（资源树中任意节点）
  - `unilabos_class` → **ClassSlot**，填入类名字符串如 `"container"`（从注册表查找）
  - `unilabos_formulation` → **FormulationSlot**，填入配方数组 `[{well_name, liquids: [{name, volume}]}]`（well_name 为目标物料的 name）
- array 类型字段 → `[{id, name, uuid}, ...]`
- 特殊：`create_resource` 的 `res_id`（ResourceSlot）可填不存在的路径

### Step 4 — 写 SKILL.md

直接复用 `unilab-device-api` 的 API 模板，修改：

- 设备名称
- Action 数量
- 目录列表
- Session state 中的 `device_name`
- **AUTH 头** — 使用 Step 0 中 `gen_auth.py` 生成的 `Authorization: Lab <token>`（不要硬编码 `Api` 类型的 key）
- **Python 源码路径** — 在 SKILL.md 开头注明设备对应的源码文件，方便参考参数含义
- **Slot 字段表** — 列出本设备哪些 action 的哪些字段需要填入 Slot（物料/设备/节点/类名）
- **action_type 速查表** — 在 API #9 说明后面紧跟一个表格，列出每个 action 对应的 `action_type` 值（从 JSON `type` 字段提取），方便 agent 快速查找而无需打开 JSON 文件

API 模板结构：

```markdown
## 设备信息

- device_id, Python 源码路径, 设备类名

## 前置条件（缺一不可）

- ak/sk → AUTH, --addr → BASE URL

## 请求约定

- Windows 平台必须用 curl.exe（非 PowerShell 的 curl 别名）

## Session State

- lab_uuid（通过 GET /edge/lab/info 直接获取，不要问用户）, device_name

## API Endpoints

# - #1 GET /edge/lab/info → 直接拿到 lab_uuid

# - #2 创建工作流 POST /lab/workflow/owner → 拼 URL 告知用户

# - #3 创建节点 POST /edge/workflow/node

# body: {workflow_uuid, resource_template_name: "<device_id>", node_template_name: "<action_name>"}

# - #4 删除节点 DELETE /lab/workflow/nodes

# - #5 更新节点参数 PATCH /lab/workflow/node

# - #6 查询节点 handles POST /lab/workflow/node-handles

# body: {node_uuids: ["uuid1","uuid2"]} → 返回各节点的 handle_uuid

# - #7 批量创建边 POST /lab/workflow/edges

# body: {edges: [{source_node_uuid, target_node_uuid, source_handle_uuid, target_handle_uuid}]}

# - #8 启动工作流 POST /lab/workflow/{uuid}/run

# - #9 运行设备单动作 POST /lab/mcp/run/action（⚠️ action_type 必须从 action-index.md 或 actions/<name>.json 的 type 字段获取，传错会导致任务永远卡住）

# - #10 查询任务状态 GET /lab/mcp/task/{task_uuid}

# - #11 运行工作流单节点 POST /lab/mcp/run/workflow/action

# - #12 获取资源树 GET /lab/material/download/{lab_uuid}

# - #13 获取工作流模板详情 GET /lab/workflow/template/detail/{workflow_uuid}

# 返回 workflow 完整结构：data.nodes[] 含每个节点的 uuid、name、param、device_name、handles

# - #14 按名称查询物料模板 GET /lab/material/template/by-name?lab_uuid=&name=

# 返回 res_template_uuid，用于 #15 创建物料时的必填字段

# - #15 创建物料节点 POST /edge/material/node

# body: {res_template_uuid(从#14获取), name(自定义), display_name, parent_uuid?(从#12获取), ...}

# - #16 更新物料节点 PUT /edge/material/node

# body: {uuid(从#12获取), display_name?, description?, init_param_data?, data?, ...}

## Placeholder Slot 填写规则

- unilabos_resources → ResourceSlot → {"id":"/path/name","name":"name","uuid":"xxx"}
- unilabos_devices → DeviceSlot → "/parent/device" 路径字符串
- unilabos_nodes → NodeSlot → "/parent/node" 路径字符串
- unilabos_class → ClassSlot → "class_name" 字符串
- unilabos_formulation → FormulationSlot → [{well_name, liquids: [{name, volume}]}] 配方数组
- 特例：create_resource 的 res_id 允许填不存在的路径
- 列出本设备所有 Slot 字段、类型及含义

## 渐进加载策略

## 完整工作流 Checklist
```

### Step 5 — 验证

检查文件完整性：

- [ ] `SKILL.md` 包含 API endpoint（#1 获取 lab_uuid、#2-#7 工作流/节点/边、#8-#11 运行/查询、#12 资源树、#13 工作流模板详情、#14-#16 物料管理）
- [ ] `SKILL.md` 包含 Placeholder Slot 填写规则（ResourceSlot / DeviceSlot / NodeSlot / ClassSlot / FormulationSlot + create_resource 特例）和本设备的 Slot 字段表
- [ ] `action-index.md` 列出所有 action 并有描述
- [ ] `actions/` 目录中每个 action 有对应 JSON 文件
- [ ] JSON 文件包含 `type`, `schema`（已提升为 goal 内容）, `goal`, `goal_default`, `placeholder_keys` 字段
- [ ] 描述能让 agent 判断该用哪个 action

## Action JSON 文件结构

```json
{
  "type": "LiquidHandlerTransfer",    // → API 的 action_type
  "goal": {                           // goal 字段映射
    "sources": "sources",
    "targets": "targets",
    "tip_racks": "tip_racks",
    "asp_vols": "asp_vols"
  },
  "schema": {                         // ← 直接是 goal 的 schema（已提升）
    "type": "object",
    "properties": {                   // 参数定义（即请求中 goal 的字段）
      "sources": { "type": "array", "items": { "type": "object" } },
      "targets": { "type": "array", "items": { "type": "object" } },
      "asp_vols": { "type": "array", "items": { "type": "number" } }
    },
    "required": [...],
    "_unilabos_placeholder_info": {   // ← Slot 类型标记
      "sources": "unilabos_resources",
      "targets": "unilabos_resources",
      "tip_racks": "unilabos_resources"
    }
  },
  "goal_default": { ... },            // 默认值
  "placeholder_keys": {               // ← 汇总所有 Slot 字段
    "sources": "unilabos_resources",  //    ResourceSlot
    "targets": "unilabos_resources",
    "tip_racks": "unilabos_resources",
    "target_device_id": "unilabos_devices"  // DeviceSlot
  }
}
```

> **注意**：`schema` 已由脚本从原始 `schema.properties.goal` 提升为顶层，直接包含参数定义。
> `schema.properties` 中的字段即为 API 创建节点返回的 `data.param` 中的字段，PATCH 更新时直接修改 `param` 即可。

## Placeholder Slot 类型体系

`placeholder_keys` / `_unilabos_placeholder_info` 中有 5 种值，对应不同的填写方式：

| placeholder 值         | Slot 类型       | 填写格式                                              | 选取范围                                  |
| ---------------------- | --------------- | ----------------------------------------------------- | ----------------------------------------- |
| `unilabos_resources`   | ResourceSlot    | `{"id": "/path/name", "name": "name", "uuid": "xxx"}` | 仅**物料**节点（不含设备）                |
| `unilabos_devices`     | DeviceSlot      | `"/parent/device_name"`                               | 仅**设备**节点（type=device），路径字符串 |
| `unilabos_nodes`       | NodeSlot        | `"/parent/node_name"`                                 | **设备 + 物料**，即所有节点，路径字符串   |
| `unilabos_class`       | ClassSlot       | `"class_name"`                                        | 注册表中已上报的资源类 name               |
| `unilabos_formulation` | FormulationSlot | `[{well_name, liquids: [{name, volume}]}]`            | 资源树中物料节点的 **name**，配合液体配方 |

### ResourceSlot（`unilabos_resources`）

最常见的类型。从资源树中选取**物料**节点（孔板、枪头盒、试剂槽等）：

- 单个：`{"id": "/workstation/container1", "name": "container1", "uuid": "ff149a9a-..."}`
- 数组：`[{"id": "/path/a", "name": "a", "uuid": "xxx"}, ...]`
- `id` 从 parent 计算的路径格式，根据 action 语义选择正确的物料

> **特例**：`create_resource` 的 `res_id`，目标物料可能尚不存在，直接填期望路径，不需要 uuid。

### DeviceSlot / NodeSlot / ClassSlot

- **DeviceSlot**（`unilabos_devices`）：路径字符串如 `"/host_node"`，仅 type=device 的节点
- **NodeSlot**（`unilabos_nodes`）：路径字符串如 `"/PRCXI/PRCXI_Deck"`，设备 + 物料均可选
- **ClassSlot**（`unilabos_class`）：类名字符串如 `"container"`，从 `req_resource_registry_upload.json` 查找

### FormulationSlot（`unilabos_formulation`）

描述**液体配方**：向哪些容器中加入哪些液体及体积。

```json
[
  {
    "sample_uuid": "",
    "well_name": "bottle_A1",
    "liquids": [{ "name": "LiPF6", "volume": 0.6 }]
  }
]
```

- `well_name` — 目标物料的 **name**（从资源树取，不是 `id` 路径）
- `liquids[]` — 液体列表，每条含 `name`（试剂名）和 `volume`（体积，单位由上下文决定；pylabrobot 内部统一 uL）
- `sample_uuid` — 样品 UUID，无样品传 `""`
- 与 ResourceSlot 的区别：ResourceSlot 指向物料本身，FormulationSlot 引用物料名并附带配方信息

### 通过 API #12 获取资源树

```bash
curl -s -X GET "$BASE/api/v1/lab/material/download/$lab_uuid" -H "$AUTH"
```

注意 `lab_uuid` 在路径中（不是查询参数）。返回结构：

```json
{
  "code": 0,
  "data": {
    "nodes": [
      {"name": "host_node", "uuid": "c3ec1e68-...", "type": "device", "parent": ""},
      {"name": "PRCXI", "uuid": "e249c9a6-...", "type": "device", "parent": ""},
      {"name": "PRCXI_Deck", "uuid": "fb6a8b71-...", "type": "deck", "parent": "PRCXI"}
    ],
    "edges": [...]
  }
}
```

- `data.nodes[]` — 所有节点（设备 + 物料），每个节点含 `name`、`uuid`、`type`、`parent`
- `type` 区分设备（`device`）和物料（`deck`、`container`、`resource` 等）
- `parent` 为父节点名称（空字符串表示顶级）
- 填写 Slot 时根据 placeholder 类型筛选：ResourceSlot 取非 device 节点，DeviceSlot 取 device 节点
- 创建/更新物料时：`parent_uuid` 取父节点的 `uuid`，更新目标的 `uuid` 取节点自身的 `uuid`

## 物料管理 API

设备 Skill 除了设备动作外，还需支持物料节点的创建和参数设定，用于在资源树中动态管理物料。

典型流程：先通过 **#14 按名称查询模板** 获取 `res_template_uuid` → 再通过 **#15 创建物料** → 之后可通过 **#16 更新物料** 修改属性。更新时需要的 `uuid` 和 `parent_uuid` 均从 **#12 资源树下载** 获取。

### API #14 — 按名称查询物料模板

创建物料前，需要先获取物料模板的 UUID。通过模板名称查询：

```bash
curl -s -X GET "$BASE/api/v1/lab/material/template/by-name?lab_uuid=$lab_uuid&name=<template_name>" -H "$AUTH"
```

| 参数       | 必填   | 说明                             |
| ---------- | ------ | -------------------------------- |
| `lab_uuid` | **是** | 实验室 UUID（从 API #1 获取）    |
| `name`     | **是** | 物料模板名称（如 `"container"`） |

返回 `code: 0` 时，**`data.uuid`** 即为 `res_template_uuid`，用于 API #15 创建物料。返回还包含 `name`、`resource_type`、`handles`、`config_infos` 等模板元信息。

模板不存在时返回 `code: 10002`，`data` 为空对象。模板名称来自资源注册表中已注册的资源类型。

### API #15 — 创建物料节点

```bash
curl -s -X POST "$BASE/api/v1/edge/material/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '<request_body>'
```

请求体：

```json
{
  "res_template_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "name": "my_custom_bottle",
  "display_name": "自定义瓶子",
  "parent_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "type": "",
  "init_param_data": {},
  "schema": {},
  "data": {
    "liquids": [["water", 1000, "uL"]],
    "max_volume": 50000
  },
  "plate_well_datas": {},
  "plate_reagent_datas": {},
  "pose": {},
  "model": {}
}
```

| 字段                  | 必填   | 类型          | 数据来源                            | 说明                                   |
| --------------------- | ------ | ------------- | ----------------------------------- | -------------------------------------- |
| `res_template_uuid`   | **是** | string (UUID) | **API #14** 按名称查询获取          | 物料模板 UUID                          |
| `name`                | 否     | string        | **用户自定义**                      | 节点名称（标识符），可自由命名         |
| `display_name`        | 否     | string        | 用户自定义                          | 显示名称（UI 展示用）                  |
| `parent_uuid`         | 否     | string (UUID) | **API #12** 资源树中父节点的 `uuid` | 父节点，为空则创建顶级节点             |
| `type`                | 否     | string        | 从模板继承                          | 节点类型                               |
| `init_param_data`     | 否     | object        | 用户指定                            | 初始化参数，覆盖模板默认值             |
| `data`                | 否     | object        | 用户指定                            | 节点数据，container 见下方 data 格式   |
| `plate_well_datas`    | 否     | object        | 用户指定                            | 孔板子节点数据（创建带孔位的板时使用） |
| `plate_reagent_datas` | 否     | object        | 用户指定                            | 试剂关联数据                           |
| `schema`              | 否     | object        | 从模板继承                          | 自定义 schema，不传则从模板继承        |
| `pose`                | 否     | object        | 用户指定                            | 位姿信息                               |
| `model`               | 否     | object        | 用户指定                            | 3D 模型信息                            |

#### container 的 `data` 格式

> **体积单位统一为 uL（微升）**。pylabrobot 体系中所有体积值（`max_volume`、`liquids` 中的 volume）均为 uL。外部如果是 mL 需乘 1000 转换。

```json
{
  "liquids": [["water", 1000, "uL"], ["ethanol", 500, "uL"]],
  "max_volume": 50000
}
```

- `liquids` — 液体列表，每条为 `[液体名称, 体积(uL), 单位字符串]`
- `max_volume` — 容器最大容量（uL），如 50 mL = 50000 uL

### API #16 — 更新物料节点

```bash
curl -s -X PUT "$BASE/api/v1/edge/material/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '<request_body>'
```

请求体：

```json
{
  "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "parent_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "display_name": "新显示名称",
  "description": "新描述",
  "init_param_data": {},
  "data": {},
  "pose": {},
  "schema": {},
  "extra": {}
}
```

| 字段              | 必填   | 类型          | 数据来源                              | 说明             |
| ----------------- | ------ | ------------- | ------------------------------------- | ---------------- |
| `uuid`            | **是** | string (UUID) | **API #12** 资源树中目标节点的 `uuid` | 要更新的物料节点 |
| `parent_uuid`     | 否     | string (UUID) | API #12 资源树                        | 移动到新父节点   |
| `display_name`    | 否     | string        | 用户指定                              | 更新显示名称     |
| `description`     | 否     | string        | 用户指定                              | 更新描述         |
| `init_param_data` | 否     | object        | 用户指定                              | 更新初始化参数   |
| `data`            | 否     | object        | 用户指定                              | 更新节点数据     |
| `pose`            | 否     | object        | 用户指定                              | 更新位姿         |
| `schema`          | 否     | object        | 用户指定                              | 更新 schema      |
| `extra`           | 否     | object        | 用户指定                              | 更新扩展数据     |

> 只传需要更新的字段，未传的字段保持不变。

## 最终目录结构

```
./<skill-name>/
├── SKILL.md              # API 端点 + 渐进加载指引
├── action-index.md       # 动作索引：描述/用途/核心参数
└── actions/              # 每个 action 的完整 JSON Schema
    ├── action1.json
    ├── action2.json
    └── ...
```
