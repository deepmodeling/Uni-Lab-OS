# HTTP API 指南

本文档介绍如何通过 HTTP API 与 Uni-Lab-OS 进行交互，包括查询设备、提交任务和获取结果。

## 概述

Uni-Lab-OS 提供 RESTful HTTP API，允许外部系统通过标准 HTTP 请求控制实验室设备。API 基于 FastAPI 构建，默认运行在 `http://localhost:8002`。

### 基础信息

- **Base URL**: `http://localhost:8002/api/v1`
- **Content-Type**: `application/json`
- **响应格式**: JSON

### 通用响应结构

```json
{
    "code": 0,
    "data": { ... },
    "message": "success"
}
```

| 字段      | 类型   | 说明               |
| --------- | ------ | ------------------ |
| `code`    | int    | 状态码，0 表示成功 |
| `data`    | object | 响应数据           |
| `message` | string | 响应消息           |

## 快速开始

以下是一个完整的工作流示例：查询设备 → 获取动作 → 提交任务 → 获取结果。

### 步骤 1: 获取在线设备

```bash
curl -X GET "http://localhost:8002/api/v1/online-devices"
```

**响应示例**:

```json
{
  "code": 0,
  "data": {
    "online_devices": {
      "host_node": {
        "device_key": "/host_node",
        "namespace": "",
        "machine_name": "本地",
        "uuid": "xxx-xxx-xxx",
        "node_name": "host_node"
      }
    },
    "total_count": 1,
    "timestamp": 1732612345.123
  },
  "message": "success"
}
```

### 步骤 2: 获取设备可用动作

```bash
curl -X GET "http://localhost:8002/api/v1/devices/host_node/actions"
```

**响应示例**:

```json
{
  "code": 0,
  "data": {
    "device_id": "host_node",
    "actions": {
      "test_latency": {
        "type_name": "unilabos_msgs.action._empty_in.EmptyIn",
        "type_name_convert": "unilabos_msgs/action/_empty_in/EmptyIn",
        "action_path": "/devices/host_node/test_latency",
        "goal_info": "{}",
        "is_busy": false,
        "current_job_id": null
      },
      "create_resource": {
        "type_name": "unilabos_msgs.action._resource_create_from_outer_easy.ResourceCreateFromOuterEasy",
        "action_path": "/devices/host_node/create_resource",
        "goal_info": "{res_id: '', device_id: '', class_name: '', ...}",
        "is_busy": false,
        "current_job_id": null
      }
    },
    "action_count": 5
  },
  "message": "success"
}
```

**动作状态字段说明**:

| 字段             | 说明                          |
| ---------------- | ----------------------------- |
| `type_name`      | 动作类型的完整名称            |
| `action_path`    | ROS2 动作路径                 |
| `goal_info`      | 动作参数模板                  |
| `is_busy`        | 动作是否正在执行              |
| `current_job_id` | 当前执行的任务 ID（如果繁忙） |

### 步骤 3: 提交任务

```bash
curl -X POST "http://localhost:8002/api/v1/job/add" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"host_node","action":"test_latency","action_args":{}}'
```

**请求体**:

```json
{
  "device_id": "host_node",
  "action": "test_latency",
  "action_args": {}
}
```

**请求参数说明**:

| 字段          | 类型   | 必填 | 说明                               |
| ------------- | ------ | ---- | ---------------------------------- |
| `device_id`   | string | ✓    | 目标设备 ID                        |
| `action`      | string | ✓    | 动作名称                           |
| `action_args` | object | ✓    | 动作参数（根据动作类型不同而变化） |

**响应示例**:

```json
{
  "code": 0,
  "data": {
    "jobId": "b6acb586-733a-42ab-9f73-55c9a52aa8bd",
    "status": 1,
    "result": {}
  },
  "message": "success"
}
```

**任务状态码**:

| 状态码 | 含义      | 说明                           |
| ------ | --------- | ------------------------------ |
| 0      | UNKNOWN   | 未知状态                       |
| 1      | ACCEPTED  | 任务已接受，等待执行           |
| 2      | EXECUTING | 任务执行中                     |
| 3      | CANCELING | 任务取消中                     |
| 4      | SUCCEEDED | 任务成功完成                   |
| 5      | CANCELED  | 任务已取消                     |
| 6      | ABORTED   | 任务中止（设备繁忙或执行失败） |

