# HTTP API 迁移到 V1.0 合同

本文是统一分支（dev 架构 + V1.0 API 合同）的 HTTP API 约定，供所有迁移工作遵循。逐路由清单见
[api_v1_route_map.md](api_v1_route_map.md)，每个路由落地后在清单中更新状态。

## 已确定的原则（2026-11-12）

1. **目录与启动沿用 dev。** 路由放在 `unilabos/server/api/<领域>/`，业务逻辑在
   `unilabos/server/services/`，数据表在 dev 的数据库（`runtime.db`、`materials.db`、
   `history.db`、`telemetry.db`），新表或新列直接写进对应库的表声明：0.13 发布线四个库都以 schema v1
   为基线（开发期的 runtime v2–v12、materials v2–v8 迁移链已压平，没有迁移），发布后改结构才从 v2 起
   追加版本化迁移。不新建 V1 的
   `app/scheduler`、`workflow/`、`workspace_host/` 等目录，也不新建第二套库存库。
2. **只提供 V1 路径。** 路径、方法、请求/响应 DTO 以 V1.0 为准。dev 中能力相同但路径或 DTO
   不同的路由直接改为 V1 合同，删除 dev 旧路径，并同步修改 dev 内部调用方：Host→权威
   客户端（`unilabos/client/`）、CLI（`unilabos/app/cli/`）、MCP 目录（`server/mcp/catalog.py`）
   以及 `server/api/edge_proxy.py` 的转发规则。不保留双路径别名。
3. **dev 独有能力保留。** V1 没有的 dev 路由（如资源区间、借还、托管、包操作恢复、
   执行许可）保持现有路径，但响应信封与业务码也改为下述 V1 约定。
4. **管理 API 不鉴权（2026-11-17 起）。** dev 原来的控制面 token（`X-UniLab-Token`、`<working_dir>/control_token`、
   `unilab token`，以及控制 WebSocket 握手的 1008 拒绝）与 V1 工站调用的 api_key（p7-d 移植的
   `Authorization: Bearer <station_api_key>`、`BasicConfig.station_api_key`、`<working_dir>/station_api_key`）都已取消；
   旧客户端仍带 `X-UniLab-Token` / `Authorization` 时忽略（CORS 预检仍放行这两个头），旧配置字段与环境变量静默忽略。
   因此没有「未授权」业务码（1002 已从码表删除）。
5. **全部 JSON API 使用 V1 信封与业务码**，包括物料、runtime、telemetry、history 等原来
   直出 DTO 的路由。2026-11-17 起（负责人「能统一全部统一」）原来的例外也改为信封：健康 / 就绪探针、
   V1 进程管理 `/v1/*`、库存命令 `POST /api/v1/inventory/commands`、Host↔权威内部通道、不存在的路由；只有 SSE
   与文件下载不包信封。
6. **V1 独有领域全部迁移**：工站异常处理、试剂/样品/当前内容物/入站、工作区与布局、
   时间计划与时间线，以及工作流作者、任务观测、设备与注册表中缺失的路由。

## 响应信封

成功：HTTP 200（创建类可 201），`{"code": 0, "data": <payload>}`；无数据的命令可省略 `data`。

失败：HTTP 200，`{"code": <业务码>, "error": {"msg": "<中文说明>", "code"?: "<符号码>", "details"?: {...}}}`。
`error.code` 仅在 V1 同路由也输出符号码时输出（如 `workflow_identity_mismatch`、
`preflight_failed`），`details` 与 V1 一致。

下列情况保留非 200 的 HTTP 状态码，但正文同样是信封：

- 全量重置维护闸门：HTTP 503，业务码 `5001`。
- 就绪探针 `GET /api/v1/readiness`：就绪 HTTP 200 + `{"code": 0, "data": <摘要>}`；未就绪 HTTP 503 +
  `{"code": 5001, "error": {"msg": <原因>, "code": "not_ready", "details": <同样的摘要>}}`（探针只看状态码；HTTP 503
  与 5001 一一对应，维护闸门的 503 正文没有摘要）。
