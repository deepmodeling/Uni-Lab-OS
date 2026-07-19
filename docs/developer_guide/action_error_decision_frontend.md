# 动作异常决策：前端接入协议

本文冻结动作执行失败后的前端接口与消息契约。接口风格参考
`feat/edge-networking-and-scheduler` 的 Edge Monitor：REST 提供权威快照和命令入口，SSE
只提供实时增量；前端不维护调度权威，也不直接调用设备。

## 1. 职责边界

| 组件 | 职责 |
|---|---|
| 设备节点 | 执行动作；失败时返回 `suc:false` 和结构化 `error_info` |
| HostNode | 根据 Host 注册表解析策略；持有 pending、超时和重试次数；执行 retry/fallback/skip/abort |
| Host 微后端 | 为本地前端提供 pending 快照、SSE 增量和决策提交接口 |
| 云后端 | 为云端任务接收异常上报、承载用户交互并回传选择 |
| 前端 | 展示 Host/后端提供的选项并提交 `option.action`；不执行 fallback，不自行判定 job 终态 |

决策通道由任务来源决定，不能交叉接管：

- 从本地 `POST /api/v1/job/add` 创建的任务：`micro_backend`。
- 从边云 WebSocket `job_start` 下发的任务：`backend`。

## 2. 完整数据流

### 2.1 Host 微后端模式

```text
Frontend                 Host microbackend        HostNode              Device
   | POST /job/add              |                    | send_goal             |
   |--------------------------->|------------------->|---------------------->|
   |                            |                    |      suc:false         |
   |                            |                    |<----------------------|
   |                            |                    | registry 匹配策略      |
   |                            |                    | 建立 pending + timer   |
   | SSE job_error_decision_required                |                       |
   |<------------------------------------------------|                       |
   | GET /error-decisions       |                    |                       |
   |--------------------------->|------------------->|                       |
   | POST /error-decisions/{id} |                    |                       |
   |--------------------------->|------------------->|                       |
   | SSE job_error_decision_resolved                 |                       |
   |<------------------------------------------------|                       |
   |                            |                    | retry/fallback goal    |
   |                            |                    |---------------------->|
   | SSE job_status / GET /job/{job_id}/status       |       result          |
   |<------------------------------------------------|<----------------------|
```

### 2.2 云后端模式

```text
Device --suc:false--> HostNode --job_error_decision_required--> Cloud Backend
Device <--goal-------- HostNode <--job_error_decision----------- Cloud Backend
Cloud Backend <------------------job_status--------------------- HostNode
```

设备不会主动连接微后端或云后端，也不等待 HTTP/WebSocket 决策。等待状态只存在于 Host。

## 3. Host 微后端接口

