# 调度与执行架构

本文描述 Uni-Lab-OS 当前微后端、统一调度器、库存与 Edge 执行链路。实现入口以
`unilabos/server/backend/` 为准；四个 SQLite 库（`runtime.db` / `materials.db` /
`telemetry.db` / `history.db`）的建表 DDL 与行模型以 `unilabos/server/database/tables/`
为唯一来源，各库通过对应 Service 单 writer 写入，业务代码不跨库外键、不 `ATTACH`。

## 1. 核心约束

系统必须始终满足以下约束：

1. 一个 Job 只能由一个调度权威准入。
2. DAG 就绪、动作互斥、物料互斥和仓储分配属于同一调度决策。
3. 执行端不保存等待队列；资源冲突必须拒绝并回到调度权威重算。
4. 库存预留必须先于动作下发，库存消费必须紧邻真实驱动调用且幂等。
5. Runtime、Materials、Telemetry、History 通过各自 Service 写入；Scheduler
   不创建私有数据库、历史表或平行物料模型。
6. WebSocket 只承担短通知；完整命令、状态和恢复数据走持久化 HTTP 数据面。

## 2. 两种部署 profile

调度语义只有一套，但权威可以位于不同进程。判定依据唯一：是否显式配置了云端
Backend 地址（`HTTPConfig.remote_addr`，CLI 统一入口为 `--address`）。

| Profile | 图和 DAG 权威 | Edge 内部行为 |
| --- | --- | --- |
| 默认本机调度（未配置云端地址） | 本进程 `BackendScheduler` | 本地 Workflow Task 转 DAG；同一调度器管理动作、物料和库存；配合 edge UI 直连使用 |
| Backend-controlled（显式配置云端地址） | 远端 Backend | 本机不调度，只接收单个已调度 Job，先写 Runtime/History，再交给执行器 |

同一进程内永远只有一个调度权威。`GET /api/v1/health` 返回 `scheduler=local`
是默认且正确的状态；接入云端后返回 `remote`。

## 3. 由谁启动

CLI 运行时的启动顺序如下：

```text
app/main.py
  -> server/startup.py::setup_host_server_stack
     -> resolve_database_paths
     -> server/composition.py::configure_server_services
     -> setup_materials_service / Materials client
     -> setup_execution_backend
        -> JobExecutionBackend
        -> WorkflowBusinessCoordinator(RuntimeService, HistoryService)
     -> [未配置云端地址] setup_local_scheduler
        -> WorkflowService
        -> BackendScheduler
```

`setup_host_server_stack()` 是 Host 的唯一组合入口。它先绑定四库路径和 Materials
Authority，再创建执行层，因此不存在执行器先于库存权威工作的启动窗口。

关闭时顺序相反：先停止本地 Scheduler 和执行 worker，再关闭四库 writer。

## 4. 图如何进入系统

### 4.1 默认本机调度

默认 Host 通过本机 Workflow API（edge UI 的写入口）接收定义和 Graph：

1. `PUT /api/v1/workflows/{uuid}/graph` 保存节点和边。
2. `POST /api/v1/workflow-tasks` 固化 `workflow_snapshot` 和 `execution_plan`。
3. `WorkflowService` 调用已绑定的 `BackendScheduler.submit(task_uuid)`。
4. Scheduler 从持久化 Task/节点运行构建 `TaskDag`，恢复已完成节点后开始走图。

`/workflows` 表示 Workflow 定义；执行需另行创建 Workflow Task。

#### 节点运行与 attempt

任务内每个工作流节点对应一个**节点运行**（`workflow_node_run`，≡ runtime.v1
`attempt_group_uuid`），它是 DAG 节点键、画布节点和任务 `output` 引用的稳定身份。每次
物理执行是一个 **attempt**（`workflow_node_job`，≡ runtime.v1 `job_uuid`，即执行器
`job_id`）。节点运行上的 `status / return_info / error_info / current_job_uuid /
attempt_count` 是当前 attempt 的投影，只由 store 在同一事务里随 attempt 变更写；
结果、反馈历史、干预记录都挂在 attempt 上。