- 不存在的路由 HTTP 404 + `{"code": 4001, "error": {"msg": "Not Found", "code": "not_found"}}`、方法不允许 HTTP 405 +
  1001（`method_not_allowed`）：由应用级兜底（`envelope.install_envelope_fallback`）输出，不再是框架默认的
  `{"detail": ...}`。

其余约定：

- 请求体不是合法 JSON 或校验失败：HTTP 200，业务码 `1000`（不再保留 FastAPI 默认 422）。
- 只有 SSE（`text/event-stream`）与文件下载（物料 3D 模型资产）不包信封。
- `GET /api/v1/health`：HTTP 200 + `{"code": 0, "data": {...}}`。readiness 摘要字段按 V1：`status`（`ready` /
  `starting` / `failed`）、`capabilities`、`phase`、`workflowRuntime`、可选 `observability`；dev 的探针摘要
  `scheduler`（`local` / `remote`）、`execution`、`reasons` 作为扩展字段保留，dev 原来的 `not_ready` 状态改为 V1 的
  `starting`。health 的 `scheduler` 按 V1 为 `ready` / `disabled`，dev 的本机 / 远端归属改名 `scheduler_mode`，
  `execution` 保留；dev 扩展 `unavailable_devices` 列出设备图里有、但没有启动的设备：当前 backend 跑不了的
  （`device_id` / `registry_name` / `backend` / `reason` / `supported_backends`），以及持有的物料建不成 PLR 实例而
  进入维护模式的（另有 `maintenance: true` / `material_uuid` / `material_type`），以及装配 / 构造 / 初始化失败而进入维护模式的（另有 `maintenance: true` / `stage` / `error_type`），分进程时经控制面读 Host。
- V1 进程管理的四个路由（原 V1 Workspace Host 路由：`/v1/snapshot`、`/v1/operations`、`/v1/operations/{id}`、
  `/v1/logs/{component}`）路径按 V1 不带 `/api` 前缀，响应是信封：成功 `data` 为快照 / 操作文档 / 日志尾部（提交操作
  HTTP 202），失败 HTTP 200 + 业务码，V1 的符号码（`operation_not_found`、`operation_conflict`、`revision_conflict`、
  `invalid_request`、`component_invalid`、`component_unavailable` …）放在 `error.code`，`details` 原样：不存在 4001、
  冲突 3003、日志暂不可读 5001、其余 1000（`envelope.process_management_business_code`）。不鉴权。详见下文
  「进程管理协议（p8 落地）」。
- `POST /api/v1/inventory/commands`：完成为 `{"code": 0, "data": {command_id, status: "completed", result, replayed}}`；
  被拒（含同一 `command_id` 的拒绝重放）为 HTTP 200 + `{"code": <业务码>, "error": {"msg", "code": <error_code>,
  "details": {command_id, replayed}}}`。它同时是 dev 全部 `materials.v1` 幂等变更的唯一写入口（body 为带
  `protocol_version` / `command_uuid` / `operation` 的变更信封，原路径参数放在 `target_uuid`），dev 按操作拆分的写路由
  已删除。
- Host↔权威内部通道 `GET /edge/commands/{uuid}`、`POST /api/v1/edge/http-responses/{uuid}` 同样是信封（不存在 4001、
  控制服务未就绪 5001、`request_uuid` 不一致 1000）。

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
  400 / 422→1000，403→1001，503→5001，其余→1。请求校验失败与非法 JSON 在
  路由类里就地换成 1000，不再依赖应用级异常处理器或路径前缀登记。未登记的异常照常 HTTP 500（服务端缺陷）。
- **不包信封**的端点用 `@raw_response` 标记，现在只有文件下载（物料 3D 模型资产）；SSE 等非 JSON 响应与生成器
  端点自动透传。探针、`/v1/*`、库存命令与 Host↔权威内部通道都是普通信封路由（Host 回送结果后按信封判断成败）。
  `edge_proxy` 字节级透传 Host 已包好的信封；Host 不在线时返回 HTTP 200 + 5001 + `Retry-After: 3`
  （CORS `expose_headers` 含 `Retry-After`，跨域前端能读到）。
