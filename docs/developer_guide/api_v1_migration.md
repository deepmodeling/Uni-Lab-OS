# HTTP API 迁移到 V1.0 合同

本文是统一分支（dev 架构 + V1.0 API 合同）的 HTTP API 约定，供所有迁移工作遵循。逐路由清单见
[api_v1_route_map.md](api_v1_route_map.md)，每个路由落地后在清单中更新状态。

## 已确定的原则（2026-11-12）

1. **目录与启动沿用 dev。** 路由放在 `unilabos/server/api/<领域>/`，业务逻辑在
   `unilabos/server/services/`，数据表在 dev 的数据库（`runtime.db`、`materials.db`、
   `history.db`、`telemetry.db`），新表或新列走现有版本化迁移。不新建 V1 的
   `app/scheduler`、`workflow/`、`workspace_host/` 等目录，也不新建第二套库存库。
2. **只提供 V1 路径。** 路径、方法、请求/响应 DTO 以 V1.0 为准。dev 中能力相同但路径或 DTO
   不同的路由直接改为 V1 合同，删除 dev 旧路径，并同步修改 dev 内部调用方：Host→权威
   客户端（`unilabos/client/`）、CLI（`unilabos/app/cli/`）、MCP 目录（`server/mcp/catalog.py`）
   以及 `server/api/edge_proxy.py` 的转发规则。不保留双路径别名。
3. **dev 独有能力保留。** V1 没有的 dev 路由（如资源区间、借还、托管、包操作恢复、
   执行许可）保持现有路径，但响应信封与业务码也改为下述 V1 约定。
4. **写操作保留 dev 控制面 token。** 前端改为在请求头 `X-UniLab-Token` 中携带 token。
   `Authorization: Bearer` 留给 V1 工站调用的 api_key（`POST /api/v1/station/workflow-invocations`），
   控制面中间件不再把 Bearer 当控制面 token，避免两者冲突。dev 内部客户端统一改用 `X-UniLab-Token`。
   工站密钥（p7-d）：`BasicConfig.station_api_key`（环境变量 `UNILABOS_BASICCONFIG_STATION_API_KEY`，兼容 V1 的
   `UNILABOS_EDGECONTROLCONFIG_API_KEY`），为空时读 `<working_dir>/station_api_key`，不自动生成；都没有时工站入口一律
   401。中间件对工站入口只认有效工站密钥（HTTP 401 + 1002），控制面 token 不能代替它，它也不能代替控制面 token。
5. **全部 JSON API 使用 V1 信封与业务码**，包括物料、runtime、telemetry、history 等原来
   直出 DTO 的路由。
6. **V1 独有领域全部迁移**：工站异常处理、试剂/样品/当前内容物/入站、工作区与布局、
   时间计划与时间线，以及工作流作者、任务观测、设备与注册表中缺失的路由。

## 响应信封

成功：HTTP 200（创建类可 201），`{"code": 0, "data": <payload>}`；无数据的命令可省略 `data`。

失败：HTTP 200，`{"code": <业务码>, "error": {"msg": "<中文说明>", "code"?: "<符号码>", "details"?: {...}}}`。
`error.code` 仅在 V1 同路由也输出符号码时输出（如 `workflow_identity_mismatch`、
`preflight_failed`），`details` 与 V1 一致。

例外，仍用 HTTP 状态码表达：

- 控制面 token 缺失或错误：HTTP 401，信封业务码 `1002`。
- 请求体不是合法 JSON 或校验失败：HTTP 200，业务码 `1000`（所有 `/api/v1` 路由一律走信封，
  不再保留 FastAPI 默认 422）。
- SSE（`text/event-stream`）、文件下载、健康探针 `/health` 的纯文本或流式响应不包信封。
- `GET /api/v1/readiness`、`GET /api/v1/health` 按 V1 原样不包信封：探针看 HTTP 状态（就绪 200、否则 503）。
  readiness 字段按 V1：`status`（`ready` / `starting` / `failed`）、`capabilities`、`phase`、`workflowRuntime`、
  可选 `observability`；dev 的探针摘要 `scheduler`（`local` / `remote`）、`execution`、`reasons` 作为扩展字段保留，
  dev 原来的 `not_ready` 改为 V1 的 `starting`。health 的 `scheduler` 按 V1 为 `ready` / `disabled`，dev 的本机 / 远端
  归属改名 `scheduler_mode`，`execution` 保留。
