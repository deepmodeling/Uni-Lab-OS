# HTTP 接入与业务域

适用版本与接口定义入口见 [手册入口](index.md)。本章补充路由所属进程、实际响应和业务约束；
完整路由、请求模型与查询参数见 [API 全目录](api-catalog.md)，
当前部署的模型字段还可在管理地址下的 `/api/docs` 查看。

## 地址、角色与健康检查

示例使用 `http://127.0.0.1:8003`，实际由 `--port` 配置；默认端口是 8002。
API 根为 `/api/v1`，Swagger `/api/docs`，ReDoc `/api/redoc`，运行中 OpenAPI `/api/openapi.json`。

```bash
python -m unilabos.app.main --port 8003 --disable_browser
curl http://127.0.0.1:8003/api/v1/health
```

健康响应示例：`{"status":"ok","scheduler":"local","execution":"ready"}`。
`execution` 可为 `ready`、`restarting`、`disabled`；HTTP 进程在线不等于目标设备已注册。
执行前还要读取 `/runtime/endpoints?state=online` 的 `device_routes` 与 `action_capabilities`。

默认分进程模式下的实际处理位置：

| 路径域（均在 `/api/v1` 下） | 处理者 |
| --- | --- |
| workflows、workflow-tasks、workflow-node-*、workflow-manual-confirmations | 权威 WorkflowService |
| registry、resource-templates | 权威 RegistryService |
| materials、graphs、lab、scheduler、health、ping、restart、reset | 权威 |
| error-decisions | 权威协调，完成后通知 Host 放行 |
| runtime、telemetry、history、hostlink、status-incidents | 经控制通道转发 Host |
| driver-packages、device-processes | 经控制通道转发 Host |
| events | 权威的工作流持久事件与瞬时日志通知 |

Host 不在线或中继超时，Host 专属请求返回 **503 + `Retry-After: 3`**，
而权威上的工作流定义和物料仍可读取。
`--role backend`、`--address`、`--no-safe-restart` 会改变装配；不带执行面的进程不能伪装 Host 能力。

导出器的 `x-openlab-role` 仅是现有目录标签，例如把 `error-decisions` 标成 `host`；
它不完全反映上述默认拓扑，不能直接作为转发或鉴权规则。

## 认证与网络边界

**写操作需要控制面 token。** 管理 API 的 POST / PUT / PATCH / DELETE 必须带请求头 `X-UniLab-Token: <token>`
或 `Authorization: Bearer <token>`，即使只监听 127.0.0.1 也一样：CORS 默认放行所有来源（`BasicConfig.cors_allow_origins`），
没有 token 时任何网页都能经浏览器向本机后端提交任务、启动设备。缺少或带错时返回 HTTP 401，
正文 `{"code": 4001, "error": {"msg": ...}, "detail": "control token missing or invalid"}`，带 `WWW-Authenticate: Bearer`。
读操作（GET、SSE `/api/v1/events`）不需要 token。

- token 由调度权威首次启动时生成在 `<工作目录>/control_token`（或取 `BasicConfig.control_token`），见配置指南「控制面 token」；
  在同一台机器上执行 `unilab token` 打印它（stdout 只有 token 本身）。
- `unilab` 启动时打开的导航页地址带 `#token=...`（URL 片段，不发给服务器），导航页只把它交给内置推荐前端
  （OpenLabSite：`#/?backend=<地址>&token=<token>`），随后从地址栏抹掉；站点索引里的第三方前端拿不到。
- Python SDK、CLI、Host 都会自动从配置 / 环境变量 / 工作目录里找到 token；MCP 端点 `/mcp` 不经此检查
  （它只接受本机且拒绝一切浏览器 Origin），其工具在进程内调用 API 时自带 token。

对外部署仍需在可信网络或反向代理上落实 TLS 与访问控制：token 只证明调用方拿到了本机的这份密钥，
驱动包安装、任务提交、干预和 reset 均可能改变设备或实验数据。SDK 中的 `Authorization: Lab <base64(ak:sk)>`
是上游 Backend 认证能力，与控制面 token 无关。