- **中间件级拒绝**保留 HTTP 状态码、正文是信封：全量重置维护闸门 503 + 5001（管理 API 不鉴权，没有 401）。
- **应用级兜底**：`envelope.install_envelope_fallback(app)` 登记的处理器把路由层的 `HTTPException`（不存在 404、
  方法不允许 405 等）也输出成信封并保留状态码；管理 API 应用（`api/app.py`）已登记。
- 契约测试 `tests/server/test_api_envelope_contract.py` 会装配全部路由（含 `/v1/*` 与内部通道），断言每个 JSON
  路由都是信封路由（白名单只有文件下载）。新增路由器忘了用 `envelope_router` 会直接失败。
- dev 内部调用方统一解包一次：`HTTPClient`（`envelope=False` 只给非微后端对端）、`client/utils/envelope.py` 的
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
  `site_occupied` 6007、`identity` 6010、`active_claim` 6011、`site_ingress_reserved` 6011、`template_immutable` 6002、`parent_cycle` 6004、`site_template` 6008、`site_cycle` 6009、
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
  `runtime.logs.changed`（日志通知）与物料的 `/api/v1/materials/events`（`materials.changed`）不在
  V1 合同里，暂保持原名。物料失效另以 V1 名 `material.changed`（`data.sequence` 为物料账本末尾，
  `data.material_uuids` 列出这段账本里变了的物料，Site 的变化归到所属物料、拓扑边归到两端；空列表表示没有物料变化，
  涉及模板、账本重置或超过 500 个物料时省略这个键，表示整体重读）随 `/api/v1/events` 推送，前端每个工站只维持这一条事件流。
  **单一序列**：物料与日志通知由权威进程合并、限速（各自每秒至多一行）后写进 `frontend_event`，与工作流事件同一个序列，
  `/api/v1/events` 的每一帧都带整数 id，断线续传、`stream.reset` 与保留清理一视同仁（见
  [实时协议](interfaces/realtime.md)）。uni-lab-fe 须知：`material.changed` 现在带 id；不带 `material_uuids` 的通知必须整体重读物料。
  CLI `workflow watch` 的游标随任何带 id 的帧前进。
- **监控流**：`/api/v1/monitor/events` 按 `frontend_event` 序号续传：重连带 `Last-Event-ID` 不再回放最近 40 条，
  过期 / 超前游标发 `stream.reset`，按页扫描、游标只推进到实际扫过的最大序号，每帧都带 id。
- **MCP**：`GET /mcp` 回 405（不开 Streamable HTTP 的独立 SSE 流）；`POST /mcp` 的 JSON 响应不变。

## 库存与工作区（p7-c 落地）

- materials.db 把 V1 inventory.db 的概念并入为表——`material_content`（试剂 / 样品 /
  当前内容物是 `material_substance` 行上的角色，数量仍以 substance 为唯一权威）、`material_content_history`、
  `material_state_history`、`material_content_reservation`（容器内容物数量预留，防并发超量抽取）、
  `material_source_binding`（`task_exclusive` / `shared_source`）、`station_ingress_reservation`、
  `inventory_processed_command`，以及未删除非空条码唯一索引（当时一并移植的实验室档案 / 分区 / 摆放
  `lab_meta` / `lab_zone` / `lab_placement` 已按负责人 2026-11-17 决定删除）；
  `inventory_lot` 带 `location_site_uuid` / `warehouse_zone_id`。这些都在 schema v1 基线里。批次可经 `correct_inventory_lot`（命令 `inventory.lot.correct`）按实测量
  校正，适用于质量 / 体积批次与已隔离批次。
