---
goal: 实现工站自治持久调度内核 v2
version: 2.2
date_created: 2026-08-30
last_updated: 2026-08-31
owner: Uni-Lab-OS Team
status: Completed
tags:
  - scheduler
  - workflow
  - inventory
  - edge-runtime
  - persistence
---

# 工站自治持久调度内核 v2 实施计划

本文档以飞书技术文档
`Y8uhwSLeui1nLVkZfVXcXpXon8d` revision 694 为规格来源，并以
`product/durable-scheduler-kernel-v2` 当前代码为实现基线。历史计划
`architecture-station-scheduler-unilabos-v1.md` 保留用于追溯，不再作为恢复状态、
输入输出特殊节点或物料拆分语义的权威。

## 1. Requirements & Constraints

- **REQ-001**: 工站调度进程是工作流任务、工作流节点作业、物料准入、库位占用、
  作业执行占用和物理结算的唯一工站内权威；Backend 只提交工作流名称和入口参数。
- **REQ-002**: 工作流任务创建时冻结不可变执行计划；运行期不得重新查询可变定义。
- **REQ-003**: 每个设备作业按“依赖、任务控制、输入物料、设备能力、库位容量、
  完整资源、原子占用与栅栏、持久派发意图、物理派发”的顺序通过九道门禁。
- **REQ-004**: 物料准入必须全有或全无；任务独占物料、共享定量试剂与库位占用是
  相互独立的持久事实。
- **REQ-005**: 作业可能影响的设备、物料和库位必须一次性形成完整占用集合；任何
  资源解析失败均关闭式拒绝派发。活动 AGV 入口预留属于库位容量事实，普通节点
  选择必须排除，不能形成两个互不知情的占用权威。
- **REQ-006**: 设备执行进程与工站调度进程只通过 HTTP 持久载荷和 WebSocket 通知
  协作，不共享可写 SQLite、Python 对象或进程内锁。
- **REQ-007**: 实际派发参数、反馈、结果和物理证据先在工站调度库提交，再由事务
  发件箱异步投影到 Backend；Backend 断线不得阻止工站内部安全推进。
- **REQ-008**: 设备执行进程重启时，已派发或运行的作业转为 `failed`，失败码为
  `execution_process_restarted`；父任务转为 `failed`，未开始的下游作业转为
  `skipped`，且 DAG 不再推进。短暂 WebSocket 断线只把在途动作标记为待对账，
  同一进程 UUID 重连后恢复；只有进程 UUID 变化才触发整任务失败。
- **REQ-009**: 业务失败不证明物理停止。仍无明确物理证据的占用、设备托管和栅栏
  必须保留，`cleanup_status` 独立进入 `requires_attention`，直至对账结算。
- **REQ-010**: 尚未越过物理派发边界的 `pending`、`ready` 或准入受阻任务可在重启
  后以原 Task/Job UUID 继续，不创建新的物理执行身份。
- **REQ-011**: 调度支持多任务交叉运行、优先级和老化；已开始的物理动作不抢占。
- **REQ-012**: AST 只编译声明式资源合同，不扫描运行时锁定/释放行为。注册表数据库
  通过 `RegistryCatalog` 接口隔离，当前阶段保留，后续可替换为启动期内存目录。
- **REQ-013**: Backend 规范调用必须携带全局 `task_uuid`、`invocation_key`、
  `workflow_id`、`revision_fingerprint`、`normalized_input` 和可选 `deadline`；Edge
  只运行指纹匹配的不可变发布修订，禁止退回同名当前定义。
- **REQ-014**: 非空输入/输出合同各编译至多一个正式纯数据节点作业；输入作业在
  创建事务内成功，输出作业只等待显式数据绑定并幂等写 `WorkflowResultRecord`。
- **REQ-015**: 任务预检使用 `runnable_now`、`temporarily_unavailable`、`invalid`
  三态并保留结构化缺口；预检不创建任务、预留或执行权。
