# Runtime 重启终止工站未完成任务

Status: superseded by [0007](0007-host-restart-reconciles-instead-of-failing.md)（统一分支保留 dev 的重启后人工核对语义，2026-11-12）

确认承载设备执行的 Runtime 已重启后，工站所有未完成的 Workflow Task
统一进入 `failed`，包括排队、暂停、执行中和取消中的任务。时间调度是否
开启，以及重启通知是否携带部分或已失效的 Job UUID，不改变这一范围。

依据调度引擎协议 r3 修订执行边界：可证明 `NOT_STARTED` 且无副作用的 Job
取消并回滚准入；`START_COMMITTED` 或 `UNKNOWN` 的未决 Job 保留原状态、
attempt/command、Claim/Fence、资源占用和 Job 容量，标记待物理核实。
不为未决 Job 写入虚构的失败结果。已完成的真实结果保留。

旧时间计划和未准入的候选失效，旧 Task 不继续派发。父 Task 已失败但仍有
未决动作时，`control_status=waiting_reconciliation`、
`cleanup_status=requires_attention`，不能将业务失败当成物理结束。
同一原命令的真实结果或有记录的物理核实可以继续结算；只有全部未决动作及
必要库存、资源清理完成后才能 `settled`。结果确认丢失、超时和执行进程更换
都不单独构成释放证据。

传输路由和旧控制会话可以撤销；保护互斥和容量的资源不得出现可被新 Task
使用的空窗。重复重启通知幂等保留未决身份与占用；跨库结算失败可重试。

库存数量异常只延迟结算与相关占用清理，不延迟父 Task 失败。已经保存的
设备成功证据仍可用于数量更正；更正完成后父 Task 仍然失败，不继续后继节点。
软件任务失败不推断设备已经停止，也不推断库存数量或物料位置发生变化。

短暂连接中断，以及执行进程仍存活时仅重建调度器，继续使用各自的恢复流程。
它们不等同于已确认的 Runtime 重启。

回归测试覆盖时间调度开关、冷启动、部分/失效 Job 列表、取消中任务、旧结果
迟到、重复通知、库存更正和重启后提交新任务。新增 r3 用例分别覆盖
`NOT_STARTED`、`START_COMMITTED`、`UNKNOWN`，以及原 Job 保持容量直到真实
结果到达。该物理恢复合同属于平台，与具体队列或 CP-SAT 引擎无关。
