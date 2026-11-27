---
goal: UniLabOS 工站自治持久调度内核开发方案
version: 1.0
date_created: 2026-08-26
last_updated: 2026-08-26
owner: Uni-Lab-OS
status: 'Planned'
tags: [architecture, scheduler, edge, workflow, material, site, migration]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

本计划把 UniLabOS 从“Backend 下发节点命令、Edge 镜像执行”和“内存 DAG/资源锁”重构为工站自治的持久调度内核。目标设计中，Backend 只负责任务（Task）的跨工站编排、工站调用参数传递、结果聚合以及暂时保留的 AGV 调度；Edge/UniLabOS 是本工站工作流任务（WorkflowTask）、工作流节点作业（WorkflowNodeJob）、库位（Site）、物料（Material）与设备执行的唯一调度权威。

本计划基于 Uni-Lab-OS `product/durable-scheduler-kernel-new` 分支提交 `427207408d2d7bb311b0c76902bf0ea50d199900`。当前实现与目标设计必须明确区分：当前 `EdgeScheduler` 仍在内存保存 `WorkflowRun`、`_inflight` 与 `_job_resource_locks`；目标设计用 SQLite WAL 中的持久事实替换它们，不在旧实现外再增加一层调度器。

设计图：

- [工站自治调度目标架构](diagrams/station-scheduler-architecture.html)
- [九道门禁与物理结算时序](diagrams/station-job-admission-sequence.html)

目标深模块及其 Interface 如下。每个模块只公开少量粗粒度操作，复杂状态转换留在 Implementation 内部。

| Module | Authority | Interface | 隐藏的 Implementation |
|---|---|---|---|
| `WorkflowRuntime` | 工作流任务（WorkflowTask）、工作流节点作业（WorkflowNodeJob）、冻结执行计划（ExecutionPlan） | `submit_station_task`、`load_candidates`、`apply_transition`、`record_dispatch_effect`、`recover` | revision 冻结、DAG 推进、参数绑定、状态机、运行日志、任务/作业结果投影 |
| `InventoryAuthority` | 库位占用（SiteOccupancy）、任务物料预留（TaskMaterialReservation）、作业执行占用（JobExecutionClaim）、栅栏（Fence）、物料变更集（MaterialChangeSet） | `reserve_ingress`、`prepare_execution`、`settle_execution`、`recover_open_claims` | 容量检查、完整资源集、原子占用、分装/拆板清单、物理结算、库存 Outbox |
| `DurableSchedulerKernel` | 工站内部唯一调度决策 | `start`、`stop`、`wake`、`snapshot` | 九道门禁、优先级与老化、准入重试（AdmissionRetry）、单决策循环、恢复协调 |
| `ExecutionKernel` | 持久派发意图（Durable Dispatch Intent）到执行回执（ExecutionReceipt）的边界 | `submit`、`request_cancel`、`reconcile` | 幂等 effect、设备会话队列、Fence 校验、结果未知、回执归一化 |
| `EdgeSyncGateway` | Backend 与 Edge 的工站调用协议 | `accept_station_task`、`accept_control_command`、`accept_transport_handoff`、`publish_outbox` | 幂等请求、断线重连、Schema 上报、Task/Job 结果上报、ACK 游标 |

## 1. Requirements & Constraints