- `POST /api/v1/inventory/commands` 按 V1 原样返回裸结果 `{command_id, status, result | error, error_code, replayed}`
  （V1 控制台按 `status` 判断，业务拒绝也是 HTTP 200）；拒绝结果另带 `code`（本表业务码）。它同时是 dev 全部
  `materials.v1` 幂等变更的唯一写入口（body 为带 `protocol_version` / `command_uuid` / `operation` 的变更信封，
  原路径参数放在 `target_uuid`），dev 按操作拆分的写路由已删除。

统一入口：`unilabos/server/api/envelope.py`。V1 的 `backend_response.py` / `_success` / `_error`
不另行复制。

### 实现机制（基础层，2026-11-12）

- **路由器一律用 `envelope_router(...)` 创建**（或给 `APIRouter` 传 `route_class=envelope_route_class(...)`）。
  路由类 `EnvelopeRoute` 把返回的 DTO / dict / list 包成 `{"code":0,"data":...}`，返回 `None` 时输出
  `{"code":0}`，OpenAPI 响应模型同步为 `Envelope[原模型]`；声明 `status_code=204` 的命令改为 200。
  已经是信封的响应（`ok_response` / `error_response` / `EnvelopeResponse`）原样返回。
- **失败只抛异常**：`ApiError(业务码常量, msg, symbol=?, details=?)`；dev 服务层异常登记在
  `envelope.DOMAIN_ERRORS`（按异常类完整路径沿 MRO 匹配）；过渡期的 `HTTPException` 按路由器的错误口径
  换算：`WORKFLOW`（404→3002）、`RESOURCE`（404→4001、409→4002）、`GENERIC`（404→4001、409→3003）；
  400 / 422→1000，401→1002（保留 HTTP 401），403→1001，503→5001，其余→1。请求校验失败与非法 JSON 在
  路由类里就地换成 1000，不再依赖应用级异常处理器或路径前缀登记。未登记的异常照常 HTTP 500（服务端缺陷）。
- **不包信封**的端点用 `@raw_response` 标记：`/api/v1/health`、`/api/v1/readiness`（探针看 HTTP 状态码）；
  SSE、文件下载等非 JSON 响应与生成器端点自动透传。Host↔权威内部通道 `/api/v1/edge/http-responses/*`
  （以及 `/api/v1` 之外的 `/edge/commands/*`）保持普通路由：Host 回送结果时按 HTTP 状态判断成败。
  `edge_proxy` 字节级透传 Host 已包好的信封；Host 不在线时返回 HTTP 200 + 5001。
- **中间件级拒绝**保留 HTTP 状态码、正文是信封：控制面 token 401 + 1002，全量重置维护闸门 503 + 5001。
- 契约测试 `tests/server/test_api_envelope_contract.py` 会装配全部路由，断言每个 `/api/v1` JSON 路由都是
  信封路由（白名单只有上面的探针与内部通道）。新增路由器忘了用 `envelope_router` 会直接失败。
- dev 内部调用方统一解包一次：`HTTPClient`（`envelope=False` 只给探针）、`client/utils/envelope.py` 的
  `request_json` / `decode_json_body`（urllib 客户端）、`edge_proxy.edge_response_data`（权威经控制面读 Host）。
  失败抛 `EnvelopeError(code, error, symbol, details, http_status)`；各领域客户端错误（`RuntimeHTTPError`
  等）的 `status_code` 按业务码换算回 HTTP 语义，`business_code` 保留原码。
- 字段转换工具在 `unilabos/server/api/v1_fields.py`：`ms_to_iso` / `iso_to_ms` 与声明式 `FieldMap`
  （`to_v1` 响应、`to_dev` 请求体）。

## 业务码表（V1 为准）

| 码 | 含义 | 来源 |
|---|---|---|
| 0 | 成功 | V1/dev |
| 1 | 未归类内部错误 | V1/dev |
| 1000 | 参数或载荷不合法 | V1/dev |
| 1001 | 只读模式、调试不允许、设备动作不允许 | V1 |
| 1002 | 写操作缺少或带错控制面 token（dev 原 4001，改号以免与 V1 冲突） | 统一分支 |
| 2005 | 数据库冲突 | V1 |
| 3002 | 工作流或任务不存在 | V1/dev |
| 3003 | 状态冲突：修订过期、唯一键重复、状态不允许 | V1/dev |
| 4000 | 试剂信息仍被引用 | V1 |
| 4001 | 资源不存在 | V1 |
| 4002 | 资源数据冲突 | V1 |
| 5000 | 资源模板不存在 | V1 |
| 5001 | 依赖暂不可用或部署不支持 | V1/dev |
| 5003 | 模板定义不合法 | V1 |
| 5004 | 模板数据冲突 | V1 |
| 6000–6011 | 物料：不存在、模板不存在或不可变、父级不存在或成环、库位不存在或已占用、模板不允许、库位成环、身份冲突、活动占用冲突 | V1 `backend_contract.py` |

