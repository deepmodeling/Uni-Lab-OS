# 实验室协议 0.1

状态：审计草案（2026-08-20）
适用实现：UniLabOS `fix/dual-ros-registry-check` / `c458e7b8` HEAD 及审计时未提交
工作区、OpenLab Protocol `1.12.0`（Edge UI v8 兼容谱系）

本文基于当前代码描述实验室前端、Backend 调度权威、UniLabOS 微后端、设备
Adapter、HostLink Slave 和物料权威之间的通信边界。它既是 0.1 接入协议，也是一次
实现审计：文中“已实现”不自动表示“UniLabOS 默认主进程已暴露”。

代码依据以以下目录为准：

- 四库协议与 HTTP：`unilabos/server/protocol/`、`unilabos/server/api/`；
- 默认装配：`unilabos/app/main.py`、`unilabos/server/api/app.py`、
  `unilabos/server/startup.py`、`unilabos/server/scheduler/integration.py`；
- Workflow Authority：`unilabos/server/workflow/`；
- Scheduler/Inventory/Resource Provider：`unilabos/server/scheduler/`；
- control.v1：`unilabos/server/backend/`；
- HostLink：`unilabos/hostlink/`、`unilabos/hostlink/local_runtime.py`；
- OpenLab 消费契约：相邻仓库 `OpenLab-site-source/packages/protocol/src/`，其中
  `catalog.ts` 是操作目录，`client.ts` 是浏览器门面。

## 1. 目标、非目标与术语

### 1.1 目标

协议 0.1 统一说明：

1. Workflow 定义、Task、Node Job 如何进入唯一调度权威并下发设备动作；
2. Resource、Material、Site、设备能力、状态当前值和历史如何标识与传输；
3. HTTP、SSE、control.v1 WebSocket 和 HostLink 各自承载什么；
4. UUID、时间、版本、幂等、ACK、错误和断线恢复的最低约束；
5. OpenLab Edge UI v8 契约的来源、消费方及其与 UniLabOS 当前实现的差异。

### 1.2 非目标

- 不把浏览器变成 Backend↔Edge 的受信控制客户端；
- 不把 WebSocket/SSE 通知正文当作数据库真相；
- 不为不存在的 `lab/tenant/site` 自定义订阅范围宣称兼容；
- 不把旧 InventoryStore、Backend Resource Provider 和 `materials.v1` 说成同一个物料
  writer；
- 不恢复默认主进程已经关闭的本地 DAG Scheduler 权威；
- 不规定设备驱动内部算法、化学步骤语义或 UI 组件样式。

### 1.3 规范词

- **必须**：0.1 互操作所需约束；
- **应当**：推荐约束，偏离时需记录原因；
- **当前实现**：代码已经存在；
- **默认暴露**：正常 Host 启动后由 `:8002` FastAPI 挂载；
- **独立挂载**：有 Router/App 工厂，但默认 Host 未安装；
- **缺口**：catalog 或设计存在，当前代码不能完成相应端到端行为。

## 2. 参与方与唯一权威

```text
OpenLab Browser
  ├─ HTTP/SSE ──> Workflow Authority / Operator API
  └─ HTTP GET ──> UniLabOS :8002 四库读投影

Backend Scheduler Authority
  ├─ control.v1 WS 短通知 ──> UniLabOS Microbackend
  └─ HTTP 命令正文 <─────────┘

UniLabOS Microbackend
  ├─ runtime.db / telemetry.db / history.db
  ├─ embedded materials.db，或一个 external materials authority
  └─ JobExecutionBackend ──> HostLink/ROS2 Adapter ──> Device
                                      └─ HostLink TCP ──> Slave Device
```

| 事实 | 唯一 writer | 其他参与方 |
| --- | --- | --- |
| Workflow 定义、Graph、Task、Scheduler revision | Backend Workflow/Scheduler Authority | 浏览器读写业务 API；Edge 只执行已路由 Job |
| Edge 控制会话、命令收件箱、执行 Job、durable outbox | `runtime.db` / `RuntimeService` | Backend 用 control.v1+HTTP 协调；浏览器只读诊断 |
| Material/Resource/Site 当前事实 | Host 选择的一个 materials authority | HostLink Slave 只能经 Host 代理；浏览器默认只读 |
| 设备最新状态与遥测事件 | `telemetry.db` / `TelemetryService` | Adapter 上报；浏览器 GET |
| 大结果与统一执行审计 | `history.db` / `HistoryService` | Edge 追加；浏览器 GET |
| 设备动作实际副作用 | 被选中的 HostLink 或 ROS2 Adapter | Backend/Edge 只持有 Job 生命周期 |

依据：`unilabos/server/composition.py`、`unilabos/server/startup.py`、
`unilabos/server/scheduler/coordinator.py`、
`unilabos/server/scheduler/integration.py`。

## 3. 实现存在与默认暴露矩阵

这是 0.1 的关键边界。路由文件存在不等于 `unilab` 默认启动后可访问。

| 能力面 | 实现位置 | 默认 Host `:8002` | 当前结论 |
| --- | --- | --- | --- |
| runtime/materials/telemetry/history 四库 | `server/api/*.py` | 是；外部物料权威时不挂本地 materials writer | 当前默认数据面 |
| Scheduler 观测 Router | `server/scheduler/api.py` | 是，但 `get_edge_scheduler()` 固定为 `None` | health/HostLink/status/error/monitor-event 可用；依赖本地 Scheduler 的路由返回 503 |
| 执行形旧 `/workflows` | `server/scheduler/api.py` | **否**，安装时 `include_execution_shaped_workflow_routes=False` | 不得用来提交默认 Host 工作流 |
| Workflow 定义/Graph/Task/SSE | `server/workflow/api.py` | **否** | 有独立 App/Router；需由 Backend Authority 或显式 composition 挂载 |
| 本地 WorkflowTaskExecutor | `server/scheduler/workflow_execution.py` | **否**；integration 明确拒绝 `bind_workflow_executor` | 仅独立 `local_scheduler` 组合可使用，不是默认产品路径 |
| Legacy Inventory `/inventory` | `server/scheduler/inventory/api.py` | **否** | 可独立挂载；不等于 `materials.v1` |
| Backend Resource Provider | `server/scheduler/inventory/backend_api.py` | **否** | 可独立挂载；响应为 Backend `{code,data,error}` |
| Lab layout/warehouse | `server/scheduler/inventory/layout.py` | **否** | 仅 Router 工厂；新四库没有空间 Authority |
| 设备目录 `/devices*` | OpenLab catalog 有定义；UniLabOS 当前未提供对应 Router | **否** | UI 应从 runtime endpoint capability/HostLink peer 读取 |
| 兼容 `/device-state*` | `server/scheduler/api.py` | 是 | 当前由 `TelemetryDeviceStateProjection` 提供 current/history/stats；底层真相仍是 telemetry.v1 |
| 虚拟物料环境 reset | `server/api/materials.py` | 是 | catalog 可读；reset 仅 `--test_mode` 且需 request UUID 与显式确认 |
| 三工位虚拟加热设备 | `devices/virtual/heating_platform.py` | 仅设备图引用后 | Driver 已存在；不是一启动 Host 就自动出现的全局设备 |
| Agent `/agent*` | OpenLab catalog | **否** | 当前 UniLabOS 树无对应 Router |
| 浏览器 Workflow SSE `/events` | `server/workflow/api.py` | 随 Workflow Authority，默认 Host 否 | durable cursor；不是 control.v1 WS |
| Monitor SSE `/monitor/events` | `server/scheduler/api.py` | 是 | 进程内有限 replay；不支持 Last-Event-ID 精确恢复 |