- **REQ-001**: 一次 Backend 全局任务可编排一个或多个工站工作流调用；一次工站工作流调用只属于一个确定工站，Backend 只发送工作流入口参数，不发送 DAG 中间节点参数。
- **REQ-002**: Edge 保存并解释工作流（Workflow）权威定义；创建工站任务时冻结工作流 revision、执行计划、入口参数和输入/输出合同，Edge 启动时只向 Backend 上报工作流输入节点与输出节点的 JSON Schema。
- **REQ-003**: 一个工作流强制具有一个逻辑输入节点和一个逻辑输出节点；Schema 可以描述多个物料（Material）与多个位置需求。一个工站只有一个逻辑入口和一个逻辑出口，但每个逻辑端口可以映射多个有限库位（Site）。
- **REQ-004**: `workflow_task.uuid` 作为 Edge 内部工站任务身份；Backend 请求必须携带 `backend_task_uuid` 与同一全局计划内稳定且唯一的 `invocation_key`。`invocation_key` 是工站调用的幂等键，不是 `parent_job_uuid`，允许同一 Backend 任务多次调用同一工作流。
- **REQ-005**: 每个可执行 DAG 节点只创建一个稳定 `job_uuid`；临时资源不足、设备忙或库位不足只更新 `waiting_reason` 并执行准入重试（AdmissionRetry），不得创建新作业或盲目增加 `attempt`。
- **REQ-006**: 工站内部执行九道准入门禁：DAG 依赖就绪、任务控制状态允许、输入/物料身份已解析、设备在线且动作可执行、库位/输出容量安全、完整资源集已解析、原子取得完整作业执行占用（JobExecutionClaim）与栅栏（Fence）、持久化派发意图、调用设备动作。
- **REQ-007**: 一般工站只有一个机械臂；所有机械臂动作通过同一机械臂执行器资源占用串行化。来源设备与目标设备的装卸互锁和防碰撞由 PLC 在动作开始边界原子保证，不在 Edge 增加 `source_access_zone`、`target_access_zone` 或同义的软件占用成员。不同独立设备在各自执行器资源占用互不冲突且库位/物料条件满足时允许并行执行。
- **REQ-008**: 调度使用非强制抢占：已进入物理动作的作业不因高优先级任务到达而中断；高优先级只在作业准入边界优先。排序依次使用恢复/安全工作、释放容量工作、`effective_priority`、提交时间、拓扑序与 UUID；`effective_priority = base_priority + floor(wait_seconds / aging_interval_seconds)`，默认 `aging_interval_seconds=30`，允许部署配置覆盖为正整数。
- **REQ-009**: 库位占用（SiteOccupancy）、任务物料预留（TaskMaterialReservation）和作业执行占用（JobExecutionClaim）是三类独立持久事实，禁止用一个通用锁表合并语义。
- **REQ-010**: 一个物理动作在派发前必须一次性解析并占用 Edge 权威的完整资源集合。机械臂转移动作的集合为机械臂执行器、源库位、目标库位和被搬运物料；不因装卸访问额外占用来源设备、目标设备或软件互斥区域。设备原位动作的集合为实际执行设备、承载库位和有关物料。任何成员冲突时整个占用事务回滚，不允许部分持有。
- **REQ-010A**: PLC 必须为机械臂转移动作返回可区分的 `accepted/started/completed`、`rejected_before_start` 与结果未知反馈，并以同一命令身份提供状态查询。只有带物理未开始证明的 `rejected_before_start` 才允许 Edge 结算本次派发并重新进入准入；发送或开始状态无法确认时必须进入 `execution_unknown` 并保留作业执行占用（JobExecutionClaim）。
- **REQ-011**: 分装和拆板动作在不可逆派发前冻结输出清单（Output Manifest），预分配输出 `material_uuid`，解析并占用全部目标库位；成功后以一个物料变更集（MaterialChangeSet）原子提交源物料、输出物料、父子关系和库位变化。
- **REQ-012**: v1 的一个工作流节点作业对应一个连续安全执行阶段和一次作业执行占用（JobExecutionClaim）。若物料必须跨设备动作长期停留，工作流必须显式拆为多个 DAG 节点，并将设备内部承载位建模为库位（Site）；v1 不允许一个作业隐式释放后重新取得 Claim。
- **REQ-013**: 普通 pick/place 动作在物理结算（PhysicalSettlement）成功前保留源库位占用并同时占用目标库位执行资源；成功后一次性切换库位事实。结果未知时保留关联 Claim，不虚构 `held_by_device` 位置。
- **REQ-014**: 取消命令只改变任务控制意图并请求执行层取消；没有明确的未执行/已停止回执时不得释放作业执行占用（JobExecutionClaim），不得强制中断正在运行的设备动作。
- **REQ-015**: Edge 与 Backend 断联不影响已安全派发的工站任务继续运行；所有 Task/Job 状态、节点结果、工站输出和物料事件进入本地 Outbox。执行结果无法判定时状态必须是 `requires_attention`/`unknown`，不得自动重放物理动作。
- **REQ-016**: AGV 暂由 Backend 调度。运输开始前，Backend 必须先向目标 Edge 请求入口预留；运输开始前预留可以过期，进入 `in_transit` 后不能自然过期，只能完成交接或人工取消。
- **REQ-017**: Backend 只长期追踪可搬运载体的 `material_uuid` 与工站入口/出口位置；Edge 追踪工站内部全部库位（Site）与局部物理事实。`material_uuid` 可跨多个 Backend 任务长期稳定，分装/拆板产生的新载体使用新的 UUID。
- **REQ-018**: Edge 必须把每个工作流节点作业（WorkflowNodeJob）的 `job_uuid`、状态、参数、反馈、结果与错误持久上报 Backend；Backend 使用 `backend_task_uuid + invocation_key` 关联同一全局任务中的多次工作流调用。
- **REQ-019**: 新调度内核与 UniLabOS 同进程、同 Python 代码库部署；调度决策运行在一个专用 actor 线程，FastAPI/WS/ROS 回调只提交持久命令并唤醒 actor，不在请求协程内执行调度扫描。
- **REQ-020**: SQLite 继续使用 WAL 与 `BEGIN IMMEDIATE` 单写事务。v1 保留 `workflow_history.db` 和 `inventory.db` 两个权威数据库，不引入独立 Go 调度服务，也不在本次迁移合并数据库。
- **SEC-001**: Backend 工站调用、控制命令、AGV 交接和结果 ACK 必须使用稳定幂等键；重复投递返回已有事实，不创建第二个 Task、Job、Claim、Effect 或 Material。
- **SEC-002**: 每个需要互斥的设备、库位和物料资源维护单调递增栅栏令牌（Fence Token）；ExecutionCommand 与 ExecutionReceipt 必须携带相同令牌集合，过期回执不得提交物理结算。
- **SEC-003**: 设备动作参数、物料 UUID、输出清单和结果正文不得写入日志或 Trace 属性；日志只记录身份、状态、哈希、错误码和因果 ID。
- **SEC-004**: Edge 启动恢复必须失败关闭。数据库版本不兼容、冻结计划非法、活动 Claim 无法与 DispatchEffect 对齐时禁止继续派发，并进入可观测的 `requires_attention`。
- **CON-001**: Uni-Lab-OS 只拥有通用调度、安全执行、物料/库位权威、通信与遥测；具体工站库位 UUID、设备 ID、机械臂点位、传感器顺序与工作流定义属于领域仓库；可复用机器人/导轨/夹爪合同属于 `unilab_robot_template`。
- **CON-002**: Uni-Lab-OS 不得导入同级领域仓库源代码，不得写入具体工站 UUID、设备地址、机械臂点位或工艺常量；领域包只能通过 Manifest、Registry、Workflow 和 Adapter 接缝注入这些事实。
- **CON-003**: 当前 `unilabos/app/scheduler/service.py`、`unilabos/workflow/service.py`、`unilabos/workflow/store.py` 和库存 store/service 已经超过维护阈值；新功能不得继续堆入这些文件，迁移必须通过命名清晰的深模块完成。
- **CON-004**: 不允许长期双写、影子调度或运行时功能开关维持两个调度权威。切换以数据库迁移版本和启动前置检查完成，回滚只能使用切换前备份且不得带回新内核已派发的物理任务。
- **GUD-001**: 每个新增或修改的函数、方法、回调、fixture 与测试函数必须提供中文 docstring，说明参数、返回值、异常、状态转换、幂等与安全不变量；领域变量在最近位置写中文语义注释。
- **GUD-002**: 新增/修改文件完成后统计物理行数；超过 500 行必须报告，达到 800 行必须给出保持完整的深度理由或执行拆分方案，新增或继续扩大的文件不得达到 1500 行。
- **GUD-003**: 所有状态转换使用关闭式命令对象，不公开任意 `set_status`、裸 SQL 连接或可变字典；Interface 返回冻结 DTO/值对象。
- **PAT-001**: 使用“持久事实 + 单决策 actor + 唤醒提示”的模式。内存队列、`threading.Event` 和缓存只提高响应速度，丢失后必须能从数据库全量恢复。
- **PAT-002**: 使用“先 Claim、再 Intent、再 Effect、后 Settlement”的可恢复阶段协议；跨 `workflow_history.db` 与 `inventory.db` 不伪装成原子事务，通过稳定 `effect_uuid` 和确定性恢复规则收敛。
- **PAT-003**: 执行 Adapter 按设备会话串行化，同一机械臂只有一个会话队列，独立设备拥有独立队列；PLC 在动作开始边界承担装卸互锁和防碰撞，Edge 不复制该互锁模型；调度 actor 不等待物理动作完成。