- **CON-001**: 离心机 `batch_run`、跨任务拼批与自适应配平由本次实施明确排除。
- **CON-002**: 不引入 `source_access_zone` 或 `target_access_zone` 软件锁；PLC 继续
  负责机械碰撞互斥。
- **CON-003**: `workflow_history.db` 与 `inventory.db` 不伪装跨库原子事务；跨库操作
  使用稳定效果 UUID、幂等回执和可恢复 Saga。
- **GUD-001**: 新增或修改的函数和测试必须使用中文 docstring，并明确 UUID、状态、
  参数、返回值和异常语义。

## 2. Implementation Steps

### Phase 1 — 收敛作业主状态与重启语义

- **GOAL-001**: 删除 `execution_unknown` 作为工作流节点作业主状态的运行路径，用
  `running` 表达仍可能在物理执行，用独立清理状态表达不确定性。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-001** | 在 `unilabos/workflow/task_runtime_projection.py` 增加进程重启失败事务：原子失败在途作业和父任务、跳过未开始下游、保留不确定占用并写运行日志。 | ✅ | 2026-08-30 |
| **TASK-002** | 在 `unilabos/workflow/task_scheduler_bridge.py` 修改启动恢复：先重放已持久结果，再对仍在途的设备作业执行重启失败事务；只恢复未越过派发边界的任务。 | ✅ | 2026-08-30 |
| **TASK-003** | 在 `unilabos/package_manager/workspace_runtime/lifecycle.py` 和排空逻辑中移除 `execution_unknown` 主状态依赖，改用在途状态与 `cleanup_status`。 | ✅ | 2026-08-30 |
| **TASK-004** | 更新工作流、调度 API 和测试中的主状态集合；保留数据库迁移读取旧值的单向收敛，不再产生新旧值。 | ✅ | 2026-08-30 |

### Phase 2 — 完整门禁、占用与栅栏

- **GOAL-002**: 让一个作业只有在同一轮门禁中取得完整资源和持久派发身份后才能
  进入设备执行进程。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-005** | 在 `unilabos/workflow/execution_plan.py` 与 `workflow_spec_compiler.py` 冻结 AST 资源合同、具体或动态设备选择、具体或等价组库位选择和物料身份。 | ✅ | 2026-08-30 |
| **TASK-006** | 在 `unilabos/app/scheduler/site_target.py` 实现显式等价组按 `sort_order` 选择首个可用库位，并在容量变化时重新评估而不修改冻结计划。 | ✅ | 2026-08-30 |
| **TASK-007** | 在 `unilabos/workflow/execution_lock_lease.py` 将设备、物料和库位一次性原子申请，生成稳定 Claim UUID 与单调栅栏令牌；冲突只形成持久等待。 | ✅ | 2026-08-30 |
| **TASK-008** | 在 `unilabos/workflow/task_runtime_projection.py::project_pre_dispatch` 中固化九道门禁和持久派发效果，证明先提交派发意图再调用执行适配器。 | ✅ | 2026-08-30 |
| **TASK-009** | 在 `unilabos/app/scheduler/service.py` 收敛旧内存锁为优化缓存，调度器每轮可派发多个互不冲突作业，并实现优先级老化。 | ✅ | 2026-08-30 |

### Phase 3 — 物料、设备托管与物理结算

- **GOAL-003**: 让物料预留、事实位置、短期作业占用和装载期间设备托管分别持久化
  并以物理证据结算。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-010** | 在 `task_material_admission.py` 与 `quantity_inventory.py` 保证具体实例和共享数量准入全有或全无，并提供只读预检。 | ✅ | 2026-08-30 |
