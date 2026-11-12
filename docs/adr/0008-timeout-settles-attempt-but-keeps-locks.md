# 硬超时结算 attempt，锁保留到驱动真实返回或人工释放

Status: accepted（2026-11-13，统一分支）

补充 [0007](0007-host-restart-reconciles-instead-of-failing.md)，关闭其"已知缺口"中的硬超时一项
（差异核查编号 SC-N5）。

## 背景

`@action(timeout=...)` 到期时执行面只能**请求**取消：协程驱动会在下一个 `await` 处被取消，
同步驱动跑在线程里，Python 杀不掉它。此前硬超时的 attempt 一结算，它的设备 / 动作锁、物料锁、
资源区间、执行 Claim 和设备托管就随之释放，驱动的迟到结果按"已被取代"丢弃——设备可能还在动，
锁却已经交给了下一个作业。这与 0007 的共同原则"超时不构成释放资源的证据"冲突。

## 决定

1. **硬超时是工作流的自动继续规则。** 超时的 attempt 照常按超时结算，工作流按今天的决策链 /
   超时策略继续（`abort` 记 `failed`，`retry` 记 `failed` 并追加新 attempt，`operator_intervention`
   以人工结果记 `succeeded`，任务取消记 `canceled`），节点运行 `error_info[0].code=action_timeout`
   不变。任务不会因为驱动没有返回而卡住。
2. **锁不释放。** 该 attempt 的执行 Claim 转为 `timed_out`（租约与栅栏保留），动作使用权与准入、
   资源区间、调度器内存里的动作 / 物料锁都继续归它；设备托管保持原样（`reserved` / `loaded`，
   托管转换保持 `prepared`）。设备可能因此无限期被锁定。
3. **只有两件事释放这些锁：**
   - **驱动真实返回。** 真实结果作为迟到结果写入 attempt 的 `control_data.late_outcome`，**不改变**
     已结算的状态与结果；Claim 释放，占用按已结算的状态收敛（失败 / 取消的装卸托管转 `retained`
     待核实，成功的转 `loaded` / `released`）。`control_data.timeout_hold.state` 记为 `released`。
   - **操作员强制释放。** 复用 V1 的 `POST /api/v1/workflow-tasks/{uuid}/execution-locks/{lease_uuid}/force-release`：
     仍要求 `physical_settlement_confirmed=true` 与 Claim / Fence CAS；对 `timed_out` 的 Claim 不要求
     任务已终结（重试可能正等这把锁），也不因任务的其他设备托管拒绝。`timeout_hold.state` 记为
     `force_released`。任务级 `unlock_resources` 同样会放开它。
4. **后续作业等待而不是失败。** 需要这些锁的作业——同任务的后续节点、超时节点的重试、其他任务——
   在持久区间准入处等待；`GET /api/v1/scheduler/resources` 里它是 `waiting`，`blockers` 是超时的
   attempt。`/execution-locks` 显示该租约 `state=timed_out`、`can_release=true`。持有期间不能人工释放
   它关联的区间或设备托管（先等驱动返回或强制释放执行锁）。
5. **跨调度权威重启保留。** `timed_out` 是持久的 Claim 状态：重启时 `restore_requests` 按原申请恢复
   内存占用，区间与托管收敛跳过它（与 0007 中 `execution_unknown` 保留锁直到核实同一原则）。迟到
   结果按持久事实处理，不依赖调度器内存，任务已结束或权威重启后到达同样生效。
6. **覆盖的超时路径：** 执行面看门狗的硬超时；HostLink 本地运行时用 `asyncio.wait_for` 先报出的
   硬超时（同步驱动线程仍在跑时 `error_info.driver_still_running=true`，线程返回后真实结果经动作
   上下文回报）；软超时（`execution_timeout`）决策选了终态选项而动作仍在执行；超时决策等待期间
   取消任务（立即按取消结算，锁继续持有）。驱动在决策放行之前就已返回时没有需要持有的锁，
   迟到结果随终态一起记录。

## 线上形状

- 执行面终态的 `return_info.timeout_hold = {kind, timeout_seconds, timed_out_at, reason: "driver_still_running"}`；
  迟到结果经结果桥 `publish_job_late_outcome(report, item)` 交给 job 的 owner。
- 默认两进程拓扑：Host 的 `execution_job` 已终态不再变化，迟到结果作为 runtime.v1 事件
  `execution.late_outcome` 上报（详情 payload 同时写入 Host history，`event_key=late_outcome`）。
- runtime.db：`workflow_execution_claim.state` 增加 `timed_out`（0.13 发布线 schema v1 基线直接修改，
  开发期旧库按 `on_unmigratable="rebuild"` 备份后重建）。

## 托管与终态同事务

同日一并决定：attempt 状态写入与由它**直接派生**的占用收敛——执行 Claim 释放、使用权与准入收回、
区间释放、设备托管转换——在同一个 runtime.db 事务里提交；`execution_unknown` 同样在同一事务里把
托管转 `retained`、Claim 转 `uncertain`。观察者（`/workflow-device-tenancies`、库存与任务接口）不会
读到"节点已成功、托管仍是 `reserved`"的中间态，崩溃也不会把两步拆开。`ResourceIntervals.recover()`
仍是兜底；收敛本身出错时只回滚到保存点，状态事实照常提交，留给后续释放与恢复。

等待决策的 `intervention_required` 不是结算，托管保持原样，结算后再转换（此前会被顺带转为
`retained`，导致同任务的重试直接失败）。超时后仍持有锁的 attempt 托管同样保持原样，与上文一致。

## 取舍

- 设备可能长时间锁定，需要人到现场核实后释放；换来的是不在设备仍可能运动时把它交给别的作业。
- 远端 slave 设备的同步驱动超时：迟到结果不经 HostLink RPC 回传，只能人工强制释放。
- Host 重启后恢复的硬超时决策：原驱动已随进程结束、不会再返回，放行后同样只能人工强制释放。
- 迟到的成功不重算库存：超时时库存预留已按失败处理，实际用量用数量核对（`reconcile-quantity`）校正。

## 依据

- 测试：`tests/server/test_action_timeout_hold.py`、`tests/server/test_custody_commits_with_terminal.py`、
  `tests/backend/hostlink/test_host_node.py::test_sync_driver_outliving_its_hard_timeout_keeps_the_hold_until_it_returns`、
  `tests/backend/hostlink/test_local_runtime_timeout.py`。
- 决策记录：统一分支用户决定（2026-11-13）"硬超时自动继续工作流，但锁不放，直到驱动返回或人工强制释放"。
