# 调度与执行架构

本文描述 Uni-Lab-OS 当前微后端、统一调度器、库存与 Edge 执行链路。实现入口以
`unilabos/server/`（`scheduler/`、`edge_dispatch.py`、`lifecycle/`）与 Host 侧的 `unilabos/client/host/`（`execution.py`、`edge_executor.py`）为准；四个 SQLite 库（`runtime.db` / `materials.db` /
`telemetry.db` / `history.db`）的建表 DDL 与行模型以 `unilabos/server/database/tables/`
为唯一来源，各库通过对应 Service 单 writer 写入，业务代码不跨库外键、不 `ATTACH`。
位置、锁、表清单与各进程的写入范围见 [微后端数据库](databases.md)。

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
2. `POST /api/v1/workflow-tasks` 固化 `workflow_snapshot` 和 `execution_plan`。按库位绑定的物料参数
   （`{"site_uuid": ...}`，见 [动作标准](action_standard.md)）在这里换成此刻占着该库位的物料并一起冻结；运行预检
   （`/run-preflight`）走同一条解析路径。按所属物料选择的库位参数（`{"owner_uuid": ...}`）在调度器准入任务时
   （`_admit_fresh_task`，先于整任务库存预留）选定、经入口预留占住并冻结；接受的库位都被占着时任务保持 `pending` 排队。
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
- 事件：`/api/v1/events` 上的 `workflow.runtime.changed`，`data.kind` 为 `node_run`（节点级）或
  `node_job`（attempt 级）；库内仍按 `workflow.node_run.changed` / `workflow.node_job.changed` 记录。

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

硬超时是工作流的自动继续规则，但不是释放资源的证据（ADR 0008）：执行面在驱动尚未返回时给终态附
`return_info.timeout_hold`，调度器照常结算 attempt、按决策继续，同时在同一事务里把该 attempt 的执行
Claim 转为 `timed_out`——动作 / 物料锁、使用权与准入、资源区间和设备托管都不随终态收回。后续需要
这些锁的作业（同任务后续节点、该节点的重试、其他任务）在区间准入处等待，`/scheduler/resources` 以
超时的 attempt 为阻塞者。驱动真实返回时执行面把结果作为迟到结果交给 owner（本机调度器的
`publish_job_late_outcome`；Backend-controlled 下为 runtime.v1 事件 `execution.late_outcome`），调度器
记入 `control_data.late_outcome`、不改已结算的结果，并释放这些锁；操作员也可以在核实现场后用
`execution-locks/{lease_uuid}/force-release` 释放。`timed_out` 是持久状态，跨调度权威重启保留。

