---
name: batch-submit-experiment
description: Batch submit experiments (notebooks) to the Uni-Lab cloud platform (leap-lab) — list workflows, generate node_params from registry schemas, submit multiple rounds, check notebook status. Use when the user wants to submit experiments, create notebooks, batch run workflows, check experiment status, or mentions 提交实验/批量实验/notebook/实验轮次/实验状态.
---

# Uni-Lab 批量提交实验指南

通过 Uni-Lab 云端 API 批量提交实验（notebook），支持多轮实验参数配置。根据 workflow 模板详情和本地设备注册表自动生成 `node_params` 模板。

> **重要**：本指南中的 `Authorization: Lab <token>` 是 **Uni-Lab 平台专用的认证方式**，`Lab` 是 Uni-Lab 的 auth scheme 关键字，**不是** HTTP Basic 认证。请勿将其替换为 `Basic`。

## 前置条件（缺一不可）

使用本指南前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

询问用户的启动参数，从 `--ak` `--sk` 或 config.py 中获取。

生成 AUTH token（任选一种方式）：

```bash
# 方式一：Python 一行生成（注意：scheme 是 "Lab" 不是 "Basic"）
python -c "import base64,sys; print('Authorization: Lab ' + base64.b64encode(f'{sys.argv[1]}:{sys.argv[2]}'.encode()).decode())" <ak> <sk>

# 方式二：手动计算
# base64(ak:sk) → Authorization: Lab <token>
# ⚠️ 这里的 "Lab" 是 Uni-Lab 平台的 auth scheme，绝对不能用 "Basic" 替代
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
# ⚠️ Auth scheme 必须是 "Lab"（Uni-Lab 专用），不是 "Basic"
AUTH="Authorization: Lab <上面命令输出的 token>"
```

### 3. req_device_registry_upload.json（设备注册表）

**批量提交实验时需要本地注册表来解析 workflow 节点的参数 schema。**

**必须先用 Glob 工具搜索文件**，不要直接猜测路径：

```
Glob: **/req_device_registry_upload.json
```

常见位置（仅供参考，以 Glob 实际结果为准）：
- `<workspace>/unilabos_data/req_device_registry_upload.json`
- `<workspace>/req_device_registry_upload.json`

找到后**检查文件修改时间**并告知用户。超过 1 天提醒用户是否需要重新启动 `unilab`。

**如果 Glob 搜索无结果** → 告知用户先运行 `unilab` 启动命令，等注册表生成后再执行。可跳过此步，但将无法自动生成参数模板，需要用户手动填写 `param`。

### 4. workflow_uuid（目标工作流）

用户需要提供要提交的 workflow UUID。如果用户不确定，通过 API #3 列出可用 workflow 供选择。

**四项全部就绪后才可开始。**

## Session State

在整个对话过程中，agent 需要记住以下状态，避免重复询问用户：

- `lab_uuid` — 实验室 UUID（首次通过 API #1 自动获取，**不需要问用户**）
- `project_uuid` — 项目 UUID（通过 API #2 列出项目列表，**让用户选择**）
- `workflow_uuid` — 工作流 UUID（用户提供或从列表选择）
- `workflow_nodes` — workflow 中各 action 节点的 uuid、设备 ID、动作名（从 API #4 获取）

## 请求约定

所有请求使用 `curl -s`，POST 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名），示例中的 `curl` 均指 `curl.exe`。
>
> **PowerShell JSON 传参**：PowerShell 中 `-d '{"key":"value"}'` 会因引号转义失败。请将 JSON 写入临时文件，用 `-d '@tmp_body.json'`（单引号包裹 `@`，否则会被解析为 splatting 运算符）。

---

## API Endpoints

### 1. 获取实验室信息（自动获取 lab_uuid）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

返回：

```json
{ "code": 0, "data": { "uuid": "xxx", "name": "实验室名称" } }
```

记住 `data.uuid` 为 `lab_uuid`。

### 2. 列出实验室项目（让用户选择项目）

```bash
curl -s -X GET "$BASE/api/v1/lab/project/list?lab_uuid=$lab_uuid" -H "$AUTH"
```