默认装配依据：`unilabos/server/api/app.py::setup_server()` 和
`unilabos/server/scheduler/integration.py::get_edge_scheduler()`。

### 3.1 两套 Workflow API 不得混用

| 路径 | 对象语义 | 默认状态 |
| --- | --- | --- |
| `POST /api/v1/workflows`（Workflow Authority） | 创建 Workflow **定义** | 独立 Authority 实现 |
| `PUT /api/v1/workflows/{uuid}/graph` | 保存定义的 canonical Graph | 独立 Authority 实现 |
| `POST /api/v1/workflow-tasks` | 为既有定义创建一次运行 | OpenLab 默认业务路径，但要求 Authority 已挂载 |
| `POST /api/v1/scheduler/workflows` | OpenLab 为旧执行形接口保留的无冲突名称 | 前端 catalog 有；UniLabOS 当前 Router 实际仍声明旧 `/workflows`，且默认关闭 |
| `POST /api/v1/workflows`（旧 EdgeScheduler） | 直接提交整图执行 | 只有显式启用 execution-shaped Router 才存在，不能与定义 Router 同挂同路径 |

因此 Demo 有两种合法部署：

1. **推荐：Backend controlled**。OpenLab 调 Workflow Authority 的
   `/workflow-tasks`；Backend Scheduler 生成 `execute_job`，经 control.v1 通知当前
   UniLabOS。默认 `unilab` Host 正是这种执行端配置。
2. **仅独立开发：local scheduler**。显式创建 `WorkflowService`（authority profile
   为 `local_scheduler`）、`WorkflowTaskExecutor` 和其唯一执行 backend，安装
   `install_workflow_api()`；不能调用当前 integration 的 `bind_workflow_executor()`，
   因为它会明确报错“workflow execution is owned by the backend scheduler”。

OpenLab 当前只配置一个 `baseUrl`，所以推荐部署需由同源 API Gateway 将
`/api/v1/workflows*`、`/api/v1/workflow-tasks*`、`/api/v1/workflow-node-jobs*` 和
`/api/v1/events` 转发到 Backend Authority，其余 Edge 路由转发到 UniLabOS。本地单进程
调试可把 `install_workflow_api()` 安装到同一 FastAPI App，但必须继续保持
`include_execution_shaped_workflow_routes=False`；当前没有开箱即用的 CLI 开关完成这个
composition。若只开启旧执行形 `/workflows`，当前加热页面的
`workflowBackend.createWorkflow/saveGraph/createTask` 调用链不能工作；不能用同路径
语义偷换的方式“兼容”。

## 4. 共同对象规则

### 4.1 JSON、Envelope 与未知字段

- 四库 Pydantic 对象继承 `ServerObject`，`extra="forbid"`、禁止 NaN/Infinity；依据
  `unilabos/server/models/base.py`。
- 哈希和物料幂等正文使用排序键、无空白、UTF-8 的 canonical JSON；依据
  `unilabos/server/protocol/common.py`。
- **当前没有一个覆盖全部 HTTP 家族的统一响应 envelope**：
  - runtime/materials/telemetry/history 和 scheduler API 直接返回 DTO；
  - Workflow/Backend Resource Provider 返回 `{code: 0, data}` 或
    `{code: <业务码>, error: {msg}}`，业务错误仍可能 HTTP 200；
  - Inventory 返回直接 DTO，领域冲突使用 HTTP 409；
  - FastAPI 默认校验错误为 `{"detail": [...]}`。
- OpenLab `createHttpTransport()` 会把非 2xx 归一为 `ApiError`，但 Backend HTTP 200
  业务错误必须由对应 domain client 检查 `code`，不能仅依赖 HTTP status。

### 4.2 身份

- 对象主键统一叫 `*_uuid` 时应当作为不透明字符串传输，不从名称、URL 或数组位置
  推导；四库多数模型当前只强制非空，Workflow Authority 另行强制 UUID 格式。
- control.v1 使用 `edge_uuid`、`session_uuid`、`authority_epoch`、
  `connection_epoch`；旧 authority/connection 的通知必须拒绝。
- Job 重试必须创建新的 `job_uuid`；同一尝试族使用 `attempt_group_uuid`，并通过
  `retry_of_job_uuid` 和 `attempt_no` 串联。
- HostLink `id` 是单次 req/resp 关联 ID（当前为 UUID hex），不是业务 Job ID；设备
  动作另带 `action_id`，正常由 `job_uuid` 传入。
- trace/span ID 只做观测关联，不能代替 command/event/aggregate UUID。

### 4.3 时间和顺序

- 名称以 `_at_ms`、`_from_ms`、`_to_ms` 结尾的字段是 Unix 毫秒整数；
- HostLink peer 的 `connected_at/last_seen`、旧 scheduler `now` 和旧 WS `timestamp`
  目前使用 Unix 秒浮点数；这是兼容字段，不能与 `_ms` 混算；
- Workflow Backend 的 `create_time/update_time/observed_at` 使用其 Backend DTO 约定，
  不能仅因字段名相似假设为毫秒；
