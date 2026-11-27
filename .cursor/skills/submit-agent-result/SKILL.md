---
name: submit-agent-result
description: Submit historical experiment results (agent_result) to Uni-Lab cloud platform (leap-lab) notebook — read data files, assemble JSON payload, PUT to cloud API. Use when the user wants to submit experiment results, upload agent results, report experiment data, or mentions agent_result/实验结果/历史记录/notebook结果.
---

# Uni-Lab 提交历史实验记录指南

通过 Uni-Lab 云端 API 向已创建的 notebook 提交实验结果数据（agent_result）。支持从 JSON / CSV 文件读取数据，整合后提交。

> **重要**：本指南中的 `Authorization: Lab <token>` 是 **Uni-Lab 平台专用的认证方式**，`Lab` 是 Uni-Lab 的 auth scheme 关键字，**不是** HTTP Basic 认证。请勿将其替换为 `Basic`。

## 前置条件（缺一不可）

使用本指南前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

询问用户的启动参数，从 `--ak` `--sk` 或 config.py 中获取。

生成 AUTH token：

```bash
# ⚠️ 注意：scheme 是 "Lab"（Uni-Lab 专用），不是 "Basic"
python -c "import base64,sys; print(base64.b64encode(f'{sys.argv[1]}:{sys.argv[2]}'.encode()).decode())" <ak> <sk>
```

输出即为 token 值，拼接为 `Authorization: Lab <token>`（`Lab` 是 Uni-Lab 平台 auth scheme，不可替换为 `Basic`）。

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

### 3. notebook_uuid（**必须询问用户**）

**必须主动询问用户**：「请提供要提交结果的 notebook UUID。」

notebook_uuid 来自之前通过「批量提交实验」创建的实验批次，即 `POST /api/v1/lab/notebook` 返回的 `data.uuid`。

如果用户不记得，可提示：

- 查看之前的对话记录中创建 notebook 时返回的 UUID
- 或通过平台页面查找对应的 notebook

**绝不能跳过此步骤，没有 notebook_uuid 无法提交。**

### 4. 实验结果数据

用户需要提供实验结果数据，支持以下方式：

| 方式      | 说明                                            |
| --------- | ----------------------------------------------- |
| JSON 文件 | 直接作为 `agent_result` 的内容合并              |
| CSV 文件  | 转为 `{"文件名": [行数据...]}` 格式             |
| 手动指定  | 用户直接告知 key-value 数据，由 agent 构建 JSON |

**四项全部就绪后才可开始。**

## Session State

在整个对话过程中，agent 需要记住以下状态：

- `lab_uuid` — 实验室 UUID（通过 API #1 自动获取，**不需要问用户**）
- `notebook_uuid` — 目标 notebook UUID（**必须询问用户**）

## 请求约定

所有请求使用 `curl -s`，PUT 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名），示例中的 `curl` 均指 `curl.exe`。
>
> **PowerShell JSON 传参**：PowerShell 中 `-d '{"key":"value"}'` 会因引号转义失败。请将 JSON 写入临时文件，用 `-d '@tmp_body.json'`（单引号包裹 `@`，否则 `@` 会被 PowerShell 解析为 splatting 运算符导致报错）。

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

### 2. 提交实验结果（agent_result）

```bash
curl -s -X PUT "$BASE/api/v1/lab/notebook/agent-result" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '<request_body>'
```

请求体结构：

```json
{
    "notebook_uuid": "<notebook_uuid>",
    "agent_result": {
        "<key1>": "<value1>",
        "<key2>": 123,
        "<nested_key>": {"a": 1, "b": 2},
        "<array_key>": [{"col1": "v1", "col2": "v2"}, ...]
    }
}
```

> **注意**：HTTP 方法是 **PUT**（不是 POST）。

#### 必要字段

| 字段            | 类型          | 说明                                        |
| --------------- | ------------- | ------------------------------------------- |
| `notebook_uuid` | string (UUID) | 目标 notebook 的 UUID，从批量提交实验时获取 |
| `agent_result`  | object        | 实验结果数据，任意 JSON 对象                |

#### agent_result 内容格式

`agent_result` 接受**任意 JSON 对象**，常见格式：

**简单键值对**：

```json
{
  "avg_rtt_ms": 12.5,
  "status": "success",
  "test_count": 5
}
```

**包含嵌套结构**：