返回：

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "uuid": "1b3f249a-...",
        "name": "bt",
        "description": null,
        "status": "active",
        "created_at": "2026-04-09T14:31:28+08:00"
      },
      {
        "uuid": "b6366243-...",
        "name": "default",
        "description": "默认项目",
        "status": "active",
        "created_at": "2026-03-26T11:13:36+08:00"
      }
    ]
  }
}
```

展示 `data.items[]` 中每个项目的 `name` 和 `uuid`，让用户选择。用户**必须**选择一个项目，记住 `project_uuid`（即选中项目的 `uuid`），后续创建 notebook 时需要提供。

### 3. 列出可用 workflow

```bash
curl -s -X GET "$BASE/api/v1/lab/workflow/workflows?page=1&page_size=20&lab_uuid=$lab_uuid" -H "$AUTH"
```

返回 workflow 列表，展示给用户选择。列出每个 workflow 的 `uuid` 和 `name`。

### 4. 获取 workflow 模板详情

```bash
curl -s -X GET "$BASE/api/v1/lab/workflow/template/detail/$workflow_uuid" -H "$AUTH"
```

返回 workflow 的完整结构，包含所有 action 节点信息。需要从响应中提取：

- 每个 action 节点的 `node_uuid`
- 每个节点对应的设备 ID（`resource_template_name`）
- 每个节点的动作名（`node_template_name`）
- 每个节点的现有参数（`param`）

> **注意**：此 API 返回格式可能因版本不同而有差异。首次调用时，先打印完整响应分析结构，再提取节点信息。常见的节点字段路径为 `data.nodes[]` 或 `data.workflow_nodes[]`。

### 5. 提交实验（创建 notebook）

```bash
curl -s -X POST "$BASE/api/v1/lab/notebook" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '<request_body>'
```

请求体结构：

```json
{
  "lab_uuid": "<lab_uuid>",
  "project_uuid": "<project_uuid>",
  "workflow_uuid": "<workflow_uuid>",
  "name": "<实验名称>",
  "node_params": [
    {
      "sample_uuids": ["<样品UUID1>", "<样品UUID2>"],
      "datas": [
        {
          "node_uuid": "<workflow中的节点UUID>",
          "param": {},
          "sample_params": [
            {
              "container_uuid": "<容器UUID>",
              "sample_value": {
                "liquid_names": "<液体名称>",
                "volumes": 1000
              }
            }
          ]
        }
      ]
    }
  ]
}
```

> **注意**：`sample_uuids` 必须是 **UUID 数组**（`[]uuid.UUID`），不是字符串。无样品时传空数组 `[]`。

### 6. 查询 notebook 状态

提交成功后，使用返回的 notebook UUID 查询执行状态：

```bash
curl -s -X GET "$BASE/api/v1/lab/notebook/status?uuid=$notebook_uuid" -H "$AUTH"
```

提交后应**立即查询一次**状态，确认 notebook 已被正确接收并开始调度。

---

## Notebook 请求体详解

### node_params 结构

`node_params` 是一个数组，**每个元素代表一轮实验**：

- 要跑 2 轮 → `node_params` 有 2 个元素
- 要跑 N 轮 → `node_params` 有 N 个元素

### 每轮的字段

| 字段           | 类型          | 说明                                      |
| -------------- | ------------- | ----------------------------------------- |
| `sample_uuids` | array\<uuid\> | 该轮实验的样品 UUID 数组，无样品时传 `[]` |
| `datas`        | array         | 该轮中每个 workflow 节点的参数配置        |

### datas 中每个节点

| 字段            | 类型   | 说明                                         |
| --------------- | ------ | -------------------------------------------- |
| `node_uuid`     | string | workflow 模板中的节点 UUID（从 API #4 获取） |
| `param`         | object | 动作参数（根据本地注册表 schema 填写）       |
| `sample_params` | array  | 样品相关参数（液体名、体积等）               |

### sample_params 中每条

| 字段             | 类型   | 说明                                                 |
| ---------------- | ------ | ---------------------------------------------------- |
| `container_uuid` | string | 容器 UUID                                            |
| `sample_value`   | object | 样品值，如 `{"liquid_names": "水", "volumes": 1000}` |

---

## 从本地注册表生成 param 模板

### 自动方式 — 运行脚本

```bash
python scripts/gen_notebook_params.py \
  --auth <token> \
  --base <BASE_URL> \
  --workflow-uuid <workflow_uuid> \
  [--registry <path/to/req_device_registry_upload.json>] \
  [--rounds <轮次数>] \
  [--output <输出文件路径>]