- 工站入站预留（`/inventory/ingress-reservations`）保护它的库位：`reserved`（未过期）或 `in_transit` 期间，
  人工移动（`move_material` 命令、V1 `site_placement`）、transfer 和工作流搬运派发前的预检都以
  `MaterialConflictError(conflict="site_ingress_reserved")` 拒绝（HTTP 6011，库存命令 `error_code`
  `conflict:site_ingress_reserved`），载体本身也只能经 `receive` 落位——例外是 `in_transit` 的载体被直接放进自己预留的库位，
  等同接收（同一事务里预留置为 `received`）；同一载体同时至多一个活动预留（再预留 3003）。预留可以给
  `owner_material_uuid` 代替 `candidate_site_uuids`：候选是该物料下接受载体的库位（模板白名单与允许的资源类别，按库位序号），
  一个都不接受 6008，都被占着 6007；工作流按所属物料选库位用这种写法，任务结束经 `POST .../ingress-reservations/finish-task`
  收尾（dev 扩展）。
  `site_ingress_reserved` 是临时资源条件：预留接收、取消或过期后自然解除，调度侧应等待而不是判任务失败
  （常量 `services.materials.SITE_INGRESS_RESERVED`）。与 V1 的差别：只锁库位，不锁库位所属设备与载体设备上的
  普通作业；设备快照（`apply_snapshot`）是现场事实，不受预留拦截。
- 化学品：V1 `/reagent-infos` 是 `chemical` 表的 V1 视图；dev `/chemicals`（带 `expected_version` 的写入）保留。
- 进程管理协议（`/v1/snapshot`、`/v1/operations`、`/v1/logs/{component}`）见下文「进程管理协议（p8 落地）」。

## 作业与库存接缝（p7-d 落地）

- materials.db 的读模型索引（账本按 `job_uuid`、内容物预留按 `job_uuid`、内容物历史按
  `subject_uuid`）供消费读模型按作业 / 内容物查询；runtime.db 结构不变。
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

## 进程管理协议（p8 落地）

V1 的 Workspace Host 是每个工作区一个独立监督进程（`unilabos/workspace_host/host.py`），拉起 / 看护 Backend、Edge、
PLC 仿真器与渲染器。统一分支**不新建这个进程**：dev 的 `unilab` 调度权威进程常驻、持有管理端口，并直接看护 Host
子进程（`app/backend_main.py` + `app/supervisor.py:HostChildSupervisor`），它就是监督者。四个 V1 路由挂在权威的管理
应用上（`server/api/process_management.py`，与 Workflow Authority 同归属：默认 `unilab`、`--role backend`、
`--no-safe-restart` 单进程都有；`--address` 的 Host 子进程没有），逻辑在 `server/services/process_management.py`。
统一分支不再有 Workspace Host 这一概念，代码、表名（`process_management_*`）和 MCP 操作（`process-management.*`）
都叫进程管理；线上合同沿用 V1 的路径、请求体、文档字段、错误符号码和 `schemaVersion: "unilab-workspace-host/v1"`，
但 2026-11-17 起响应包统一信封、错误不再用 HTTP 状态码区分（符号码在 `error.code`），V1 客户端需要改为解包信封。