- durable 流必须按分配方序列恢复：runtime `backend_sequence/event_sequence`、materials
  ledger `sequence`、telemetry/history `sequence`、Workflow SSE `id`；不同流的序列
  不可比较。

## 5. Resource、Material 与 Site

### 5.1 Resource Template

`materials.v1` 的 Resource Template 包含：稳定 `template_uuid`、name/display_name、
resource/class/module、template_version、category、available_sites、handles、definition、
definition_hash、status 和 version。Handle 是模板内嵌值，不是独立四库记录。

设备动作模板来源是 Registry 的 `class.action_value_mappings`；同步适配会提取
goal/default/feedback/result/schema/handles，并移除旧式设备选择参数。依据：
`unilabos/server/backend/sync/templates.py`、`unilabos/registry/registry.py`。

### 5.2 Material 聚合

`MaterialAggregateRead` 是当前值：

```json
{
  "material": {"material_uuid": "...", "resource_id": "...", "template_uuid": "..."},
  "position": {"layout": "x-y", "position3d_x": 0},
  "position_version": 1,
  "data": {
    "data": {"temperature_c": 42.5},
    "substances": [],
    "state_status": "heating",
    "content_version": 3,
    "state_hash": "...",
    "observed_at_ms": 1787184000000
  },
  "sites": [],
  "state_hash": "..."
}
```

- `material` 保存身份和低频字段；`position`、`data` 是独立版本 section；
- `data.data` 是设备/领域自定义 JSON 的规范落点，例如温度、压力或可视状态；
- `state_status` 是简短生命周期/观测标签，不替代结构化 `data`；
- substances 使用具名对象，不在线协议传 PLR 三元组；
- 更新必须整体提交 `MaterialDataWrite`，不能把任意数据库列 patch 当协议。

依据：`unilabos/server/protocol/materials.py`、
`unilabos/server/models/materials.py`、`unilabos/server/services/materials.py`。

### 5.3 Site 与树

- Site 由 `site_uuid` 标识，归属 `owner_material_uuid`；
- `occupied_material_uuid` 表示具名位当前占用，Material 的
  `parent_material_uuid` 表示组件树父子关系；移动操作必须原子维护二者；
- Site 带 template_name/index/label/pose/allowed categories/visible/version；
- 创建树使用临时 `client_ref` 解析父子和占用关系，Authority 返回 UUID map；客户端
  不能预造 Site UUID；
- `GET .../tree` 返回同一 `snapshot_sequence` 下的聚合树和 state hash。

### 5.4 运行时序列化边界

当前 PLR→UniLabOS 边界调用 `resource.serialize()` 和
`resource.serialize_all_state()`，后者进入 `ResourceDict.data`；随后
`resource_tree_to_create()` / `resource_tree_to_snapshot()` 将其映射为
`MaterialDataWrite.data`。依据：`unilabos/resources/resource_tracker.py` 和
`unilabos/server/adapters/plr_materials.py`。

协议 0.1 要求自定义资源的可展示状态必须是 JSON-safe，并最终落入
`MaterialDataWrite.data`。当前三工位加热 Demo 已实现 `VirtualHeatingPlatform.serialize()`，
设备快照返回三个 Site；动作过程则把每个物料各自的可展示快照写到
`data.serialized_state`：

```json
{
  "serialized_state": {
    "site_id": 2,
    "temperature_c": 42.5,
    "target_temperature_c": 60.0,
    "progress": 48.6,
    "state": "heating",
    "observed_at_ms": 1787184000000
  }
}
```

`serialized_state` 目前仍是 `demo.heating.v1` 的自定义 data key，不是所有
Material 必有的公共字段；前端必须容忍它缺失。代码中没有
`civilized_state`；若该词指的就是“序列化状态”，0.1 的实际名称是
`serialized_state`，两者不应同时作为别名写入。依据：
`unilabos/devices/virtual/heating_platform.py`。

### 5.5 当前值与历史

- 当前物料值：`GET /api/v1/materials/instances/{uuid}` 或 `/tree`；
- 变更游标：`GET /api/v1/materials/changes?after_sequence=`；
- `update_data` ledger delta 当前只含 `content_version` 与 `data_state_hash`，**不含完整
  温度值**，所以它能做失效通知和审计索引，不能重建温度曲线；
- 加热 Demo 的 Material 只被动保存最新 `data.temperature_c`、观测时间和
  `temperature_source`，不保存曲线数组；温度曲线必须来自加热台
  `site_{n}_temperature_c` telemetry event；
- Backend Resource Provider 的 `/materials/{uuid}/states` 有独立 append state 模型，
  但它属于旧 Backend-shaped store，默认未挂载，也不是 `materials.db` 的同一历史；
- 若 Demo 要展示物料温度历史，0.1 后续必须定义 material observation event/payload
  或把完整观测写入 `history.v1`，不能假装现有 ledger 已保存曲线。

## 6. 设备描述、动作与状态

### 6.1 设备描述和 Action Schema

HostLink 描述符由 `HostLinkDeviceNode.describe()` 产生：

```json
{
  "id": "heater-1",
  "registry_name": "virtual_heater",
  "display_name": "虚拟加热台",
  "actions": ["heat"],
  "status_fields": ["temperature"],
  "action_value_mappings": {
    "heat": {"goal": {}, "feedback": {}, "result": {}, "schema": {}}
  },
  "resource_uuid": "..."
}
```

运行微后端会把设备 route 和 `DeviceActionCapability` 保存到 endpoint snapshot：
`device_uuid/action_name/action_type/concurrency_mode/state/availability/active_job_uuid/`
`descriptor/descriptor_hash/observed_at_ms`。浏览器 0.1 应从
`GET /api/v1/runtime/endpoints` 读取动作能力，而不是依赖当前未实现的
`/api/v1/devices/{id}/actions/...` Router。

依据：`unilabos/hostlink/local_runtime.py::HostLinkDeviceNode.describe()`、
`unilabos/server/models/runtime.py::DeviceActionCapability`、
`unilabos/server/scheduler/coordinator.py`。

### 6.2 动作调用

动作调用最小字段为 `device_uuid`、`action_name`、JSON object `action_args` 和稳定
`job_uuid/action_id`。执行 Adapter 必须使用描述符/schema 校验或驱动签名绑定参数；
未知参数、缺少必填参数和非对象 arguments 应失败，不能静默丢弃。