| **TASK-011** | 在库存存储中补齐库位占用、任务独占物料和 `task_while_loaded` 设备托管，装载开始前取得、最终卸载结算后释放。 | ✅ | 2026-08-30 |
| **TASK-012** | 在机械臂转运动作合同中冻结来源/目标库位、物料、机械臂及所需设备完整资源；不生成 PLC 已承担的访问区域锁。 | ✅ | 2026-08-30 |
| **TASK-013** | 在工作流发布时构建资源取得图并做静态死锁检查；无法证明安全的跨设备持有关系拒绝发布。 | ✅ | 2026-08-30 |
| **TASK-014** | 将 Edge 侧物料变化改为不可变变更集，由调度进程校验 Job、Claim、Fence 和幂等键后结算库存与库位。 | ✅ | 2026-08-30 |
| **TASK-014A** | 增加目标 Edge 入口预留状态机：运输前 TTL、运输中不自然过期、目标 Edge 原子接收或人工取消；普通 DAG 库位选择共享同一活动预留事实。 | ✅ | 2026-08-30 |

### Phase 4 — 双进程结果闭环与 Backend 投影

- **GOAL-004**: 所有设备执行证据在工站内可恢复，并能按稳定序列向 Backend 重放。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-015** | 扩展 `job_evidence.py`，在同一工作流事务内保存实际参数快照、反馈、结果、回执与工站事件发件箱。 | ✅ | 2026-08-30 |
| **TASK-016** | 在 Edge Control HTTP/WS 合同中携带 Task UUID、Job UUID、Command UUID、Claim UUID、Fence、尝试序号和结果幂等键。 | ✅ | 2026-08-30 |
| **TASK-017** | 实现工站事件发件箱发送器，使用 `event_id + station_sequence` 向 Backend 投影任务/作业状态、实际参数和结果；ACK 后再标记已发布。 | ✅ | 2026-08-30 |
| **TASK-018** | 补齐双进程 READY、独立断线、重连、结果先落 Edge 后落调度库以及调度结果待投影的恢复测试。 | ✅ | 2026-08-30 |
| **TASK-018A** | 在配套 Backend 增加 PostgreSQL/SQLite 工站事件 Inbox、连续游标、批量回放校验和精确 ACK；每个 `station_key` 使用独立绑定凭据，不复用开发者密钥。 | ✅ | 2026-08-30 |

### Phase 5 — 验证与切换

- **GOAL-005**: 以公开双进程路径证明多任务交叉运行和故障安全。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-019** | 增加两任务争抢设备、库位和独占物料，以及共享试剂并行使用的门禁测试。 | ✅ | 2026-08-30 |
| **TASK-020** | 增加机械臂转运、装载期间设备托管、静态死锁拒绝和等价库位选择测试。 | ✅ | 2026-08-30 |
| **TASK-021** | 增加派发前/后崩溃、执行进程重启、结果迟到、Backend 断线和发件箱重放测试。 | ✅ | 2026-08-30 |
| **TASK-022** | 运行目标 pytest、完整工作流/网络/工作区回归、Ruff、`git diff --check`、文件长度与仓库边界检查。 | ✅ | 2026-08-30 |

### Phase 6 — revision 694 协议与边界作业

- **GOAL-006**: 消除按名称漂移、隐式输入输出和预检状态词汇差异。

| Task | Description | Completed | Date |
|---|---|---|---|
| **TASK-023** | 工站调用支持发布修订指纹、规范输入、全局任务身份和 deadline；错误指纹关闭式拒绝，旧名称调用保留兼容入口。 | ✅ | 2026-08-31 |
| **TASK-024** | 编译并持久化正式 WorkflowInputNode / WorkflowOutputNode Job；新增唯一 WorkflowResultRecord，并按显式输出绑定投影任务结果。 | ✅ | 2026-08-31 |
| **TASK-025** | 将 TaskPreflight 顶层状态收敛为 `runnable_now`、`temporarily_unavailable`、`invalid`，保留逐项检查与 `can_run`。 | ✅ | 2026-08-31 |

## 3. Alternatives

- **ALT-001**: 在现有内存 `EdgeScheduler` 外再包一层持久调度器。拒绝，因为会形成
  两套状态推进权威。
