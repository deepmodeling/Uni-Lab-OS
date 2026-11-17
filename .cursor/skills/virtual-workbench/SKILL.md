---
name: virtual-workbench
description: Operate the Virtual Workbench through the local Uni-Lab-OS management API — prepare materials, move them to heating stations, heat, move to output, call another device, transfer materials, build and run workflows. Use when the user mentions virtual workbench, virtual_workbench, 虚拟工作台, heating stations, material processing, or workbench operations.
---

# Virtual Workbench API Skill

## 设备信息

- **注册表名（模板）**: `virtual_workbench`
- **device_id**: 设备图里该节点的 `id`（示例图 `unilabos/test/experiments/virtual_bench.json` 里是 `workbench_1`）
- **Python 源码**: `unilabos/devices/virtual/workbench.py`，设备类 `VirtualWorkbench`
- **backend**: `hostlink`（默认）与 `ros2` 都支持；默认 HostLink 下动作真实执行，不需要 `--test_mode`
- **动作**: 8 个，见 [action-index.md](action-index.md)
- **设备描述**: 1 个机械臂（独占锁）+ 3 个加热台（可并行）。耗时取自设备图节点的
  `config`，示例图写成 `{"config": {"arm_operation_time": 3.0, "heating_time": 10.0, "num_heating_stations": 3}}`
  （未配置时默认机械臂 2 s、加热 60 s）

### 典型流程

1. `auto-prepare_materials` — 生成物料编号（输出 `material_1` … `material_5`）
2. `auto-move_to_heating_station` — 物料竞争机械臂，放到空闲加热台（输出 `station_id`、`material_number`）
3. `auto-start_heating` — 启动加热（3 个加热台可并行）
4. `auto-move_to_output` — 加热完成后移到输出位 Cn

失败（加热台编号无效、加热台上没有物料、尚未加热完成等）会抛异常：作业失败并挂起错误决策，
不会再以 `success: false` 的「成功」结果返回。

## 启动

```bash
unilab -g unilabos/test/experiments/virtual_bench.json   # 默认 HostLink；示例图另带一台 stirrer_1 供跨设备调用
```

## 前置条件

- **BASE**: `unilab` 的管理地址，`http://127.0.0.1:<--port>`，默认 `http://127.0.0.1:8002`。不确定时问用户启动参数。
- **不需要任何凭据**：本机管理 API 不鉴权，旧的 ak/sk、`Authorization: Lab <token>`、`X-UniLab-Token`、
  `lab_uuid` 都已取消，不要再向用户索要。
- 先确认后端在线：`curl -s "$BASE/api/v1/health"`（`execution` 为 `ready`）。

```bash
BASE="http://127.0.0.1:8002"
```

所有响应是统一信封 `{"code": 0, "data": ...}`；`code != 0` 时看 `error.msg`。POST/PUT 加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名）。

## Session State

- `device_id` — 工作台的设备 id（如 `workbench_1`），从实时拓扑读
- `workbench_uuid` — 工作台设备物料的 uuid（工作流节点的 `material_uuid`）
- 需要物料入参（`transfer`、`manual_confirm`、`test`）时，相关物料的 uuid

---

## API

### 1. 实时拓扑（设备 id、物料 uuid）

```bash
curl -s "$BASE/api/v1/graphs/live/payload"
```

`data.nodes[]` 是设备与物料：`id` 是带父级前缀的路径（如 `workbench_1`、`Station/flask`，设备 id 取最后一段），
`uuid` 是物料身份，`display_name` 是展示名，`template_name` 是注册表名。物料的 `name` 恒等于 uuid，不是标签。

### 2. 运行设备单动作

```bash
curl -s -X POST "$BASE/api/v1/job/add" -H "Content-Type: application/json" -d '{
  "device_id": "workbench_1",
  "action": "auto-move_to_heating_station",
  "action_type": "UniLabJsonCommand",
  "action_args": {"material_number": 1}
}'
# 返回 data.jobId
curl -s "$BASE/api/v1/job/<jobId>/status"
```

`status`: 1 已受理、2 执行中、4 成功（`result.return_value` 是动作返回值）、5 取消、6 失败。
`action` 按注册表名原样写（带 `auto-` 前缀的照写）；`action_type` 从 [action-index.md](action-index.md) 取，必须填对。
`action_args` 直接放参数，不要再包一层 `{"goal": ...}`。

### 3. 失败与错误决策

动作失败后作业挂起错误决策，等人选择：

```bash
curl -s "$BASE/api/v1/error-decisions"                       # 看 error_message / traceback
curl -s -X POST "$BASE/api/v1/error-decisions/<decision_id>" -H "Content-Type: application/json" \
  -d '{"action": "abort"}'                                  # 或 "retry"
```

### 4. 物料入参

动作的物料入参（`resource`、`mount_resource`）执行期间一律加锁，必须传带 uuid 的引用：
`[{"uuid": "<物料 uuid>"}]`。`transfer` 的 `mount_resource` 是目标设备上的承载物料（台面、载架、容器），
不能是设备本身；`target_device` 是目标设备 id。

### 5. 人工确认（manual_confirm）

`manual_confirm` 由调度器挂起等人确认，不会调用驱动；批准后原样输出参数给下游：

```bash
curl -s -X POST "$BASE/api/v1/workflow-node-jobs/<job_uuid>/manual-confirmation" \
  -H "Content-Type: application/json" -d '{"action": "approve"}'   # 或 "reject"
```