- `GET /api/v1/workflow-tasks/{uuid}/node-runs`：每节点一条，`return_info` 是当前
  （重试后的）结果，`attempts` 按 `attempt_no` 升序是运行历史。画布与结果读取用它。
- `GET /api/v1/workflow-tasks/{uuid}/jobs`、`/workflow-node-jobs/{job_uuid}`
  （含 `/results`、`/feedback-history`）：attempt 粒度，与 `/error-decisions` 报告里的
  `job_id` 一致；报告同时携带 `node_run_uuid`。
- 事件：`workflow.node_run.changed`（节点级）与 `workflow.node_job.changed`（attempt 级）。

派发以执行适配器就绪为前提：`JobExecutionBackend.host_ready()` 为假（ROS 2 host node 在
全部设备初始化后才注册适配器，晚于管理 API 与 `@workflow` 模板就绪）时，已持有资源的节点
留在等待集合，不派发也不判失败；适配器 `notify_ready` → `publish_host_ready` →
`BackendScheduler.resume_pending_dispatches` 后原样派发。

执行面按 job 的生命周期 owner（派发载荷里的 `origin`）路由 started / status / 决策挂起
回调：本机调度器拥有 `local_scheduler`，`WorkflowBusinessCoordinator` 拥有 `backend_control`，
未声明 `job_origins` 的 bridge（如旧协议镜像客户端）是观察者。失败 attempt 被挂起等待决策时，
本机调度器把 attempt 与节点运行置为 `intervention_required`，并在 attempt 的
`control_data.pending_decision` 记录决策摘要（decision_id / 选项 / retry_count / 截止时间）。

失败 attempt 进入错误决策链（`/api/v1/error-decisions`）后，本机调度器按决策收敛：

- `abort`：attempt 记 failed，节点运行投影为 failed，任务 fail-fast。
- `operator_intervention`：同一 attempt 以人工提供的 `result` 成功放行
  （`suc_type=operator_intervention`），不产生新 attempt。
- `retry`：store 在**一个事务**里把当前 attempt 记 failed（`error_resolution.selected_action=retry`）、
  为同一节点运行插入 `attempt_no+1` 的新 attempt（`retry_of_job_uuid` 指回、
  `trigger=retry_decision`）并把节点运行切回 `pending`；调度器拿到 `next_job` 后为新 attempt
  重新预留库存、申请资源并下发，DAG 节点保持运行中，任务不中断。节点运行永远不会因
  retry 而显示 failed。重试上限由注册表 `error_policy.max_retries` 决定（缺省 3），
  Host 报告只如实携带 `retry_count / max_retries`，超限的 `retry` 在本机放行时被拒绝。
- `wait`（仅 `execution_timeout` 软超时决策，`exception_type=ExecutionTimeoutException`）：
  动作从未停止，执行面按同样秒数重新计时，调度器把 attempt 与节点运行从
  `intervention_required` 收回 `running`（`control_data.pending_decision` 移入
  `resumed_decisions`）；不产生新 attempt。动作在等待期间真实完成时同样收回决策，真实结果
  照常收口。

超时看门狗在派发前解析：节点 `execution_policy.timeout_seconds` /
`execution_timeout_seconds`（正整数）显式声明优先，否则取注册表 `@action(timeout /
execution_timeout)`，软超时表达式（如 `duration * 1.5 + 30`）用最终 `action_args` 求值；
解析结果随 `execute_job` 载荷下发（`timeout_seconds` / `execution_timeout_seconds`），软超时秒数
写回节点运行的 `execution_timeout_seconds` 供前端展示。硬超时到期由执行面协作式取消动作并以
`TimeoutException` 进入本决策链，节点运行 `error_info[0].code=action_timeout`。

任务恢复（进程重启）以节点运行为判定单元：在飞的 attempt 与其节点运行同时转
`execution_unknown`；历史 failed attempt 不参与判定。设备是否已经做过这一步无法证明，
所以调度器**不重放**这些 attempt，而是把它们当成异常交给人处理：

- 任务控制态转 `waiting_reconciliation`（`attention_reason` 说明原因），调度器照常接管
  DAG；已成功节点不重跑，不依赖未知节点的分支照常执行。