- **ALT-002**: 把库位占用、物料预留和作业占用合并为通用锁表。拒绝，因为三者
  生命周期和物理含义不同。
- **ALT-003**: 进程重启后自动重发旧作业。拒绝，因为不能证明旧物理命令未执行。
- **ALT-004**: 两个进程共享 SQLite 或直接调用 Python 方法。拒绝，因为无法独立
  恢复和证明唯一写权威。

## 4. Dependencies

- **DEP-001**: 当前 Edge Control HTTP/WebSocket 协议和双进程 Workspace Host。
- **DEP-002**: AST 动作合同编译器、内存设备目录和领域包工作流源码。
- **DEP-003**: `workflow_history.db`、`inventory.db`、`edge_control.db` 各自迁移所有者。
- **DEP-004**: 真实设备验收需要领域工站包或经过批准且走同一公开装配路径的模拟器。

## 5. Files

- **FILE-001**: `unilabos/workflow/task_runtime_projection.py` — 作业/任务事务状态推进。
- **FILE-002**: `unilabos/workflow/task_scheduler_bridge.py` — 持久写模型与调度执行接缝。
- **FILE-003**: `unilabos/workflow/execution_lock_lease.py` — 完整占用、等待和栅栏。
- **FILE-004**: `unilabos/workflow/job_evidence.py` — 参数、反馈、结果和发件箱证据。
- **FILE-005**: `unilabos/app/scheduler/service.py` — 工站调度循环和九道门禁编排。
- **FILE-006**: `unilabos/app/scheduler/site_target.py` — 动态库位等价组选择。
- **FILE-007**: `unilabos/app/edge_control/` — 双进程命令、反馈、结果和恢复协议。
- **FILE-008**: `unilabos/app/scheduler/inventory/` — 物料、库位、共享数量和变更集权威。
- **FILE-009**: `tests/workflow/` — 状态、门禁、占用、结果和重启测试。
- **FILE-010**: `tests/networking/` — Edge Control 协议和重放测试。
- **FILE-011**: `tests/workspace_host/` — 双进程启动、排空、停止和独立重启测试。

## 6. Testing

- **TEST-001**: 执行进程重启后，在途作业和父任务失败、未开始下游跳过、DAG 不推进。
- **TEST-002**: 业务失败后无安全停止证据时 Claim/Fence 保留，清理状态需要人工处理。
- **TEST-003**: 两个 Task 对同一设备/库位/独占物料互斥，对无冲突资源并行。
- **TEST-004**: 共享试剂准入按数量全有或全无，预检不产生任何写入。
- **TEST-005**: 等价库位严格按 `sort_order` 选择首个当前可用项。
- **TEST-006**: 派发异常不能越过未持久化的 DispatchIntent；重放复用原身份。
- **TEST-007**: 实际参数、反馈和结果先写工站库，Backend 断线后仍可查询并可重放。
- **TEST-008**: 两进程独立停止时不共享写连接，安全排空后均正常退出。

## 7. Risks & Assumptions

- **RISK-001**: 当前 `task_scheduler_bridge.py`、`task_runtime_projection.py`、
  `service.py` 和 `store.py` 已超过 800 行；新增逻辑必须提取深模块，不能继续增长
  单文件状态分支。
- **RISK-002**: 旧分支测试以 `execution_unknown` 为主状态；迁移必须区分协议中的
  “未知命令列表”与工作流节点作业主状态，不能误删物理不确定证据。
- **RISK-003**: 两 SQLite 的 Saga 在掉电窗口可能短暂不一致；恢复扫描必须以稳定
  效果身份重放，不能猜测成功。
- **ASSUMPTION-001**: 本项目尚未发布需要长期兼容的历史生产数据；仍保留单向迁移
  只用于开发库可恢复性。
- **ASSUMPTION-002**: PLC 已可靠实现机械臂碰撞区互斥，本计划只管理业务设备、
  物料和库位竞争。

## 8. Maintained File Size Review

