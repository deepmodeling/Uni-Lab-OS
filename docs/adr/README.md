# 架构决策记录（ADR）

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-compile-resource-occupancy-intervals.md) | 派发前编译资源占用区间 | accepted |
| [0002](0002-task-level-edit-boundary.md) | 任务编辑只限工作流入口参数 | accepted |
| [0003](0003-task-scoped-pause.md) | 暂停作用于单个任务 | accepted |
| [0005](0005-runtime-restart-fails-station-tasks.md) | Runtime 重启终止工站未完成任务 | superseded by 0007 |
| [0006](0006-frontend-event-subscription.md) | 前端事件订阅与有限重连窗口 | accepted |
| [0007](0007-host-restart-reconciles-instead-of-failing.md) | Host 重启后人工核对未决动作，不整体失败任务 | accepted |

0001–0006 来自 V1.0（tag `v1.0`），随统一分支一并纳入；代码与文档中“v1.0 ADR 0002 / 0006”
的引用即指这里的文件。0004 在 V1.0 中不存在。0001–0006 中出现的 V1 模块路径是当时的实现位置，
统一分支的对应实现见 `docs/developer_guide/api_v1_migration.md` 与各 ADR 的后续说明。