- 每个 `execution_unknown` attempt（以及失败后等待决策、但决策上下文随旧进程丢失的
  `intervention_required` attempt）在 `GET /api/v1/error-decisions` 里出现一条与失败决策
  同形的报告：`exception_type=ExecutionStateUnknown`、`category=execution_state_unknown`、
  `recovered_status`，选项固定为 `retry` / `skip` / `operator_intervention` / `abort`。
  attempt 自身保持 `execution_unknown`，`control_data.pending_decision` 记录 `decision_id`。
  同一 attempt 的 `decision_id` 跨进程稳定（再次重启后旧 id 仍可提交）。
- `POST /api/v1/error-decisions/{decision_id}` 提交裁决，由本机调度器直接收敛：`retry`
  追加新 attempt 正常派发；`skip` 记 `skipped` 放行下游；`operator_intervention` 以人工给出的
  `result`（可缺省）记 `succeeded`；`abort` 记 `failed`。重试上限仍取注册表
  `error_policy.max_retries`。
- 该任务的裁决全部收敛后控制态恢复为进入前的值（`active`），任务继续走图。

#### 循环容器（for / while）

图里的控制流除了编辑器提交时求值的条件分支，还有**运行时**执行的循环：`type="loop"` 节点是
容器，`parent_uuid` 指向它的节点是循环体（可嵌套）。节点 `param` 是 `LoopSpec`
（`unilabos.protocol.runtime.loop`）：

| 字段 | 含义 |
| --- | --- |
| `mode` | `for`（固定 `count` 轮）或 `while`（每轮开始前求值 `condition`，假即结束） |
| `condition.source` | `device_state`：设备 `device_id` 的状态字段 `field`（telemetry 最新快照，即设备 `@topic_config` / status_fields 上报的值）；`node_output`：节点 `node_uuid` 最近一次成功返回值里的 `data_key`（空串取整个返回值） |
| `condition.op` / `value` | `==` `!=` `>` `>=` `<` `<=` `contains` `exists`；数值 / 布尔 / 字符串按前端条件分支同一套规则收敛 |
| `max_iterations` | while 的安全上限（缺省 1000），达到仍未结束按失败收敛（`error_info[0].code=loop_failed`） |
| `interval_seconds` | 两轮之间的等待；空循环体的 while（"等到某状态"）必须 > 0 |

执行语义与 attempt 模型同构：

- 调度器把每个循环体组装成子 DAG（跨边界的边 / `depends_on` 提升到容器所在层级：外部节点连到
  循环体里的节点 = 外部节点先于整个循环；容器与自己的后代之间不允许连边）。循环节点自己有一个
  `executor_kind=loop` 的节点运行，但没有执行器 job，由调度器直接 running → 终态。
- **每轮**开始时 store（`begin_loop_iteration`）给循环体的每个节点运行追加一个新 attempt
  （`trigger=loop_iteration`，`retry_of_job_uuid` 为空），节点运行切回 pending 再正常派发；
  上一轮的 attempt 保留为事实，前端按 attempt 历史看每一轮，`return_info` 是最后一轮的结果。
  嵌套循环的容器运行同样每轮重臂。
- 循环节点运行的 `control_data.loop` 记录当前轮次（`iteration` 从 0 起）与摘要，
  `return_info.return_value` 同步给出 `index / iteration / count`；结束时 `return_value.iterations`
  是完成的轮数。循环体节点参数里的 `{{loop.index}}` / `{{loop.iteration}}` / `{{loop.count}}`
  在派发前按最内层循环的当前轮替换（整串恰为占位符时保留 int）。
- `node_output` 条件引用的节点在本任务里还没有产出时按"继续"处理，循环体至少跑一轮——
  "重复直到探测达标"就是把探测步骤放进循环体、条件引用它。
- 循环体节点失败 / 被取消：循环节点同态收敛，外层 fail-fast 与普通节点一致。
- 重启恢复：在飞的循环 attempt 没有设备副作用，退回 pending 由调度器按
  `control_data.loop.iteration` 续跑（本轮已成功的循环体节点不重跑）；循环体里在飞的设备
  attempt 仍走上面的 `execution_unknown` 裁决。

