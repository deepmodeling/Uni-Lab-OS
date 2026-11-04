# 实时通知、日志、控制面与 HostLink

## 连接哪个通道

| 使用者 | 通道 | 内容 |
| --- | --- | --- |
| 浏览器工作流页、日志页 | SSE `/api/v1/events` | 持久工作流变更 + 瞬时日志失效通知 |
| 浏览器物料库、地图 | SSE `/api/v1/materials/events` | 物料账本变更通知 |
| Host ↔ 微后端/上游 Backend | WS `/api/v1/ws/schedule` + HTTP | runtime.v1 控制、命令正文与事件回收 |
| Host ↔ Slave | HostLink TCP，默认 7302 | 握手、路由、RPC、topic、物料投影、日志 |
| 本机 AI 客户端 | Streamable HTTP `/mcp` | 工具调用，不是上述浏览器事件流 |

管理 HTTP 示例端口为 8003；HostLink 端口独立，不能把 TCP 地址交给 WebSocket 客户端。
**浏览器不要连接 `/ws/schedule`**：本版 EdgeControlService 只有一个活动执行控制连接，
新连接会更换 connection_epoch，使旧连接退出。它不是可供多个前端订阅的广播服务。

## 浏览器 SSE 契约

两个 SSE 都是 GET，Content-Type 为 `text/event-stream`，
响应包含 `Cache-Control: no-cache`、`X-Accel-Buffering: no`，重连建议 `retry: 3000`。
服务端开始停机时主动结束流，前端重连并重新校准 HTTP 状态。

| 项目 | `/api/v1/events` | `/api/v1/materials/events` |
| --- | --- | --- |
| 无 Last-Event-ID 首连 | 固定当前最新序号，先发只含 `id` 的水位帧，仅推之后变化 | 固定当前账本末尾，仅推之后变化 |
| 持久 SSE id | 工作流事件 id | inventory ledger sequence |
| 恢复方式 | Last-Event-ID，从该 id 之后重放；显式 `0` 回放全部保留历史 | Last-Event-ID，从该 sequence 之后重放 |
| 游标大于最新序号 | `stream.reset`（`reason: cursor_ahead`，`id` 为新水位），连接保持 | 无 |
| 非法游标 | Workflow code=1000，HTTP 200 JSON | HTTP 422 |
| 无事件时 | keepalive 注释；工作流检查约 1 秒，心跳约 15 秒 | keepalive 注释；约 1 秒检查账本 |

三个游标相互独立：工作流 id、物料 sequence、日志 stream:offset。不能混用，也不能以最后日志通知替换工作流游标。
后端内部检查账本不等于要求浏览器每秒 GET 全量数据；前端维持事件流，仅在失效时拉取。

### 物料通知

```text
id: 42
event: materials.changed
data: {"sequence":42,"operation":"update_position","aggregate_type":"material","aggregate_uuid":"..."}

```

通知只说明什么发生了变化，不带完整树、液体或模板正文。
初次应**先建立 SSE，在 onopen 后读取 HTTP 快照**。重连同样重新读快照，
避免“首次取数据 → 开始订阅”之间的变更遗漏。

下面是事件驱动、串行合并刷新示例，不执行写操作：

```javascript
function subscribeMaterials(baseUrl, render, reportError) {
  let closed = false, dirty = false, running = false;
  const events = new EventSource(`${baseUrl}/api/v1/materials/events`);

  async function refresh() {
    dirty = true;
    if (running || closed) return;
    running = true;
    try {
      while (dirty && !closed) {
        dirty = false;
        const response = await fetch(`${baseUrl}/api/v1/materials/instances`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const materials = await response.json(); // 直出列表，不是 code 信封
        if (!closed) render(materials);
      }
    } catch (error) {
      if (!closed) reportError(error); // UI 提供重试；重连也会重新校准
    } finally {
      running = false;
    }
  }

  events.onopen = refresh;
  events.addEventListener("materials.changed", refresh);
  events.onerror = () => { if (!closed) reportError(new Error("事件流重连中")); };
  return () => { closed = true; events.close(); };
}
```

每个页面维护自己的生命周期，不要切换组件时不断新建而不关闭 EventSource。
对大量物料可按 aggregate_uuid 精确失效，但删除、挂载和模板变化可能影响多个视图，
需要同时刷新物料树、仓储与相关 Site，不能只更新列表中的一个名称。
原生 EventSource 的自动重连会维护当前实例的 Last-Event-ID；新建实例不能假设继承旧游标。