```

> 脚本位于本文档同级目录下的 `scripts/gen_notebook_params.py`。

脚本会：

1. 调用 workflow detail API 获取所有 action 节点
2. 读取本地注册表，为每个节点查找对应的 action schema
3. 生成 `notebook_template.json`，包含：
   - 完整 `node_params` 骨架
   - 每个节点的 param 字段及类型说明
   - `_schema_info` 辅助信息（不提交，仅供参考）

### 手动方式

如果脚本不可用或注册表不存在：

1. 调用 API #4 获取 workflow 详情
2. 找到每个 action 节点的 `node_uuid`
3. 在本地注册表中查找对应设备的 `action_value_mappings`：
   ```
   resources[].id == <device_id>
   → resources[].class.action_value_mappings.<action_name>.schema.properties.goal.properties
   ```
4. 将 schema 中的 properties 作为 `param` 的字段模板
5. 按轮次复制 `node_params` 元素，让用户填写每轮的具体值

### 注册表结构参考

```json
{
  "resources": [
    {
      "id": "liquid_handler.prcxi",
      "class": {
        "module": "unilabos.devices.xxx:ClassName",
        "action_value_mappings": {
          "transfer_liquid": {
            "type": "LiquidHandlerTransfer",
            "schema": {
              "properties": {
                "goal": {
                  "properties": {
                    "asp_vols": {
                      "type": "array",
                      "items": { "type": "number" }
                    },
                    "sources": { "type": "array" }
                  },
                  "required": ["asp_vols", "sources"]
                }
              }
            },
            "goal_default": {}
          }
        }
      }
    }
  ]
}
```

`param` 填写时，使用 `goal.properties` 中的字段名和类型。

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 1: 确认 ak/sk → 生成 AUTH token
- [ ] Step 2: 确认 --addr → 设置 BASE URL
- [ ] Step 3: GET /edge/lab/info → 获取 lab_uuid
- [ ] Step 4: GET /lab/project/list → 列出项目，让用户选择 → 获取 project_uuid
- [ ] Step 5: 确认 workflow_uuid（用户提供或从 GET #3 列表选择）
- [ ] Step 6: GET workflow detail (#4) → 提取各节点 uuid、设备ID、动作名
- [ ] Step 7: 定位本地注册表 req_device_registry_upload.json
- [ ] Step 8: 运行 gen_notebook_params.py 或手动匹配 → 生成 node_params 模板
- [ ] Step 9: 引导用户填写每轮的参数（sample_uuids、param、sample_params）
- [ ] Step 10: 构建完整请求体（含 project_uuid）→ POST /lab/notebook 提交
- [ ] Step 11: 检查返回结果，记录 notebook UUID
- [ ] Step 12: GET /lab/notebook/status → 查询 notebook 状态，确认已调度
```

---

## 常见问题

### Q: workflow 中有多个节点，每轮都要填所有节点的参数吗？

是的。`datas` 数组中需要包含该轮实验涉及的每个 workflow 节点的参数。通常每个 action 节点都需要一条 `datas` 记录。

### Q: 多轮实验的参数完全不同吗？

通常每轮的 `param`（设备动作参数）可能相同或相似，但 `sample_uuids` 和 `sample_params`（样品信息）每轮不同。脚本生成模板时会按轮次复制骨架，用户只需修改差异部分。

### Q: 如何获取 sample_uuids 和 container_uuid？

这些 UUID 通常来自实验室的样品管理系统。向用户询问，或从资源树（API `GET /lab/material/download/$lab_uuid`）中查找。
