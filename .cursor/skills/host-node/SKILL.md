---
name: host-node
description: Operate Uni-Lab host node via REST API — create resources, test latency, test resource tree, manual confirm. Use when the user mentions host_node, creating resources, resource management, testing latency, or any host node operation.
---

# Host Node API Skill

## 设备信息

- **device_id**: `host_node`
- **Python 源码**: `unilabos/ros/nodes/presets/host_node.py`
- **设备类**: `HostNode`
- **动作数**: 4（`create_resource`, `test_latency`, `auto-test_resource`, `manual_confirm`）

## 前置条件（缺一不可）

使用本 skill 前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

从启动参数 `--ak` `--sk` 或 config.py 中获取，生成 token：`base64(ak:sk)` → `Authorization: Lab <token>`

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
AUTH="Authorization: Lab <token>"
```

**两项全部就绪后才可发起 API 请求。**

## Session State

在整个对话过程中，agent 需要记住以下状态，避免重复询问用户：

- `lab_uuid` — 实验室 UUID（首次通过 API #1 自动获取，**不需要问用户**）
- `device_name` — `host_node`

## 请求约定

所有请求使用 `curl -s`，POST/PATCH/DELETE 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名）。

---

## API Endpoints

### 1. 获取实验室信息（自动获取 lab_uuid）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

返回 `data.uuid` 为 `lab_uuid`，`data.name` 为 `lab_name`。

### 2. 创建工作流

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/owner" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"<名称>","lab_uuid":"<lab_uuid>","description":"<描述>"}'
```

返回 `data.uuid` 为 `workflow_uuid`。创建成功后告知用户链接：`$BASE/laboratory/$lab_uuid/workflow/$workflow_uuid`

### 3. 创建节点

```bash
curl -s -X POST "$BASE/api/v1/edge/workflow/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"workflow_uuid":"<workflow_uuid>","resource_template_name":"host_node","node_template_name":"<action_name>"}'
```

- `resource_template_name` 固定为 `host_node`
- `node_template_name` — action 名称（如 `create_resource`, `test_latency`）

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

`param` 直接使用创建节点返回的 `data.param` 结构，修改需要填入的字段值。参考 [action-index.md](action-index.md) 确定哪些字段是 Slot。

### 6. 查询节点 handles

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/node-handles" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"node_uuids":["<node_uuid_1>","<node_uuid_2>"]}'
```

### 7. 批量创建边

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/edges" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"edges":[{"source_node_uuid":"<uuid>","target_node_uuid":"<uuid>","source_handle_uuid":"<uuid>","target_handle_uuid":"<uuid>"}]}'
```

### 8. 启动工作流

```bash
curl -s -X POST "$BASE/api/v1/lab/workflow/<workflow_uuid>/run" -H "$AUTH"
```

### 9. 运行设备单动作

```bash
curl -s -X POST "$BASE/api/v1/lab/mcp/run/action" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"lab_uuid":"<lab_uuid>","device_id":"host_node","action":"<action_name>","action_type":"<type>","param":{...}}'
```

`param` 直接放 goal 里的属性，**不要**再包一层 `{"goal": {...}}`。

> **WARNING: `action_type` 必须正确，传错会导致任务永远卡住无法完成。** 从下表或 `actions/<name>.json` 的 `type` 字段获取。

#### action_type 速查表

| action | action_type |
|--------|-------------|
| `test_latency` | `UniLabJsonCommand` |
| `create_resource` | `ResourceCreateFromOuterEasy` |
| `auto-test_resource` | `UniLabJsonCommand` |
| `manual_confirm` | `UniLabJsonCommand` |

### 10. 查询任务状态

```bash
curl -s -X GET "$BASE/api/v1/lab/mcp/task/<task_uuid>" -H "$AUTH"
```

### 11. 运行工作流单节点

```bash
curl -s -X POST "$BASE/api/v1/lab/mcp/run/workflow/action" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"node_uuid":"<node_uuid>"}'
```

### 12. 获取资源树（物料信息）

```bash
curl -s -X GET "$BASE/api/v1/lab/material/download/$lab_uuid" -H "$AUTH"
```