本轮修改文件中超过 500 行的文件如下。新能力已经优先提取到
`action_resource_contract.py`、`device_target.py`、`transfer_resource_set.py`、
`execution_claim.py`、`device_tenancy.py`、`execution_restart_recovery.py`、
`station_event_*.py` 等深模块；下表中的超大文件均为既有公共组合根或覆盖同一公共
接口的行为测试，没有复制外部仓库实现。

| 文件 | 行数 | 类别 | 当前决定 |
|---|---:|---|---|
| `unilabos/workflow/service.py` | 5439 | A | 保留公共服务门面；请求规范化与指纹已提取到 `station_workflow_submission.py`，服务只编排查找、持久创建和桥提交。 |
| `unilabos/workflow/store.py` | 3816 | A | 保留单一 SQLite 事务门面；本轮未另建第二写权威，后续按 A 计划迁移。 |
| `unilabos/app/scheduler/service.py` | 2640 | B | 保留唯一调度循环；资源策略、设备/库位目标和转运集合均已拆为深模块，本文件只保持门禁顺序与生命周期编排。 |
| `unilabos/workspace_host/host.py` | 2534 | C | 保留工作区生命周期状态机；本轮只调整双进程所有权和探针。 |
| `unilabos/workflow/task_runtime_projection.py` | 1870 | A | 保留同库事务投影入口；Claim、托管和重启 SQL 已拆到独立模块。 |
| `unilabos/app/scheduler/inventory/service.py` | 1797 | D | 保留库存公共服务；入口预留另由 `ingress.py` 持有。 |
| `unilabos/app/edge_control/local_authority.py` | 1835 | B | 保留本地协议权威路由；进程 UUID 对账区分临时断线与真实重启。 |
| `unilabos/workflow/task_scheduler_bridge.py` | 1618 | A | 保留工作流写模型与调度器的唯一接缝；后续按 A 计划迁移恢复逻辑。 |
| `tests/networking/test_edge_control_protocol.py` | 1642 | T | 既有完整协议合同；本轮扩展 Claim/Fence、进程身份和双凭据覆盖。 |
| `tests/workspace_host/test_workspace_host.py` | 1470 | T | 既有工作区生命周期合同；双进程场景与原启动/停止测试共享装配夹具。 |
| `unilabos/app/workflow_api.py` | 1459 | A | 保留统一工作流 HTTP 路由；新增工站调用 DTO/路由未复制业务状态机。 |
| `unilabos/app/edge_control/client.py` | 1382 | B | 保留动作进程协议客户端；HTTP 身份校验与进程 UUID 属于同一会话生命周期。 |
| `unilabos/app/scheduler/inventory/station_resource.py` | 652 | D | 统一读取设备、库位、入口预留与转运事实，避免调度器穿透库存表。 |
| `unilabos/app/scheduler/inventory/ingress.py` | 582 | D | 独立拥有 AGV 入口预留状态机、审计与接收结算。 |
| `unilabos/registry/ast_registry_scanner.py` | 1293 | E | 保留 AST 单次遍历；资源合同编译实现已拆到独立模块。 |
| `tests/workflow/test_f05_task_scheduler_bridge.py` | 1264 | T | 同一公开桥纵向合同；本轮增加断线与恢复证明，后续可按恢复/结算行为拆分。 |
| `unilabos/app/edge_control/store.py` | 1181 | B | 保留动作镜像单库事务门面；Claim/Fence 是同一 Job 身份的一部分。 |
| `tests/workflow/test_f05_task_runtime_projection.py` | 1078 | T | 同一事务状态机合同；重启失败测试需要复用既有任务夹具。 |
| `unilabos/workflow/graph_validation.py` | 934 | E | 保留发布期全图校验入口；资源取得图算法保持内部纯函数。 |
| `unilabos/registry/decorators.py` | 859 | E | 保留装饰器公共 API；本轮仅增加声明式资源合同字段。 |
| `unilabos/workflow/composition.py` | 784 | A | 组合根需要看到创建/反向关闭顺序；Backend 发布器只挂接启动与停止。 |
| `unilabos/app/scheduler/backend.py` | 733 | B | 保留执行后端组合入口；断线监听与结果监听使用同一装配面。 |
| `tests/app/test_inventory_site_allocation.py` | 731 | T | 同一库存/库位公共接口合同；多 Task 竞争共享既有数据库夹具。 |
| `unilabos/workflow/execution_plan.py` | 720 | E | 保留冻结计划编译入口；AST 资源合同解析已在 Registry 深模块完成。 |
| `unilabos/workflow/store_migrations.py` | 696 | A | 保留单库按所有者执行的幂等迁移集合，不另建运行时迁移器。 |
| `unilabos/workflow/execution_lock_lease.py` | 695 | A | 保留一个原子 Claim/Fence 事务模块；职责内聚且低于强制拆分阈值。 |
| `tests/networking/test_local_edge_authority.py` | 669 | T | 同一 HTTP/WS 权威合同；断线监听复用真实 SQLite。 |
| `tests/workflow/test_local_execution_lock_lease.py` | 652 | T | 同一 Claim/Fence 事务合同；保留竞争、重放和释放的共同夹具。 |
| `unilabos/app/web/server.py` | 651 | C | 保留 FastAPI 组合根；事件投影实现已在 `station_event_http.py`。 |
| `tests/app/test_edge_scheduler_service.py` | 634 | T | 同一调度循环合同；新增优先级与动态资源用例复用既有规格构造器。 |
| `unilabos/workspace_host/launch.py` | 632 | C | 保留双进程命令/环境变量的纯计划生成器。 |
| `tests/workflow/test_f05_workflow_spec_compiler_boundaries.py` | 609 | T | 同一冻结规格编译边界；AST、设备和库位选择共享发布夹具。 |
| `unilabos/package_manager/workspace_runtime/generation.py` | 596 | C | 既有运行配置生成器；本轮只收敛状态词汇。 |
| `tests/workflow/test_f07_task_input_execution_plan.py` | 561 | T | 入口参数与冻结计划合同；工站调用复用相同生产入口。 |
| `unilabos/app/scheduler/integration.py` | 551 | B | 保留调度器对既有设备管理器的公共集成面。 |
| `unilabos/workflow/workflow_spec_compiler.py` | 542 | E | 保留 WorkflowTask/Job 到调度规格的单一适配器。 |