HostLink 设备调用返回 `status: succeeded|cancelled`、result，并在本地调用时附最新
state；反馈通过独立 `action.feedback` 关联 `action_id`。依据：
`unilabos/hostlink/backend.py`。

### 6.3 状态当前值与历史

规范路径是 telemetry.v1：

- 当前：`GET /api/v1/telemetry/states[?endpoint_uuid=]`；
- 单设备：`GET /api/v1/telemetry/states/{endpoint_uuid}/{device_uuid}`；
- 历史：`GET /api/v1/telemetry/events`，按 endpoint/device/event type/source epoch/
  observed time 过滤；
- 恢复位置：`GET /api/v1/telemetry/sources/{endpoint_uuid}/cursor`。

HostLink/ROS2 Adapter 只接受标量属性进入当前 properties 投影；非标量结构应使用
telemetry event payload，而不能塞进旧 `/device-state/report`。状态值变化会写一条
`property_sample` 事件及一份完整 latest snapshot。依据：
`unilabos/server/scheduler/telemetry_state.py`、
`unilabos/server/services/telemetry.py`。

旧 `/device-state*` 路径仍在 Edge UI v8 catalog。当前
`TelemetryDeviceStateProjection` 已实现 `set/latest_all/latest_for/history/history_all/stats`，
因此这组兼容读路由默认可用；它们只是 telemetry.v1 的投影，新客户端仍应
把 `/telemetry/states` 和 `/telemetry/events` 视为规范语义。

## 7. Workflow、Task 与 Job

### 7.1 定义与运行

- Workflow：可编辑定义；Graph 保存携带 `revision` 乐观锁；
- WorkflowTask：一次运行，状态
  `pending/running/canceling/succeeded/failed/canceled/timeout`；
- WorkflowNodeJob：Task 内一个节点 attempt，状态包含
  `pending/dispatched/running/intervention_required/cancel_requested/execution_unknown/`
  `succeeded/failed/skipped/canceled/timeout`；
- canonical 成功词是 `succeeded`；设备 Adapter 的旧内部词 `success` 只在 adapter
  边界转换；依据 `unilabos/server/scheduler/models.py` 和
  `unilabos/server/workflow/schema.py`。

Graph 节点参数保存在 node/job `param`；运行时边把它变为 `action_args`。当前
`WorkflowTaskCreateRequest` 只接收 workflow_uuid、run_mode、target_node_uuid、
description、meta_data。`_BackendModel` 忽略未知字段，API 又强制向服务传
`input_value={}`；所以 HTTP 请求里偷带的 `input` 会被静默忽略，不是参数覆盖。
服务层仅在被 Python 直接调用且传入非空 `input_value` 时拒绝。因此带参数 Demo
必须先把参数保存到 Graph 节点 `param`，再创建 Task；“提交 Task 时覆盖任意
action 参数”尚未成为冻结合同。

### 7.2 默认 Backend-controlled 下发链

```text
OpenLab POST /workflow-tasks
  -> Backend Scheduler 创建 Task/Node Job、解析 Graph param
  -> Backend 持久化 execute_job command + payload
  -> WS backend_change（只含 UUID/sequence/hash）
  -> UniLabOS HTTP GET /edge/commands/{command_uuid}
  -> runtime.db command inbox + execution_job + adapter outbox
  -> WorkflowBusinessCoordinator._dispatch(action_args)
  -> JobExecutionBackend.dispatch（锁/状态联锁）
  -> HostLinkExecutionAdapter.send_goal
  -> HostLink device.call {device_id, action, arguments, action_id}
  -> feedback/result -> runtime/history/telemetry -> edge_change
  -> Backend HTTP 拉正文并更新 Task/Job
  -> Browser SSE 失效通知 -> HTTP 重读
```

代码依据：`server/workflow/api.py`、`server/protocol/control.py`、
`server/backend/websocket.py`、`server/scheduler/coordinator.py`、
`server/scheduler/backend.py`、`hostlink/execution_adapter.py`、
`hostlink/server.py`。

### 7.3 错误闸门

设备失败先进入 `terminal_waiting` 并保存 raw failure、error snapshot；Backend 完成
人工询问并推进 scheduler revision 后，只能下发 `release_failed`、`replace_result`
或 cancel。retry 必须创建新 Job attempt，不能原地重跑旧 Job。History replacement
必须保留被替换事件和 actor。依据：`server/scheduler/coordinator.py`、
`server/protocol/history.py`。

## 8. 命令、事件、幂等与恢复

| 流 | 写前持久化 | 去重键/顺序 | 恢复 |
| --- | --- | --- | --- |
| Backend command | Backend + Edge `command_inbox` | command_uuid、session 内 backend_sequence、payload hash | WS 只通知；固定 HTTP 路径重拉正文 |
| Edge backend event | `backend_event_outbox` | event_uuid、session ACK through_sequence | lease 到期重领，重连后按 ACK 重发 |
| Adapter command | `adapter_command_outbox` | adapter_command_uuid、endpoint lease | claim/ACK；未 ACK 可重领 |
| Material mutation | `inventory_command_effect` + ledger | `(command_uuid,effect_key)` + canonical payload fingerprint | 同载荷回放返回 `replayed=true`；异载荷冲突 |
| Telemetry ingest | telemetry event/cursor | event_uuid + endpoint source epoch/generation/sequence/hash | replay 同内容返回 replayed；来源倒退/分叉拒绝 |
| History append | history event | event_uuid、全局 sequence | after_sequence 分页 |
| Workflow browser event | `frontend_event` | SQLite id | `Last-Event-ID` 精确续传，重连后 HTTP 校准 |
| Monitor event | 内存 ring | 进程内 seq | 仅 backlog replay，慢消费者/重启有缺口，需 snapshot 校准 |
| Legacy `job_start` | 内存 cache | `(task_id,job_id)`，24h/1024 条 | 非 durable；进程重启丢失 |

并发写必须带 aggregate expected_version/state_hash 或 Graph revision；409 后必须重新读
Authority，再基于新版本重算，不能盲目重试旧 body。

## 9. HTTP API 清单

### 9.1 默认四库 API（默认挂载）

