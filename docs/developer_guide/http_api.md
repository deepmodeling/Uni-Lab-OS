# 微后端 HTTP API

完整的版本化接口、物料/工作流流程、SSE/WS/HostLink、Python/CLI/MCP 与 Jazzy 以来的迁移说明，
请阅读 [接口手册](interfaces/index.md)。以下保留简要概览；具体响应与例外以手册和对应源码为准。

Uni-Lab-OS 的 `8002` 端口提供微后端 API 和入口导航页。用户界面由独立前端
项目提供，可部署到 GitHub Pages，并通过 HTTP 和 SSE 读取本机微后端。

## 入口

- `http://localhost:8002/`：导航页——「推荐前端」内置 OpenLab 兜底卡，并由浏览器读
  [awesome-lab-sites](https://github.com/Xuwznln/awesome-lab-sites) 索引补充其他站点；
  「开发与接入」列出 API 工具与文档
- `http://localhost:8002/api/docs`：Swagger/OpenAPI Explorer
- `http://localhost:8002/api/redoc`：ReDoc
- `http://localhost:8002/api/openapi.json`：机器可读 OpenAPI
- `https://deepmodeling.github.io/Uni-Lab-OS/`：官方 GitHub Pages 文档

实际可用接口以当前进程的 OpenAPI 为准。数据库 API 按所有权分组：

| 命名空间 | 所有权 |
| --- | --- |
| `/api/v1/runtime/*` | Job、执行尝试及运行时状态 |
| `/api/v1/materials/*` | 物料模板、实例（按件登记 `instantiate`）、位置和液体数据；按量库存 `lots`、调度预留 `reservations`、变更账本 `changes` |
| `/api/v1/telemetry/*` | 高频设备状态与遥测事件 |
| `/api/v1/edge/history/*` | 可长期保留的事件 / 载荷历史（Host 数据面）；`/api/v1/history/*` 是 V1 工作流 / 作业运行历史 |
| `/api/v1/graphs/*` | 设备图快照与实时拓扑 |
| `/api/v1/workflows/*` | 本地调度模式的工作流定义与任务 |
| `/api/v1/error-decisions/*` | 后端协调后的错误决策观测/释放协议 |
| `/api/v1/status-incidents/*` | 设备状态异常与调度联锁 |

当物料权威配置为外部微后端时，本进程不会挂载本地 `/materials/*` writer，避免
出现两个可写物料中心。

物料的幂等业务写请求统一经 `POST /api/v1/inventory/commands` 提交 `materials.v1` 变更 `InventoryMutation`
（`command_uuid` + `effect_key` 幂等，`operation` 指明操作，`actor_type` 落账本；浏览器 / 操作员发起的写请求应填
`human`）。变更的 `payload` 会与该操作的类型化请求体**逐字段比对**，缺省字段也要写全（例如 `inbound_inventory_lot`
的 `expiry_at_ms: null`），否则被拒（1000，`mutation.payload differs from the typed request body`）。完成时信封
`data` 是 `{command_id, status: "completed", result, replayed}`；被拒是 HTTP 200 的失败信封，拒绝码在 `error.code`、
`command_id` / `replayed` 在 `error.details`。库存分两种账目：`instantiate` 按件登记
实例（有 uuid、可放位点，工作流 `kind: "material"` 需求选取），`lots/inbound` 按量登记批次
（只记数量 / 单位 / 有效期，工作流 `kind: "lot"` 需求预留与扣减）。

例外包括 links、notify-device、changes/ack，以及只读的 snapshots/compare；它们直接接收各自模型，
不能统一套 Mutation。详见 [物料接口](interfaces/materials.md)。

前端不应直接向 Host 创建或重试 Job。执行命令由调度后端下发；Host 报错后，后端
负责询问前端、更新调度图或 attempt，再释放最终结果。

## 响应与鉴权

- 全部 JSON 响应（含健康 / 就绪探针、`/v1/*` 进程管理、库存命令和 Host↔权威内部通道）都是统一信封
  `{"code": 0, "data": ...}` / `{"code": N, "error": {"msg", "code"?, "details"?}}`，业务失败为 HTTP 200。保留其它状态码
  的只有：全量重置维护闸门 503 + 5001、就绪探针未就绪 503 + 5001（摘要在 `error.details`）、
  不存在的路由 404 + 4001 / 方法不允许 405 + 1001。只有 SSE 与文件下载不包信封。码表见
  [HTTP 接入](interfaces/http.md#响应信封)。
- 管理 API 不鉴权（含 V1 工站调用 `POST /api/v1/station/workflow-invocations`）：控制面 token 与 V1 工站密钥都已取消，
  旧客户端带的 `X-UniLab-Token` / `Authorization: Bearer` 被忽略。管理端口只应暴露给可信网络。

## Backend 控制面

Backend WebSocket 发送 `runtime.v1` 控制面的轻量通知；业务命令、结果与完整
状态通过微后端 HTTP API 交换。