`@workflow` 模板用 `with ctx.loop_for(3):` / `with ctx.loop_while(condition):` 声明循环体，
条件用 `ctx.device_state(device_id, field, op, value)` 或 `ctx.step_output(step_or_name, data_key,
op, value)` 构造（循环体里的步骤在 `with` 打开时还没声明，用步骤 `name` 引用，块结束时解析）。
模板节点 `kind="loop"`，成员带 `parent`；实例化时 `node_key` 换成节点 uuid。

### 4.2 Backend-controlled（接入云端）

显式配置云端地址后，Edge 不接收也不保存整张工作流图，本机 Workflow 写 API 不
挂载。远端 Backend 持有图、完成 DAG 和资源调度，然后下发一条 `execute_job` 命令：

1. Backend 经 control WebSocket 发送带 UUID、类型、sequence 和内容哈希的短通知。
2. `WorkflowBusinessCoordinator` 经 HTTP 拉取完整命令和 payload。
3. Coordinator 校验通知身份、payload 哈希、endpoint 和 transport。
4. 命令先写 `runtime.command_inbox`，payload 写 `history.payload_object`。
5. 创建或恢复 `runtime.execution_job`，推进到 `dispatch_pending`。
6. 将规范化执行 payload 交给 `JobExecutionBackend`。

Edge 不根据单个 Job 反推整图，也不在本地创建 retry。此模式下 retry 是 Backend 新建的
attempt 和新 `job_uuid`（与 4.1 本机调度器的 retry 语义相同，只是调度权威在远端）。
软超时决策的 `wait` 对应 `resume_pending` 命令：Edge 关闭已打开的终态闸门（job 从
`terminal_waiting` 回到 `running`）、重新计时，并回发 `execution.error_resumed` 事件；动作在等待
期间真实完成时 Edge 同样先发该事件再发终态事件。

## 5. 一轮统一调度如何进行

`BackendScheduler` 对每个 DAG-ready 节点执行以下步骤：

1. 解析上游 Job 输出到当前 action 参数。
2. 从设备 action 注册信息读取 `materials_need_lock` 参数名（未声明时注册表已按物料入参默认填好，
   `materials_free` 排除的除外）与 `always_free` 声明
   （节点 `execution_policy.always_free` 显式声明优先）。`always_free` 只免除该动作自身的
   `(device_id, action_name)` 隐式动作锁，物料 claim 仍要获取。
3. 从 action 参数提取权威 `material_uuid`；参数未传或为空表示没有物料、不加锁；给了值却解析不出
   权威 UUID 直接失败。
4. 合并 Task 仓储 reservation 已分配的实体物料 UUID。
5. 构造一个 `SchedulerResourceRequest`，其中同时包含：
   - `(device_id, action_name)` 动作 claim；
   - 所有 action 参数物料 claim；
   - 所有仓储分配实体物料 claim。
6. `SchedulerResourceManager.acquire()` 以 all-or-nothing 方式申请完整集合。
7. 状态为 `held` 才标记 Node Job running 并下发；状态为 `waiting` 时只留在
   Scheduler 的等待集合中，执行端完全不可见。

资源管理器使用稳定登记顺序避免后来者越过有冲突的早期等待者，也不会让一个 Job
只持有部分锁。

### 5.1 可插拔的授予策略

“这一轮谁拿到完整资源集合”由授予策略决定（`scheduler/grant_policy.py`）。宿主只交给策略决策所需的事实：
按到达顺序的等待请求、已被持有的键、被 pending handoff 预留的键、哪些请求是 handoff 目标（键与 owner
在本轮编号为整数）；策略返回授予的请求下标。宿主逐条复核——下标有效且不重复、不授予 handoff 目标、
与已持有 / 预留 / 彼此不相交——不合格或抛异常时整轮退回 Python 参考实现，坏的策略不会造成重复占用。
handoff、部分持有断言与下发仍由宿主负责。

