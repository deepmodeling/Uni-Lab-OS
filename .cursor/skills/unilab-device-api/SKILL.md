---
name: unilab-device-api
description: Operate Uni-Lab devices via REST API — create workflows, manage nodes, run device actions, query tasks. Use when the user mentions lab devices, workflows, running actions, creating nodes, or any Uni-Lab platform operation.
---

# Uni-Lab Device API Skill

## 前置条件（缺一不可）

使用本 skill 前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

询问用户的启动参数，从 `--ak` `--sk` 或 config.py 中获取：

```bash
python .cursor/skills/create-device-skill/scripts/gen_auth.py <ak> <sk>
```

### 2. --addr → BASE URL

| `--addr` 值  | BASE                                |
| ------------ | ----------------------------------- |
| `test`       | `https://leap-lab.test.bohrium.com` |
| `uat`        | `https://leap-lab.uat.bohrium.com`  |
| `local`      | `http://127.0.0.1:48197`            |
| 不传（默认） | `https://leap-lab.bohrium.com`      |

确认后设置：

```bash
BASE="<根据 addr 确定的 URL>"
AUTH="Authorization: Lab <gen_auth.py 输出的 token>"
```

**两项全部就绪后才可发起 API 请求。**

## Session State

在整个对话过程中，agent 需要记住以下状态，避免重复询问用户：

- `lab_uuid` — 实验室 UUID（首次通过 API #1 自动匹配，**不需要问用户**）
- `device_name` — 设备名称（如 `host_node`）

## 请求约定

所有请求使用 `curl -s`，POST/PATCH/DELETE 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名），示例中的 `curl` 均指 `curl.exe`。

---

## API Endpoints

### 1. 获取实验室信息（自动获取 lab_uuid）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

通过 ak/sk 的 Lab token **直接**返回对应实验室的信息，**不需要询问用户**。返回：

```json
{
  "code": 0,
  "data": {
    "uuid": "xxx",
    "name": "实验室名称",
    "access_key": "...",
    "status": "init"
  }
}
```

记住 `data.uuid` 为 `lab_uuid`，`data.name` 为 `lab_name`。

### 2. 创建工作流

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/owner" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"<名称>","lab_uuid":"<lab_uuid>","description":"<描述>"}'
```

返回 `data.uuid` 为 `workflow_uuid`。创建成功后**告知用户工作流链接**：

```
$BASE/laboratory/$lab_uuid/workflow/$workflow_uuid
```

### 3. 创建节点

```bash
curl -s -X POST "$BASE/api/v1/edge/workflow/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"workflow_uuid":"<workflow_uuid>","resource_template_name":"<device_id>","node_template_name":"<action_name>"}'
```

- `resource_template_name` — 设备的注册表名（即 `device_id`，如 `liquid_handler.prcxi`、`host_node`）
- `node_template_name` — action 名称（如 `transfer_liquid`、`create_resource`）

返回 `node_uuid`。

### 4. 删除节点

```bash
curl -s -X DELETE "$BASE/api/v1/lab/workflow/nodes" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"node_uuids":["<uuid1>"],"workflow_uuid":"<workflow_uuid>"}'
```

### 5. 更新节点参数

```bash
curl -s -X PATCH "$BASE/api/v1/lab/workflow/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"workflow_uuid":"<wf_uuid>","uuid":"<node_uuid>","param":{...}}'
```

`param` 的格式：直接使用创建节点返回的 `data.param` 结构（goal 字段已展开），修改需要填入的字段值即可。参考 [action-index.md](action-index.md) 和 `schema.properties.goal._unilabos_placeholder_info` 确定哪些字段是 Slot。

### 6. 查询节点 handles

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/node-handles" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"node_uuids":["<node_uuid_1>","<node_uuid_2>"]}'
```

传入一组 `node_uuid`，返回每个节点的输入/输出 handle 信息（含 `handle_uuid`）。**创建边之前必须先查询 handles**，拿到 `source_handle_uuid` 和 `target_handle_uuid`。