### 步骤 4: 查询任务状态和结果

```bash
curl -X GET "http://localhost:8002/api/v1/job/b6acb586-733a-42ab-9f73-55c9a52aa8bd/status"
```

**响应示例（执行中）**:

```json
{
  "code": 0,
  "data": {
    "jobId": "b6acb586-733a-42ab-9f73-55c9a52aa8bd",
    "status": 2,
    "result": {}
  },
  "message": "success"
}
```

**响应示例（执行完成）**:

```json
{
  "code": 0,
  "data": {
    "jobId": "b6acb586-733a-42ab-9f73-55c9a52aa8bd",
    "status": 4,
    "result": {
      "error": "",
      "suc": true,
      "return_value": {
        "avg_rtt_ms": 103.99,
        "avg_time_diff_ms": 7181.55,
        "max_time_error_ms": 7210.57,
        "task_delay_ms": -1,
        "raw_delay_ms": 33.19,
        "test_count": 5,
        "status": "success"
      }
    }
  },
  "message": "success"
}
```

> **注意**: 任务结果在首次查询后会被自动删除，请确保保存返回的结果数据。

## API 端点列表

### 设备相关

| 端点                                                       | 方法 | 说明                   |
| ---------------------------------------------------------- | ---- | ---------------------- |
| `/api/v1/online-devices`                                   | GET  | 获取在线设备列表       |
| `/api/v1/devices`                                          | GET  | 获取设备配置           |
| `/api/v1/devices/{device_id}/actions`                      | GET  | 获取指定设备的可用动作 |
| `/api/v1/devices/{device_id}/actions/{action_name}/schema` | GET  | 获取动作参数 Schema    |
| `/api/v1/actions`                                          | GET  | 获取所有设备的可用动作 |

### 任务相关

| 端点                          | 方法 | 说明               |
| ----------------------------- | ---- | ------------------ |
| `/api/v1/job/add`             | POST | 提交新任务         |
| `/api/v1/job/{job_id}/status` | GET  | 查询任务状态和结果 |

### 资源相关

| 端点                | 方法 | 说明         |
| ------------------- | ---- | ------------ |
| `/api/v1/resources` | GET  | 获取资源列表 |

### 库存与试剂相关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/reagent-infos` | 试剂身份目录列表 |
| POST | `/api/v1/reagent-infos` | 新增一条试剂身份 |
| POST | `/api/v1/reagent-infos/batch` | JSON 批量新增试剂身份，默认原子提交 |
| POST | `/api/v1/reagent-infos/import` | 从 JSON/CSV/TSV/XLSX 文件批量导入试剂身份 |
| GET | `/api/v1/reagents` | 试剂库存列表，含 `active_workflow_reserved_quantity`（未结束任务的活动预留量） |
| GET | `/api/v1/reagents/{reagent_uuid}` | 单条试剂记录 |
| POST | `/api/v1/reagents` | 把目录项登记到具体容器 |
| POST | `/api/v1/reagents/batch` | JSON 批量新增容器级试剂，默认原子提交 |
| POST | `/api/v1/reagents/import` | 从 JSON/CSV/TSV/XLSX 文件批量导入容器级试剂 |
| PUT | `/api/v1/reagents/{reagent_uuid}` | 修正数量、浓度、说明；`meta_data` 整体覆盖，需原样带回 |
| DELETE | `/api/v1/reagents/{reagent_uuid}` | 软删试剂记录，容器变回空容器 |
| GET | `/api/v1/materials/{material_uuid}/reagent-history` | 容器上的试剂台账 |
| POST | `/api/v1/inventory/commands` | 库存命令入口，按 `command_id` 幂等；命令类型见 `type` 字段，其中 `reagent.dispense` 为试剂分装 |

批量接口的请求体格式为 `{"items": [...], "atomic": true}`。每次最多导入 500 行，文件大小不超过 5 MiB。`atomic` 默认为 `true`，校验或写入失败时整批回滚；设为 `false` 时会写入有效行，并在返回的 `errors` 中列出失败行及字段原因。CSV/TSV/XLSX 的 `aliases` 和 `meta_data` 列使用 JSON 单元格，例如 `"[\"酒精\"]"` 和 `"{\"storage\":\"阴凉通风\"}"`。XLSX 导入读取第一个工作表，第一行必须是字段名。

