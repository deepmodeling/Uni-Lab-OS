# Host 重启后人工核对未决动作，不整体失败任务

Status: accepted（2026-11-12，统一分支）

取代 [0005](0005-runtime-restart-fails-station-tasks.md)。

## 决定

承载设备执行的 Host 进程重启后，**不**把工站上未完成的 Workflow Task 一律置为 `failed`。
统一分支保留 dev（#336）的语义：

- Host 重新启动时，本地执行库中仍处于 `dispatched` / `running` 的 job 属于上一个 Host 进程，
  动作已随进程中断、结果无法确认，统一转为 `execution_unknown`（`error_code=host_restarted`），
  **不重放**设备调用（`server/backend/coordinator.py` `restore()`）。
- 调度权威依据 Host 身份变化为这些 attempt 打开人工核对决定（`list_reconciliation_decisions`
  / `resolve_reconciliation_decision`，对外为 `/api/v1/error-decisions`）。操作员核实现场后选择
  重试、跳过、人工干预或终止；任务按决定继续或结束。
- 未决 attempt 的执行声明、栅栏、资源区间与设备/物料锁在核实前**一直保留**；其他任务不能借此
  空窗使用同一资源。旧的在途工作核对完成前，派发闸门保持关闭。
- 排队中、暂停中的任务和未受影响的节点照常继续；已完成的真实结果保留。
- 搬运、分装等物理动作在结果未知时只能经物理结算补记实际结果或确认恢复原状，不提供直接重试
  （见 `docs/developer_guide/workflow_inventory_recovery.md`）。

## 与 0005 的共同原则（继续有效）

- 结果确认丢失、超时和执行进程更换都不构成释放资源的证据。
- 不为未决动作写入虚构的失败结果；同一原命令的真实结果或有记录的物理核实才可结算。
- 重复的重启通知必须幂等，不得制造新的占用空窗。

## 取舍

0005 以“整站任务失败”换取简单、确定的终态；dev 的做法让一次 Host 重启只影响真正在执行的
attempt，其余工作不被连带中止，代价是需要操作员对每个未决 attempt 做一次核对。实验室场景中
Host 重启（升级驱动包、进程崩溃恢复）比真正的现场事故更常见，统一分支选择后者。

## 已知缺口

- 硬超时（`server/backend/execution.py` `_handle_hard_timeout`）在 abort / retry 后会释放资源，
  而同步驱动可能仍在执行，与本 ADR“超时不构成释放证据”的原则冲突。作为后续修复项跟踪
  （差异核查编号 SC-N5）。
- 已证明从未开始的 attempt（`workflow_execution_dispatch.invocation_uuid IS NULL`）目前也进入
  人工核对，未来可自动取消以减少核对量（差异核查编号 R02）。

## 依据

- 测试：`tests/server/test_host_restart_reconciliation.py`、
  `tests/server/test_restart_keeps_recovered_locks.py`、
  `tests/server/test_workflow_restart_reconciliation.py`。
- 决策记录：统一分支用户决定“Keep dev, write ADR”（2026-11-12）。