### 7. 批量创建边

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/edges" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"edges":[{"source_node_uuid":"<uuid>","target_node_uuid":"<uuid>","source_handle_uuid":"<uuid>","target_handle_uuid":"<uuid>"}]}'
```

用 API #6 返回的 handle UUID 将节点连接起来。`edges` 是数组，可一次创建多条边。

### 8. 启动工作流

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/<workflow_uuid>/run" \
  -H "$AUTH"
```

### 9. 运行设备单动作

```bash
curl -s -X POST "$BASE/api/v1/lab/mcp/run/action" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"lab_uuid":"<lab_uuid>","device_id":"<device>","action":"<action_name>","action_type":"<从JSON的type字段>","param":{...goal里的属性直接展开...}}'
```

**注意**：`param` 字段直接放 goal 里的属性，**不要**再包一层 `{"goal": {...}}`。云端 API 会将 `param` 内容直接作为 `function_args` 传给设备函数。

> **WARNING: `action_type` 必须正确，传错会导致任务永远卡住无法完成。** `action` 和 `action_type` 参考 [action-index.md](action-index.md)，或从 `actions/<name>.json` 的 `type` 字段获取。

### 10. 查询任务状态

```bash
curl -s -X GET "$BASE/api/v1/lab/mcp/task/<task_uuid>" \
  -H "$AUTH"
```

### 11. 运行工作流单节点

```bash
curl -s -X POST "$BASE/api/v1/lab/mcp/run/workflow/action" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"node_uuid":"<node_uuid>"}'
```

### 12. 获取资源树（物料信息）

```bash
curl -s -X GET "$BASE/api/v1/lab/material/download/$lab_uuid" \
  -H "$AUTH"
```

注意 `lab_uuid` 在路径中（不是查询参数）。返回实验室的完整资源树。每个节点包含 `id`（路径格式）、`name`、`uuid`、`type`、`parent` 等字段。用于填写 action 参数中的 Placeholder Slot 字段。

### 13. 获取工作流模板详情

```bash
curl -s -X GET "$BASE/api/v1/lab/workflow/template/detail/$workflow_uuid" \
  -H "$AUTH"
```

返回 workflow 的完整结构，包含所有节点、边、参数。响应结构：

```json
{
  "code": 0,
  "data": {
    "uuid": "xxx",
    "name": "工作流名称",
    "lab_uuid": "xxx",
    "nodes": [
      {
        "uuid": "节点UUID",
        "name": "action_name",
        "param": { "key": "value" },
        "device_name": "DEVICE_NAME",
        "handles": [
          {
            "uuid": "handle_uuid",
            "handle_key": "ready",
            "io_type": "source|target"
          }
        ]
      }
    ],
    "edges": []
  }
}
```

> **注意**：`GET /lab/workflow/{uuid}` 或 `GET /lab/workflow/{uuid}/nodes` 等路径会返回 404，必须使用 `/lab/workflow/template/detail/{uuid}`。

### 14. 提交实验（创建 notebook）

```bash
curl -s -X POST "$BASE/api/v1/lab/notebook" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"lab_uuid":"<lab_uuid>","workflow_uuid":"<wf_uuid>","name":"<实验名称>","node_params":[{"sample_uuids":[],"datas":[{"node_uuid":"<node_uuid>","param":{...},"sample_params":[]}]}]}'
```

`node_params` 数组每个元素代表一轮实验，`datas` 中包含该轮每个 workflow 节点的参数。返回 `data.uuid` 为 notebook UUID。

---

## Placeholder Slot 填写规则

Action JSON 中 `placeholder_keys` 标记的字段需根据类型用不同格式填入：