| 域 | 方法与路径 | 角色 |
| --- | --- | --- |
| runtime | `PUT/GET /api/v1/runtime/sessions[/{uuid}]` | 受信 session upsert；浏览器列表/详情 |
| runtime | `PUT /endpoints/{uuid}/snapshot`; `GET /endpoints[/{uuid}]` | Adapter 完整 capability 快照；浏览器读取 |
| runtime | `POST /commands`; `GET /commands[/{uuid}]` | Backend command ingest；浏览器诊断读取 |
| runtime | `POST /jobs`; `GET /jobs[/{uuid}]` | 执行 Job 创建；浏览器诊断读取 |
| runtime | `POST /jobs/{uuid}/transitions|feedback|cancel|error-gate/open|error-gate/decision` | 受信状态机写入 |
| runtime | `POST/GET /adapter-commands`; `POST .../claim|ack`; `GET .../{uuid}` | Adapter durable outbox |
| runtime | `POST/GET /backend-events`; `POST .../claim|ack`; `GET .../{uuid}` | Backend durable outbox |
| materials | `POST/PUT/GET/DELETE /api/v1/materials/templates[/{uuid}]` | 模板 mutation/read |
| materials | `POST /trees`; `GET /instances`; `GET /instances/by-resource-id/{id}` | 创建树/列表/稳定 resource id 查询 |
| materials | `GET /instances/{uuid}[ /tree]`; `PATCH /instances/{uuid}` | 聚合/树读取、身份 patch |
| materials | `PUT /instances/{uuid}/position|data`; `DELETE /instances/{uuid}` | section 更新、递归软删除 |
| materials | `POST /move`; `POST /snapshots/compare|apply` | 原子占位移动、设备快照同步 |
| materials | `GET /changes`; `POST /changes/ack` | append ledger 与内部 ACK |
| materials | `GET /virtual-environments`; `POST /virtual-environments/{organic|biology|materials}/reset` | catalog；reset 仅 `--test_mode` 且需确认/请求 UUID |
| telemetry | `POST/GET /api/v1/telemetry/events`; `GET /events/{uuid}` | 受信追加、浏览器历史 |
| telemetry | `GET /sources/{endpoint}/cursor`; `GET /states[/{endpoint}/{device}]` | 来源恢复、当前状态 |
| history | `POST /api/v1/history/payloads`; `GET /payloads/{uuid}` | inline Base64 或 external immutable payload |
| history | `POST/GET /events`; `GET /events/{uuid}` | append-only 统一历史 |
| history | `POST /events/{uuid}/replacement`; `GET .../replacement-chain` | 人工替换审计链 |

完整签名依据：`unilabos/server/api/runtime.py`、`materials.py`、`telemetry.py`、
`history.py`。浏览器协议有意只公开其中 GET 投影和 test-mode reset；claim/ACK/ingest
等写入口不是操作员 API。

### 9.2 默认 Scheduler 兼容/观测 Router

| 路径 | 默认状态 |
| --- | --- |
| `GET /api/v1/health`、`GET /hostlink/peers` | 可用 |
| `GET /status-incidents`; `POST /{id}` | backend/status manager 就绪时可用；后者只接受 incident 公布 option |
| `POST /status-incidents/{id}/acknowledge|resolve` | 显式确认/解除状态事件，仍要求 status manager 就绪 |
| `GET /monitor/events` | 可用，五通道 `material/device/action/scheduler/status` |
| `GET /error-decisions`、`POST /error-decisions/{id}` | execution backend 就绪时可用；属于本地兼容决策面 |
| `POST /device-state/report` | 标量调试上报可用 |
| `POST /jobs/{id}/finish`、`POST /reschedule`、`GET /timeline`、`GET /monitor/snapshot` | 依赖已禁用的本地 EdgeScheduler，默认 503 |
| `GET /history/workflows*|jobs` | 未注入旧 history store，默认 503 |
| `GET /device-state*` | 默认可用；是 telemetry.v1 的 current/history/stats 兼容投影 |
| 旧 `POST/GET /workflows*` | 默认根本不注册 |

依据：`unilabos/server/scheduler/api.py`、`unilabos/server/api/app.py`。

### 9.3 Workflow Authority API（实现存在、默认未挂）

| 路径 | 语义 |
| --- | --- |
| `POST/GET /api/v1/workflows`; `GET/PUT/DELETE /workflows/{uuid}` | 定义 CRUD（delete 为软删除） |
| `GET/PUT /workflows/{uuid}/graph` | 整图 hydration、revision 乐观锁保存 |
| `POST/GET /workflow-tasks`; `GET /workflow-tasks/{uuid}` | 创建/分页/读取运行 |
| `GET /workflow-tasks/{uuid}/jobs`; `GET /workflow-node-jobs/{uuid}` | Node Job |
| `GET /workflow-tasks/{uuid}/manual-confirmations|interventions` | 人工确认和干预历史 |
| `GET /workflow-node-jobs/{uuid}/results|feedback-history` | 结果与反馈历史 |
| `GET /workflows/{uuid}/authoring`; `PUT .../draft`; `POST .../apply` | Draft/Candidate authoring 状态机 |
| `GET /api/v1/events` | durable frontend_event SSE，Last-Event-ID |

依据：`unilabos/server/workflow/api.py`。当前没有已冻结的 Task cancel/pause/resume、
intervention decision 或 ad-hoc device action HTTP 端点。

### 9.4 Legacy Inventory API（独立挂载）

前缀 `/api/v1/inventory`：`health`、统一幂等 `POST /commands`，以及
`lots`、`instances`、`reservations`、`templates`、workflow reservations、relations、
contents、snapshot、ledger、outbox backlog/events、processed commands、sync cursors 的
GET。兼容查询另为 `POST /api/v1/edge/material/query`。

依据：`unilabos/server/scheduler/inventory/api.py`。它使用旧 InventoryStore，不能与
默认 materials.db writer 同时宣称物料 Authority。

### 9.5 Backend Resource Provider 与 Lab API（独立挂载）

- Resource Provider：`/api/v1/resource-templates*`、`/api/v1/materials*`、
  `/api/v1/material-states/{uuid}`、`/api/v1/sites/{uuid}`；包含 Material state append/
  list/latest；依据 `server/scheduler/inventory/backend_api.py`。
- Lab：`/api/v1/lab/profile|layout|warehouse|zones|placements|assembly|demo`；依据
  `server/scheduler/inventory/layout.py`。
- 两者默认均未由 `server/api/app.py` 安装；与 materials.v1 同时挂载前必须先决定
  writer 和路径所有权。

## 10. control.v1 WebSocket