### 工作流通知

订阅语义对齐 v1.0 ADR 0006：通知只表示 REST 状态失效，新页面在 `onopen` 后读 REST，
不需要回放历史。首连的水位帧（只有 `id: N`，没有 `event` / `data`）只用于断线续传，
不触发业务刷新。收到 `stream.reset` 时游标可能回退（例如数据库被重置），客户端必须
重读 REST，不能把它当成某个任务的业务变更；该帧不写入 `frontend_event`。
需要历史时显式发送 `Last-Event-ID: 0`。

持久事件包括 `workflow.task.changed`、`workflow.node_run.changed`、
`workflow.node_job.changed`、`workflow.manual_confirmation.changed` 等。
data 为相应对象身份、状态等失效信息；例如 node job 事件带 workflow_node_job_uuid、
workflow_node_run_uuid、attempt_no、status。未知事件名应安全忽略或触发相关 HTTP 校准，不能让整个流崩溃。
有 `event:` 名称的 SSE 必须用 addEventListener 注册；仅设置 onmessage 接不到这些命名事件。

## Host / 本机和远端 Slave 日志

1. GET `/api/v1/hostlink/log-sources` → `RuntimeLogSources {sources:[...]}`。
2. 选择 source_id，GET `/api/v1/hostlink/logs?source_id=...&limit=300` 获取尾部。
3. 保留返回 cursor，订阅 `/events` 的 `runtime.logs.changed`。
4. 通知涉及当前 source 或 all_sources=true 时带 cursor 增量拉取；has_more=true 则继续串行读到追平。
5. sources_changed=true 时重读源列表；SSE 重连后重新校准源列表和当前日志。

| 模型 | 字段 |
| --- | --- |
| RuntimeLogSource | source_id、name、role(host/slave)、machine_name、node_id、pid、device_ids、online、managed、supported、detail |
| RuntimeLogBatch | source_id、stream_id、cursor、lines、has_more、reset、truncated、path、pid |
| RuntimeLogLine | offset（文件内字节位置）、text |
| RuntimeLogNotice | source_ids、sources_changed、all_sources |

cursor 是不透明字符串，当前形态为 24 位十六进制 stream id 加冒号和偏移量；
limit 默认 300，范围 1–1000。offset 不是时间，也不能跨文件排序。
reset 表示文件轮转/进程重启等导致游标重新定位；truncated 表示历史缺口，应在界面提示，不能伪装日志连续。
无此源返回 404，暂不可用返回 503（可带 Retry-After）。

日志通知**没有 SSE id、没有日志正文、不落持久工作流事件表**。
突发追加会合并通知，所以通知数量不等于新增行数。日志读取不消费数据，两个页面应各自持有 cursor。

传递链路：Slave 文件追加 → `process.log.changed` 空通知 → Host → runtime.v1
`runtime_logs_changed` → 权威 SSE `runtime.logs.changed` → 浏览器 HTTP 拉正文。
远端日志由 Host 经 `process.log.read` 读取；source_id 指定逻辑源，浏览器不能传任意文件路径。
控制台中 EdgeControl 的调度日志不能代替 Slave 的驱动日志。

## runtime.v1：上游 Backend 与执行 Host 的控制合同

WebSocket 消息统一为 `{"action":"...","data":{...}}`。不是 HostLink 的 req/resp 信封。

| action | 方向 | data 模型 / 含义 |
| --- | --- | --- |
| backend_session | Backend → Host | BackendSessionNotice：session_uuid、edge_uuid、authority_epoch、connection_epoch |
| backend_change | Backend → Host | BackendCommandNotice：命令身份、类型、序号、内容哈希；Host 再拉正文 |
| edge_change | Host → Backend | EdgeChangeNotice：持久事件身份、序号、聚合、可选详情 payload UUID |
| edge_change_ack | Backend → Host | EdgeChangeAck：session_uuid、through_sequence |
| ping / pong | 应用层心跳 / 回显 | ping_id、client_timestamp；pong 增加 server_timestamp（浮点秒） |
| runtime_logs_changed | Host → Backend | RuntimeLogNotice，瞬时合并日志通知 |
| backend_http | Backend → Host | BackendHttpRequest，Host 专属 HTTP 的内部转发 |