## 三类响应，不能统一盲解包

| 类型 | 形状 / 判断方式 | 主要使用者 |
| --- | --- | --- |
| Backend 信封 | 成功 `{"code":0,"data":...}`，无数据时可以只有 `code`；先判 HTTP，再判 `code` | workflow、graphs、chemicals、registry（成功） |
| 直出 DTO / 数组 | 直接是资源或列表；错误通常 `{"detail":...}` | runtime、telemetry、history、诊断、驱动包、日志 |
| materials.v1 | 读操作直出；幂等写结果 `MutationResult`，含 `data`、账本区间与 replayed | 物料聚合/库存写入 |

信封式路由（workflow、graphs、chemicals）的业务失败一律 HTTP 200，形状固定：

```json
{"code":3003,"error":{"msg":"冲突详情"}}
```

三者共用一张业务码表（`unilabos/server/api/envelope.py`）：

| code | 含义 |
| --- | --- |
| 0 | 成功（创建类接口 HTTP 201） |
| 1000 | 请求参数 / 载荷不合法：字段校验、JSON 解析、模板或 Site 绑定错误 |
| 3002 | 资源不存在或已删除 |
| 3003 | 状态冲突：revision / hash 过期、唯一键重复、当前状态不允许该操作 |
| 5001 | 依赖暂不可用或该部署不支持 |
| 1 | 其余内部错误；检查 error.msg |

这些路由的请求体 / 查询参数校验失败也返回 1000 信封，而不是 FastAPI 默认 422。
直出 DTO 的域（runtime、telemetry、history、materials.v1、诊断、驱动包）仍用 HTTP 状态码表达失败
（404 / 409 / 422 / 503，正文 `{"detail": ...}`）；Registry 成功走信封，无效 gzip / JSON 等错误返回 HTTP 4xx。

实际成功状态码中的重要例外：

- `POST /workflows`、`/workflows/from-template`、`/workflow-tasks`：**201**，虽离线 OpenAPI 当前标 200。
- `POST /device-processes`：201；删除进程与重置布局成功：204，无 JSON。
- 安装/卸载驱动包、提交全量 reset：202；202 只代表已接受，不代表操作完成。
- 物料创建/更新通常 200；Workflow 删除返回 code=0，并非 204。

HTTP 404 不自动等于“允许新建”；例如 material 不存在、图未登记、Host 路由未装配，
需要分别处理。网络重试只重发同一个幂等请求，不得创建新的实验 attempt 来“重试网络”。

## 身份、版本与分页

| 字段 | 用途 |
| --- | --- |
| material_uuid / site_uuid / template_uuid | 权威分配的实例、位点、模板身份 |
| resource_id / device_id | 设备/资源逻辑标识，可能是可读字符串；不能假设一定符合 UUID 格式 |
| endpoint_uuid / route_uuid | 当前执行端与路由身份 |
| workflow_uuid / task_uuid / node_run_uuid / job_uuid | 定义、一次运行、逻辑节点运行、一次物理执行 |
| revision / control_revision / version | 不同聚合自己的乐观锁版本，不能相互代用 |
| command_uuid + effect_key | 物料命令幂等标识；不是工作流 job 身份 |

`*_at_ms` 是 UTC epoch 毫秒；现有 Workflow 的 `created_at` 等字段可能是时间字符串，
Ping 时间戳是浮点秒。不要把所有时间字段强行按同一单位转换。

Workflow、Graph 等使用 `page/page_size`；runtime/history/telemetry 常用 `limit/offset`
或序号游标；物料若干目录直接返回数组。逐项清单给出每条路由真实参数，不能统一追加分页字段。

## 注册表、图与布局

1. `GET /registry/digest` 获取已持有内容哈希。
2. `POST /resource-templates` 可发送 gzip JSON `RegistryReport`：
   `entries:[{id,content_sha256,payload?}]`，权威缺少的内容才附 payload；检查返回的 `missing`。