连接地址为 `/api/v1/ws/schedule`；Edge 请求头：

- `Authorization: Lab <base64(ak:sk)>`；
- `EdgeSession: <client UUID>`；
- `EdgeProtocol: control.v1`。

外层线格式为 `{"action":"...","data":{...}}`。

| 方向 | action | data |
| --- | --- | --- |
| Backend→Edge | `backend_session` | session/edge/authority epoch/connection epoch/time |
| Backend→Edge | `backend_change` | notice/command UUID、command type、sequence、epochs、content hash；**无业务正文** |
| Backend→Edge | `edge_change_ack` | session + 单调 through_sequence |
| Edge→Backend | `edge_change` | event UUID/sequence/type、aggregate identity/version、可选 job/payload UUID |
| 双向 | `ping`/`pong` | 网络诊断，不承载业务状态 |

Backend command type 当前冻结为 `execute_job/cancel_job/release_failed/replace_result/`
`inventory_apply/reconcile`。Edge 只按 command UUID 从固定
`GET <backend>/edge/commands/{uuid}` 拉 `BackendCommandDocument` 并校验 Pydantic/hash；
WS 不能指定任意正文 URL。依据：`server/protocol/control.py`、
`server/backend/http.py`、`server/backend/websocket.py`。

### 10.1 Scope

已定义 scope：edge、session、authority epoch、connection epoch、单向 sequence、
aggregate type/uuid/version、可选 job/payload UUID。

尚未定义：lab/site/tenant、workflow/task/device/material 多选、通配/排除、浏览器角色、
每 scope 独立 cursor。0.1 不允许把 SQL、URL 或任意用户表达式塞进 WS 代替 scope。

## 11. HostLink v1

HostLink 是 TCP 上的 UTF-8 newline-delimited JSON；单帧最大 8 MiB：

```json
{"v":1,"kind":"req","id":"...","action_type":"device.call","data":{}}
{"v":1,"kind":"resp","id":"...","ok":true,"data":{}}
{"v":1,"kind":"resp","id":"...","ok":false,"error":"...","error_info":{}}
```

`error_info` 可含 exception type/MRO/message/traceback/category/severity。依据：
`unilabos/hostlink/protocol.py`。

| 类别 | action_type |
| --- | --- |
| 会话/发现 | `hello`、`ping`、`ros_info`、`device.state` |
| 设备 | `device.call`、`action.feedback`、`action.cancel`、`service.call` |
| Topic | `topic.publish`、`topic.subscribe`、`topic.unsubscribe`、`topic.deliver` |
| 物料 Host 代理 | `material.template.list`、`material.template.create`、`material.create`、`material.tree.get`、`material.resource-id.get`、`material.data.put`、`material.move`、`material.delete`、`material.snapshot.compare`、`material.snapshot.apply` |

Slave hello 上报 machine/node/role、device descriptors、protocol version 和 capabilities；
Host 根据 `connected && now-last_seen < heartbeat_timeout` 计算 `online`。peer 行断线后仍
保留，所以 UI 必须使用 `online`，不能用“列表中存在”判活。

HostLink 设备状态是当前快照/变化通知；它最终由 JobExecutionBackend 写 telemetry.v1。
Material mutation 由 Host 代理到启动时选择的 embedded 或 external authority，Slave
不能持有第二个 materials writer。依据：`hostlink/server.py`、`hostlink/backend.py`、
`client/materials.py`。

## 12. SSE 与事件恢复

| 端点 | Authority | 恢复语义 |
| --- | --- | --- |
| `/api/v1/events` | Workflow `frontend_event` SQLite | `Last-Event-ID` 非负 int64；按 id>cursor 每页 100；重连后必须 HTTP 校准 |
| `/api/v1/monitor/events` | 进程内 `monitor_bus` | channels 逗号过滤，backlog 0..200，15s keepalive；忽略 Last-Event-ID，无跨重启保证 |

Monitor 五通道为 material/device/action/scheduler/status。慢消费者会丢事件，设计要求
发现 seq 空洞后读 snapshot；但当前默认 `/monitor/snapshot` 依赖已禁用 Scheduler 而
返回 503，这是一个明确缺口。依据：`server/scheduler/monitor.py`、
`server/scheduler/api.py`、`server/workflow/api.py`。

## 13. 安全与权限

### 13.1 已实现

- control.v1 WebSocket 和 Backend HTTP 使用 `Authorization: Lab ...`；
- Backend template/instance 同步客户端分别支持 developer/operator bearer token；
- virtual environment reset 强制 `--test_mode`，生产返回 403；
- Workflow/Material mutation 使用 schema、版本和业务状态机限制写集合；
- HostLink 对 Slave 发起的跨设备/service/cancel 调用校验 caller device 是否属于该 peer。

### 13.2 当前缺口

- `:8002` FastAPI 没有统一认证/授权 middleware，并允许 `*` CORS；内部 runtime
  ingest/claim/ACK、materials mutation、telemetry/history append 与调试 report 都可能
  被网络可达客户端调用；
- CORS `allow_methods` 当前缺少 `PATCH`，因此 GitHub Pages 等跨源前端无法
  直接调用已存在的 `PATCH /api/v1/materials/instances/{uuid}`，除非由同源网关
  代理或修正 CORS 配置；
- HostLink TCP 没有 TLS、共享密钥或节点证书，不能跨不可信网络裸露；
- `HTTPMaterialsClient` 当前不附 Authorization；external materials authority 必须靠
  受控网络，或后续补 service credential；
- 浏览器角色、实验室/租户隔离和逐操作权限未进入 0.1；
- Backend `{code}` 和 FastAPI HTTP status 两套错误面增加了网关误判风险。

生产部署应在反向代理/防火墙限制 `:8002` 和 `:7302`，只向浏览器开放明确 GET 与已
授权 operator command；受信 ingest/claim/ACK 必须与浏览器路由隔离。

## 14. 错误模型

| 家族 | not found | conflict | validation | 其他 |
| --- | --- | --- | --- | --- |
| runtime/materials/telemetry/history | HTTP 404 | HTTP 409 | HTTP 422 | materials rejected mutation 410；virtual reset 403 |
| Scheduler | HTTP 404/409/422 | 依端点 | FastAPI 422 | 依赖未启用为 503 |
| Inventory | HTTP 404 | version conflict 409 | FastAPI 422 | body 可含 `error_code` |
| Workflow/Resource Provider | 通常 HTTP 200 + business code 3002 | code 3003 | code 1000 | 5001 等；`error.msg` |
| control.v1 | 非法/未知 action 被忽略或协调器失败 | command fingerprint/epoch/version 拒绝 | Pydantic 拒绝 | 依赖 durable 状态诊断 |
| HostLink | `ok=false` | `ok=false` | `ok=false` | `error_info` 保留远端异常身份 |