### 持久化状态与事务协议

`workflow_history.db` 保留现有 `workflow_task` 与 `workflow_node_job`，新增 `workflow_job_dispatch_effect`。`inventory.db` 新增 `task_material_reservation`、`task_material_reservation_member`、`job_execution_claim`、`job_execution_claim_resource`、`resource_fence`、`material_changeset`、`material_changeset_item`、`station_ingress_reservation` 与 `station_ingress_reservation_site`。禁止创建第二套 Task/Job 表。

| Phase | 权威事务 | 必须提交的持久事实 | 崩溃后的确定性处理 |
|---|---|---|---|
| Prepare | `inventory.db` | `claim_uuid`、`effect_uuid`、完整资源成员、Fence Token、状态 `active` | 若没有 DispatchEffect，则证明未越过派发边界，恢复器关闭孤儿 Claim |
| Intent | `workflow_history.db` | DispatchEffect `prepared`、Job `dispatching`、Claim/Fence 引用 | 可安全首次提交同一 `effect_uuid`；不得创建新 effect |
| Dispatch | `workflow_history.db` 后调用 Adapter | Effect 在外部调用前改为 `submitting` | 重启发现 `submitting` 且无明确回执时标记 `unknown`，不得盲重放 |
| Receipt | `workflow_history.db` | 标准执行回执、设备结果、Fence Token | 明确 success/failure 进入结算；unknown 保留 Claim 并转人工关注 |
| Settlement | `inventory.db` | ChangeSet、SiteOccupancy、Material、关闭 Claim、库存 Outbox | 已结算但 Job 未投影时，从 ChangeSet 重建 Job 终态投影 |
| Projection | `workflow_history.db` | Job 终态、DAG 下游就绪、Task 终态、结果 Outbox | Backend ACK 仅推进游标，不改变物理事实 |

## 2. Implementation Steps

### Implementation Phase 1 — 固化目标合同与数据库迁移骨架

- GOAL-001: 建立单一身份、状态和持久化协议，使后续模块可以并行开发且不会再次引入第二个调度权威。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 在 `unilabos/workflow/runtime/models.py` 新建冻结 DTO：`StationTaskSubmission`、`JobCandidate`、`RuntimeTransition`、`DispatchEffect`、`ExecutionReceiptProjection`。`StationTaskSubmission` 必须包含 `backend_task_uuid`、`invocation_key`、`workflow_uuid`、`workflow_revision`、`input`、`priority`、`idempotency_key`；禁止接受中间节点参数。 | | |
| TASK-002 | 在 `unilabos/workflow/runtime/schema.py` 添加 `workflow_history.db` additive migration：给 `workflow_task` 增加 `backend_task_uuid`、`invocation_key`、`workflow_revision`、`priority`；给 `workflow_node_job` 增加 `waiting_reason`、`next_admission_at`；创建 `workflow_job_dispatch_effect(effect_uuid PRIMARY KEY, workflow_node_job_uuid, attempt, claim_uuid UNIQUE, command_hash, status, receipt_json, prepared_at, submitting_at, finished_at)`，并为 `(backend_task_uuid, invocation_key)` 创建活动唯一索引。 | | |
| TASK-003 | 在 `unilabos/app/scheduler/inventory/execution_schema.py` 添加 `inventory.db` additive migration，创建任务物料预留、作业执行占用、资源成员、Fence、物料变更集和入口预留九张表；所有状态列使用 CHECK，所有活动唯一性使用 partial unique index，所有引用 `material.uuid`/`site.uuid` 的 FK 使用 `ON DELETE RESTRICT`。 | | |
| TASK-004 | 在 `unilabos/app/scheduler/contracts.py` 定义 `WorkflowRuntimePort`、`InventoryAuthorityPort`、`JobExecutionPort` 三个 Protocol；只放目标 Interface DTO 与异常，不导入 FastAPI、ROS、SQLite 或具体 Adapter。 | | |
| TASK-005 | 在 `docs/adr/` 新建 ADR，记录“工站调度与 UniLabOS 同进程使用 Python、Backend 不参与工站内部调度、保留双 SQLite 并使用可恢复阶段协议”的已确认决策；ADR 只在 REQ-004 的 `invocation_key` 身份语义获得产品确认后合入。 | | |