dev 内部异常到业务码的映射集中在 `envelope.py`，不在各路由散写数字。命名常量与码表一一对应
（`INVALID_PARAMETER`、`OPERATION_NOT_ALLOWED`、`UNAUTHORIZED`、`WORKFLOW_NOT_FOUND`、`STATE_CONFLICT`、
`RESOURCE_NOT_FOUND`、`RESOURCE_DATA_CONFLICT`、`SERVICE_UNAVAILABLE`、`MATERIAL_*` 等）。

- 工作流领域错误按 V1 `_business_code` 分类，dev 独有的 `local_task_authority_forbidden` 归 1001；
  `workflow_identity_mismatch`、`debug_not_allowed`、`debug_task_conflict`、`device_action_not_allowed`、
  `preflight_failed` 同时输出 `error.code` 符号码。
- 物料服务异常显式携带业务含义，不再按消息文字判断（p7-c 起）：`MaterialNotFoundError(msg, subject=...)`，
  `subject` 为 `material` / `site` / `template` / `parent` / `resource_template` 时分别为 6000 / 6006 / 6001 / 6003 / 5000，
  其余（`lot`、`reservation`、未给）为 4001；`MaterialConflictError(msg, conflict=...)`，`conflict` 为
  `site_occupied` 6007、`identity` 6010、`active_claim` 6011、`template_immutable` 6002、`parent_cycle` 6004、
  `database` 2005、`template_data` 5004、`state` 3003，未给为 4002。映射表在 `envelope.py`，幂等拒绝重放时
  `subject` / `conflict` 随拒绝码（如 `not_found:material`）一起还原。
- 化学品（V1 的试剂信息）与设备图按资源口径：不存在 4001、冲突 4002。

## 字段约定（API 层转换，存储保持 dev）

- **ID**：对外用 V1 字段名（`uuid`、`parent_uuid`、`resource_template_uuid` 等）。存储仍是 dev
  列名（`material_uuid`、`template_uuid`、`parent_material_uuid` 等），由路由 DTO 转换。
- **时间**：对外为 ISO 8601 UTC 毫秒（`2026-11-12T08:00:00.000Z`），存储仍是 dev 的 `*_at_ms`。
- **分页**：按 V1 各路由原样：工作流类 `page`/`page_size` + `has_more`；资源列表 `page=0`
  表示全部（上限 100）；边缘 `/inventory/*` 用 `limit`。
- **SSE**：`/api/v1/events` 游标协议不变，事件名改为 V1：`workflow.runtime.changed`、
  `workflow.authoring.changed`、`station.error_handling.changed` 等。dev 细粒度事件
  （`workflow.task|node_run|node_job|manual_confirmation|resource.changed`）作为 `data.kind`
  （`task` / `node_run` / `node_job` / `manual_confirmation` / `resource`）放进 V1 事件，不再单独发事件名；
  原载荷字段保留。换名在 API 层（`api/runtime/events.py:to_v1_event`）做，`frontend_event` 表仍存 dev 名。
  `runtime.logs.changed`（无 id 的日志通知）与物料的 `/api/v1/materials/events`（`materials.changed`）不在
  V1 合同里，暂保持原名。CLI `workflow watch` 同步修改。

## 库存与工作区（p7-c 落地）

- materials.db 升到 **schema v7**：v6 把 V1 inventory.db 的概念并入为表——`material_content`（试剂 / 样品 /
  当前内容物是 `material_substance` 行上的角色，数量仍以 substance 为唯一权威）、`material_content_history`、
  `material_state_history`、`material_content_reservation`（容器内容物数量预留，防并发超量抽取）、
  `material_source_binding`（`task_exclusive` / `shared_source`）、`station_ingress_reservation`、
  `inventory_processed_command`、`lab_meta` / `lab_zone` / `lab_placement`，以及未删除非空条码唯一索引（迁移时重复条码
  留给最早创建的物料，其余清空并记入 `extra_json.duplicate_barcode`）；v7 重建 `inventory_lot` 增加
  `location_site_uuid` / `warehouse_zone_id`。批次可经 `correct_inventory_lot`（命令 `inventory.lot.correct`）按实测量
  校正，适用于质量 / 体积批次与已隔离批次。
- 化学品：V1 `/reagent-infos` 是 `chemical` 表的 V1 视图；dev `/chemicals`（带 `expected_version` 的写入）保留。
- Workspace Host（`/v1/snapshot`、`/v1/operations`、`/v1/logs/{component}`）尚未迁移：dev 没有独立的工作区监督进程。

## 作业与库存接缝（p7-d 落地）