除 Ping/Pong/日志模型外，上述对应控制模型声明 protocol_version=runtime.v1。
数据与控制源应整体切换，不独立协商一套“只换 WS 不换数据库权威”的版本。

### 命令下发

1. Backend 发送 backend_session 建立 epoch 上下文。
2. 发送 backend_change，包含 notice_uuid、command_uuid、command_type、backend_sequence、
   session/edge/epoch、content_sha256、occurred_at_ms。
3. Host HTTP GET **`/edge/commands/{command_uuid}`**（注意没有 `/api/v1` 前缀）。
4. 获得 `BackendCommandDocument {protocol_version, command:CommandEnvelope, payload}`；
   执行端校验身份/版本/正文，经 inbox 准入后执行，不能只凭通知就执行一次。
5. 生命周期和结果先记 runtime/history，再 edge_change 通知 Backend 取详情；ACK 仅推进对应 session 的连续序号。

| command_type | payload |
| --- | --- |
| execute_job | ExecuteJobContent |
| cancel_job | CancelJobContent |
| release_failed / replace_result / resume_pending | ErrorDecisionContent |
| inventory_apply / reconcile | 由对应 runtime 数据面处理的命令正文，见 CommandEnvelope 与协议模型 |

ExecuteJobContent 将 task/node/job/attempt 身份与实际 device/action 关联：
job_uuid、task_uuid、node_uuid、attempt_group_uuid、retry_of_job_uuid、attempt_no、attempt_trigger、
retry_count、device_uuid、action_name/type、action_args、scheduler_revision、timeouts、material_bindings、
materials_need_lock、inventory_requirements、inventory_reservation_uuid。
route_uuid、endpoint_uuid、transport 必须一起设置或一起省略；不能只填 transport 来猜目标。
数据面字段及默认值见 [Runtime 数据协议](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/data.py)，
控制面字段、枚举和跨字段规则见 [Runtime 控制协议](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/control.py)。

session/epoch 用于区分旧连接与当前权威，不是用户名；业务幂等必须落到 command/job/event 身份。
本版本有持久 inbox/outbox，但控制服务的部分命令缓存在内存，
不能据此承诺任意进程崩溃下端到端 exactly-once 或自动恢复所有未拉取命令。
执行结果未知应走 reconcile/干预，不能“断线即重做”。

### Host 不监听 HTTP：请求中继

权威为 Host 专属 API 创建 BackendHttpRequest：
request_uuid、method、path（含 query）、headers、body_base64、timeout_seconds（默认 60）。
Host 在本进程 ASGI 应用执行后出站 POST
`/api/v1/edge/http-responses/{request_uuid}`，body 为 EdgeHttpResponse，含 status_code、headers、body_base64。
路径 request_uuid 必须等于 body，否则 422；成功返回 `{accepted: bool}`。

**这是“WS 只带轻通知”的现存例外**：backend_http 当前会携带 base64 请求 body，
不能把该版本写成“所有 WS 消息绝不携带正文”。浏览器仍仅请求一个管理地址。

离线 OpenAPI 导出器未包含或隐藏的内部 HTTP 路由：

| 接口 | 请求 / 返回 |
| --- | --- |
| GET /edge/commands/{command_uuid} | BackendCommandDocument；不存在 404、控制服务未就绪 503 |
| POST /api/v1/edge/http-responses/{request_uuid} | EdgeHttpResponse → `{accepted}` |
| POST /api/v1/hostlink/material-sync | MaterialDeviceSync → 设备 resource service 同步结果 |
| POST /api/v1/hostlink/notify-device | ResourceTreeNotify → ResourceTreeNotifyResult |

后两条只用于权威→Host 的设备投影中继，普通前端使用 materials 公开业务接口。
这些路由与 `/mcp`、WS 需单独阅读，不能仅凭 163 条 OpenAPI 操作推断全部通道。

## HostLink TCP 协议

普通帧是一行 UTF-8 JSON，末尾换行：

```json
{"v":1,"kind":"req","id":"请求唯一ID","action_type":"material.tree.get","data":{"root_material_uuid":"实际UUID"}}
```

响应复用 id：成功 `{v:1,kind:"resp",id,ok:true,data:...}`；
失败 `{v:1,kind:"resp",id,ok:false,error:"...",error_info:{...}}`。
error_info 保留 exception_type、exception_mro、error_message、traceback，必要时 category/severity，
使远端异常仍能匹配动作错误策略。此请求 id 是 RPC 关联号，不是物料 command_uuid。