### Implementation Phase 2 — 提取工作流运行时深模块

- GOAL-002: 让持久工作流任务（WorkflowTask）与工作流节点作业（WorkflowNodeJob）成为 DAG 推进和派发意图的唯一运行事实，移除对内存 `WorkflowRun` 的依赖。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | 新建 `unilabos/workflow/runtime/runtime.py` 的 `WorkflowRuntime`。实现 `submit_station_task` 在一个 `workflow_history.db` 事务内验证输入 Schema、冻结 revision/ExecutionPlan、幂等创建 Task/Jobs 和 runtime journal；重复 `(backend_task_uuid, invocation_key)` 返回同一聚合。 | | |
| TASK-007 | 新建 `unilabos/workflow/runtime/dag_evaluator.py` 的纯函数 `evaluate_dag(plan, jobs)`，从冻结 ExecutionPlan 与持久 Job 状态计算 ready/blocked/terminal，不保存 `_pending_parents`、`_consumed` 或返回值缓存；上游输出参数绑定在派发候选生成时确定性重算。 | | |
| TASK-008 | 新建 `unilabos/workflow/runtime/store.py`，从 `unilabos/workflow/store.py` 搬迁 Task/Job/command/runtime journal/dispatch effect SQL；保持 `WorkflowStore` 作者态 SQL不变。运行态写入只通过 `WorkflowRuntime.apply_transition` 的关闭式命令执行。 | | |
| TASK-009 | 将 `unilabos/workflow/execution_plan.py` 的构建逻辑迁入 `unilabos/workflow/runtime/execution_plan.py`，保留旧导入路径作为短期兼容 re-export；计划必须冻结设备动作合同、资源需求、输入/输出 Manifest 规则、逻辑端口到候选 Site UUID 的映射，不读取运行时库存占用。 | | |
| TASK-010 | 把 `unilabos/workflow/service.py` 中创建/控制/查询任务的调用改为 `WorkflowRuntime` Interface；删除 `TaskSchedulerBridge` 的新调用路径，保留兼容类只用于迁移测试，禁止新增桥接监听器。 | | |

### Implementation Phase 3 — 建立库存执行权威

- GOAL-003: 用一个库存深模块原子管理有限库位、物料、任务预留、完整作业资源占用、栅栏和物理结算。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-011 | 新建 `unilabos/app/scheduler/inventory/execution_authority.py` 的 `InventoryAuthority.prepare_execution`。在单个 `BEGIN IMMEDIATE` 中重新读取所有 Site/Material/Reservation/Claim，验证完整资源集合和容量约束，全部可用才写 Claim、资源成员与递增 Fence；冲突返回结构化 `AdmissionBlocked(reason, retry_trigger)`，不得抛通用异常或部分写入。 | | |
| TASK-012 | 在同一模块实现 `settle_execution`：验证 `claim_uuid + effect_uuid + fence_tokens`、执行回执和冻结 ChangeSet；明确 success/failure 时原子提交 Material/Site/ledger/outbox 并关闭 Claim；unknown 只记录观察事实并保持 Claim 活动。 | | |
| TASK-013 | 新建 `unilabos/app/scheduler/inventory/output_manifest.py`，验证分装/拆板输出数量、UUID 唯一性、模板/父子关系、目标 Site 兼容性和完整性；在派发前生成冻结 MaterialChangeSet 草案，在结算时禁止设备 Adapter 增加未声明输出。 | | |
| TASK-014 | 新建 `unilabos/app/scheduler/inventory/ingress.py`，实现逻辑入口到多个物理 Site 的原子选择与预留；状态为 `reserved -> in_transit -> received` 或 `reserved -> expired/canceled`，`in_transit` 不允许 TTL 自动过期，人工取消必须写审计理由。 | | |
| TASK-015 | 在领域包发布/加载接缝中验证工站拓扑：恰好一个逻辑输入端口和一个逻辑输出端口，每个端口映射至少一个 Site，所有 Site UUID 存在且不重复；将映射冻结到 ExecutionPlan。UniLabOS 不保存具体工站常量。 | | |

### Implementation Phase 4 — 实现唯一持久调度内核