默认策略是严格按键 FIFO：不相交的集合并行授予，被挡住的请求阻塞与它重叠的后来者。
`native/grant-policy` 是同一策略的原生实现（Rust 编译出的 C ABI 动态库，导出
`uni_grant_abi_version` / `uni_grant_decide` / `uni_grant_free`，输入输出是小端 u32 序列），
进程内经 ctypes 调用；CI 在 Linux / macOS / Windows 上编译它，并用随机场景与 Python 实现逐条比对。
`python scripts/build_grant_policy.py --install <working_dir>/native` 编译并安装；
`BasicConfig.scheduler_grant_policy` 取 `auto`（默认：找到原生库就用，否则 Python）、`python`、
`native`（找不到或加载失败即报错）。库位置：`BasicConfig.scheduler_grant_library`、
环境变量 `UNILABOS_SCHEDULER_GRANT_LIBRARY`，缺省 `<working_dir>/native/`。

## 6. 什么时候重算

统一 Scheduler 没有独立定时轮询。重算由事实变化触发：

- 新 DAG-ready 节点登记完整资源申请时；
- Job 成功、失败、跳过、取消并释放资源时；
- Task fail-fast 或取消后，在跑 Job 真实结束时（停止后排空：不再派发，未下发的直接取消，
  已下发的只请求取消并继续持有锁，直到执行面回报它真实返回）；
- 资源 handoff 完成或取消时；
- 进程恢复后重新提交可恢复 Task 时。

每次 `acquire/release/cancel` 都会让 `SchedulerResourceManager` 重新提升可满足的等待
请求；释放后 `BackendScheduler._reconcile_resources()` 只下发本轮已经变为 `held`
的 Job。DAG runner 收到节点终态后，再计算新的 ready 节点。

终态顺序固定为：

```text
持久化 Node Job 终态
  -> 释放动作和物料 claims
  -> 重算并提升等待 Job
  -> 通知 DAG runner
  -> 计算新 ready 节点
```

这保证下一个 Job 起跑前，上一个 Job 的结果已经可供参数解析和审计查询。

## 7. 物料和仓储扣减语义

“每轮调度都带着物料和仓储”不等于“每轮重复扣库存”。正确事务边界是：

1. **Task 准入时一次性预留**：Scheduler 将该 Task 所有声明的
   `inventory_requirements` 交给 `MaterialsService.reserve_task_inventory()`，使用
   一个事务 all-or-nothing 创建 Job reservation。数量不足（`InsufficientInventoryError`）
   或设备/动作缺失时，任务在派发前落 `failed` + `error_info[0].code=plan_not_executable`
   （message 为权威原文，如 ``requirement 'water' is short by 440 ml``），节点运行
   `canceled`，设备不被调用；这是调度的正常业务终态，调度器只记 WARNING。
   需求来源有两处：画布节点 `meta_data.inventory_requirements`，或 `@workflow` 步骤的
   `ctx.run(..., inventory=[...])`（声明时按 `InventoryRequirement` 校验，`lot_uuid`
   指定批次，`template_uuid` 由权威按 FIFO 选批次）。`InventoryRequirement` 只是节点上的
   声明，不是设备参数：权威预留后解析出的**出库内容**由调度器按需求 `key` 注入同名动作参数
   （`material` → ResourceSlot 引用 `{"uuid": material_uuid, ...}`，框架在 `send_goal`
   解析成 PLR 实例；`lot` → `{"quantity", "unit", "lots": [{"lot_uuid", "quantity"}]}`），
   设备拿到的已经是具体分配，不需要也不应该自己选 lot 或扣数量。两种 `kind` 区分的是账目
   形态而不是物料种类：`material` 是有 uuid、可放到位点的实例（按件登记，
   `active → reserved → in_use`，不扣数量）；`lot` 是 `inventory_lot` 的按量库存
   （`available → reserved`，动作开始 `total/reserved` 同减），耗材同样可以走这一形态。
   `material` 需求不带 `material_uuid` 时权威按 `(created_at_ms, uuid)` 顺序选第一个
   `active` 且模板匹配的实例，不区分它是否已挂在某个台面上；要"从仓库取一件"就给该模板
   单独的实例，或用 `parent_material_uuid` / `site_uuid` 选择器限定。同一张任务同时含
   `material` 与 `lot` 需求时任一不足整张回滚——LabDeviceMaterialsDemo 的出库装板阶段
   即以此证明"板够、水不够 → 板也不会被预留"。