单帧最多 8 MiB，整消息最多 256 MiB；超过单帧时使用 chunk：
一行 `{v:1,kind:"chunk",id,seq,n,len}` 头，后接 len 字节原始 JSON 片段，
连续发送同一消息各片，接收端重组后交给 req/resp handler。
不能把每片原始字节再 JSON 转义，也不能将多个消息片段交错发送。

### 握手与身份

先 hello，再发布设备能力和 topic。hello 携带 machine_name、node_id、role、
protocol_version、capabilities、device_ids/devices 等；响应有 assigned_node_id、server_time、heartbeat_timeout。
当前 peer 身份实际以 **machine_name** 为准，node_id 与之对应，不根据设备列表推断。
同一个 HostLink 服务上的两个活跃 peer 不能共用 machine_name，Slave 同样受此约束；
同机启动多进程应显式提供不同名称，不能把操作系统机器名当成所有进程的唯一身份。
未注册的发布设备会被拒绝；endpoint 的总数量不能证明某个连接有权发布 material_bench 的 topic。

### ActionType 全集与 payload 入口

以下列出固定动作族；动态驱动动作的 arguments/结果由注册表 schema 决定。
未使用独立 Pydantic DTO 的帧以 handler 为准，不承诺保留任意未知字段。

| action_type | data 要点 |
| --- | --- |
| hello / ping / ros_info | 身份、设备能力/状态、ROS 组网信息；与 runtime.v1 的 WS ping 不同 |
| process.log.read | RuntimeLogQuery：cursor、limit |
| process.log.changed | **空对象**，身份取已注册连接，不接收正文或别人的身份 |
| material.template.list | name 可选，include_definition 默认 false |
| material.template.create | InventoryMutation，ResourceTemplateWrite payload |
| material.create | InventoryMutation，MaterialTreeCreate payload |
| material.tree.get | root_material_uuid |
| material.resource-id.get | resource_id |
| material.search | name |
| material.data.put | InventoryMutation，另在信封同层提供 material_uuid；payload=MaterialDataWrite |
| material.move / material.transfer / material.delete | InventoryMutation + 对应 MaterialMove/Transfer/Delete payload |
| material.snapshot.compare | MaterialSnapshot，直接传模型 |
| material.snapshot.apply / material.delta.apply | InventoryMutation + MaterialSnapshot/MaterialDelta |
| resource.tree.sync | device_id、operations（action/data 的操作列表） |
| resource.append | device_id、resource_uuid 列表、bind_parent_id、bind_location、other_calling_param |
| material.sync | MaterialDeviceSync：device_id、transfer_uuid、action、material_uuids、destination_site_uuids |
| device.manage | device_id、action(add/remove)、data（设备配置） |
| device.call | device_id、action、arguments、action_id；跨 peer 调用含 caller_device_id |
| device.state | device_id；状态上报可含 state/states，查询由对应方向 handler 处理 |
| service.call | service、request、caller_device_id |
| action.feedback | action_id、feedback |
| action.cancel | action_id；跨 peer 调用含 caller_device_id |
| topic.publish / topic.deliver | event: TopicEvent |
| topic.subscribe / topic.unsubscribe | topic |

TopicEvent 为 topic、value、publisher_device_id、message_type、message_id、published_at、retain。
相对 topic 在设备命名空间下规范为 `/devices/{device_id}/{topic}`。
JSON 消息转换位于 `backend/hostlink/topic.py`，并不是所有 backend 的公共序列化接口。

HostLink 支持 Python 驱动动作、普通 service/topic、Workstation/sub-device 初始化与物料通信；
**不支持 MoveIt、RViz，也不承诺 ROS2 原生 QoS/高频图像流的等价性**。依赖这些功能的设备保持 ROS2 backend。
ROS2 模式仍复用 HostLink 进行组网辅助和物料/管理下行。

## 源码依据

- [runtime.v1 控制模型](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/protocol/runtime/control.py)
- [WS 与命令 HTTP](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/control.py)
- [SSE](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/server/api/runtime/events.py)
- [HostLink framing / ActionType](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/backend/hostlink/protocol.py)
- [HostLink handlers](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/backend/hostlink/backend.py)
- [物料/管理下行](https://github.com/deepmodeling/Uni-Lab-OS/blob/862ce87a38222ac2d51739f2f89e5daecd3cd907/unilabos/backend/hostlink/downlink.py)