- GOAL-004: 用单 actor 决策循环替换内存 WorkflowRun 扫描，实现多任务交叉运行、优先级老化和九道门禁。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-016 | 新建 `unilabos/app/scheduler/kernel.py` 的 `DurableSchedulerKernel`，只公开 `start/stop/wake/snapshot`。actor 线程每次从 WorkflowRuntime 读取候选，完成一次有界调度批次后等待 `threading.Event` 或最早 `next_admission_at`；任何唤醒丢失都能由启动恢复与超时扫描补偿。 | | |
| TASK-017 | 重写 `unilabos/app/scheduler/ordering.py`，实现稳定排序键：安全恢复类、释放容量类、有效优先级、提交时间、拓扑序、Task UUID、Job UUID。有效优先级使用 REQ-008 公式；排序函数保持纯函数并接收显式 `now`。 | | |
| TASK-018 | 新建 `unilabos/app/scheduler/admission.py`，按 REQ-006 固定顺序执行九道门禁。门 1–6 只产生候选/阻塞原因；门 7 调用 InventoryAuthority；门 8 调用 WorkflowRuntime 持久化 DispatchEffect；门 9 调用 ExecutionKernel。禁止 Listener 链绕过门禁。 | | |
| TASK-019 | 实现容量安全策略：`capacity_group` 在领域拓扑中声明 `min_free_sites`，默认 1；调度器优先执行可证明释放该组容量的作业。若一个工站明确声明无缓冲且物理流程允许占满，可把该组配置为 0，但拆分/拆板作业仍必须在派发前取得全部输出 Site。 | | |
| TASK-020 | 把 `unilabos/app/scheduler/integration.py` 改为唯一组合根：构造 WorkflowRuntime、InventoryAuthority、ExecutionKernel、DurableSchedulerKernel、EdgeSyncGateway 并注入 Interface；移除模块级 `_scheduler/_backend/_inventory` 互相取用的隐藏依赖，生命周期关闭顺序为 Gateway → Scheduler → Execution → Stores。 | | |

### Implementation Phase 5 — 执行、取消与崩溃恢复

- GOAL-005: 确保设备调用的不可原子边界可证明、可恢复，并且不会因重启或取消重复物理动作。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-021 | 新建 `unilabos/app/scheduler/execution/kernel.py` 的 `ExecutionKernel`。`submit` 必须以 `effect_uuid` 幂等，先把 Effect 改为 `submitting` 再调用 Adapter；同一设备使用一个串行 session queue，不同设备使用独立 queue；调度 actor 只接收提交回执，不等待物理完成。 | | |
| TASK-022 | 新建 `unilabos/app/scheduler/execution/ports.py` 的 `DeviceExecutionAdapter` Interface 和 `ExecutionCommand/ExecutionReceipt` DTO；为现有 `Dispatcher`、ROS2 action、RPC/PLC 设备实现 Adapter。PLC Adapter 必须规范化 `accepted/started/completed`、`rejected_before_start` 和 unknown，并支持按稳定命令身份查询；Adapter 在提交和回执时校验 Fence Token，不得直接写 Task/Job/Inventory 数据库。 | | |
| TASK-023 | 在 `ExecutionKernel.request_cancel` 实现非强制取消：持久化 cancel intent，向可取消设备发送一次幂等请求；仅 `not_started` 或明确 `stopped` 回执允许结算释放，`cancel_requested`/超时转 unknown 并保留 Claim。 | | |
| TASK-024 | 新建 `unilabos/app/scheduler/recovery.py` 的 `RecoveryCoordinator`，按 Prepare/Intent/Dispatch/Receipt/Settlement/Projection 六阶段扫描两个数据库；实现孤儿 Claim 释放、prepared effect 首次提交、submitting effect 转 unknown、已结算结果补投影和 Outbox 重发。恢复完成前 Scheduler 不得开放门 9。 | | |
| TASK-025 | 删除 `EdgeScheduler` 的 `_workflows`、`_inflight`、`_job_resource_locks`、`WorkflowRun` 恢复和 cancel 释放逻辑；删除 `TaskSchedulerBridge`、`TaskRuntimeProjection` 的运行写路径。旧 `EdgeScheduler` 名称只可保留为一版明确弃用的构造兼容 Adapter，并在下一主版本删除。 | | |

### Implementation Phase 6 — 切换 Backend/Edge 工站调用协议

- GOAL-006: 让 Backend 只提交工站工作流入口参数并可靠接收 Task/Job 事实，彻底停止下发 Edge 内部节点命令。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-026 | 在 `unilabos/app/edge_control/station_task_protocol.py` 定义版本化 wire DTO：`StationWorkflowCatalogReport`、`StationTaskSubmit`、`StationTaskControl`、`StationIngressReserve`、`StationTransportTransition`、`StationTaskEvent`、`StationJobEvent`；拒绝携带中间节点参数或 Backend 分配的 Edge Job 指令。 | | |
| TASK-027 | 重构 `unilabos/app/edge_control/runtime.py` 与 `http.py`，将现有 per-node `edge_job` 命令入口替换为 StationTask/transport 接口；保留旧路由只返回明确的版本不支持错误，禁止转换为新任务后继续执行。 | | |
| TASK-028 | 扩展本地协议 Outbox 与 ACK 游标：Task/Job 事件使用 `event_id` 幂等，Backend ACK 只推进连续游标；网络断开时不阻塞 Scheduler，恢复后按 sequence 重发。节点参数与结果通过受控 payload 上报，不进入日志。 | | |
| TASK-029 | 修改启动拓扑语义：`edge_runtime` 无论连接 Backend 与否都启动本地 WorkflowRuntime/InventoryAuthority/Scheduler/Execution；`control_plane=backend` 只表示外部业务编排来源，不再表示 Backend 拥有工站内部 Scheduler/Inventory。更新 CLI 文案、`CONTEXT.md` 和部署文档，明确 supersede 旧语义。 | | |

### Implementation Phase 7 — 数据迁移、一次性切换与退役