`assignee_user_ids` 必须显式给出（`[]` 表示任何人可确认）。

### 6. 跨设备调用（call_peer）

```bash
curl -s -X POST "$BASE/api/v1/job/add" -H "Content-Type: application/json" -d '{
  "device_id": "workbench_1", "action": "call_peer", "action_type": "UniLabJsonCommand",
  "action_args": {"target_device": "stirrer_1", "function_name": "stir",
                  "function_args": "{\"stir_time\": 5, \"stir_speed\": 300, \"settling_time\": 0}"}
}'
```

`function_name` 是目标设备的动作名（注册表名带 `auto-` 的照写），`function_args` 是 JSON 字符串。
不能调用工作台自己（会报「跨设备动作不能回调当前设备自身」）。

---

## 工作流

### 7. 查节点模板与 Handle

```bash
curl -s "$BASE/api/v1/workflow-node-templates?keyword=move_to_heating_station"
# 取 resource_template.name == "virtual_workbench" 的那一项的 uuid
curl -s "$BASE/api/v1/workflow-node-templates/<template_uuid>"
# data.template（schema、goal_default）与 data.handles（handle_key、io_type、uuid）
```

Handle 的 `handle_key` 就是数据键：输出（`io_type: source`）是返回值字段，输入（`io_type: target`）是参数名；
每个模板另有一对 `ready` Handle 只表达先后顺序。

### 8. 建工作流并写入整图

```bash
curl -s -X POST "$BASE/api/v1/workflows" -H "Content-Type: application/json" -d '{"name": "加热流程"}'
# data.uuid / data.revision
curl -s -X PUT "$BASE/api/v1/workflows/<workflow_uuid>/graph" -H "Content-Type: application/json" -d '{
  "revision": <当前 revision>,
  "nodes": [
    {"uuid": "<新 uuid>", "workflow_node_template_uuid": "<prepare 模板 uuid>", "material_uuid": "<workbench_uuid>",
     "name": "准备物料", "type": "ILab", "action_name": "auto-prepare_materials", "action_type": "UniLabJsonCommand",
     "meta_data": {"target_device_id": "workbench_1"}, "param": {"count": 2}},
    {"uuid": "<新 uuid>", "workflow_node_template_uuid": "<move 模板 uuid>", "material_uuid": "<workbench_uuid>",
     "name": "移到加热台", "type": "ILab", "action_name": "auto-move_to_heating_station",
     "action_type": "UniLabJsonCommand", "meta_data": {"target_device_id": "workbench_1"}, "param": {}}
  ],
  "edges": [
    {"uuid": "<新 uuid>", "source_node_uuid": "<准备物料节点>", "target_node_uuid": "<移到加热台节点>",
     "source_handle_uuid": "<prepare 的 material_1(source)>", "target_handle_uuid": "<move 的 material_number(target)>"}
  ]
}'
```

- PUT graph 是带 revision 的**全图替换**，不是增量追加；每次先读当前 revision。
- 设备节点用 `meta_data.target_device_id` 指定执行设备（工作台的 device_id）。
- 由连线提供的参数不要再在 `param` 里写静态值，否则报「输入 … 存在多个 Provider」。

### 9. 运行与观测

```bash
curl -s -X POST "$BASE/api/v1/workflow-tasks" -H "Content-Type: application/json" -d '{"workflow_uuid": "<workflow_uuid>"}'
curl -s "$BASE/api/v1/workflow-tasks/<task_uuid>"          # status: pending / running / succeeded / failed / canceled
curl -s "$BASE/api/v1/workflow-tasks/<task_uuid>/jobs"     # 每次设备尝试的 param 与 return_info
```

### 连线表（典型 5 物料并发加热）

| source 动作 | source handle | target 动作 | target handle |
| --- | --- | --- | --- |
| `auto-prepare_materials` | `material_N` | `auto-move_to_heating_station` | `material_number` |
| `auto-move_to_heating_station` | `station_id` | `auto-start_heating` | `station_id` |
| `auto-move_to_heating_station` | `material_number` | `auto-start_heating` | `material_number` |
| `auto-start_heating` | `station_id` | `auto-move_to_output` | `station_id` |
| `auto-start_heating` | `material_number` | `auto-move_to_output` | `material_number` |

`start_heating` 之后要接 `move_to_output`，否则加热完成的物料一直占着加热台。

## 渐进加载

1. **SKILL.md**（本文件）— 接口、状态、流程
2. **[action-index.md](action-index.md)** — 8 个动作的说明与核心参数
3. **[actions/\<name\>.json](actions/)** — 构造请求时再读对应动作的参数 schema（从当前注册表导出）；
   以运行中后端为准时读 `GET /api/v1/workflow-node-templates/<uuid>` 的 `data.template`

## Checklist

```
- [ ] GET /api/v1/health 确认后端在线
- [ ] GET /api/v1/graphs/live/payload 记下 device_id、workbench_uuid（和需要的物料 uuid）
- [ ] 读 action-index.md 选动作
- [ ] 单动作：POST /api/v1/job/add → GET /api/v1/job/<jobId>/status
- [ ] 工作流：查模板与 Handle → POST /workflows → PUT /workflows/<uuid>/graph → POST /workflow-tasks → 轮询
- [ ] 失败：GET /api/v1/error-decisions，按用户意见 retry / abort
```