attempt 状态与由它派生的占用收敛（Claim 释放、使用权 / 准入、区间、设备托管）在同一个 runtime.db
事务里提交；`_release_job_resources` 随后只释放调度器内存锁并唤醒等待者，`ResourceIntervals.recover()`
仍在启动时兜底。

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
源码也可以直接写原生 `while <条件>:`（与 if 同一套表达式，默认上限 1000 轮）和 `for i in range(n)`；
循环前赋值、循环体里重新赋值的变量在轮次之间携带，见 [静态条件流程](workflow_static_conditions.md#循环与跨轮携带的变量)；
本轮携带的值随轮次写在 `control_data.loop.carry`，重启后续用。

#### 人工装入 / 取走 / 补量（material_io）

`type="material_io"` 节点（`executor_kind=material_io`）是人工物料边界：执行到这一步时停下等人确认，
确认后在库存里装入、取走或补量。它由**权威侧调度器本地执行**，和 `condition` / `loop` 一样不派发到 Host /
edge、不占设备动作锁，执行器永远收不到它。作者写法见
[静态条件流程](workflow_static_conditions.md#人工装入--取走--补量material_io)，参数合同在
`protocol/runtime/material_io.py`，调度器侧实现在 `server/scheduler/material_io.py` 与
`BackendScheduler._start_material_io / _open_material_io / _settle_material_io`。

1. **试运行**：起跑时解析参数里的工作流输入、上游结果引用与循环占位符，再请物料权威试运行一次
   （`check_material_io`，HTTP 为 `POST /api/v1/inventory/material-io/check`）。试运行就是真正写入的那套代码，
   在保存点里执行后回滚，返回执行计划：挂载物料 / 库位 / 模板 / 物料的 UUID 与**锁定集合**——`mount`，
   unload / refill 另加 `material`。它抛出的异常与写入时相同，按 `resource_wait_policy.py` 分类：库位已有物料
   （`site_occupied`，6007）、物料被其他任务的预留 / 来源绑定 / 未结算物理动作占着（`active_claim` → `site_claimed`，
   6011）、库位入口预留是临时条件，attempt 不持有任何东西地排队（`control_data.wait_reason`），资源变化时重试；
   库位 / 模板 / 物料不存在、模板不在库位白名单、物料不在给出的库位上、`material_only` 却有内容物、挂载物料属于设备
   而 load 的模板不能实例化成 PLR 资源（`invalid_material:template`，1000，见第 4 条）等是合同错误，节点直接失败
   （`error_info[0].code=material_io_invalid`，带 `material_error_code` / `business_code`），不打扰操作员。
2. **锁定**：以 attempt 为 owner 申请锁定集合的物料锁（`SchedulerResourceRequest` 只有 `material_lock_claims`，
   没有 `current_action`），走与设备动作相同的持久区间准入、老化 FIFO 与内存锁队列。dev 的锁身份是物料级：库位的锁
   就是它所属挂载物料的锁——任何要用这个库位的动作（搬运进出、带库位选择器的动作、把挂载物料当物料参数的动作）
   都要同一把锁，于是在这一步终结前排队等待；它自己的 Claim 落 `workflow_execution_claim`（`reserved`，没有派发记录），
   重启时先于任何任务恢复。持锁后再试运行一次（排队期间库位可能已被占），通过才把计划冻结在 attempt 的
   `control_data.material_io`，此后重启只用冻结的计划，锁定集合与确认单都不变。
3. **人工确认**：复用 `manual_confirm` 的 durable 确认单（`open_workflow_manual_confirmation`；
   `timeout_seconds` 1..86400、缺省 3600；`assignee_user_ids` 必须显式给出，`[]` 表示任何人可确认）。确认单的
   `description` 是给操作员的一句话（装入什么、放到哪个库位），`param` 是去掉任务身份的执行计划，
   `meta_data.executor_kind=material_io`。确认前不改库存；它和人工确认一样计入在途作业容量，但不算设备作业
   （不阻止 runtime 排空）。调度器每 5 秒的兜底重算也把到期的确认单收为超时。
4. **一次库存命令**：批准后提交一条类型化库存命令 `material.io`（`inventory_commands.py` → `materials/material_io.py`），
   `command_id = workflow.material_io:<attempt>`，全部写入在一个 materials.db 事务里；首次结果（完成或拒绝）存在
   `inventory_processed_command`，同一 attempt 重放只返回首次结果。load 按模板建物料：注册表同步出的资源模板以资源
   条目 id 为名（`resources/adapters/registry_materials.py:registry_definition_to_template`），所以模板名就是注册表资源类名，
   与库存命令 `instantiate_material` 走同一条路（`resources/materials.py:instantiate_registry_resource` →
   `resources/adapters/plr_materials.py:plr_resources_to_create`；调度权威不扫描注册表，资源类目录是 Host 上报的生效条目，
   见 `registry/catalog.py`），建出带 PLR `config` 与默认子物料的整棵树（如枪头盒连同 96 个孔位与枪头）。根节点是这一步的
   身份：名字 / 展示名、条码、`meta_data.material_io = {task_uuid, job_uuid}`、注册表模板身份；根 `resource_id` 为了不重名
   改了时，子物料的路径前缀跟着改。模板不能实例化（没有注册表条目、不是 `pylabrobot` 类、本进程导入不了或构造失败）时：
   挂载物料属于某台设备（`find_owner_device`）就在计划 / 试运行阶段拒绝，原因写在错误里——只有身份的物料设备反序列化
   不了，写进去台面也同步不上；写库时再判一次，计划冻结后注册表条目才没了的同样拒绝、什么都不写。挂在普通货架（没有所属
   设备）上的照旧只按模板写一件物料的身份（展开模板库位）。
5. **台面同步**（负责人 2026-11-17 决定）：库存命令完成后，把变化投影到挂载物料所属设备的台面
   （`scheduler/material_io.py:sync_material_io_device`）。设备 = 从 `mount` 沿权威父链往上最近的设备物料
   （`resources/materials.py:find_owner_device`；独立根树按它登记的所属设备）。load 与 transfer 的目标端相同，发
   `MaterialDeviceSync(action="load", destination_site_uuids=[库位])`，设备从权威拉取新物料挂到同一库位；unload 发
   `action="unload"`，设备把物料连同子树从台面移除；refill 发资源树 `update`（与 `notify-device` 同一路径），设备按权威
   当前状态重载这只容器的内容物。下行经 `client/host/hostlink/downlink.py`（`send_material_sync` /
   `notify_resource_tree_update`）：调度权威与 Host 分开时经控制面交给 Host 的 `/api/v1/hostlink/{material-sync,notify-device}`
   中继（权威不查本进程设备表），Host 本进程的设备直调，托管 Slave 上的设备经 HostLink RPC；和 transfer 一样在收敛批准的
   同一调用里等设备确认（下行超时 30 秒，Host 不在线时立即失败）。结果记在节点输出
   `return_value.device_sync = {status: ok | skipped | failed, device_id, action, material_uuids, reason}`（`failed` 只出现在
   取消收口时，见下）。挂载物料不属于任何设备
   （普通货架）时 `skipped`，不告警。同步失败（设备或 Host 不在线、设备没能应用）按 transfer 的合同处理：transfer 提交权威
   位置后全部设备确认才返回成功，否则报 503（可能已经落库）、不回滚，由调用方用同一命令重试。这里调用方是调度器：库存是权威、
   不回滚，记一条中文告警，attempt 留在 `pending`、继续持锁，兜底重算（每 5 秒）按同一命令身份重提——库存命令只重放首次
   结果（不建第二份、不再加一次量），同步沿同一 `transfer_uuid` 重发，设备确认后节点才成功。不能让节点失败后重试：重试是
   新 attempt、新命令，会因为库位被自己上一次装入的物料占着而一直排队。设备一直回不来时取消任务：这一步以 `canceled`
   收口（`error_info[0].code=material_io_device_sync_failed`，输出带 `device_sync.status=failed` 与原因），库存保持已写入。

| 决策 / 结果 | 节点 | 库存 |
| --- | --- | --- |
| 批准，load 完成 | succeeded，`return_value.material = {uuid}`（`material_uuid`、`name`、`site_uuid`、`device_sync` …），后面的步骤像引用动作结果一样引用它 | 按模板建新物料（注册表资源类实例化出的整棵 PLR 树；普通货架上不能实例化的模板只写身份），放到库位上；可选登记试剂 |
| 批准，unload 完成 | succeeded，`affected_material_uuids`、`device_sync` | `detach`：解除挂载，物料留在库存；`soft_delete`：软删除（`material_with_contents` 连同子物料与内容物） |
| 批准，refill 完成 | succeeded，`quantity_added` / `quantity`、`device_sync` | 原位容器唯一试剂加量（单位可换算），记一条 `add` 内容物历史；位置不变、不新建物料 |
| 批准但写库被拒（如确认期间有人手工占了库位） | failed，`material_io_rejected` + `material_error_code`（`conflict:site_occupied`）+ `business_code`（6007） | 不变 |
| 拒绝 | canceled（`manual_confirmation_rejected`）；按取消收口：下游不派发、canceled，任务 canceled（`error_info[0].code=manual_confirmation_rejected`），整任务预留的库存与正常取消一样释放。普通人工确认（`manual_confirm`）被拒绝同样如此 | 不变 |
| 超时 | timeout（`manual_confirmation_timed_out`），任务 failed | 不变 |
| 批准、写库完成，台面同步失败 | 仍是 running（attempt `pending`），按同一命令身份重提直到设备确认，之后同上 | 已写入，不回滚，重提不再写 |
| 取消任务 | 确认单收为 `canceled`，节点 canceled；等台面同步时取消也是 canceled（`material_io_device_sync_failed`） | 确认前取消：不变；写库后取消：保持已写入 |

恢复：确认待决时重启，恢复的 Claim 继续持锁，任务接管后读回同一张确认单等决策；批准后、写库前或写库后应答前崩溃
（或物料权威暂时不可达），以及台面同步还没被设备确认时，attempt 保持 `pending` 继续持锁，兜底重算或重启接管时用同一
命令身份重提——没写过就写一次，写过就只重放结果。写库后、记终态前崩溃时，重放会再送一次台面同步，这是安全的：三种投影
都是「对齐权威现状」（load 先丢掉台面上同 UUID 的副本再挂，unload 只移除还在的，update 载入权威的绝对数量而不是增量），
命令身份同时作 `transfer_uuid`，同一设备进程已应用过的直接回 `replayed`；发送前再读一次权威，物料已不在（load / refill）或又回到（unload）这个库位时
`skipped`，不投影旧变化。只在 attempt 还是 `pending` 时发送：终态落库后同一决策再次送达（例如批准按同一幂等键重发）
不再重发同步。确认单的 `skip` 决策（`decision_action=skip`）把节点记为 skipped，不写库存。

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

`BackendScheduler` 对每个 DAG-ready 节点执行以下步骤（`_start_node`）：

1. 全局在途作业容量已满时带 `job_dispatch_capacity` 排队（5.0.1）；`material_io` 节点改走自己的路径（4.1）。
2. 就绪时才预留的库存（`on_ready`、条件分支、引用上游结果的需求）在这里预留（7）。
3. 解析上游 Job 输出到当前 action 参数，按需求 key 注入库存分配，按严格动作合同校验参数形状。
4. 从设备 action 注册信息读取 `materials_need_lock` 参数名（注册表按签名推导出的全部物料入参，
   驱动不能按动作豁免）与 `always_free` 声明
   （节点 `execution_policy.always_free` 显式声明优先）。`always_free` 只免除该动作自身的
   `(device_id, action_name)` 隐式动作锁，物料 claim 仍要获取。
5. 从 action 参数提取权威 `material_uuid`；参数未传或为空表示没有物料、不加锁；给了值却解析不出
   权威 UUID 直接失败。合并 Task 仓储 reservation 已分配的实体物料 UUID。
6. **派发前物料健全性检查**（见下文）：引用的物料此刻是否仍能用。
7. 物料 / 库位合同（`validate_resource_values`）、物理合同（`physical_contract.py`）与托管合同
   （`tenancy_contract.py`）；遇到临时资源条件排队（见下文）。
8. 构造一个 `SchedulerResourceRequest`，其中同时包含：
   - `(device_id, action_name)` 动作 claim；
   - 所有 action 参数物料 claim；
   - 所有仓储分配实体物料 claim。
9. 先过持久准入 `ResourceIntervals.admit()`（`runtime.db` 的资源区间、使用权与等待记录，
   见 5.0），通过后 `SchedulerResourceManager.acquire()` 以 all-or-nothing 方式登记内存占用。
10. 状态为 `held` 才标记 Node Job running 并下发；状态为 `waiting` 时只留在
   Scheduler 的等待集合中，执行端完全不可见。

一个 Job 不会只持有部分锁；等待顺序由持久准入决定（5.0）。

**临时资源条件只排队、不失败**（V1 `resource_wait_policy`，`scheduler/resource_wait_policy.py`）：解析动作参数与
物理合同时（`MaterialRole.site_picker` 关系的库位被占、搬运来源暂时不在库位、夹爪库位有物），或物料权威准备物理动作时
（`prepare_material_execution` 发现目标库位刚被占、另一作业的物理动作尚未结算），attempt 不记 `dispatch_not_sent`，
而是撤回已取得的 Claim、区间准入与内存锁，回到等待集合，下一次资源变化（任一作业释放资源、5 秒兜底重算）时
按首次排队先后重试；排队原因写在 attempt 的 `control_data.wait_reason`（`{code, message, waiting_since}`，作业
投影的 `wait_reason`），attempt 一直是 `pending`，重启后重建计划时再判一次。条件码与 V1 相同：`site_occupied`、
`gripper_site_occupied`、`transfer_source_site_missing`、`site_claimed`（物料权威冲突类别 `active_claim`）、
`site_ingress_reserved`、`site_group_unavailable`、`operate_in_place_site_*`。经 HTTP 物料网关时冲突类别不过线，
只剩信封业务码：6007 按 `site_occupied`、6011（活动 Claim 或入口预留）按 `site_claimed` 等待。合同错误（库位不属于所选容器、
夹爪角色缺失或重复、参数不符合 Schema、未知冲突）照常失败。这类等待者不持有资源，也不算在等设备：同一任务里
要同一台设备的兄弟节点不为它让行（腾出库位的可能正是兄弟节点）。

**派发前物料健全性检查**（`scheduler/material_sanity.py`，负责人 2026-11-17 批准）：建任务、预留库存之后到节点就绪之间，
物料可能被删除、消耗、隔离、挪走，或被别的任务拿去。节点就绪后、解析物料 / 物理合同和构造资源申请之前，调度器收集动作
引用的全部物料——严格合同的 ResourceSlot 字段（锁或免锁）、`materials_need_lock` 参数、本作业库存预留里的物料实例与
内容物容器——连同本作业的预留，向物料权威做**一次**批量只读（`material_sanity_facts`；HTTP 物料网关为
`POST /api/v1/inventory/material-sanity`，不写库、不做判定）。没有物料参数、也没有预留的节点不做这次读取。

| 检查 | 不通过时 |
| --- | --- |
| 物料还在：未删除、生命周期不是 `consumed` / `retired` / `quarantined` | 预检报错（`deleted` / `unusable`） |
| 生命周期相容：`active`，或被本作业 / 本任务的库存预留占着（`reserved` / `in_use`），或停在已结束任务留下的 `in_use`（dev 不把用过的物料改回 `active`，视为可用） | 预留它的其他任务还没结束（`reserved` 或 `in_use`）→ 排队 `inventory_reserved_by_other_tasks`；`reserved` 却没有未结束任务的预留 → 预检报错（`lifecycle_conflict`） |
| 物理合同声明的来源库位（搬运来源 owner / 库位、分步抓取来源） | 复用物理合同的结论，不重算位置：物理合同报 `transfer_source_site_missing` 时，物料被另一作业未结算的物理动作占着、或调度侧另一 attempt 正在实际使用它（持久准入 `workflow_resource_use`，可能正在搬）→ 照旧排队；否则不会自己回来 → 预检报错（`moved`：在别的库位；`off_site`：不在任何库位） |
| 库存分配与本作业持有的预留一致：预留仍是 `active` 且属于本作业，同一容器 / 批次，批次未隔离，现量 > 0，单位可换算 | 预检报错（`reservation_missing` / `reservation_inactive` / `allocation_mismatch` / `lot_missing` / `lot_quarantined` / `content_missing` / `quantity_depleted` / `unit_not_convertible` …） |
| 没有被另一作业未结算的物理动作占着（`material_execution_hold`） | 排队 `site_claimed` |

归类沿用已有的三类，不新增类别：会自己解除的条件按上面的临时资源条件排队（attempt 不持有任何东西，原因写在
`control_data.wait_reason`，资源变化或 5 秒兜底时重试）；物料没了或不能用、但操作员能处理（恢复、替换、补料、挪回原位）
的，开动作执行前预检决策（`category="preflight"`，与库存不足同一套 retry / skip / operator_intervention / abort），错误项为
`{"code": "material_sanity", "stage": "preflight", "scope": "action", "message", "materials": [{"reason", "material_uuid",
"sources", "message", "details"}]}`，`sources` 是引用该物料的参数名（库存分配记为 `inventory:<需求键>`），预留与批次 / 内容物
的项没有 `material_uuid`，在 `details` 里给 `reservation_uuid` 与需求 `key`；同一次检查里
既有要处理的项又有临时条件时先开决策。预检重试不消耗 attempt，同一 attempt 重新走 `_start_node`，恢复后通过即派发；
放弃时任务按 `node_preflight_failed` 收敛。参数不是物料、类型不符、引用从未在物料权威登记过的 UUID 属于合同 / 编程错误，
不归这里判，照常由合同校验失败。物理合同已经校验的内容（目标库位、夹爪、设备祖先）不在这里重复检查；
物料权威准备物理动作时（`prepare_material_execution`）仍会在事务内再核一次。

### 5.0 持久准入与等待顺序

准入以设备（`/devices/<id>`）和物料为键，同一事务里取得完整区间集合与实际使用权。资源被别的任务持有
就等待：写一条 `workflow_resource_waiter`（等待的键、持有者、第一次等待的 `enqueued_at`）。
资源此刻空闲也不一定立刻给：如果有排在前面、也需要其中某个键的等待者，本请求让行并排队
（`server/services/runtime/workflow/resource_waiters.py`），否则一个要多个资源的任务会被只要其中一个资源的
后到者反复插队而饿死。

- 排名与 V1 一致：有效优先级降序（任务 `priority`：`high` = 1、`normal` = 0，每等待
  `WAITER_AGING_INTERVAL_S` = 30 秒加 1），再按 `enqueued_at`、任务创建时间、任务 / 作业 UUID 升序。
  `enqueued_at` 只在第一次等待时写入，重试与权威重启都保留原位置。
- 本任务已经持有、借入或托管的键不参与排队；同一任务的等待者之间不让行；暂停的任务不占排队位置
  （恢复后按原 `enqueued_at` 回到队列）。
- 让行不会造成死锁：先到者可能正在等后到者所在任务持有的资源（例如后到任务在显式作用域里持有先到者要的
  样品，这一步只要一台空闲设备）。每次准入按当前事实重算等待图（节点是任务，持有边来自真实占用），按排名
  从前往后给等待者加让行边，会闭合环路的让行边不加——后到者直接取得空闲资源。同一状态下任何调用顺序
  得到同样的裁决。
- 等待图接口（`wait_graph`）的 `waiting_since` 就是 `enqueued_at`。

物料锁是共享资源锁（负责人 2026-11-17 决定「默认共享锁」）：物料键不进冻结的区间计划，每把物料锁的所有者
就是本次 attempt——准入时取得，正常终态释放；取消未完成、执行结果未知或超时后驱动仍在执行时继续持有。物料空闲
时不被任何任务持有，等待者直接取得，不需要借出；正在使用的物料不能借出（`lend` 要求区间没有实际使用）。
设备键不同：同一任务相邻动作的设备区间跨动作持有，空闲时只能经显式借出（`workflow_resource_custody.md`）给别的
任务用。不能借出的独占只有设备托管（`resource_contract.device_tenancy`）：装载到卸载之间样品和设备都不能借出。

派发每个 attempt 时，调度器还会按注册表 `@action(estimate_duration_fixed / estimate_duration_express)`
用最终 `action_args` 求出预计时长，写在 attempt 上（`estimated_s` / `estimate_source`）。
它只用于时间线、监控与 ETA，求值失败退回兜底，解析或写入失败都不阻断派发（见 `add_device.md`）。

### 5.0.1 调度容量

默认值同 V1（`workflow/scheduler_capacity.py`），由 `BasicConfig` 配置：全局在途作业 `scheduler_max_in_flight_jobs`
（100）、全局运行任务 `scheduler_max_active_tasks`（500）、同一工作流运行任务 `scheduler_max_tasks_per_workflow`
（100）。超出的一律排队、不失败：

- 任务：尚未开始的任务在转 `running` 之前按持久任务状态计数（`running` / `canceling`），满了就保持 `pending`，
  不建 runner、不预留库存；有任务结束或 5 秒兜底重算时按创建先后重新提交，更早排队且所在工作流还有余量的
  任务先走。重启后待启动任务照常恢复、按创建先后重新判定。
- 作业：attempt 起跑时（预留库存、申请资源之前）在途作业已满、或有更早排队等容量的 attempt，就带
  `wait_reason.code=job_dispatch_capacity` 排队，不持有任何东西；派发前在同一把锁里复核硬上限，期间被别人先占满
  就放掉取得的资源回到排队。在途作业计已派发到落终态之间的 attempt（含人工确认、待核实）。

### 5.1 可插拔的授予策略

授予策略只裁决内存资源管理器里的争用（`scheduler/grant/`）。工作流节点先过持久准入（5.0），到达内存
管理器时已经不与任何人冲突，所以正常运行时的先后由 5.0 决定；内存里的争用只出现在权威启动时恢复遗留占用、
以及不确定执行重新持锁这类路径上，这时“这一轮谁拿到完整资源集合”由授予策略决定。宿主只交给策略决策所需的事实：
按到达顺序的等待请求、已被持有的键、被 pending handoff 预留的键、哪些请求是 handoff 目标（键与 owner
在本轮编号为 `0..n` 的整数）；策略返回授予的请求下标。宿主逐条复核——下标有效且不重复、不授予 handoff
目标、与已持有 / 预留 / 彼此不相交——不合格或抛异常时整轮退回 Python 参考实现，坏的策略不会造成重复占用。
handoff、部分持有断言与下发仍由宿主负责。

默认策略是严格按键 FIFO：不相交的集合并行授予，被挡住的请求阻塞与它重叠的后来者。
`native/grant-policy` 是同一策略的原生实现：`no_std`、不分配内存的 Rust C ABI 动态库（约 4 KB），
键状态与输出缓冲区由调用方提供，导出 `uni_grant_abi_version` / `uni_grant_decide`。接口文件全部生成：
`cargo build` 的 build.rs 经 cbindgen 从 Rust 源码生成 C 头文件 `include/unilab_grant_policy.h`，
`scripts/gen_grant_bindings.py` 再从头文件生成 Python ctypes 绑定 `scheduler/grant/_abi.py`；
`python scripts/build_grant_policy.py` 一次完成编译与生成，`--check` 在生成物过期时失败（CI 用）。
CI 在 Linux / macOS / Windows 上编译它，并用随机场景与 Python 实现逐条比对；发布版本附带各平台的
`unilab-grant-policy-<platform>.zip`（linux-64 / win-64 / osx-64 / osx-arm64），解压到 `<working_dir>/native/`
（默认 `~/.unilabos/native/`）即可。`BasicConfig.scheduler_grant_policy` 取 `auto`（默认：找到原生库就用，
否则 Python）、`python`、`native`（找不到或加载失败即报错）。库位置：`BasicConfig.scheduler_grant_library`、
环境变量 `UNILABOS_SCHEDULER_GRANT_LIBRARY`，缺省 `<working_dir>/native/`。

## 6. 什么时候重算

统一 Scheduler 的重算由事实变化触发，另有一个 5 秒的兜底重算：

- 新 DAG-ready 节点登记完整资源申请时；
- Job 成功、失败、跳过、取消并释放资源时；
- Task fail-fast 或取消后，在跑 Job 真实结束时（停止后排空：不再派发，未下发的直接取消，
  已下发的只请求取消并继续持有锁，直到执行面回报它真实返回）；
- 资源 handoff 完成或取消时；
- 调度线程每 5 秒兜底重算一次（库存恢复循环）：让行等待者即使错过释放事件，最迟在下一轮重新申请；
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

1. **预留按尝试建，默认在 Task 准入时一次建齐**：每个尝试（job）一行 `inventory_reservation`。Task 准入时，
   Scheduler 把所有**不延迟**的需求（各节点运行首个尝试的 `inventory_requirements`）交给
   `MaterialsService.reserve_task_inventory()`，一个事务 all-or-nothing 建齐。延迟的需求在节点就绪时才预留：
   节点 `meta_data.inventory_mode="on_ready"`（`ctx.run(..., inventory_mode="on_ready")`），或节点带条件守卫、需求字段引用了入口参数 / 上游结果
   （`protocol/runtime/inventory.py:inventory_deferred`）；解析后的请求冻结在尝试的 `control_data.inventory_request`。
   重试的新尝试、循环的下一轮各自重新预留一次。准入时数量不足（`InsufficientInventoryError`）
   或设备/动作缺失时，任务在派发前落 `failed` + `error_info[0].code=plan_not_executable`
   （message 为权威原文，如 ``requirement 'water' is short by 440 ml``），节点运行
   `canceled`，设备不被调用；这是调度的正常业务终态，调度器只记 WARNING。
   **实物够、只是被其他任务预留**不算缺料：预留被拒后调度器用 `preview_inventory` 判定原因（每条短缺带
   `cause`：`insufficient` 实物不够 / `reserved_by_other_tasks` 被其他预留占着），全部是后者时任务在转
   `running` 之前就停下，保持 `pending` 排队，不持有任何预留或资源；排队原因写在相关 attempt 的
   `control_data.wait_reason`（`code=inventory_reserved_by_other_tasks`）。任务终结（预留释放 / 结算）或 5 秒
   兜底重算时按任务创建先后重新提交，每次重试换新的预留命令身份；重启后待启动任务照常恢复、再次排队。
   动作就绪时才预留的需求（以及重试、循环下一轮的重新预留）同理：被占着时 attempt 排队等释放，真缺料才开
   `category="preflight"` 决策。
   任务的 `inventory_bindings` 在建任务时落到冻结计划里对应的 `content` 需求（换成绑定的容器与内容物），
   之后的预留、预检与恢复都按绑定的来源进行。
   需求来源有两处：画布节点 `meta_data.inventory_requirements`，或 `@workflow` 步骤的
   `ctx.run(..., inventory=[...])`（声明时按 `InventoryRequirement` 校验，`lot_uuid`
   指定批次，`template_uuid` 由权威按 FIFO 选批次）。`InventoryRequirement` 只是节点上的
   声明，不是设备参数：权威预留后解析出的**出库内容**由调度器按需求 `key` 注入同名动作参数
   （`material` → ResourceSlot 引用 `{"uuid": material_uuid, ...}`，框架在 `send_goal`
   解析成 PLR 实例；`lot` → `{"quantity", "unit", "lots": [{"lot_uuid", "quantity"}]}`），
   设备拿到的已经是具体分配，不需要也不应该自己选 lot 或扣数量。`material` 与 `lot` 区分的是账目
   形态而不是物料种类：`material` 是有 uuid、可放到位点的实例（按件登记，
   `active → reserved → in_use`，不扣数量）；`lot` 是 `inventory_lot` 的按量库存
   （`available → reserved`，动作开始 `total/reserved` 同减），耗材同样可以走这一形态。
   `material` 需求不带 `material_uuid` 时权威按 `(created_at_ms, uuid)` 顺序选第一个
   `active` 且模板匹配的实例，不区分它是否已挂在某个台面上；要"从仓库取一件"就给该模板
   单独的实例，或用 `parent_material_uuid` / `site_uuid` 选择器限定。同一张任务同时含
   `material` 与 `lot` 需求时任一不足整张回滚——LabDeviceMaterialsDemo 的出库装板阶段
   即以此证明"板够、水不够 → 板也不会被预留"。

   第三种 `content` 是**某个容器里的一份内容物**（`material_substance`，例如试剂瓶里的 PBS）：
   `{"key": "buffer", "kind": "content", "material_uuid": <容器>, "substance_name": "PBS"
   （或 "substance_uuid"）, "quantity": 4, "unit": "ml"}`。可用量 = 内容物现量 − 其他任务仍有效的预留，
   所以多个任务共享同一瓶的余量，不足时整张回滚并说明缺多少。预留随作业预留一起写入
   `material_content_reservation`，结算规则与 `lot` 相同：动作开始扣减、未开始即失败释放、
   结果不明按预留量扣减且不退回；同一作业还在这只容器上分装时，预留留到物理结算，按分装实际取走的量记为
   已消费（来源只扣一次）。注入动作参数时，带物料锁的参数得到容器的 ResourceSlot 引用，
   否则得到 `{"material_uuid", "substance_uuid", "quantity", "unit"}`。动作开始后可经
   `POST /api/v1/workflow-node-jobs/{uuid}/reconcile-quantity` 按实际用量更正**一次**：少用的退回容器，
   多用的只能从未预留余量里扣。作业持有有效预留的内容物不能被（包括设备快照）移除，单位也不能改成
   与预留无法换算的单位；内容物单位换算一律按物态 / 浓度 / 密度进行，换算不了就拒绝。
2. **每轮资源重算都带入物料**：每个 Node 的完整资源申请包含 action 参数物料和
   reservation 分配出的实体物料，所以不同设备也不能同时操作同一物料。
3. **驱动调用前只消费一次**：Host 的 `ExecutionInventoryCoordinator`（`server/inventory.py`）派发时校验 reservation
   的 `job_uuid`、Task、revision 和 requirements；有开始许可的作业在领到许可（`begin_execution` 返回 START）之后、
   记录开始回执之前 consume，没有许可的作业在 `send_goal` 前 consume。
4. **终态收口按尝试，不按 Task**：
   - Host 收到驱动终态：成功不再改预留（动作开始时已 consume）；失败、取消或硬超时时，仍 `active`（未开始）的预留释放，
     已 `consumed` 的转 `quarantined`、数量不退回；派发前被拒、开始前被撤销的作业释放预留。
   - 调度器对账（`scheduler/inventory_recovery.py`；启动时、作业释放资源时、任务收尾时，以及每轮资源重算，含每 5 秒的兜底）按尝试的持久状态补齐：
     pending 且预留 active 时复用；在飞、结果不明、待人工时保持不动；succeeded 时 active 的预留补 consume；
     failed / canceled / timeout / skipped 时，开始许可已领用（旧作业没有开始凭据时以 `started_at` 为准）或预留已
     consumed，转 `quarantined`，否则释放——同一 Task 仍有设备托管时释放推迟到托管结束。
     对不上的情况（成功尝试的预留已被释放或隔离、可能执行过的尝试已被释放、身份或需求不一致、派发引用的预留不存在）
     记为 issue，并加全局派发暂停原因 `inventory_reconciliation`，见 [工作流跨库库存恢复](workflow_inventory_recovery.md)。
   - 实际用量：批次可以按设备返回值里的 `inventory_consumptions`（物理结算时）或
     `POST /api/v1/workflow-node-jobs/{uuid}/reconcile-quantity`（预留已 consumed / quarantined 后）各按差额更正**一次**，
     两条路径共用同一个幂等效果键 `physical.inventory:<lot>`，少用的退回批次，多用的从可用量里扣；内容物的规则见上文。

Scheduler 不直接修改 `material_substance` 快照：批次的数量权威是 `inventory_lot`，容器内容物的
数量权威是 `material_substance`（预留在 `material_content_reservation`，消费与更正记在
`material_content_history`，不写 `inventory_ledger`）。批次、物料实例与预留本身的 reserve / consume / release /
quarantine 事实进入 `inventory_ledger`。命令以
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

HostLink 主从通信位于 `unilabos/client/host/hostlink/`。Slave 同步设备 action 注册信息到 Host，
Host 因而能读取远端设备推导出的 `materials_need_lock` 和 status policy；动作执行仍由
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
| `/api/v1/edge/history` | HistoryService 数据面 |
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
- 重启前在飞、被标为 execution_unknown 的 attempt 开人工裁决，并以该 attempt 为 owner 重新登记它的
  动作锁 / 物料锁（锁表只在内存里）：操作员核对设备之前，后来的任务在同一设备、同一物料上排队；
  裁决收敛（retry / skip / 人工结果 / abort）时随 attempt 终态释放；
- 执行端只登记 active Job，不承担跨重启调度恢复。

## 13. 实现入口

| 职责 | 文件 |
| --- | --- |
| Host 组合与启动 | `unilabos/server/startup.py` |
| 四库组合根 | `unilabos/server/composition.py` |
| 运行时组件装配 | `unilabos/server/execution_composition.py` |
| Backend 命令协调 | `unilabos/client/host/edge_executor.py` |
| 执行适配 | `unilabos/client/host/execution.py` |
| 库存执行边界 | `unilabos/server/inventory.py` |
| 统一调度服务 | `unilabos/server/scheduler/service.py` |
| 动作/物料资源管理 | `unilabos/server/scheduler/resource_manager.py` |
| 临时资源条件 / 派发前物料健全性检查 | `unilabos/server/scheduler/resource_wait_policy.py`、`material_sanity.py`（物料权威侧只读事实 `unilabos/server/services/materials/sanity.py`） |
| DAG runner | `unilabos/server/scheduler/dag/` |
| Backend 诊断 API | `unilabos/server/api/runtime/diagnostics.py` |
| HostLink 网络与执行 adapter | `unilabos/client/host/hostlink/` |

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