强制拆分阈值（≥800 行）的后续设计：

- **A — 工作流运行写模型**：先把工站调用编排迁到
  `station_workflow_submission.py`，再把重启/断线恢复迁到
  `execution_recovery_coordinator.py`，最后让 `WorkflowService`、`WorkflowStore`、
  `TaskSchedulerBridge` 只保留事务和组合门面；迁移顺序是纯服务 → 存储仓储 →
  桥恢复，逐步运行现有 F05/F07、状态投影和发件箱测试。
- **B — 调度与动作协议**：先提取 `scheduler_resource_gate.py`，再提取
  `local_edge_session.py` 和 `edge_job_delivery.py`，保留 `EdgeScheduler` 与
  `EdgeControlClient` 的公共签名；每步用调度器、Local Authority 和 Edge Control
  协议测试证明生产路径未改变。
- **C — 工作区生命周期**：提取 `workspace_readiness.py` 与
  `workspace_process_lifecycle.py`，`WorkspaceHost` 继续拥有状态机和事件发布；以
  Workspace Host 启停、排空和 Backend 上游模式测试作为迁移门禁。
- **D — 库存服务**：按库位占用、数量库存和物料位置投影拆为内部服务，但继续
  通过同一个 `InventoryService` 暴露；以库存/库位分配和物料转运结算测试门禁。
- **E — 编译与发布校验**：把资源取得图静态检查迁到
  `resource_acquisition_validation.py`，AST 扫描器只收集语法事实；以 Registry、
  Graph Validation 和 Workflow Spec 编译边界测试门禁。