注意 `lab_uuid` 在路径中。返回 `data.nodes[]` 含所有节点（设备 + 物料），每个节点含 `name`、`uuid`、`type`、`parent`。

### 13. 获取工作流模板详情

```bash
curl -s -X GET "$BASE/api/v1/lab/workflow/template/detail/$workflow_uuid" -H "$AUTH"
```

> 必须使用 `/lab/workflow/template/detail/{uuid}`，其他路径会返回 404。

### 14. 按名称查询物料模板

```bash
curl -s -X GET "$BASE/api/v1/lab/material/template/by-name?lab_uuid=$lab_uuid&name=<template_name>" -H "$AUTH"
```

返回 `data.uuid` 为 `res_template_uuid`，用于 API #15。

### 15. 创建物料节点

```bash
curl -s -X POST "$BASE/api/v1/edge/material/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"res_template_uuid":"<uuid>","name":"<名称>","display_name":"<显示名>","parent_uuid":"<父节点uuid>","data":{...}}'
```

### 16. 更新物料节点

```bash
curl -s -X PUT "$BASE/api/v1/edge/material/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"uuid":"<节点uuid>","display_name":"<新名称>","data":{...}}'
```

---

## Placeholder Slot 填写规则

| `placeholder_keys` 值 | Slot 类型    | 填写格式                                              | 选取范围               |
| --------------------- | ------------ | ----------------------------------------------------- | ---------------------- |
| `unilabos_resources`  | ResourceSlot | `{"id": "/path/name", "name": "name", "uuid": "xxx"}` | 仅物料节点（非设备）   |
| `unilabos_devices`    | DeviceSlot   | `"/parent/device_name"`                               | 仅设备节点（type=device） |
| `unilabos_nodes`      | NodeSlot     | `"/parent/node_name"`                                 | 所有节点（设备 + 物料）   |
| `unilabos_class`      | ClassSlot    | `"class_name"`                                        | 注册表中已注册的资源类    |

### host_node 设备的 Slot 字段表

| Action            | 字段        | Slot 类型    | 说明                           |
| ----------------- | ----------- | ------------ | ------------------------------ |
| `create_resource` | `res_id`    | ResourceSlot | 新资源路径（可填不存在的路径） |
| `create_resource` | `device_id` | DeviceSlot   | 归属设备                       |
| `create_resource` | `parent`    | NodeSlot     | 父节点路径                     |
| `create_resource` | `class_name`| ClassSlot    | 资源类名如 `"container"`       |
| `auto-test_resource` | `resource`  | ResourceSlot | 单个测试物料                |
| `auto-test_resource` | `resources` | ResourceSlot | 测试物料数组                |
| `auto-test_resource` | `device`    | DeviceSlot   | 测试设备                    |
| `auto-test_resource` | `devices`   | DeviceSlot   | 测试设备                    |

---

## 渐进加载策略

1. **SKILL.md**（本文件）— API 端点 + session state 管理
2. **[action-index.md](action-index.md)** — 按分类浏览 4 个动作的描述和核心参数
3. **[actions/\<name\>.json](actions/)** — 仅在需要构建具体请求时，加载对应 action 的完整 JSON Schema

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 1: GET /edge/lab/info 获取 lab_uuid
- [ ] Step 2: 获取资源树 (GET #12) → 记住可用物料
- [ ] Step 3: 读 action-index.md 确定要用的 action 名
- [ ] Step 4: 创建工作流 (POST #2) → 记住 workflow_uuid，告知用户链接
- [ ] Step 5: 创建节点 (POST #3, resource_template_name=host_node) → 记住 node_uuid + data.param
- [ ] Step 6: 根据 _unilabos_placeholder_info 和资源树，填写 data.param 中的 Slot 字段
- [ ] Step 7: 更新节点参数 (PATCH #5)
- [ ] Step 8: 查询节点 handles (POST #6) → 获取各节点的 handle_uuid
- [ ] Step 9: 批量创建边 (POST #7) → 用 handle_uuid 连接节点
- [ ] Step 10: 启动工作流 (POST #8) 或运行单节点 (POST #11)
- [ ] Step 11: 查询任务状态 (GET #10) 确认完成
```