- GOAL-007: 在没有双调度权威的前提下迁移历史数据并完成可回滚的一次性发布。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-030 | 编写 `unilabos/migrations/station_scheduler_v1.py`：启动前备份两个数据库；终态历史 Task/Job 原样保留；未派发 pending 作业补齐 execution plan/version 后迁移；存在 `dispatched/running` 且没有明确回执的旧作业统一迁移为 `requires_attention` 并创建人工恢复记录，禁止自动重放。 | | |
| TASK-031 | 在启动前置检查中拒绝以下状态：旧 EdgeScheduler 仍启用、两个协议 authority 同时配置、活动 legacy reservation 无法映射、同一资源有多个活动 Claim、Site 占用引用不存在 Material、运行 Task 缺少冻结 plan/revision。所有拒绝项输出稳定错误码和修复命令。 | | |
| TASK-032 | 删除 Legacy `workflow_runs/job_runs` 的新写入、内存 Timeline 权威和 Backend per-node 调度配置；保留只读历史查询一个发布周期。清理完成后用 `rg` 证明运行路径不再导入 `WorkflowRun` 或写 `_job_resource_locks`。 | | |
| TASK-033 | 执行切换演练：备份 → 停止旧进程 → 运行 migration/check → 启动新 Edge → 恢复/对账 → 开放 Backend StationTask 提交。若开放门 9 前失败，可用备份回滚；一旦新内核派发任何 effect，禁止数据库降级回滚，只能前向修复。 | | |

### Implementation Phase 8 — 验收、压力与运维证据

- GOAL-008: 证明多任务交叉运行、有限库位、并行设备、崩溃恢复和协议断连在真实工站条件下安全成立。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-034 | 实现 TEST-001 至 TEST-010 的自动化测试，所有测试使用真实 SQLite 临时文件和目标 Interface；测试不得直接修改数据库绕过深模块。 | | |
| TASK-035 | 构建虚拟工站 E2E：1 个机械臂、2 个独立设备、6 个内部 Site、2 个入口 Site、2 个出口 Site；并发提交至少 20 个 Task，包含普通搬运、长设备动作、分装和拆板，证明机械臂串行、设备并行、无 Site/Material 双占用且低优先级最终被老化执行。 | | |
| TASK-036 | 执行故障注入矩阵：每个阶段事务提交后强制终止进程并重启，校验 Job/Claim/Effect/ChangeSet/Outbox 的确定性状态；对 `submitting` 动作不得自动重放，对已结算未投影动作必须自动补投影。 | | |
| TASK-037 | 在真实 ROS2 工站运行最小安全 E2E：入口接收 → 机械臂搬运 → 独立设备处理 → 出口；验证 Fence、取消、设备离线、Backend 断联和 Edge 重启。测试前锁定安全区域，使用可恢复载体，不执行破坏性工艺。 | | |
| TASK-038 | 增加运维投影：候选等待原因、活动 Claim、Fence、Site 占用、Task/Job 状态、unknown、Outbox backlog、调度周期耗时。投影只读权威表，不得反向驱动调度；提供按 `backend_task_uuid/invocation_key/job_uuid/material_uuid/site_uuid` 查询入口。 | | |
| TASK-039 | 运行全仓测试、changed-function 中文文档检查、迁移重放检查和文件行数审计；报告所有超过 500 行的修改文件。完成后更新 Taskboard LOCAL-10 与 Core #164，逐条附上测试命令、提交 SHA 和运行证据。 | | |

## 3. Alternatives

- **ALT-001**: 新建独立 Go 工站调度器。未选择，因为当前瓶颈是设备 I/O 与持久一致性，不是 Python CPU；跨进程会增加协议、双写、部署和恢复复杂度，并削弱 Workflow/Inventory 的事务 Locality。Go 仍可用于未来 Backend 全局/AGV 调度。
- **ALT-002**: 继续让 Backend 下发每个 DAG 节点。未选择，因为 Backend 无法掌握工站内部有限 Site、机械臂、设备和物料的瞬时物理事实，会产生高延迟竞态与双调度权威。
- **ALT-003**: 在现有 `EdgeScheduler` 外包一层 `StationKernel`。未选择，因为删除该层后复杂性不会消失，它只是浅模块；正确做法是直接替换内存运行状态和 Listener 链。
- **ALT-004**: 合并 `workflow_history.db` 与 `inventory.db` 获得单数据库事务。v1 未选择，因为迁移面过大且现有权威已经分库；阶段协议能安全覆盖崩溃窗口。待新内核运行稳定后可单独评估，不在本次方案内。
- **ALT-005**: 一个 Job 内允许多次释放/重取 Claim。v1 未选择，因为它会让一个 Job 对应多个不可清晰恢复的物理执行阶段；工作流显式拆节点能保持一 Job、一 Effect、一 Claim 的安全模型。
- **ALT-006**: 用一个通用 `resource_lock` 表同时表示库存位置、任务预留和执行占用。未选择，因为三者的身份、生命周期、释放条件和失败语义不同，合并后无法正确处理取消、重启和物理结果未知。
- **ALT-007**: 物料被机械臂拿起后立即写 `held_by_device`。v1 未选择；普通连续 pick/place 在成功结算前保留源占用并冻结目标资源。只有确实跨 Job 长期持有物料的承载位才作为显式 Site 建模。

## 4. Dependencies