```json
{
  "summary": { "total": 100, "passed": 98, "failed": 2 },
  "measurements": [
    { "sample_id": "S001", "value": 3.14, "unit": "mg/mL" },
    { "sample_id": "S002", "value": 2.71, "unit": "mg/mL" }
  ]
}
```

**从 CSV 文件导入**（脚本自动转换）：

```json
{
  "experiment_data": [
    { "温度": 25, "压力": 101.3, "产率": 0.85 },
    { "温度": 30, "压力": 101.3, "产率": 0.91 }
  ]
}
```

---

## 整合脚本

本文档同级目录下的 `scripts/prepare_agent_result.py` 可自动读取文件并构建请求体。

### 用法

```bash
python scripts/prepare_agent_result.py \
    --notebook-uuid <uuid> \
    --files data1.json data2.csv \
    [--auth <token>] \
    [--base <BASE_URL>] \
    [--submit] \
    [--output <output.json>]
```

| 参数              | 必选       | 说明                                            |
| ----------------- | ---------- | ----------------------------------------------- |
| `--notebook-uuid` | 是         | 目标 notebook UUID                              |
| `--files`         | 是         | 输入文件路径（支持多个，JSON / CSV）            |
| `--auth`          | 提交时必选 | Lab token（base64(ak:sk)）                      |
| `--base`          | 提交时必选 | API base URL                                    |
| `--submit`        | 否         | 加上此标志则直接提交到云端                      |
| `--output`        | 否         | 输出 JSON 路径（默认 `agent_result_body.json`） |

### 文件合并规则

| 文件类型              | 合并方式                                     |
| --------------------- | -------------------------------------------- |
| `.json`（dict）       | 字段直接合并到 `agent_result` 顶层           |
| `.json`（list/other） | 以文件名为 key 放入 `agent_result`           |
| `.csv`                | 以文件名（不含扩展名）为 key，值为行对象数组 |

多个文件的字段会合并。JSON dict 中的重复 key 后者覆盖前者。

### 示例

```bash
# 仅生成请求体文件（不提交）
python scripts/prepare_agent_result.py \
    --notebook-uuid 73c67dca-c8cc-4936-85a0-329106aa7cca \
    --files results.json measurements.csv

# 生成并直接提交
python scripts/prepare_agent_result.py \
    --notebook-uuid 73c67dca-c8cc-4936-85a0-329106aa7cca \
    --files results.json \
    --auth YTFmZDlkNGUt... \
    --base https://leap-lab.test.bohrium.com \
    --submit
```

---

## 手动构建方式

如果不使用脚本，也可手动构建请求体：

1. 将实验结果数据组装为 JSON 对象
2. 写入临时文件：

```json
{
    "notebook_uuid": "<uuid>",
    "agent_result": { ... }
}
```

3. 用 curl 提交：

```bash
curl -s -X PUT "$BASE/api/v1/lab/notebook/agent-result" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '@tmp_body.json'
```

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 1: 确认 ak/sk → 生成 AUTH token
- [ ] Step 2: 确认 --addr → 设置 BASE URL
- [ ] Step 3: GET /edge/lab/info → 获取 lab_uuid
- [ ] Step 4: **询问用户** notebook_uuid（必须，不可跳过）
- [ ] Step 5: 确认实验结果数据来源（文件路径或手动数据）
- [ ] Step 6: 运行 prepare_agent_result.py 或手动构建请求体
- [ ] Step 7: PUT /lab/notebook/agent-result 提交
- [ ] Step 8: 检查返回结果，确认提交成功
```

---

## 常见问题

### Q: notebook_uuid 从哪里获取？

从之前「批量提交实验」时 `POST /api/v1/lab/notebook` 的返回值 `data.uuid` 获取。也可以在平台 UI 中查找对应的 notebook。

### Q: agent_result 有固定的 schema 吗？

没有严格 schema，接受任意 JSON 对象。但建议包含有意义的字段名和结构化数据，方便后续分析。

### Q: 可以多次提交同一个 notebook 的结果吗？

可以，后续提交会覆盖之前的 agent_result。

### Q: 认证方式是 Lab 还是 Api？

本指南统一使用 `Authorization: Lab <base64(ak:sk)>` 方式（`Lab` 是 Uni-Lab 平台的 auth scheme，**绝不能用 `Basic` 替代**）。如果用户有独立的 API Key，也可用 `Authorization: Api <key>` 替代。
