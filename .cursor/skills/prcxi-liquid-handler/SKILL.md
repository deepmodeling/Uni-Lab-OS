---
name: prcxi-liquid-handler
description: Operate PRCXI 9300 liquid handler via REST API — create workflows, manage nodes, run liquid handling actions, query tasks. Use when the user mentions PRCXI, 移液站, liquid handler workflows, running liquid handling actions, or any PRCXI platform operation.
---

# PRCXI 9300 移液站 API Skill

## 设备信息

- **device_id**: `liquid_handler.prcxi`
- **Python 源码**: PRCXI 驱动包中的 `prcxi.py`（原 `unilabos/devices/liquid_handling/prcxi/prcxi.py`，已迁出主仓库）
- **设备类**: `PRCXI9300Handler`
- **Action 数量**: 24

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
- `device_name` — 设备名称：`liquid_handler.prcxi`

## 请求约定

所有请求使用 `curl -s`，POST/PATCH/DELETE 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名），示例中的 `curl` 均指 `curl.exe`。

---

## API Endpoints

### 1. 获取实验室信息（自动获取 lab_uuid）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

通过 ak/sk 的 Lab token **直接**返回对应实验室信息，**不需要询问用户**。记住 `data.uuid` 为 `lab_uuid`。

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
  -d '{"workflow_uuid":"<workflow_uuid>","resource_template_name":"liquid_handler.prcxi","node_template_name":"<action_name>"}'
```

- `resource_template_name` — 固定为 `liquid_handler.prcxi`
- `node_template_name` — action 名称（如 `transfer_liquid`、`pick_up_tips`）

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

`param` 的格式：直接使用创建节点返回的 `data.param` 结构（goal 字段已展开），修改需要填入的字段值即可。

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
  -d '{"lab_uuid":"<lab_uuid>","device_id":"liquid_handler.prcxi","action":"<action_name>","action_type":"<从JSON的type字段>","param":{...goal里的属性直接展开...}}'
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

注意 `lab_uuid` 在路径中。返回实验室的完整资源树。每个节点包含 `id`（路径格式）、`name`、`uuid`、`type`、`parent` 等字段。用于填写 action 中的 Placeholder Slot 字段。

---

## Placeholder Slot 填写规则

> 本设备（liquid_handler.prcxi）所有 placeholder 均为 **ResourceSlot**（`unilabos_resources`），无 DeviceSlot。
> 其他设备可能包含：
>
> - `unilabos_devices`（DeviceSlot）→ 填路径字符串如 `"/host_node"`
> - `unilabos_nodes`（NodeSlot）→ 填路径字符串如 `"/PRCXI/PRCXI_Deck"`，范围 = 设备 + 物料
> - `unilabos_class`（ClassSlot）→ 填注册表中的类 name 如 `"container"`

**ResourceSlot 格式**（资源树中 `id` 本身是路径格式）：

- 单个：`{"id": "/workstation/container1", "name": "container1", "uuid": "ff149a9a-..."}`
- 数组：`[{"id": "/path/a", "name": "a", "uuid": "xxx"}, {"id": "/path/b", "name": "b", "uuid": "yyy"}]`

**步骤**：

1. 调用 API #12 获取资源树，拿到每个物料的 `id`（路径）、`name` 和 `uuid`
2. 查看 action JSON 的 `placeholder_keys`，确认哪些字段是 ResourceSlot
3. 根据 action 语义选取物料（如 `sources` = 液体来源孔，`targets` = 目标孔，`tip_racks` = 枪头盒）
4. 用 `{id, name, uuid}` 格式填入

**本设备常见 ResourceSlot 字段**：

| 字段              | 出现在                                     | 含义           |
| ----------------- | ------------------------------------------ | -------------- |
| `sources`         | transfer_liquid                            | 液体来源孔位   |
| `targets`         | transfer_liquid, add_liquid, remove_liquid | 目标孔位       |
| `tip_racks`       | transfer_liquid                            | 枪头盒         |
| `tip_spots`       | pick_up_tips, drop_tips                    | 枪头位置       |
| `reagent_sources` | add_liquid                                 | 试剂来源       |
| `resource`        | set_liquid, set_liquid_from_plate          | 操作的孔板资源 |

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
- [ ] Step 5: 创建节点 (POST #3, resource_template_name=liquid_handler.prcxi, node_template_name=<action>) → 记住 node_uuid + data.param
- [ ] Step 6: 根据返回的 _unilabos_placeholder_info 和资源树，填写 data.param 中的 Slot 字段
- [ ] Step 7: 更新节点参数 (PATCH #5)
- [ ] Step 8: 查询节点 handles (POST #6) → 获取各节点的 handle_uuid
- [ ] Step 9: 批量创建边 (POST #7) → 用 handle_uuid 连接节点
- [ ] Step 10: 启动工作流 (POST #8) 或运行单节点 (POST #11)
- [ ] Step 11: 查询任务状态 (GET #10) 确认完成
```