默认地址为 `http://<host-ip>:8002`，OpenAPI 位于 `/api/docs`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/error-decisions` | 当前本地 pending 的权威列表 |
| POST | `/api/v1/error-decisions/{decision_id}` | 提交一次决策 |
| GET | `/api/v1/monitor/events?channels=action&backlog=40` | SSE 实时增量与有限回放 |
| GET | `/api/v1/monitor/snapshot` | 初始化及 SSE 丢事件后的权威快照 |
| GET | `/api/v1/job/{job_id}/status` | 查询原 job 当前状态或最终结果 |

## 4. 异常报告结构

`job_error_decision_required` 和 `GET /error-decisions` 使用同一结构：

```json
{
  "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
  "job_id": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
  "task_id": "3f39b087-aec2-4b76-b31d-a3da277e7ec1",
  "device_id": "pump-1",
  "action_name": "transfer",
  "exception_type": "CommunicationError",
  "category": "network",
  "severity": "error",
  "error_message": "serial port closed",
  "traceback": "Traceback ...",
  "options": [
    {
      "action": "retry",
      "label": "重试"
    },
    {
      "action": "reset_connection",
      "label": "重置连接",
      "description": "重置设备连接后结束本次人工干预",
      "fallback_action": {
        "action_name": "reset",
        "params": {"channel": 2}
      }
    },
    {
      "action": "skip",
      "label": "跳过"
    },
    {
      "action": "abort",
      "label": "终止"
    }
  ],
  "retry_count": 0,
  "max_retries": 2,
  "created_at": 1786440000.0,
  "decision_timeout_seconds": 300.0,
  "expires_at": 1786440300.0,
  "default_on_decision_timeout": "abort",
  "require_confirmation": true
}
```

字段规范：

| 字段 | 必需 | 前端含义 |
|---|---:|---|
| `decision_id` | 是 | 决策唯一键；POST 路径参数 |
| `job_id` / `task_id` | 是 | 关联原任务；最终状态仍按原 `job_id` 查询 |
| `device_id` / `action_name` | 是 | 展示和日志定位，不作为前端执行地址 |
| `exception_type` | 是 | 异常类名 |
| `category` / `severity` | 否 | 设备异常提供时透传 |
| `error_message` | 是 | 面向用户的简要错误 |
| `traceback` | 是 | 调试详情；默认折叠，不建议直接 toast 全文 |
| `options` | 是 | Host 从注册表匹配出的唯一合法选择集合 |
| `retry_count` / `max_retries` | 是 | 当前已重试次数和上限 |
| `created_at` / `expires_at` | 是 | Unix 秒；用于展示倒计时 |
| `default_on_decision_timeout` | 是 | 到期后 Host 自动执行的动作 |

前端必须以 `option.action` 为稳定值，`label/description` 只用于展示。
`fallback_action` 是只读说明，浏览器不得调用其中的设备动作或修改参数。

## 5. REST 示例

### 5.1 初始化或断线恢复

```http
GET /api/v1/error-decisions HTTP/1.1
Accept: application/json
```

```json
{
  "decisions": [
    {
      "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
      "job_id": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
      "device_id": "pump-1",
      "action_name": "transfer",
      "exception_type": "CommunicationError",
      "error_message": "serial port closed",
      "options": [{"action": "retry", "label": "重试"}],
      "retry_count": 0,
      "max_retries": 2,
      "created_at": 1786440000.0,
      "expires_at": 1786440300.0,
      "decision_timeout_seconds": 300.0,
      "default_on_decision_timeout": "abort",
      "require_confirmation": true,
      "traceback": "Traceback ...",
      "task_id": "3f39b087-aec2-4b76-b31d-a3da277e7ec1"
    }
  ]
}
```

### 5.2 提交 retry/skip/abort

```http
POST /api/v1/error-decisions/8a714f4c-5bb0-47b7-9245-9ddf907ef8d4
Content-Type: application/json

{"action":"retry","reason":"operator confirmed"}
```

成功只表示 Host 接受了命令，不表示恢复动作已经成功：

```json
{
  "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
  "status": "delivered"
}
```

提交注册表自定义选项时，仍只传稳定 action：

```json
{
  "action": "reset_connection",
  "reason": "operator selected registered recovery"
}
```

如果选项要求人工给出替代结果，可附加 `result`：

```json
{
  "action": "manual_result",
  "result": {"confirmed_volume": 10.0},
  "reason": "verified on instrument"
}
```

错误语义：

- `404`：不存在、已被其他请求处理、已经超时，或通道来源不匹配。
- `503`：HostNode 尚未就绪。
- 第一次合法决策获胜；前端收到 `404` 时重新 GET 列表。若列表中已不存在该 ID，关闭弹窗并继续追踪原 job。

### 5.3 查询原 job

```http
GET /api/v1/job/df958dcb-b2bf-4a48-94a2-81410bf95a6b/status
```

等待决策、retry 或 fallback 执行期间，状态保持 `2`：

```json
{
  "code": 0,
  "data": {
    "jobId": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
    "status": 2,
    "result": {}
  },
  "message": "success"
}
```

状态码：`0 UNKNOWN`、`1 ACCEPTED`、`2 EXECUTING`、`3 CANCELING`、
`4 SUCCEEDED`、`5 CANCELED`、`6 ABORTED`。

成功终态的 `result.suc_type`：

- `normal`：原动作或 retry 正常成功。
- `skip`：人工选择跳过；调度可继续，但物料侧应进入复核/隔离流程。
- `operator_intervention`：fallback 或人工替代结果成功。

## 6. SSE 事件流

连接：

```text
GET /api/v1/monitor/events?channels=action&backlog=40
Accept: text/event-stream
```

每一帧与参考分支 MonitorBus 一致：

```text
id: 17
event: action
data: {"seq":17,"ts":1786440000.0,"channel":"action","type":"job_error_decision_required","data":{...report...},"trace_id":"","span_id":""}