- **DEP-001**: Uni-Lab Core `CONTEXT.md` 和 Core #164 提供规范术语、仓库所有权与已接受的持久调度原则；目标协议变更必须在 Core Issue 中留证。
- **DEP-002**: Backend 必须生成并在重试中复用 `backend_task_uuid + invocation_key`，并停止向 Edge 发送中间节点作业命令。
- **DEP-003**: 领域仓库必须提供每个工站的逻辑入口/出口 Site 集合、`capacity_group/min_free_sites`、设备与机器人资源映射、工作流定义和 Adapter 配置。
- **DEP-004**: `unilab_robot_template` 必须提供稳定的机器人/导轨/夹爪执行 Interface，使 UniLabOS ExecutionKernel 不依赖具体厂商点位和工艺常量。
- **DEP-005**: SQLite 版本必须支持 WAL、partial unique index、CHECK、FK 与 `BEGIN IMMEDIATE`；部署文件系统必须保证本地磁盘语义，不使用不可靠网络文件系统承载两个权威数据库。
- **DEP-006**: Backend AGV 调度必须调用目标 Edge 入口预留和运输状态转换 Interface，并处理 Edge 离线、预留拒绝和人工取消。
- **DEP-007**: PLC 必须在机械臂命令开始边界原子执行装卸互锁与防碰撞校验，并提供稳定命令身份、明确的未开始拒绝证明、开始/完成反馈和断连后的状态查询；否则 Edge 不能安全删除软件互斥区域后自动释放相关占用。

## 5. Files

- **FILE-001**: `unilabos/workflow/runtime/` — 新工作流运行时深模块，包含 DTO、状态机、DAG evaluator、ExecutionPlan 和 runtime store。
- **FILE-002**: `unilabos/workflow/store.py`、`unilabos/workflow/service.py`、`unilabos/workflow/execution_plan.py` — 从超大遗留模块搬迁运行职责并保留最小兼容接缝。
- **FILE-003**: `unilabos/workflow/task_scheduler_bridge.py`、`unilabos/workflow/task_runtime_projection.py` — 退役旧 Task → 内存 Scheduler 桥和 Listener 投影路径。
- **FILE-004**: `unilabos/app/scheduler/kernel.py`、`admission.py`、`ordering.py`、`recovery.py`、`contracts.py` — 唯一持久调度内核与内部策略。
- **FILE-005**: `unilabos/app/scheduler/service.py`、`dag_state.py`、`models.py` — 移除内存 WorkflowRun、在途作业和资源锁权威，保留短期兼容 Adapter 后退役。
- **FILE-006**: `unilabos/app/scheduler/inventory/execution_authority.py`、`execution_schema.py`、`output_manifest.py`、`ingress.py` — 任务预留、执行 Claim/Fence、分装/拆板与入口预留深模块。
- **FILE-007**: `unilabos/app/scheduler/inventory/store.py`、`service.py` — 复用现有 SQLite 连接和物料/Site 权威，迁出新的执行事务逻辑，不继续扩大公共 Interface。
- **FILE-008**: `unilabos/app/scheduler/execution/`、`unilabos/app/scheduler/dispatch.py`、`backend.py` — 执行内核、设备 Adapter、每设备会话与旧 Dispatcher 兼容接缝。
- **FILE-009**: `unilabos/app/edge_control/station_task_protocol.py`、`runtime.py`、`http.py`、`store.py` — StationTask/AGV/结果同步协议和幂等 Outbox。
- **FILE-010**: `unilabos/app/scheduler/integration.py`、`unilabos/app/runtime_topology.py`、`unilabos/app/main.py`、`unilabos/config/config.py` — 唯一组合根与新 authority 语义。
- **FILE-011**: `unilabos/migrations/station_scheduler_v1.py`、`unilabos/workspace_host/reset_safety.py` — 数据迁移、启动前置检查与重置安全。
- **FILE-012**: `tests/workflow/runtime/`、`tests/app/scheduler/kernel/`、`tests/app/scheduler/inventory/`、`tests/app/scheduler/execution/`、`tests/networking/station_task/` — 按深模块 Interface 拆分的新测试套件。
- **FILE-013**: `CONTEXT.md`、`docs/adr/`、部署文档 — supersede 旧 Backend 控制工站内部 Scheduler/Inventory 的语义并记录不可逆决策。
- **FILE-014**: `plan/diagrams/station-scheduler-architecture.json`、`plan/diagrams/station-job-admission-sequence.json` — 可验证架构与时序图源文件。

## 6. Testing