3. 条目可能为 active/pending/removed/unusable。使用 `/registry/entries/{name}` 与 `/versions`
   查看差异，`/apply`、`/dismiss`、`/restore/{version}` 才改变生效状态。
4. `GET /registry/workflow-templates` 返回声明式模板，不是已经运行过的工作流。

**哈希增量是“正文增量”，不是“条目列表增量”**：RegistryReport.entries 必须包含 Host 当前全部条目。
缺席的旧条目会被软移除，不能只上报本次改变的一个 entry。missing 要求补正文后仍按完整目录重报。
当前 handler 也接受 `{resources:[...],workflow_templates:[...]}` 全量定义形状；
这不是让 Edge 自动启用旧调度协议。
列表返回 data.templates、data.entries、data.versions、data.impacts 等不同包装，不能都按 data 数组处理。

Registry 目录里的动作 schema、goal_default、placeholder_keys、error_policy、timeout、
execution_timeout、materials_need_lock、supported_backends 是编辑与调度输入。
真正在线能力仍需比对 runtime endpoint，尤其是 Workstation 动态子设备。

设备图用 `/graphs`，工作流图用 `/workflows/{uuid}/graph`，不能混用。
设备图上传支持 `on_existing=replace/adopt`：前端/CLI 编辑通常 replace；启动登记 adopt，
以已有权威物料为准补新增。`GET /graphs/live/payload` 从当前物料及拓扑边生成实时图，
不等同于原始上传快照。布局通过 `/lab/layout` 保存，PUT 使用该布局自己的 revision。

Graph 业务失败与 Workflow 同一张码表：not_found=3002，invalid_input / invalid_payload=1000，
identity_conflict=3003，unsupported=5001，error 为 `{"msg": ...}`。

## Runtime、Telemetry 与 History

| 域 | 读能力 | 写入方与约束 |
| --- | --- | --- |
| runtime sessions/endpoints | 会话、路由、在线动作能力 | 控制面/执行适配器上报，浏览器不伪造 |
| runtime commands/jobs | inbox、执行状态、反馈、error gate | 调度下发与执行端生命周期；前端应提交 workflow-task |
| adapter-commands/backend-events | durable outbox、claim/ack、重投递观测 | 适配器/后台消费者；观察端不要 ack 或 claim 别人的事件 |
| telemetry events/states | 历史遥测与设备 latest 状态 | 设备事件写入；不是工作流成功状态的替代 |
| history payloads/events | 不可变正文、追加历史、replacement-chain | 执行/反馈/人工替换追加；不要覆盖原始失败记录 |
| debug databases/tables | 四库表结构、行数与只读分页 | 运维诊断，不能替代业务 CRUD |

`/runtime/jobs/{uuid}/cancel` 是执行层接口，不应绕开工作流调度自行操作。
当前没有通用的公开 `DELETE /workflow-tasks/{uuid}` 或任务 cancel 路由；不要根据 REST 惯例臆造。

### Runtime 数据面状态机

这组写接口供执行适配器使用，不是数据库表的任意 CRUD：

- `PUT /runtime/sessions/{uuid}`：BackendSessionUpsert，body 与路径一致；
  disconnected 必须带 disconnected_at_ms，event_ack_sequence 不能超过 event_send_cursor。
- `PUT /runtime/endpoints/{uuid}/snapshot`：EndpointSnapshotUpsert，全量替换能力快照，
  避免 partial patch 混入不同 epoch。读取用 ExecutorEndpointRecord；提交结果为 EndpointSnapshotResult。
- `POST /runtime/commands`：CommandEnvelope 幂等接收，不开放随意覆盖已执行命令。
- `/runtime/jobs/{uuid}/transitions|feedback|cancel|error-gate/open|error-gate/decision`：
  通过对应模型推进 ExecutionJobRecord，不提供任意 status PATCH。
- adapter-commands 的 enqueue/claim/ack 与 backend-events 的 enqueue/claim/ack 是独立队列，
  各自携带幂等身份、租约或序号；backend-events/ack 返回 `{acknowledged:数量}`。