2. **每轮资源重算都带入物料**：每个 Node 的完整资源申请包含 action 参数物料和
   reservation 分配出的实体物料，所以不同设备也不能同时操作同一物料。
3. **驱动调用前只消费一次**：`ExecutionInventoryCoordinator` 校验 reservation
   的 `job_uuid`、Task、revision 和 requirements，然后在 `send_goal` 前 consume。
4. **终态收口**：未 consume 的 active reservation 在 Task 终态释放；已经 consume
   的数量不返还，失败或取消的实体物料进入 quarantine，成功按账本完成。

Scheduler 不直接修改 `material_substance` 快照，数量权威是 `inventory_lot`，所有
reserve/consume/release/quarantine 事实进入 `inventory_ledger`。命令以
`(command_uuid, effect_key)` 幂等，因此恢复和重复投递不会重复扣减。

## 8. Edge 内部通信机制

```text
Backend WebSocket notice
  -> Coordinator HTTP fetch + hash validation
  -> RuntimeService / HistoryService durable write
  -> JobExecutionBackend.dispatch
  -> active-only DeviceActionManager
  -> worker event queue
  -> HostLink adapter or ROS2 adapter
  -> device action
  -> feedback/result callback
  -> Coordinator
  -> Runtime transition + History append + Backend event outbox
```

需要区分两种“队列”：

- `JobExecutionBackend` 的 worker queue 只做线程间事件串行化，不参与排序，也不保存
  等待资源的 Job；
- Scheduler 的 waiting request 才是调度等待状态，且只存在于唯一调度权威。

执行层仍保留最后一道防线：同一动作已有 active Job、状态 incident 持有设备、库存
reservation 不合法或物料 UUID 契约错误时，立即拒绝。它不会在 Edge 内部悄悄排队。

HostLink 主从通信位于 `unilabos/backend/hostlink/`。Slave 同步设备 action 注册信息到 Host，
Host 因而能读取远端设备的 `materials_need_lock` 和 status policy；动作执行仍由
HostLink adapter 路由到实际 Slave。

## 9. 四库与 Service

四库组合根是 `unilabos/server/composition.py::ServerServices`。

| 数据库 | 调度/执行使用方式 |
| --- | --- |
| `runtime.db` | 命令 inbox、execution job 状态机、endpoint、可靠 adapter/backend outbox |
| `materials.db` | Material/Site、lot、Task/Job reservation、库存 ledger |
| `telemetry.db` | endpoint/device latest 状态和追加事件，供状态联锁投影读取 |
| `history.db` | payload、feedback、result、error、decision 和 replacement chain |

Store 基座负责 SQL 与事务，Service 负责幂等和状态机。Scheduler 和执行层不得
直接创建 SQLite connection 或声明业务表。跨库只使用规范 UUID，不使用
`ATTACH DATABASE` 或跨库外键。

本机调度的 Workflow 定义、Graph 和 Task 事实由 `WorkflowService` 的
`WorkflowStore` 基座保存；它不是 Runtime/History 的替代物。Backend-controlled
执行的完整 Job 生命周期始终进入 Runtime 和 History。

## 10. HTTP 面

默认 Host 挂载：

| 前缀 | 语义 |
| --- | --- |
| `/api/v1/runtime` | RuntimeService 数据面 |
| `/api/v1/materials` | MaterialsService 数据面；外置 Materials 时不重复挂本地写 API |
| `/api/v1/telemetry` | TelemetryService 数据面 |
| `/api/v1/history` | HistoryService 数据面 |
| `/api/v1/health` | Scheduler 位置和执行器 readiness |
| `/api/v1/hostlink/peers` | Host/Slave 连接诊断 |
| `/api/v1/status-incidents` | 状态联锁事件与人工决策 |
| `/api/v1/error-decisions` | 动作失败终态决策 |
| `/api/v1/scheduler/resources` | 本机调度器的动作/物料资源快照 |
| `/api/v1/workflows`、`/api/v1/workflow-tasks` | 本机 Workflow Authority 写 API（默认挂载；接入云端后不挂载） |

