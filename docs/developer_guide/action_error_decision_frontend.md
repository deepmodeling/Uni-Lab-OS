# 动作失败决策与调度恢复契约

## 1. 权威边界

| 组件 | 职责 |
|---|---|
| 设备 | 执行动作并返回原始成功或失败结果 |
| Host | 暂存有 `error_policy` 的失败；只在收到后端 release 后发布终态 |
| 调度后端 | 持久化失败，询问前端，更新调度图/attempt，并向 Host release |
| 前端 | 展示注册表声明的选项并把选择提交给调度后端 |

Host 不执行 retry、skip 或 fallback，也不自行应用超时默认策略。人工干预是唯一可以在
Host result boundary 替换有效结果的选择。

## 2. 时序

```text
Device             Host                 Scheduler Backend          Frontend
  |-- raw failed -->|                            |                    |
  |                 |-- decision_required ----->|                    |
  |                 |     (暂不报 failed)        |-- ask ----------->|
  |                 |                            |<-- choice ---------|
  |                 |                            | update schedule     |
  |                 |<-- decision release ------|                    |
  |                 |-- job_status failed ----->|                    |
```

`retry` 时，后端在 release 前为同一 `node_id` 创建新的 attempt/job；旧 job 仍由 Host
如实上报 failed。`skip`、`abort`、fallback/补偿也由后端更新调度，Host 不创建新 goal。
本机调度（默认 profile）下"后端"就是同进程的 Workflow Authority：它在同一事务里把失败
attempt 记 failed、为同一节点运行追加 `attempt_no+1` 的新 job，并保持节点运行为
pending；节点运行（`node_run_uuid`）是画布节点的稳定身份，attempt 是它的历史。

## 3. Host → Backend

消息：`job_error_decision_required`

```json
{
  "action": "job_error_decision_required",
  "data": {
    "decision_id": "decision-uuid",
    "task_id": "workflow-run-id",
    "node_id": "logical-node-id",
    "node_run_uuid": "node-run-uuid（≡ attempt_group_uuid，本机调度时给出）",
    "job_id": "attempt-job-id",
    "device_id": "pump-1",
    "action_name": "transfer",
    "exception_type": "CommunicationError",
    "error_message": "serial port closed",
    "options": [
      {"action": "retry", "label": "重试"},
      {"action": "operator_intervention", "label": "人工替代结果"},
      {"action": "abort", "label": "终止"}
    ],
    "retry_count": 0,
    "max_retries": 2,
    "decision_timeout_seconds": 300,
    "default_on_decision_timeout": "abort"
  }
}
```

Host 断线期间保留 pending，WebSocket 重连后按同一个 `decision_id` 重放。后端必须幂等
upsert。没有 `error_policy` 或没有后端 bridge 时，Host 直接上报原始 failed。

## 4. Backend → Host

消息：`job_error_decision`

```json
{
  "action": "job_error_decision",
  "data": {
    "decision_id": "decision-uuid",
    "job_id": "attempt-job-id",
    "device_id": "pump-1",
    "action": "retry",
    "reason": "operator confirmed",
    "scheduler_updated": true
  }
}
```

`scheduler_updated` 必须严格为 `true`。缺失或为 false 时 Host 保持 pending，不得提前
发布 failed。`decision_id + job_id + device_id` 必须与 pending 完全一致；第一次合法 release
获胜，重复 release 只命中短期 tombstone，不重复发布终态。

除人工干预外，无论选择 retry、skip、abort 还是 fallback，Host 都发布原始 failed，并在
`return_info.error_resolution` 中附加：

```json
{
  "decision_id": "decision-uuid",
  "selected_action": "retry",
  "reason": "operator confirmed",
  "scheduler_updated": true
}
```

## 5. 人工干预

人工替代必须显式选择 `operator_intervention` 并携带 `result` 或 `return_value`：

```json
{
  "decision_id": "decision-uuid",
  "job_id": "attempt-job-id",
  "device_id": "pump-1",
  "action": "operator_intervention",
  "result": {"confirmed": true},
  "scheduler_updated": true
}
```

Host 将 effective result 上报为 success，`suc_type=operator_intervention`，同时在
`result_data.raw_return_info` 保留不可变的设备原始失败。人工操作如果还需要真实设备动作，
后端必须另建 attempt 并正常调度，不能利用本消息让 Host 偷跑动作。

## 6. Edge REST/SSE

- `POST /api/v1/job/add` 固定返回 HTTP 409；动作只接受调度后端 WebSocket `job_start`。
- `GET /api/v1/error-decisions` 仅用于只读诊断 Host pending。
- `POST /api/v1/error-decisions/{decision_id}` 固定返回 HTTP 409；前端必须向调度后端提交。
- Edge SSE 可观测 `job_error_decision_required`、`job_error_decision_resolved` 和最终
  `job_status`，但不是决策写入口。