- **TEST-001**: `WorkflowRuntime` 单元/SQLite 测试：同一 `(backend_task_uuid, invocation_key)` 重复提交只创建一个 Task/Job 集；同一 Backend Task 的不同 `invocation_key` 可多次调用同一 workflow；DAG 参数只从冻结入口与上游结果解析。
- **TEST-002**: `DagEvaluator` 属性测试：随机合法 DAG 的 ready 集与参考拓扑算法一致；失败、取消、pause、step 和 terminal 状态关闭；重启前后输出完全相同。
- **TEST-003**: `InventoryAuthority.prepare_execution` 并发测试：多个线程争用同一机器人、实际执行设备、Site 或 Material 时只有一个完整 Claim 成功；失败方没有任何部分资源成员或 Fence 泄漏。转移动作的 Claim 中不得出现来源/目标设备访问区域。
- **TEST-004**: 有限 Site 与调度排序测试：容量满时普通占用作业阻塞，释放容量作业优先；高优先级在安全边界优先，低优先级通过 aging 最终执行；运行中设备动作不被强制抢占。
- **TEST-005**: 分装/拆板测试：输出 Site 不足时动作不派发；输出 UUID/Manifest 冻结；成功结算原子创建/更新所有 Material 与 Site；重复回执幂等；额外输出被拒绝并转人工关注。
- **TEST-006**: Fence 与取消测试：旧 Fence Receipt 不能结算；cancel requested 不释放 Claim；明确 not_started/stopped 才释放；cancel timeout 与设备离线转 unknown 并冻结相关资源。
- **TEST-007**: 崩溃恢复矩阵：在 Claim 后、Intent 后、submitting 后、物理 success 后、Settlement 后、Job 投影后、Backend 上报后逐点终止并恢复，验证无重复物理动作、无资源泄漏、无虚构成功。
- **TEST-008**: StationTask 协议测试：Backend 不能提交中间节点参数；Schema catalog 只暴露输入/输出；Outbox 断线积压与连续 ACK 重放；AGV 入口预留在 `in_transit` 后不自然过期。
- **TEST-009**: 多任务虚拟工站 E2E：一个机器人与多个独立设备交叉运行，证明机器人互斥、设备并行、Site/Material 单占、Task/Job 全量上报及 Backend 断联续跑。
- **TEST-010**: 迁移与启动安全测试：历史终态保留，pending 可迁移，旧 dispatched/running 变 `requires_attention`；非法 Claim、SiteOccupancy、revision 或双 authority 配置均失败关闭。
- **TEST-011**: 真实 ROS2/PLC 工站安全 E2E：使用非破坏性载体完成入口、设备、出口流程；验证 PLC 独占承担装卸互锁，Edge Claim 不含软件访问区域；并注入 PLC `rejected_before_start`、开始后断联、设备离线、取消、Edge 重启与 Backend 断联。
- **TEST-012**: 静态质量门禁：changed-function 中文 docstring、声明 ID 唯一性、`rg` 无运行路径导入 `WorkflowRun`/`_job_resource_locks`、修改文件行数审计、全仓测试通过。

## 7. Risks & Assumptions

- **RISK-001**: 用户此前希望只使用 Backend `task_uuid` 与 Edge `job_uuid`，但同一 Backend Task 多次调用同一 workflow 时，仅凭两者无法在 Job 创建前做工站调用幂等。目标设计增加 `invocation_key`；若该键不被接受，协议和数据库唯一性必须重新设计，不能直接进入 TASK-001。
- **RISK-002**: 两个 SQLite 权威数据库不存在跨库原子提交；阶段协议只能通过“不越过派发边界”的严格顺序与恢复器保证收敛。任何 Adapter 绕过 DispatchEffect 会破坏该安全证明。
- **RISK-003**: 部分设备不能查询命令是否已经执行，也不理解 Fence。`submitting` 崩溃只能保守标记 unknown 并人工确认，会降低自动恢复率但不能牺牲物理安全。
- **RISK-004**: `capacity_group/min_free_sites` 是候选设计术语，当前规范词汇中尚未固化。若真实工站的逃生位策略不同，领域拓扑合同必须在 TASK-015 前调整。
- **RISK-005**: 旧 `control_plane=backend` 语义与目标架构直接冲突；若部署脚本、Backend 或操作手册未同步切换，可能启动错误 authority。TASK-029、TASK-031 和一次性切换必须同一发布完成。
- **RISK-006**: 现有超大文件和 Listener 测试与 `EdgeScheduler` 耦合很深。为了避免长期双实现，迁移期间会出现较大的测试重写面，必须按 Module Interface 逐阶段替换而非复制旧行为。
- **ASSUMPTION-001**: Backend 冻结的全局计划能为每个工作流调用提供稳定 `invocation_key`；推荐直接复用 Backend 全局 DAG 的调用节点标识，不要求新增 `parent_job_uuid`。
- **ASSUMPTION-002**: v1 中每个物理工作流节点都是连续安全阶段；物料不会在没有显式 Site 的情况下跨节点长期由机械臂或设备持有。
- **ASSUMPTION-003**: 分装/拆板在派发前可以确定输出载体数量上限和目标 Site 数量；若实际数量可变，Workflow 必须按最大数量预留并在成功回执中只激活 Manifest 子集，未使用预留在同一 Settlement 释放。
- **ASSUMPTION-004**: 工站 Edge 主机使用本地可靠文件系统，SQLite 写延迟与调度扫描不是 CPU 瓶颈；设备 I/O 与物理动作耗时远大于单次决策事务。
- **ASSUMPTION-005**: Backend 继续负责 AGV，但不会读写 Edge 内部 SiteOccupancy、Claim 或 DAG 状态；它只消费逻辑入口/出口预留和上报事件。
- **ASSUMPTION-006**: 工站领域包能在 Edge 启动时提供完整 Site、设备、机器人和端口映射；映射验证失败时 Edge 不上报可用 workflow，也不接受对应 StationTask。

## 8. Related Specifications / Further Reading

- [Uni-Lab-OS repository context](../CONTEXT.md)
- [Target architecture diagram](diagrams/station-scheduler-architecture.html)
- [Admission and settlement sequence](diagrams/station-job-admission-sequence.html)
- [Architecture diagram source](diagrams/station-scheduler-architecture.json)
- [Sequence diagram source](diagrams/station-job-admission-sequence.json)
- Uni-Lab Core canonical glossary: `~/Uni-Lab-Core/CONTEXT.md`
- Uni-Lab Core GitHub Issue #164: Durable Edge Scheduler Kernel and Physical Authority