OpenLab 客户端必须同时检查 HTTP status 与对应家族的 business code；错误日志应包含
operation、object UUID、command/event UUID 和可用的 traceparent，不记录 Lab secret。

## 15. 版本与兼容

| 层 | 当前版本 |
| --- | --- |
| 本文 | Laboratory Protocol 0.1 |
| HTTP 路径 | `/api/v1` |
| 四库 | `runtime.v1`、`materials.v1`、`telemetry.v1`、`history.v1` |
| Backend/Edge 控制 | `control.v1` |
| HostLink wire | 数字 `v=1` |
| OpenLab TypeScript package | `OPENLAB_PROTOCOL_VERSION=1.12.0` |
| UI 兼容谱系 | Edge UI v8 catalog/table/entity contract |

兼容规则：

1. `/api/v1` 内新增可选响应字段可以后向兼容；删除/重命名字段、改变状态词、单位、
   幂等键或 Authority 属于破坏变更；
2. 四库 `ServerObject` 写模型禁止未知字段；Workflow Backend 兼容模型则忽略
   未知字段。新增写字段前需同时升级双方，客户端不得以“请求未报错”
   判定字段已生效；
3. control/HostLink 新 action 或 command type 必须扩枚举和版本测试，不能复用旧 action
   传不同正文；
4. `OPENLAB_PROTOCOL_VERSION` 是前端包发布版本，不等同于 wire `control.v1`；
5. Edge UI v8 契约由 OpenLab `packages/protocol/src/catalog.ts`、各 domain client 和
   `scripts/validate-contract.mjs` 共同体现；它不是由某个 UniLabOS 分支自动生成。

## 16. OpenLab Edge UI v8 来源与消费方

| 契约 | OpenLab 来源 | 页面/Store 消费 | UniLabOS 依据/差异 |
| --- | --- | --- | --- |
| 四库 read | `runtime-v1.ts`、`materials-v1.ts`、`telemetry-v1.ts`、`history-v1.ts` | Devices、Inventory、Monitor、Assembly、action-param-form、organic workbench | `server/api/*.py`；当前匹配默认主进程 |
| Workflow 默认业务 | `workflow-backend.ts` | `stores/scheduler.ts`、Workflows、WorkflowDetail/TaskDetail、Editor | `server/workflow/api.py` 实现存在但默认未挂；需 Backend Authority |
| SSE | `workflow-backend.ts::eventsUrl()` | `stores/connection.ts`，仅做失效通知 | Workflow SSE 可恢复；monitor SSE 是另一条非 durable 流 |
| HostLink/health | `common.ts` | Console | scheduler Router 默认可用 |
| Status | `status.ts` | `stores/status.ts` | 默认 backend ready 时可用 |
| Inventory v8 | `inventory.ts` | 旧扁平 API/兼容页面 | legacy Inventory Router 默认未挂；新页面主要已转 materials.v1 |
| Resource Provider v8 | `resource.ts` | entity adapter/旧资源操作 | Provider 实现默认未挂；不能与 materials.v1 混写 |
| Device v8 | `device.ts` | 旧 device API；加热 Demo 使用 state/history 兼容读 | `/devices*` Router 缺失；`/device-state*` 已由 telemetry projection 提供 |
| Lab | `lab.ts` | Console/LabMap | Router 默认未挂；新空间 Authority 未定义 |
| Agent | `agent.ts` | Agent 页面 | 当前 UniLabOS 无 Router |

`packages/protocol/src/client.ts` 将上述 domain 组装成 `createEdgeApi()`；OpenLab 页面
不应自行拼 URL 或根据数据库列猜接口。旧 archive/分支只能作为历史输入，当前合同真相
是版本化 protocol package、contract tests 和 UniLabOS 实际 Router 的交集。

## 17. 最小端到端加热 Demo

### 18.1 前置条件

1. 常规部署的 Backend Workflow Authority 已挂 `/workflows`、`/workflow-tasks`、
   `/events`；`--demo-mode` 是明确例外，会在当前 UniLabOS Host 内挂唯一的本地
   Workflow Authority；
2. UniLabOS Host 用 `hostlink` 启动且 control.v1 已连接 Backend；
3. 设备图显式引用 `virtual_heating_platform`，runtime endpoint snapshot 可见它的
   `heat_site(site_id,target_temperature_c,duration_seconds)` action schema；
4. materials authority 中已有三个实际 Material，并分别占用三个 Site；
5. OpenLab 只从 materials.v1 和 telemetry.v1 读取当前值。

本机三工位演示可使用单一命令启动：

```powershell
unilab --demo-mode
```

该模式选择内置三工位 graph、`hostlink` Host/test mode，并把本进程拥有的 HTTP
微后端监听在 `0.0.0.0:6005`。Demo registry 只扫描 JSON HostLink 所需的
`container.py` 与 `heating_platform.py`，因此该演示路径不要求安装 ROS message Python
包。它同时在四库目录旁创建独立 `workflow.db`，以 `local_scheduler` profile 装配
`WorkflowService → WorkflowTaskExecutor → JobExecutionBackend → HostLink`；该显式 Demo
组合不改变普通 Host 的 Backend-controlled 单权威规则。本机微前端连接
`http://127.0.0.1:6005`；公开演示微前端使用
`https://edge.whalent.com`，由可信 HTTPS 入口反向代理到本机 `6005`。健康检查失败后
每 5 秒继续尝试。这里的 `6005` 是 Edge HTTP 端口，不是 HostLink TCP 端口；HostLink
listener 仍使用独立配置（默认 `7302`）。浏览器不直接访问明文 6005，因此 GitHub
Pages 不会触发 mixed-content 拦截。

### 18.2 创建并放置物料

受信调用方用三个不同 `(command_uuid,effect_key)` 创建 Material tree，并用一次
`move_material` 把每个 Material 放入一个 `destination_site_uuid`。每次响应保存
material/site UUID、aggregate version 和 ledger sequence。浏览器操作员写入口尚未冻结，
Demo 初始化可使用 test-mode setup 或受信后端脚本，不能在页面直开所有 internal writer。