本机调度（默认 profile）时调度后端就是同一进程，上述两个 `/error-decisions` 接口即是
决策的读写入口。除执行面挂起的失败 attempt 外，清单还包含进程重启后执行态未知的
attempt（`exception_type=ExecutionStateUnknown`，选项 `retry` / `skip` /
`operator_intervention` / `abort`），提交方式相同，见
`scheduling_and_execution.md` §4.1 的任务恢复部分。

## 7. 注册表

每个 action 的 completion 固定包含 `error_policy`；未配置时为 `{}`。策略由后端用于前端
展示、retry 上限、超时默认动作和 fallback 调度，Host 只负责按异常 MRO 选择并上报对应
option 列表。

## 8. 超时闸门：`timeout` 与 `execution_timeout`

`@action(timeout=..., execution_timeout=...)`（见 `add_device.md` §动作方法）由执行面
`JobExecutionBackend` 在动作真正下发后启动看门狗，两者都进入本文的决策链，但语义不同：

| | `timeout`（硬超时） | `execution_timeout`（业务软超时） |
|---|---|---|
| 声明 | 正数秒 | 正数秒，或引用动作入参的四则运算表达式，如 `"duration * 1.5 + 30"` |
| 求值 | 声明即定 | 调度器派发前按**最终** `action_args`（叠加注册表 `goal_default`）求值；节点 `execution_policy.execution_timeout_seconds`（正整数）显式声明优先 |
| 到期动作 | 协作式取消动作（`cancel_goal`；HostLink 本地运行时同时以 `asyncio.wait_for` 真正取消协程动作） | **不取消**，动作继续执行 |
| 报告 | `exception_type=TimeoutException`，`category=timeout`，`severity=error`，`timeout_kind=timeout` | `exception_type=ExecutionTimeoutException`，`severity=warning`，`timeout_kind=execution_timeout`，`action_still_running=true` |
| 选项 | 与普通失败相同（`error_policy` 按异常类名匹配；缺省 `retry` / `abort` / `operator_intervention`） | 额外前置一个 `wait`（继续等待，按同样秒数重新计时）；其余选项先取消动作再按失败放行 |
| 设备迟到的结果 | 不改变已结算的 attempt，记入 `control_data.late_outcome`，并释放该 attempt 仍持有的锁（见下文） | 优先于待决策：决策被自动收回（`selected_action=superseded`），真实结果照常放行 |

`wait` 的收回在两种拓扑下都成立：本机调度时调度器把 attempt 与节点运行从
`intervention_required` 收回 `running`（`control_data.resumed_decisions` 留痕）；Backend-controlled
（默认两进程 / `--role backend`）时权威签发 `runtime.v1` 的 `resume_pending` 命令，Edge 关闭终态
闸门、job 回到 `running` 并回发 `execution.error_resumed` 事件。执行面的报告字段
`timeout_seconds` / `timeout_spec` 供前端展示阈值与来源。

节点运行的 `error_info` 对超时失败记 `code=action_timeout`（附 `exception_type`、`message`、
`timeout_seconds`），普通失败仍为 `action_failed`。

### 超时后锁不放（ADR 0008）

硬超时（以及软超时决策选了终态选项而动作仍在执行）结算 attempt 时，驱动线程可能还在跑。此时：

- attempt 照常结算，工作流按所选决策继续；attempt 的 `control_data.timeout_hold` 记录
  `{kind, timeout_seconds, timed_out_at, state: "held"}`，执行 Claim 为 `timed_out`。
- 它的设备 / 物料锁、资源区间与设备托管都不释放。需要这些锁的后续作业（含该节点的重试）在
  `GET /api/v1/scheduler/resources` 中为 `waiting`，`blockers` 是超时的 attempt；前端应提示
  "等待超时动作的驱动返回"，而不是报错。
- 驱动真实返回：`control_data.late_outcome` 记录真实结果（不改状态 / 结果），`timeout_hold.state`
  变为 `released`，锁释放，等待的作业继续。
- 设备确实已停止而驱动迟迟不返回：`GET /api/v1/workflow-tasks/{uuid}/execution-locks` 中该租约
  `state=timed_out`、`can_release=true`，提交 `force-release`（`physical_settlement_confirmed=true`、
  `expected_claim_uuid`、`expected_fencing_token`、`reason`）；任务不必已结束。`timeout_hold.state`
  变为 `force_released`。
- 决策等待期间取消任务：attempt 立即按取消结算，锁同样继续持有。
