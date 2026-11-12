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
   `Authorization: Bearer` 留给 V1 工站调用的 api_key（`/stations/*` 调用），
   控制面中间件不再读取 Bearer，避免两者冲突。dev 内部客户端统一改用 `X-UniLab-Token`。
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

统一入口：`unilabos/server/api/envelope.py`。V1 的 `backend_response.py` / `_success` / `_error`
不另行复制。

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

dev 内部异常到业务码的映射集中在 `envelope.py`，不在各路由散写数字。

## 字段约定（API 层转换，存储保持 dev）

- **ID**：对外用 V1 字段名（`uuid`、`parent_uuid`、`resource_template_uuid` 等）。存储仍是 dev
  列名（`material_uuid`、`template_uuid`、`parent_material_uuid` 等），由路由 DTO 转换。
- **时间**：对外为 ISO 8601 UTC 毫秒（`2026-11-12T08:00:00.000Z`），存储仍是 dev 的 `*_at_ms`。
- **分页**：按 V1 各路由原样：工作流类 `page`/`page_size` + `has_more`；资源列表 `page=0`
  表示全部（上限 100）；边缘 `/inventory/*` 用 `limit`。
- **SSE**：`/api/v1/events` 游标协议不变，事件名改为 V1：`workflow.runtime.changed`、
  `workflow.authoring.changed`、`station.error_handling.changed` 等。dev 细粒度事件
  （`workflow.task|node_run|node_job|manual_confirmation|resource.changed`）作为 `data.kind`
  放进 V1 事件，不再单独发事件名。CLI `workflow watch` 同步修改。

## 同路径不同语义的处理（V1 合同优先）

| 路径 | 处理 |
|---|---|
| `/api/v1/history/*` | 归 V1（工作流与作业历史）。dev 的事件/载荷存储改到 `/api/v1/edge/history/*`，Host 客户端与 edge 代理同步修改 |
| `GET /api/v1/lab/layout` | 归 V1（已放置实例视图）。dev 的带修订网格文档并入 V1 的 lab profile/placements 模型 |
| `POST /api/v1/resource-templates` | 归 V1（返回身份回执）。Host 启动时的注册表上报改为 `POST /api/v1/registry/reports` |
| `/api/v1/error-decisions` | 归 V1 DTO（`items`、`resolved`、409 语义按 V1） |
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