| V1 | dev |
|---|---|
| V1 Workspace Host 进程（`host` 段） | 常驻的调度权威进程（pid、管理地址、平台；V1 的 `tokenPath` 不输出：没有 token 文件） |
| 组件 `backend` | 调度权威本身（常驻；`metadata.scheduler` = 排空状态，`metadata.restart` = 重启协调器状态） |
| 组件 `edge` | Host：默认拓扑是 `HostChildSupervisor` 看护的子进程（pid / 崩溃次数 / 退出码 + 控制面连接推导阶段）；`--role backend` 是经控制面接入的远端 Host（不受本进程看护）；单进程时就是本进程 |
| （无） | 组件 `managed:<process_id>`：Host 的受管设备进程（`services/device_processes.py`），与 `/hostlink/log-sources` 的来源标识一致 |
| 组件 `plc` / `renderer` | dev 不运行：快照里恒为 `idle`（`metadata.supported=false`），日志为空 |
| `revision` / `eventCursor`（内存计数） | runtime.db `process_management_state`：观测到的进程状态指纹（各组件阶段 / pid / 代次、排空阶段、重启登记）变化或写操作成功时递增；`expectedRevision` 提交前先重新观测 |
| 操作文件 `operations/<id>.json` | runtime.db `process_management_operation`（幂等指纹、阶段、结果 / 错误、`progress` 挂接既有事实）；权威重启后未完成的记为 `operation_interrupted` 失败 |
| `workspace.start` / `os.start` | `HostChildSupervisor.start()` 并等 Host 接入控制面；`workspace.start` 另外解除排空（V1 同口径） |
| `workspace.stop` / `os.stop` | 先排空（`SchedulerLifecycle`，超时 `workspace_drain_timeout` 且 Host 保持运行），再 `HostChildSupervisor.stop()`；排空闸门保留到 `workspace.start` / `scheduler.resume`（V1 的 `os.stop` 不排空，dev 一律先排空） |
| `workspace.restart` / `os.restart` | `RestartCoordinator.request(scope=edge)`：安静点后 Host 退出码 75 被原参数再拉起，以 Host 新进程身份重连为成功；`DELETE /api/v1/restart` 取消时操作失败 `restart_canceled` |
| `backend.start` / `backend.stop` / `backend.restart` | 权威必然在运行：`start` 返回快照；`stop` / `restart` 失败 `authority_resident`（与 `/restart` 拒绝 `scope=process` 同理） |
| （内部的 drain / resume） | 命令 `scheduler.drain`（`timeoutSeconds`）/ `scheduler.resume` |
| （无） | `device-process.start\|stop\|restart`（`processId`）、`driver-package.install\|uninstall\|enable`：经控制面交给 Host 自己的 `/device-processes`、`/driver-packages` 路由；驱动包等 Host 操作台账终态，记录里挂 `packageOperationId` |
| `material.layout.inspect\|preview\|apply`（设备图文件 + 字节摘要 CAS） | `services/materials/workspace_layout.py`：布局源是 materials.db 物料树，`revision` = 范围内各根树快照状态哈希的汇总；preview 只读（结果存在操作记录里，`previewId` 按源修订 + 候选修订 + 规范化变更集派生）；apply 在一个 materials.db 写事务内核对 `expectedRevision` 与 preview 源修订、重建候选并核对候选修订，再按根树走 `apply_material_snapshot`（变化节点带状态哈希前置条件）。`sourceNodeId` 是 `material_uuid`，也接受 `resource_id` |
| `local.reset-state`、`plc.*`、`renderer.*`、`configuration.update`、`authority.switch`、`release.*`、`material.template.validate` | 照 V1 先受理、执行失败 `command_unsupported`（全量重置走 `POST /api/v1/reset`） |
| `GET /v1/logs/{component}?maxBytes=` 直接读日志文件尾部 | 只读应用选定的日志：权威主日志（`utils/runtime_logs.read_log_file`）、Host 的 `/hostlink/logs` 来源 `host` / `managed:<id>`；`maxBytes` 范围同 V1（1–4 MiB），但读取端单次最多 256 KiB / 1000 行（`truncated` 标出） |

时间字段（`submittedAt` 等）是 ISO 8601 UTC 毫秒（V1 为秒）。响应是统一信封（见上文「响应信封」），V1 的错误符号码在
`error.code`。客户端：`HTTPLifecycleClient.workspace_snapshot` / `submit_workspace_operation` / `workspace_operation` /
`wait_workspace_operation` / `execute_workspace_operation` / `workspace_logs`（解包信封；失败抛
`protocol.runtime.process_management.ProcessManagementError`，`code` 取自 `error.code`）；MCP：`process-management.*` 四个操作。

## 同路径不同语义的处理（V1 合同优先）

| 路径 | 处理 |
|---|---|
| `/api/v1/history/*` | 归 V1（工作流与作业历史）。dev 的事件/载荷存储改到 `/api/v1/edge/history/*`，Host 客户端与 edge 代理同步修改 |
| `GET /api/v1/lab/layout` | 曾归 V1（已放置实例视图），dev 的带修订网格文档并入 V1 的 lab profile/placements 模型；负责人 2026-11-17 决定把档案 / 布局 / 分区 / 摆放路由（`/lab/profile`、`/lab/layout`、`/lab/zones`、`/lab/placements`）连同 `lab_meta` / `lab_zone` / `lab_placement` / `lab_layout` 删除；不用这些表的 `/lab/warehouse`、`/lab/assembly/{id}`、`/lab/demo` 保留 |
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