诊断 Router 不复制 Runtime、History 或 Telemetry 的业务查询。接入云端后请求
本地 scheduler resource snapshot 会返回 503，以明确表示 authority 不在该进程。

## 11. 状态联锁与错误闸门

Telemetry latest 投影用于执行前状态策略判断。违反 hold/reject 条件时创建 status
incident，并拒绝新动作；恢复条件满足或人工决策后解除 hold。

设备动作失败时先保存原始失败 payload 和 History event，再打开 Runtime error gate。
Backend 必须先更新调度事实，然后下发 `release_failed` 或 replacement result 决策。
人工替换通过 `supersedes_event_uuid` 关联原结果，不能覆盖原始历史。

## 12. 恢复与幂等

- Backend 命令由 `command_uuid` 和 backend sequence 去重；
- Runtime job transition 使用 version 校验；
- Materials mutation 使用 command/effect 幂等；
- Coordinator 启动时恢复未完成 dispatch、error gate 和可靠 outbox；
- 本地 Scheduler 启动时查询可恢复 Workflow Task，并从持久化 Job 终态重建 DAG walk；
- 执行端只登记 active Job，不承担跨重启调度恢复。

## 13. 实现入口

| 职责 | 文件 |
| --- | --- |
| Host 组合与启动 | `unilabos/server/startup.py` |
| 四库组合根 | `unilabos/server/composition.py` |
| 运行时组件装配 | `unilabos/server/backend/composition.py` |
| Backend 命令协调 | `unilabos/server/backend/coordinator.py` |
| 执行适配 | `unilabos/server/backend/execution.py` |
| 库存执行边界 | `unilabos/server/backend/inventory.py` |
| 统一调度服务 | `unilabos/server/backend/scheduler/service.py` |
| 动作/物料资源管理 | `unilabos/server/backend/scheduler/resource_manager.py` |
| DAG runner | `unilabos/server/backend/scheduler/dag/` |
| Backend 诊断 API | `unilabos/server/api/runtime/diagnostics.py` |
| HostLink 网络与执行 adapter | `unilabos/backend/hostlink/` |

## 14. 扩展约束

### 工作流单点放行

`POST /api/v1/workflow-tasks` 使用 `run_mode="step"` 时仅创建任务，初始为 paused。
运行详情里的 `control_revision` 是已受理控制命令的版本。向
`POST /api/v1/workflow-tasks/{task_uuid}/commands` 提交
`{"type":"step","expected_revision":0,"idempotency_key":"本次点击的唯一标识"}`，
微后端仅给一个依赖就绪的叶动作分配许可，之后再申请原有动作/物料锁。
执行结束回到 paused；循环体的下一动作或下一轮仍需新的许可。
逐步运行不是 `single_node` 子图或 ad-hoc 单点任务，Task / NodeRun / Job 身份保持不变。

使用 `type="resume"` 从原进度转自动，不中断正在执行的动作，也不重跑成功节点。
自动运行中的任务可用 `type="pause"` 转为逐步：已下发的动作照常跑完；尚未下发的节点不再派发，
排队等锁的申请撤回（`lock.owner_canceled`，reason=`task_paused`），放行或恢复后重新申请、排到队尾。
不提供跳过异常的快捷路径：失败继续由原错误策略处理；retry 追加的新 attempt 也需再次放行。
同键重传幂等、旧版本及在飞期间重复 step 返回既有业务码 3003（HTTP 200）。
命令及其 job_uuid / node_run_uuid / target_node_uuid 关联存在既有 workflow_task_command，
不改 schema、不新增设备锁机制；库存仍按原整任务策略预留。
停机保留步进进度；恢复时在飞 attempt 走 execution_unknown / waiting_reconciliation，禁止盲目重放。

### 通用约束

- 调度、库存、历史和设备状态能力必须复用对应领域 Service。
- 数据库表及 DDL 只在 `unilabos/server/database/tables/` 声明。
- schema 变化必须提升对应库的 `schema_version` 并补迁移（见 `unilabos/server/database/schema.py`）；Scheduler 目录不得创建私有表。