- **T — 大型测试文件**：按公开行为接缝而非行号拆成“恢复”“资源门禁”“协议重放”
  和“工作区生命周期”文件；生产夹具先提到同目录 `conftest.py`，再逐类迁移测试，
  确保测试数量和节点 ID 合同不减少。

## 9. Related Specifications / Further Reading

- 飞书技术文档：`Y8uhwSLeui1nLVkZfVXcXpXon8d` revision 694。
- `CONTEXT.md`：当前仓库规范词汇和权威边界。
- `plan/architecture-unilabos-dual-process-v1.md`：双进程既有实现与未完成项。
- `plan/architecture-station-scheduler-unilabos-v1.md`：历史讨论记录，已被本计划取代。

## 10. Verification Record

- 本功能变更测试通过 `354 passed, 1 skipped`；工作流、网络协议与工作区广域回归通过
  `1025 passed, 10 skipped`，覆盖普通断线/同进程重连、真实动作进程重启、完整
  资源门禁、入口预留互斥和结果重放。
- 配套 Backend 全仓 `go test ./...`、`go vet ./...` 与 `git diff --check` 均通过；
  PostgreSQL 和默认 SQLite 均实际迁移并提交/重放工站事件。
- UniLabOS 变更文件 Python 编译、Ruff 致命错误扫描、新深模块完整 Ruff、
  `git diff --check` 与仓库边界增量检查通过。
- 当前非 ROS Python 环境执行 `pytest tests/` 时有 39 个收集错误，均由未安装
  `action_msgs`、`rclpy`、`rosidl_parser`、完整 `unilabos_msgs` 或 Pillow 引起；
  与本功能相关且可在无 ROS 环境运行的广域回归已全部通过。
- Backend 已提供 `/api/v1/edge/station-events`；工站事件按绑定工站凭据、严格连续
  序列和精确 ACK 持久化。Backend 不可达时 UniLabOS 发件箱保留未确认事件，且
  不把网络断线误判为动作进程重启。

## 11. Current-task Change Manifest

本功能同时修改 `Uni-Lab-OS` 与配套 `uni-lab-backend-github`。以下是当前任务的
准确文件清单；用户此前未跟踪的
`plan/architecture-station-scheduler-unilabos-v1.md` 与 `plan/diagrams/` 被完整保留，
不计入本任务变更。

- 应用与双进程组合：`unilabos/app/control_plane.py`、
  `unilabos/app/main.py`、`unilabos/app/runtime_topology.py`、
  `unilabos/app/web/__init__.py`、`unilabos/app/web/server.py`、
  `unilabos/app/workflow_api.py`、
  `unilabos/config/config.py`、`unilabos/workspace_host/host.py`、
  `unilabos/workspace_host/launch.py`。
- 动作协议：`unilabos/app/edge_control/client.py`、
  `unilabos/app/edge_control/http.py`、
  `unilabos/app/edge_control/local_authority.py`、
  `unilabos/app/edge_control/store.py`。
- 工站调度：`unilabos/app/scheduler/backend.py`、
  `unilabos/app/scheduler/device_target.py`、
  `unilabos/app/scheduler/dispatch.py`、
  `unilabos/app/scheduler/integration.py`、
  `unilabos/app/scheduler/inventory/service.py`、
  `unilabos/app/scheduler/inventory/ingress.py`、
  `unilabos/app/scheduler/inventory/ingress_api.py`、
  `unilabos/app/scheduler/inventory/ingress_schema.py`、
  `unilabos/app/scheduler/inventory/station_resource.py`、
  `unilabos/app/scheduler/main.py`、`unilabos/app/scheduler/models.py`、
  `unilabos/app/scheduler/ordering.py`、`unilabos/app/scheduler/runtime.py`、
  `unilabos/app/scheduler/service.py`、`unilabos/app/scheduler/site_target.py`、
  `unilabos/app/scheduler/transfer_resource_set.py`。