```

当前 action 事件类型：

| `type` | `data` | 前端动作 |
|---|---|---|
| `job_error_decision_required` | 完整异常报告 | 按 `decision_id` upsert 弹窗/通知 |
| `job_error_decision_resolved` | ID、job、选择、原因、时间 | 移除 pending，锁定本次操作 |
| `job_status` | 与边云 `job_status.data` 同形状 | 更新 job 的 running/success/failed 投影 |

`job_error_decision_resolved.data` 示例：

```json
{
  "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
  "job_id": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
  "task_id": "3f39b087-aec2-4b76-b31d-a3da277e7ec1",
  "device_id": "pump-1",
  "action_name": "transfer",
  "selected_action": "retry",
  "reason": "operator confirmed",
  "resolved_at": 1786440020.0
}
```

SSE 是增量通知，不是权威数据库：

1. 页面启动先 GET `/monitor/snapshot` 或 `/error-decisions`。
2. 再建立 EventSource，并用 `addEventListener("action", ...)` 接收命名事件。
3. 保存最近 `seq`；忽略 `seq <= lastSeq` 的回放重复帧。发现向前跳号、浏览器重连或页面恢复可见时，重新 GET snapshot。
4. 慢消费者可能丢事件，Host 执行不会被 SSE 反压。
5. SSE 约每 15 秒发送 keepalive，浏览器按 `retry: 3000` 自动重连。

Snapshot 示例：

```json
{
  "now": 1786440010.0,
  "host_ready": true,
  "pending_error_decisions": [],
  "recent": {
    "action": []
  }
}
```

## 7. TypeScript 接入示例

```ts
type ErrorOption = {
  action: string;
  label: string;
  description?: string;
  fallback_action?: {
    action_name: string;
    params?: Record<string, unknown>;
  };
};

type ErrorDecision = {
  decision_id: string;
  job_id: string;
  task_id: string;
  device_id: string;
  action_name: string;
  exception_type: string;
  category?: string;
  severity?: string;
  error_message: string;
  traceback: string;
  options: ErrorOption[];
  retry_count: number;
  max_retries: number;
  created_at: number;
  expires_at: number;
  decision_timeout_seconds: number;
  default_on_decision_timeout: "abort" | "retry" | "skip";
  require_confirmation: true;
};

type MonitorEvent = {
  seq: number;
  ts: number;
  channel: "action";
  type:
    | "job_error_decision_required"
    | "job_error_decision_resolved"
    | "job_status";
  data: Record<string, unknown>;
  trace_id: string;
  span_id: string;
};

const baseUrl = "http://127.0.0.1:8002";
const pending = new Map<string, ErrorDecision>();
let lastSeq = 0;

async function refreshDecisions() {
  const response = await fetch(`${baseUrl}/api/v1/error-decisions`);
  if (!response.ok) throw new Error(`decision snapshot ${response.status}`);
  const body = (await response.json()) as { decisions: ErrorDecision[] };
  pending.clear();
  for (const decision of body.decisions) {
    pending.set(decision.decision_id, decision);
  }
  renderDecisionCenter([...pending.values()]);
}

function connectMonitor() {
  const source = new EventSource(
    `${baseUrl}/api/v1/monitor/events?channels=action&backlog=40`,
  );

  source.addEventListener("action", async (message) => {
    const event = JSON.parse((message as MessageEvent).data) as MonitorEvent;
    if (lastSeq !== 0 && event.seq <= lastSeq) return;
    if (lastSeq !== 0 && event.seq > lastSeq + 1) {
      await refreshDecisions();
    }
    lastSeq = event.seq;

    if (event.type === "job_error_decision_required") {
      const decision = event.data as unknown as ErrorDecision;
      pending.set(decision.decision_id, decision);
      renderDecisionCenter([...pending.values()]);
    } else if (event.type === "job_error_decision_resolved") {
      pending.delete(String(event.data.decision_id));
      renderDecisionCenter([...pending.values()]);
    } else if (event.type === "job_status") {
      updateJobProjection(event.data);
    }
  });

  source.onerror = () => {
    // EventSource 会自动重连；恢复后仍应以 REST snapshot 校准。
    // 服务进程重启时 seq 会从 1 重新开始，因此不能沿用旧连接的 lastSeq。
    lastSeq = 0;
    void refreshDecisions();
  };
  return source;
}