```bash
# 直接提交 JSON 批量登记试剂身份
curl -X POST "http://localhost:8002/api/v1/reagent-infos/batch" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"cas":"64-17-5","name":"乙醇","physical_state":"liquid"}]}'

# 导入试剂身份（CSV）
curl -X POST "http://localhost:8002/api/v1/reagent-infos/import" \
  -F "file=@reagent-infos.csv;type=text/csv"

# 导入容器级试剂（JSON），允许跳过失败行
curl -X POST "http://localhost:8002/api/v1/reagents/import?atomic=false" \
  -F "file=@reagents.json;type=application/json"
```

## 常见动作示例

### test_latency - 延迟测试

测试系统延迟，无需参数。

```bash
curl -X POST "http://localhost:8002/api/v1/job/add" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"host_node","action":"test_latency","action_args":{}}'
```

### create_resource - 创建资源

在设备上创建新资源。

```bash
curl -X POST "http://localhost:8002/api/v1/job/add" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "host_node",
    "action": "create_resource",
    "action_args": {
        "res_id": "my_plate",
        "device_id": "host_node",
        "class_name": "Plate",
        "parent": "deck",
        "bind_locations": {"x": 0, "y": 0, "z": 0}
    }
}'
```

### reagent.dispense - 试剂分装

把一瓶试剂分到若干空容器：同一事务内源瓶扣量、每个目标建一条同身份试剂记录、写成组台账；任一目标不合格整体拒绝；相同 `command_id` 重放返回首次结果并带 `replayed: true`。

```bash
curl -X POST http://localhost:8300/api/v1/inventory/commands \
  -H "Content-Type: application/json" \
  -d '{
    "command_id": "e1d2c3b4-0000-4000-8000-000000000001",
    "type": "reagent.dispense",
    "actor": "operator-1",
    "payload": {
      "source_reagent_uuid": "源瓶试剂记录 UUID",
      "expected_revision": 3,
      "quantity_unit": "mL",
      "targets": [
        {"material_uuid": "空容器 A 的物料 UUID", "quantity": 100},
        {"material_uuid": "空容器 B 的物料 UUID", "quantity": 100}
      ],
      "reason": "分装"
    }
  }'
```

拒绝时返回 `{"status": "rejected", "error": "...", "error_code": "4002"}`；常见原因：目标容器非空或不是容器、单位与源瓶不一致、总量超过"数量减活动预留"、`expected_revision` 过期。

## 错误处理

### 设备繁忙

当设备正在执行其他任务时，提交新任务会返回 `status: 6`（ABORTED）：

```json
{
  "code": 0,
  "data": {
    "jobId": "xxx",
    "status": 6,
    "result": {}
  },
  "message": "success"
}
```

此时应等待当前任务完成后重试，或使用 `/devices/{device_id}/actions` 检查动作的 `is_busy` 状态。

### 参数错误

```json
{
    "code": 2002,
    "data": { ... },
    "message": "device_id is required"
}
```

## 轮询策略

推荐的任务状态轮询策略：

```python
import requests
import time

def wait_for_job(job_id, timeout=60, interval=0.5):
    """等待任务完成并返回结果"""
    start_time = time.time()

    while time.time() - start_time < timeout:
        response = requests.get(f"http://localhost:8002/api/v1/job/{job_id}/status")
        data = response.json()["data"]

        status = data["status"]
        if status in (4, 5, 6):  # SUCCEEDED, CANCELED, ABORTED
            return data

        time.sleep(interval)

    raise TimeoutError(f"Job {job_id} did not complete within {timeout} seconds")

# 使用示例
response = requests.post(
    "http://localhost:8002/api/v1/job/add",
    json={"device_id": "host_node", "action": "test_latency", "action_args": {}}
)
job_id = response.json()["data"]["jobId"]
result = wait_for_job(job_id)
print(result)
```

## 相关文档

- [设备注册指南](external_device_packages.md)
- [动作定义指南](add_action.md)
- [网络架构概述](networking_overview.md)