- AST 与注册表：`unilabos/registry/action_resource_contract.py`、
  `unilabos/registry/ast_registry_scanner.py`、
  `unilabos/registry/decorators.py`。
- 工作流持久内核：`unilabos/workflow/composition.py`、
  `unilabos/workflow/device_tenancy.py`、
  `unilabos/workflow/execution_claim.py`、
  `unilabos/workflow/execution_lock_lease.py`、
  `unilabos/workflow/execution_plan.py`、
  `unilabos/workflow/execution_resource_policy.py`、
  `unilabos/workflow/execution_restart_recovery.py`、
  `unilabos/workflow/graph_validation.py`、
  `unilabos/workflow/job_evidence.py`、
  `unilabos/workflow/material_transfer_settlement.py`、
  `unilabos/workflow/scheduler_capacity.py`、`unilabos/workflow/service.py`、
  `unilabos/workflow/station_event_http.py`、
  `unilabos/workflow/station_event_outbox.py`、
  `unilabos/workflow/station_event_publisher.py`、
  `unilabos/workflow/station_status_projection.py`、
  `unilabos/workflow/station_workflow_submission.py`、
  `unilabos/workflow/store.py`、`unilabos/workflow/store_migrations.py`、
  `unilabos/workflow/task_runtime_projection.py`、
  `unilabos/workflow/task_scheduler_bridge.py`、
  `unilabos/workflow/workflow_spec_compiler.py`。
- 工作区包运行时：
  `unilabos/package_manager/workspace_runtime/generation.py`、
  `unilabos/package_manager/workspace_runtime/lifecycle.py`。
- 应用、协议与工作区测试：`tests/app/test_control_plane_runtime.py`、
  `tests/app/test_device_target.py`、`tests/app/test_edge_scheduler_service.py`、
  `tests/app/test_inventory_site_allocation.py`、
  `tests/app/test_scheduler_resource_module_boundaries.py`、
  `tests/app/test_station_ingress.py`、
  `tests/app/test_scheduler_drain_api.py`、
  `tests/networking/test_edge_control_protocol.py`、
  `tests/networking/test_edge_control_store_lifecycle.py`、
  `tests/networking/test_local_edge_authority.py`、
  `tests/package_manager/test_workspace_package_runtime.py`、
  `tests/registry/test_action_resource_contract.py`、
  `tests/workspace_host/test_scheduler_drain_lifecycle.py`、
  `tests/workspace_host/test_workspace_host.py`。
- 工作流测试：`tests/workflow/test_access_region_runtime.py`、
  `tests/workflow/test_device_tenancy.py`、
  `tests/workflow/test_execution_resource_policy.py`、
  `tests/workflow/test_f05_authoring_fixed_executor_projection.py`、
  `tests/workflow/test_f05_task_runtime_projection.py`、
  `tests/workflow/test_f05_task_scheduler_bridge.py`、
  `tests/workflow/test_f05_task_scheduler_bridge_failures.py`、
  `tests/workflow/test_f05_workflow_spec_compiler_boundaries.py`、
  `tests/workflow/test_f07_task_input_execution_plan.py`、
  `tests/workflow/test_job_evidence.py`、
  `tests/workflow/test_local_execution_lock_lease.py`、
  `tests/workflow/test_local_scheduler_capacity.py`、
  `tests/workflow/test_material_transfer_settlement.py`、
  `tests/workflow/test_station_event_http.py`、
  `tests/workflow/test_uncertain_resolution_api.py`。
- 动作进程轻量运行接缝：`unilabos/ros/hostlink_runtime.py`、
  `unilabos/ros/main_slave_run.py`。
- 配套 Backend：`POST /api/v1/edge/station-events` Handler/Service/Repository、
  工站绑定凭据配置、PostgreSQL/SQLite `000084_station_event_inbox` 迁移、前端能力
  声明及其单元/集成测试。
- 实施记录：`plan/feature-durable-scheduler-kernel-v2.md`。