Runtime 未找到 404、状态/身份冲突 409、协议验证失败 422，均不是 Workflow code 信封。
部分 handler 直接返回数据库 Record 的公共字段；具体类型见
[Runtime 数据记录](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/database/tables/runtime/data.py)，
并以 handler 的包装/转换为准，不能据此绕过 API 直接写表。
记录中的 state/config/properties 等 JSON 字段不要求客户端使用 SQLite 的 `_json` 列名。

### Telemetry：设备状态与来源游标

`POST /telemetry/events` 接受 TelemetryIngestRequest：protocol_version=telemetry.v1、
event（TelemetryEventWrite）、可选 device_state（DeviceStateSnapshot）。
携带 device_state 时，event.device_uuid 必须存在并与快照的 device_uuid 一致。
event 包含 source_epoch、source_generation、source_sequence、event_uuid，以及 observed_at_ms/received_at_ms。
这些来源顺序用来识别重复或陈旧上报，不能只用接收时间覆盖最新状态。

返回 TelemetryIngestResult：accepted、replayed、event、cursor、device_state。
GET events 返回事件数组；GET states 返回 DeviceStateLatestRecord 数组，
单项以 endpoint_uuid + device_uuid 定位，包含 state、properties、connection_state、alarms、来源序号和 version。
`GET /telemetry/sources/{endpoint_uuid}/cursor` 读来源进度，浏览器不自行写 cursor。
事件类型为 state/property_sample/connection/alarm；数据库最新设备状态与 job 生命周期是两套记录。

### History：正文、事件与替换链

`POST /history/payloads` 的 PayloadWrite 分两种：

| storage_kind | 内容 | 约束 |
| --- | --- | --- |
| inline | media_type、inline_payload 等 | HTTP JSON 中 inline_payload 为 Base64；解码后最多 262,144 字节 |
| external | media_type、byte_length、sha256、external_uri 等 | 不可变外部对象引用；不是让服务端自动下载任意 URI 的命令 |

两者都可带 encoding/compression、created_at_ms/expires_at_ms；返回 PayloadObjectRead。
Python bytes 与 HTTP Base64 要经过协议序列化，不能直接按 UTF-8 文本猜测二进制。

`POST /history/events` 用 HistoryEventAppend 关联 job、endpoint、device、payload、state_version 与来源；
返回 HistoryEventRecord，sequence 由服务器分配。事件类型包含 job_transition/action_availability/
job_feedback/job_result/job_log/error_snapshot/decision_audit。
查询可按 after_sequence、job、设备、类型与时间窗口过滤，详情和 replacement-chain 有独立 GET。
人工替换用 `POST /history/events/{uuid}/replacement`，supersedes_event_uuid 必须与路径相同，
保留 actor 及原始事件；不提供历史事件/正文的通用覆盖或删除。

## 驱动包、受管进程与维护

安装入口 `POST /driver-packages/install` 返回后台 operation；轮询 `/driver-packages/operations/{id}`
直到完成再读取台账、随包 graphs 并 launch。安装成功不等于设备进程已经启动或注册。
启停受管进程用 `/device-processes/{id}/{start,stop,restart}`；在线状态用进程详情 + endpoint 双重确认。

本版卸载 handler 移除包台账、删除受管下载的源码并标记 restart_required；本机原地登记的源码和共用依赖保留。
**该 handler 本身没有保证同步停掉所有相关进程或清理关联物料**，前端若另做编排需独立核对回执，
不能将“包卸载完成”显示成“物料也全部清理完成”。不要因设备暂离线就删除用户物料。

`POST /restart` 接受 `mode=quiescent|immediate`、`scope=auto|edge|process`。
默认权威进程的 auto 只重启 Host，保持管理端口；GET 查询，DELETE 取消尚可取消的重启请求。

`GET /reset` 仅预览。真正全量重置需要 POST 同时提供 confirmation_token 与
`confirmation="清空全部数据"`，仅默认本机分离部署支持。它会停机、关库、归档权威/Host
四库与受管配置，保留驱动包源码；不能在示例脚本或重连流程里自动调用。
