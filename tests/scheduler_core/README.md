# Scheduler Core Test Suite

这个目录只装配 `EdgeScheduler + TaskSchedulerBridge + WorkflowStore`，使用两份临时
SQLite、真实 `InventoryService`、Fake Dispatcher 和手动时钟。运行过程不会启动
Backend、SigNoZ、ROS、网络监听或真实设备。

```bash
python -m pytest -q tests/scheduler_core
```

验收矩阵覆盖：多 Task 交叉调度、优先级与 aging、容量门禁、step/pause/resume、
cancel/drain、失败/超时/不确定/重复与并发回调、命名 Site 组、库存补料与预留、
DispatchPermit/Claim/Fence、物料转运结算、条件分支、RepeatUntil、等待环诊断，以及
50 Task × 8 Job 的有界压力场景。所有调度超时均由手动时钟推进，没有真实 sleep。

重启恢复明确不属于本套件。未来 Runtime 重启策略是：正在执行的 Job 自动失败，
尚未执行的 Job 不再执行。

## Coverage gate

行为矩阵是主门禁。数值门禁只统计可独立定义的确定性调度决策内核：
`ordering.py`、`resource_wait_policy.py` 和 `execution_wait_graph.py`；三个历史组合根
文件同时承担编辑、发布、API、恢复等非本次职责，不能用排除行伪装成全文件高覆盖。
CI 分别要求 statements ≥ 85%、branches ≥ 75%，并始终先运行完整行为矩阵。