| `placeholder_keys` 值 | Slot 类型    | 填写格式                                              | 选取范围                                  |
| --------------------- | ------------ | ----------------------------------------------------- | ----------------------------------------- |
| `unilabos_resources`  | ResourceSlot | `{"id": "/path/name", "name": "name", "uuid": "xxx"}` | 仅**物料**节点（不含设备）                |
| `unilabos_devices`    | DeviceSlot   | `"/parent/device_name"`                               | 仅**设备**节点（type=device），路径字符串 |
| `unilabos_nodes`      | NodeSlot     | `"/parent/node_name"`                                 | **设备 + 物料**，所有节点，路径字符串     |
| `unilabos_class`      | ClassSlot    | `"class_name"`                                        | 注册表中已上报的资源类 name               |

数组类型字段用 `[...]` 包裹多个值。资源树中每个节点的 `id` 本身就是路径格式。

**ResourceSlot 示例**（transfer_liquid）：

```json
{
  "goal": {
    "sources": [
      { "id": "/workstation/plate_1.A1", "name": "A1", "uuid": "xxx" }
    ],
    "targets": [
      { "id": "/workstation/plate_2.B1", "name": "B1", "uuid": "yyy" }
    ],
    "tip_racks": [
      { "id": "/workstation/tip_rack_1", "name": "tip_rack_1", "uuid": "zzz" }
    ],
    "asp_vols": [100.0],
    "dis_vols": [100.0]
  }
}
```

**DeviceSlot 示例**（transfer_materials_to_reaction_station）：

```json
{ "goal": { "target_device_id": "/bioyond_cell/reaction_station" } }
```

**NodeSlot 示例**（create_resource 的 parent）：

```json
{ "goal": { "parent": "/PRCXI/PRCXI_Deck" } }
```

**操作步骤**：

1. 调用 API #12 获取资源树
2. 查看 action JSON 的 `placeholder_keys`，确认每个字段的 slot 类型
3. `unilabos_resources` → 从资源树选取**物料**节点，填 `{id, name, uuid}`
4. `unilabos_devices` → 从资源树筛选 **type=device** 节点，填路径字符串 `/parent/device`
5. `unilabos_nodes` → 从资源树选取**任意**节点（设备 + 物料），填路径字符串
6. `unilabos_class` → 从 `req_resource_registry_upload.json` 查找已注册的类 name
7. 修改 `data.param` 中对应字段，用 PATCH #5 提交

> **特例**：`create_resource` 的 `res_id`（ResourceSlot）可能指向**尚不存在**的物料，此时直接填期望路径如 `"/workstation/container1"`。

---

## 渐进加载策略

1. **SKILL.md**（本文件）— API 端点模板 + session state 管理
2. **[action-index.md](action-index.md)** — 按分类浏览 24 个动作的描述和核心参数，找到要用的 action
3. **[actions/\<name\>.json](actions/)** — 仅在需要构建具体请求时，加载对应 action 的完整 JSON Schema（含嵌套类型、默认值、required 字段）

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 1: GET /edge/lab/info 获取 lab_uuid
- [ ] Step 2: 获取资源树 (GET #12) → 记住可用物料
- [ ] Step 3: 读 action-index.md 确定要用的 action 名
- [ ] Step 4: 创建工作流 (POST #2) → 记住 workflow_uuid，告知用户链接
- [ ] Step 5: 创建节点 (POST #3, resource_template_name + node_template_name) → 记住 node_uuid + data.param
- [ ] Step 6: 根据返回的 _unilabos_placeholder_info 和资源树，填写 data.param 中的 Slot 字段
- [ ] Step 7: 更新节点参数 (PATCH #5)
- [ ] Step 8: 查询节点 handles (POST #6) → 获取各节点的 handle_uuid
- [ ] Step 9: 批量创建边 (POST #7) → 用 handle_uuid 连接节点
- [ ] Step 10: 启动工作流 (POST #8) 或运行单节点 (POST #11)
- [ ] Step 11: 查询任务状态 (GET #10) 确认完成
- [ ] (可选) Step 12: 获取工作流模板详情 (GET #13) 查看已有节点
```