### 18.3 带参数 Task 到 HostLink action

Graph 的一个设备节点保存例如：

```json
{
  "material_uuid": "heater-platform-material-uuid",
  "action_name": "heat_site",
  "param": {
    "site_id": 2,
    "target_temperature_c": 60,
    "duration_seconds": 30
  }
}
```

三个工位需要三个 Node Job（可并行或由 Graph edge 排序），每个 Job 只对一个
`site_id` 执行一次 `heat_site`；当前 action 不接收 `site_targets` 批量数组。

保存 Graph revision 后，OpenLab `POST /workflow-tasks`。Backend 必须用
`material_uuid + action_name` 解析目标 endpoint/device/action，只把 `param` 中的实际动作
参数写入 `ExecuteJobContent.action_args`；UniLabOS 再原样交给 HostLink
`device.call.arguments`。`device_id/action` 这类路由字段不得混入驱动参数。
在 endpoint capability、runtime job 和 HostLink Slave 三处核对同一 device/action，
并以 job UUID 贯穿 action ID。任何一段都不得把参数藏在 WS notice。

### 18.4 状态当前值与历史

加热台设备状态（台面/控制器温度、busy 等）走 telemetry：

1. Adapter `publish_device_status`；
2. `TelemetryDeviceStateProjection.set()` 追加 `property_sample` 并更新 latest；
3. 规范 UI 用 `/telemetry/states` 展示当前值，用 `/telemetry/events` 展示历史；
4. 当前 `HeatingPlatformDemo.vue` 为复用旧 Device client，调用
   `/device-state/{device}` 和 `/device-state/{device}/history`；这两个响应由同一
   telemetry projection 生成，不是第二个状态 Authority。

### 18.5 每个物料的不同温度

物料温度属于 Material 自身状态。当前 Driver 通过 Host 选定的 materials gateway
执行受信 `put_data` mutation，把每个物料各自序列化为：

```json
{
  "data": {
    "temperature_c": 61.3,
    "temperature_observed_at_ms": 1787184000000,
    "temperature_source": {
      "device_id": "virtual-heater",
      "property": "site_2_temperature_c"
    },
    "serialized_state": {
      "temperature_c": 61.3,
      "target_temperature_c": 75,
      "site_id": 2,
      "progress": 72.6,
      "state": "heating",
      "observed_at_ms": 1787184000000
    }
  },
  "state_status": "heating",
  "source_job_uuid": "job-uuid",
  "observed_at_ms": 1787184000000
}
```

前端自定义组件必须按 `material_uuid` 读取各自 `aggregate.data.data`，不能复用设备全局
temperature。刷新可轮询当前聚合，或消费 changes ledger 后按 UUID HTTP 重读；现有
ledger 不含完整温度历史。当前 Demo 的数值卡从 Material 最新快照读取并按物料/温区
着色；曲线只读取加热台 telemetry history。Material 是被动投影，不得再从
`data.temperature_history` 绘图或把它当成第二个测量 Authority。

### 18.6 完成判定

- Task/Job 从 pending/running 到 succeeded，历史仍能读取；
- runtime job 的 action args 与 HostLink driver 收到值一致；
- 三个 Site 的占用 UUID 不互换；
- 三个 Material 当前 temperature 可不同，且 source_job_uuid 指向本次 Job；
- 设备状态历史来自 telemetry event，而物料当前值来自 materials aggregate；
- 重连后 UI 通过 HTTP 校准，不依赖最后一帧 SSE/WS data。

## 18. 已发现缺口与 0.1 后续清单

### P0：Demo/安全闭环前必须处理

1. 明确部署并挂载 Workflow Authority；默认 Host 自身没有 `/workflow-tasks`，只有
   `--demo-mode` 会显式挂本地 Authority。
2. 冻结“Task 提交参数覆盖”合同，或确认 Demo 参数只保存在 Graph node `param`；当前
   HTTP Task `input` 会被忽略并强制为空对象。
3. 冻结 Material observation history；当前 changes ledger 不保存温度值。
4. 为 `:8002` internal writers 加认证/权限分层，并为 external materials client 加
   service credential。
5. 为作为自定义 data key 的 `serialized_state` 增加 schema/version 标识，或冻结一个
   可跨 Demo 使用的公共观测字段。

### P1：前端与观测一致性

6. 提供不依赖本地 EdgeScheduler 的 monitor snapshot，或让 UI 完全转四库 snapshot。
7. 冻结浏览器可用的 Task cancel/pause/resume、intervention decision、ad-hoc action。
8. 把 `/devices*` catalog 明确映射到 runtime endpoint capability，或实现真实 Router。
9. 为 material/component/Site 变化定义 durable SSE 或按 ledger sequence 的统一失效通知。
10. 统一 RFC3339/Unix ms 的字段命名和前端 formatter。
11. 在 `:8002` CORS 中允许已公开 Router 实际使用的 `PATCH`，并增加跨源
    preflight 合同测试。

### P2：部署与扩展

12. 定义 lab/site/tenant 自定义 scope、角色权限、每 scope cursor/限额。
13. 为 HostLink 增加双向认证与 TLS，或正式限制为可信局域网传输。
14. 完成旧 Inventory/Resource Provider/Lab layout 数据迁移，避免双 writer。
15. 将 Backend `{code}` 与 FastAPI status 统一为一个可机器识别错误合同。

## 19. 审计与校验方式

- HTTP 路由：检索所有 `@router.get/post/put/patch/delete`，并与
  `server/api/app.py::setup_server()` 的实际 include 条件交叉检查；
- wire DTO：读取 `server/protocol/*.py` 和 `hostlink/protocol.py`；
- Authority/数据库：读取 `server/composition.py`、`server/database/DESIGN.md`、
  `server/scheduler/integration.py`；
- Edge UI：对照 OpenLab `packages/protocol/src/catalog.ts`、domain clients、
  `client.ts`、`scripts/validate-contract.mjs` 及页面调用点；
- 推荐持续校验：UniLabOS 路由/服务测试 + OpenLab protocol TypeScript 编译、Vitest 和
  `node packages/protocol/scripts/validate-contract.mjs`。

文档只陈述代码中可定位的能力；上文缺口在代码或契约测试完成前不视为 0.1 已实现。