- materials.db 升到 **schema v8**：只加索引（账本按 `job_uuid`、内容物预留按 `job_uuid`、内容物历史按
  `subject_uuid`），供消费读模型按作业 / 内容物查询；runtime.db 不变（仍是 v11）。
- **消费事实**（`/workflow-tasks|workflow-node-jobs|reagents/{uuid}/inventory-consumptions`）是 materials.db 现有事实的
  只读投影（`services/materials/consumption.py`），不另建 V1 的消费台账：批次 = 作业库存预留按生命周期结算的
  预留量减去实际用量校正（`inventory_type: lot`，dev 扩展值）；容器内容物 = 已结算的内容物数量预留（实际量与前后
  余量记在 `material_content_history` 的 `consume` 扩展里）；物理分装 = 已结算 `material_execution` 的来源内容物
  （无计划量）。批次账本的预留结算记录携带 `reservation_uuid` / `job_uuid` / 前后总量。
- **数量核对**（`reconcile-quantity`）对任何已有结果或结果未知的作业生效；批次实际用量与物理回执结算共用
  `MaterialExecutions.apply_actual_lot_quantity`（效果键 `(job_uuid, physical.inventory:<lot>)`），同量重放、异量
  3003，先后任意都不会重复扣减；内容物预留只结算一次。全部写入同一 materials.db 事务。
- **结果未知处置**（`resolve-uncertain`）只接受 `canceled`，经 `ErrorDecisionDesk` 投递 `abort`；物理结果未知的
  动作终止后仍保留物料 / 库位占用，直到物理恢复核实结算。
- **库存命令**补齐 V1 的实例类型（实例入库、上台、内容物写 / 清、消耗、标为已使用、批次调整），V1 实例状态映射见
  `services/materials/inventory_commands.py` 模块说明。
- `GET /api/v1/resources` 按 V1 文档仍在用的旧资源列表实现为物料树的薄读（信封，`page=0` 返回全部）；
  `POST /api/v1/jobs/{job_id}/finish` 是 V1 设备执行端回报结果的内部通道，dev 由运行时传输承担，按设计不移植。

## 同路径不同语义的处理（V1 合同优先）

| 路径 | 处理 |
|---|---|
| `/api/v1/history/*` | 归 V1（工作流与作业历史）。dev 的事件/载荷存储改到 `/api/v1/edge/history/*`，Host 客户端与 edge 代理同步修改 |
| `GET /api/v1/lab/layout` | 归 V1（已放置实例视图）。dev 的带修订网格文档并入 V1 的 lab profile/placements 模型 |
| `POST /api/v1/resource-templates` | 归 V1（返回身份回执）。Host 启动时的注册表上报改为 `POST /api/v1/registry/reports` |
| `/api/v1/error-decisions` | 归 V1 实际合同（`app/scheduler/api.py`）：列表 `{"decisions": [...]}`；请求体即 V1 `ErrorDecisionIn`（`action` / `option` / `result` / `reason` 均可选，`action` 不强制、不默认，都不给时执行面按 V1 设备侧口径缺省 `abort`）；成功 `{"decision_id", "status": "delivered"}`；决策已不在等待按 V1 的 HTTP 404，经信封换成该路由口径的"不存在"（诊断路由为 GENERIC，即 4001）。dev 增量扩展可选：`Idempotency-Key` 重放、操作员身份（请求体 `operator_id` 或请求上下文）、物理结果未知只允许补记或终止（3003）。**勘误**：本行早先写作"`items`、`resolved`、409 语义按 V1"，与 V1 实际合同不符（那是 dev 原口径），已按 V1 代码更正 |
| 任务命令 | 归 V1：`pause` 与 `switch_to_step` 分开，`expected_revision` 可选；dev 的重复放行保护改由幂等键承担 |
| `composite-invocations`、`authoring/apply` | 归 V1 请求体；dev 的 CAS 字段由服务层从当前修订推导 |
| 图保存 `site_binding_mode` | 默认值按 V1；dev 的程序化导入显式传值 |
| `/readiness`、`/health` | 归 V1 字段（`capabilities`、`phase`）；dev 探针改读新字段 |
| `/api/v1/materials/{material_uuid}` | V1 的 UUID 路由注册在字面量路由（`/lots`、`/changes` 等）之后 |

## 测试要求

- 每个迁移路由至少一个契约测试：路径、方法、请求示例、响应信封与业务码，示例取自 V1 实现或
  V1 测试（`/root` 外的 V1 源码只作参考，不复制其目录结构）。
- dev 原有测试改为断言 V1 合同；不能为了通过而删除断言。
- 全量 `pytest tests`（CPython 3.14、最新 PLR）必须通过。