async function resolveDecision(
  decision: ErrorDecision,
  action: string,
  result?: unknown,
) {
  if (!decision.options.some((option) => option.action === action)) {
    throw new Error("option is not allowed by Host registry policy");
  }
  const response = await fetch(
    `${baseUrl}/api/v1/error-decisions/${decision.decision_id}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action,
        ...(result === undefined ? {} : { result }),
        reason: "operator confirmed",
      }),
    },
  );
  if (response.status === 404) {
    await refreshDecisions();
    return;
  }
  if (!response.ok) throw new Error(`resolve decision ${response.status}`);
}

void refreshDecisions().then(connectMonitor);
```

`renderDecisionCenter` 和 `updateJobProjection` 是前端自己的 store/UI 适配点。

## 8. 云后端 WebSocket 契约

Host → Backend：

```json
{
  "action": "job_error_decision_required",
  "data": {
    "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
    "job_id": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
    "task_id": "3f39b087-aec2-4b76-b31d-a3da277e7ec1",
    "device_id": "pump-1",
    "action_name": "transfer",
    "exception_type": "CommunicationError",
    "error_message": "serial port closed",
    "options": [{"action": "retry", "label": "重试"}],
    "retry_count": 0,
    "max_retries": 2,
    "created_at": 1786440000.0,
    "expires_at": 1786440300.0,
    "decision_timeout_seconds": 300.0,
    "default_on_decision_timeout": "abort",
    "require_confirmation": true,
    "traceback": "Traceback ..."
  }
}
```

Backend → Host：

```json
{
  "action": "job_error_decision",
  "data": {
    "decision_id": "8a714f4c-5bb0-47b7-9245-9ddf907ef8d4",
    "job_id": "df958dcb-b2bf-4a48-94a2-81410bf95a6b",
    "device_id": "pump-1",
    "action": "retry",
    "reason": "operator confirmed"
  }
}
```

云前端不应直接连接 Host 的异常决策 REST；它只消费云后端持久投影并向云后端提交选择。
云后端必须原样保留 `decision_id/job_id/device_id`，回包时三者共同校验。

## 9. 前端状态机

```text
absent
  └─ required/snapshot ─> pending
                         ├─ POST 中 ─> resolving（按钮禁用）
                         ├─ resolved ─> tracking_job
                         └─ expires_at 到达 ─> refresh snapshot
tracking_job
  ├─ job status=2 ─> tracking_job
  └─ job status=4/5/6 ─> terminal
```

关键不变量：

1. `decision_id` 是弹窗/通知的唯一键，不能用 `device_id` 去重。
2. pending 期间原 job 仍为执行中，不能先标记 failed。
3. POST `delivered` 不是 job 成功，只是 Host 已接受选择。
4. retry 使用新的 ROS transport goal UUID，但前端始终追踪原 `job_id`。
5. 只允许提交报告 `options` 中的 action；最终合法性仍由 Host 校验。
6. Host 超时是权威；浏览器倒计时归零后只刷新，不自行执行默认动作。
7. fallback 由 Host 通过 ActionClient 发给真实设备，浏览器绝不调用设备 Service/Action。

## 10. 前端验收清单

- 页面刷新后能通过 REST 恢复已有 pending。
- 新异常通过 SSE 在不刷新页面时出现。
- 同一 `decision_id` 的 snapshot/SSE 重复消息只产生一个 UI 项。
- 点击后立即禁用按钮，成功响应后继续追踪原 job。
- POST 响应丢失后再次提交得到 404，前端通过 snapshot 正确收敛。
- SSE `seq` 出现空洞时重新拉 snapshot。
- timeout、另一个浏览器先处理、云/本地通道错投时不会重复执行。
- retry/fallback 成功后展示 `suc_type`；skip 明确提示需要物料复核。
- traceback 默认折叠，错误摘要、设备、动作、重试次数和倒计时默认可见。
