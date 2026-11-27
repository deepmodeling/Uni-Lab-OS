---
goal: Workflow Resource Locking Durable Scheduler Kernel v3
version: 1.0
date_created: 2026-09-04
last_updated: 2026-09-04
owner: Uni-Lab-OS Scheduler
status: In progress
tags: [architecture, scheduler, resource-locking, deadlock, workflow, safety]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

本计划把附件《Uni-Lab 工作流资源锁架构设计》和《子工作流与资源锁编写指南》落地为
`Uni-Lab-OS` 内的调度内核改造。当前仓库已经有 `ResourceDict`、物料/Site 锁、
`station_execution_claim`、`station_execution_lock_lease` 和全有或全无的派发准入；
缺口是没有一个贯穿作者语言、冻结执行计划、整站资源绑定和运行时跨 Job 所有权的
“资源占用区间”深模块。本计划先建立该深模块的接口和不可变计划产物，再接入现有
Inventory/Claim/Fence seam；实现阶段不得把锁语义放入前端、实验室仓库或机器人模板。

本计划针对分支 `product/durable-scheduler-kernel-v3`，仓库为
`~/Uni-Lab-OS`。附件文档是资源锁语义的权威来源；若实现与本文
冲突，以附件中的已确定决策为准，并通过新的诊断而不是静默降级解决冲突。

本轮已落地一条可运行纵切片：资源计划深模块、模板/绑定计划序列化与静态环检、
作者根/词法资源声明及固定点往返、执行计划与 WorkflowSpec seam、Action resource
contract v2 的参数角色规范化，以及 `tests/scheduler_core/test_resource_lock_plan.py`
模块测试。Inventory 事实绑定、pick/place 完整搬运、跨 Job Claim/lease 所有权和
运行时按计划释放仍属于后续阶段，未在本轮伪装为已完成。

## 1. Requirements & Constraints

- **REQ-001**: 作者语言只提供三种资源声明：Action 默认资源、`@workflow(resources=(...))` 根资源和 `with resources(...)` 词法范围；不得新增 `phase`、`acquire()` 或 `release()` 语义。
- **REQ-002**: 第一版只支持单实例独占资源。资源必须在站点绑定后解析为稳定的设备、Material、Site、工具或运动资源实例；共享读锁、容量资源和多实例池另行设计。
- **REQ-003**: 每个资源申请边界必须是全有或全无；同一边界内不得先持有一部分再等待另一部分。已有外层持有资源可以复用，但新增集合必须原子取得。
- **REQ-004**: 同一路径的同名资源合并为一项实际所有权，内层退出不得释放仍被外层声明覆盖的资源；并行兄弟的局部声明必须保留 branch identity，不能按 root task id 合并。
- **REQ-005**: 直接后继继续使用同一资源时默认连续持有，可跨 group、子 Workflow、结构节点、并行 fork/join；join 后继必须先通过兄弟分支自锁检查。
- **REQ-006**: 根 `resources` 与 `with resources(...)` 是硬边界。编译器不得为消除环而静默缩短、扩大、上收或下推显式范围；只能报出可定位诊断。
- **REQ-007**: `pick`/`place` 必须按 Material 配对。`pick` 可调度前要解析来源、目标、搬运器、工具/夹爪、地轨或运动区，先预留目标 Site，再整组取得完整搬运集合。
- **REQ-008**: 资源绑定后的整站计划必须组合所有可能并发的 Workflow/样品实例，记录 `H -> N` 取得关系并做完整有向无环检查；不能用预计动作时长证明安全。
- **REQ-009**: 运行时只执行已编译的资源计划：校验请求存在、原子授予增量集合、依赖/可用性排队，并在物理完成和安全交接证据成立后按计划释放。
- **REQ-010**: 计划必须输出可持久化、可解释的资源区间、取得集合、关系边、释放点、来源节点/分支/条件和资源绑定结果，供日志、锁泳道、恢复和评审复核。
- **SEC-001**: Action/driver 申请计划外资源、未知别名、一个别名对应多个实例、Site owner 不一致或无法证明物理事实时，必须拒绝派发，不能把未知资源省略为“无锁”。
- **SEC-002**: 失败、取消、未知物理结果和进程重启不得自动释放仍可能承载物料/工具/运动状态的资源；必须保留 lease/Claim，等待设备确认或人工恢复。
- **CON-001**: 只修改 `Uni-Lab-OS`。不得导入或复制 sibling repository（`uni-lab-fe`、`unilab_robot_template` 或实验室仓库）的实现、设备 ID、Site 常量或领域装配。
- **CON-002**: `ResourceDict` 仍是内存资源模型的唯一权威；`StationResourceInventory` 是设备、Site、rail、tool 和物料事实的唯一运行时读取 seam；调度器不得直读库存 SQL。
- **CON-003**: 兼容旧 `execution_plan` 版本 1/2。没有资源声明的旧计划继续按现有路径运行；带新资源声明的任务必须携带资源计划能力标记并通过新校验，不能半启用。
- **CON-004**: 先复用现有 `execution_plan` JSON、Claim/Fence 和 lease 表，不为资源区间另建必需数据库表；只有当查询/审计需求证明 JSON 不足时才新增迁移。
- **GUD-001**: 在 `unilabos/workflow/resource_lock_plan.py` 放置深模块，把区间计算、嵌套合并、静态环检测、诊断和序列化隐藏在小 Interface 后；调用方只提交冻结图、资源事实和并发上下文。
- **GUD-002**: 所有公开字段使用稳定 UUID/规范键和中文诊断；函数同时说明输入事实、排序约束、错误模式和不可变性。Interface 是单元测试和运行时 Adapter 的共同 seam。
- **GUD-003**: 资源释放只由 Action 完成证据、稳定 Site/交接事实、显式作用域出口和计划边界共同决定；任何时间估计只能用于队列优先级，不参与安全证明。
- **PAT-001**: 保留现有 `resolve_execution_resource_policy`、`validate_static_device_tenancy_order`、`resolve_transfer_resource_set` 的兼容能力，把它们作为新资源计划深模块的 Adapter/专用校验，不在多个调用方复制规则。
- **PAT-002**: 模板计划和整站绑定计划分两级生成。模板只保留符号资源和可能关系；绑定计划才允许进入静态无环证明和可运行任务快照。

现状证据：`authoring.py` 尚无 `resources` marker；`authoring_ast.py` 的
`WorkflowProgram`/`_BodyState` 没有资源范围；`execution_plan.py` 只调用设备
托管顺序检查并输出 nodes/edges/handles；`WorkflowSpec` 和 `WorkflowNode` 没有
区间身份；`service.py` 以 `_job_resource_locks` 按 Job 立即释放；
`TransferResourceFacts` 没有 rail/motion 资源。上述差异是本计划的实现边界，
不是本分支新引入的缺陷。

## 2. Implementation Steps

### Implementation Phase 1 — 资源计划深模块与合同版本

- **GOAL-001**: 建立可独立测试的资源占用区间 Interface，并把 Action 资源合同扩展为可解析、可绑定、可诊断的版本化数据。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 新建 `unilabos/workflow/resource_lock_plan.py`。定义不可变 `CanonicalResource`（`resource_id`、`canonical_key`、`kind`、`alias`、`instance_uuid`）、`ResourceScope`（`scope_id`、`kind`、`parent_scope_id`、`entry_node_uuid`、`exit_node_uuid`、`hard_boundary`）、`ResourceInterval`（资源、取得/释放事件、scope、workflow instance、branch、condition、physical state、safe-release flags）、`AcquireSet`（`resource_ids`、`preheld_resource_ids`、`atomic=True`）、`ResourceRelation`（`from_resource_id`、`to_resource_id`、source interval/node、possible concurrency、reason）和 `ResourcePlan`（`version=1`、`plan_id`、`binding_state`、resources/scopes/intervals/acquire_sets/relations/diagnostics）。只公开 `compile_template_resource_plan(graph, root_scopes)`, `bind_station_resource_plan(template_plan, facts, concurrency)`, `validate_resource_plan(plan)`, `serialize_resource_plan(plan)` 和 `resource_plan_for_node(plan, node_uuid)`；所有返回值深拷贝/冻结，禁止调用方直接改内部集合。 | ✅ 2026-09-04 |
| TASK-002 | 在 `unilabos/registry/action_resource_contract.py` 的规范化与 Schema 校验中加入合同版本 2 的 `resource_params`（`[{"param": <ResourceSlot 字段>, "role": "device|tool|motion|site|material"}]`）和 `transfer.motion_resource_roles`/`transfer.tool_resource_roles`。旧 `required_device_params` 规范化为 role=device 的兼容项；同一参数重复或角色冲突报稳定 `ActionResourceContractError`。扩展 `validate_action_resource_contract_schema` 检查 ResourceSlot/字符串类型和 role 值；`unilabos/workflow/execution_resource_policy.py` 的 merge/normalize/resolve 只能补充超时、Site group 等工作流字段，不能覆盖动作资源语义。 | 部分：合同 v2 规范化与角色校验已完成，policy/Inventory 传播待后续 |
| TASK-003 | 在 `unilabos/workflow/execution_plan.py` 增加 `RESOURCE_PLAN_VERSION=1`、`RESOURCE_PLAN_CAPABILITY="resource_intervals_v1"`、`RESOURCE_DEADLOCK_CAPABILITY="static_resource_dag_v1"`；序列化计划时把 `resource_plan` 放入现有 `execution_plan` JSON，保留 `PLAN_VERSION`/`CONTROL_PLAN_VERSION` 的旧值。更新 `workflow_spec_compiler.py` 的版本/能力校验：旧计划不要求新字段，新能力计划缺少 `resource_plan`、`binding_state="bound"` 或 DAG 证明时返回确定性的 `invalid_resource_plan`。 | 部分：执行计划/WorkflowSpec 接入与 bound 校验已完成，任务创建绑定待后续 |

Phase 1 completion criteria: `resource_lock_plan.py` 在不依赖 Scheduler、SQLite 或设备
运行时的情况下能构造一组嵌套/并行/转运样例；合同 v1 仍通过现有测试；合同 v2 的
未知字段、重复参数、非法 role、未绑定资源和非原子 acquire set 均被拒绝；旧计划
fixture 经 `WorkflowSpecCompiler` 编译结果不变。

### Implementation Phase 2 — 作者语言、AST 与图投影

- **GOAL-002**: 让作者可以静态声明根资源、词法资源范围和 Action 默认资源，并把作用域/分支身份完整冻结到候选图，保证 round-trip 不丢语义。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 在 `unilabos/workflow/authoring.py` 增加 `resources(*resource_ids: str)` 的不可执行 `_ResourceBlock` marker，校验非空、去首尾空格、无重复、无动态值；`workflow(**metadata)` 保留 `resources` JSON 字面量供 AST 读取。禁止提供 `acquire`/`release`。 | ✅ 2026-09-04 |
| TASK-005 | 在 `unilabos/workflow/authoring_ast.py` 的 `_AUTHORING_MARKERS`、`_workflow_declaration`、`WorkflowProgram`、`_BodyState`、`_parse_statement` 和 `_with_marker` 中加入资源语法。`@workflow(resources=(...))` 只能接收字符串 tuple/list 字面量；`with resources(...)` 只能接收同样的字符串字面量；为每个词法范围生成稳定 `scope_id`、父范围、入口/出口和 `hard_boundary=True`。资源声明不创建虚拟执行节点，不改变 group/parallel/repeat 的执行边。 | ✅ 2026-09-04 |
| TASK-006 | 在 `unilabos/workflow/authoring_ast.py` 的 Action 声明解析中读取模板 `resource_contract`，把 Action 默认资源来源、transfer 的 pick/place 配对键、safe-release/physical-state 事实写入 AST IR；拒绝无法静态识别物料身份、动态资源表达式和 pick 没有对应 place 的图。 | | |
| TASK-007 | 在 `unilabos/workflow/authoring_graph.py` 的 `build_authoring_graph`、候选节点生成和 composite invocation 展开中写入 `meta_data.unilab.resource_scopes`、`resource_defaults`、`resource_transfer_bindings` 和根 `authoring_root_fields += resources`。在 `unilabos/workflow/authoring_python.py` 的 `_authoring_root_fields`、资源范围渲染和解析辅助函数中恢复 `@workflow(resources=...)`/`with resources(...)`；旧图缺字段时保持旧源码输出。 | 部分：root/with 图投影与 round-trip 已完成，Action/composite resource_defaults 待后续 |
| TASK-008 | 在子 Workflow 发布/展开 seam 增加资源合同传播：父调用节点保存子计划的根资源与 scope 边界，`ExecutionPlanGraphNormalizer.flatten_composite_edges` 展开后保留 `workflow_instance_id`、parent scope 和 branch identity；子资源不得静默上收到父根。Site owner 与显式资源不一致、别名未解析或一对多解析在创作发布阶段失败。 | | |

Phase 2 completion criteria: 静态作者源码可在 AST → candidate graph → Python round-trip
往返后保持 root/with/action 三类声明、嵌套边界和 parallel branch identity；旧作者
语法输出无变化；所有动态资源和隐式 acquire/release 示例得到稳定错误码；复合
Workflow 的资源范围在展开后仍可定位到原始 workflow instance。

### Implementation Phase 3 — 执行计划、整站绑定与静态无环证明

- **GOAL-003**: 把图、Action 合同和 Inventory 事实编译成可解释的 bound `ResourcePlan`，并以整站可能并发关系完成静态死锁证明。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | 在 `unilabos/workflow/execution_plan.py` 的 `ExecutionPlanBuilder.build` 中，在 composite flatten、planned nodes/jobs 完成后调用 `compile_template_resource_plan`。按拓扑和控制区域计算实际区间；同路径合并重复资源；保留 sibling branch；对每个申请点计算持有集合 H 与新增集合 N，生成 H→N 边；join 后继先执行 self-lock check；显式 root/with 作为不可缩短边界。把 `resource_interval_ids` 写入 planned node/job。 | 部分：计划计算/节点作业投影已完成，join self-lock 与物理安全交接待后续 |
| TASK-010 | 为 `pick`/`place` 实现按 Material 配对的完整搬运区间。扩展 `unilabos/app/scheduler/inventory/station_resource.py` 的 `TransferResourceFacts`/`TransferResourceRequest` 与 `resolve_transfer_resources`，返回来源/目标 owner、搬运器、夹爪/工具和 `motion_resource_material_uuids`；`unilabos/app/scheduler/transfer_resource_set.py` 只通过该 seam 生成完整规范资源。编译器规定目标 Site 在 pick 前由 `resolve_target_site` 解析并预留，已由外层持有的成员复用，缺少成员组成单一 atomic `AcquireSet`。 | | |
| TASK-011 | 在 `resource_lock_plan.py` 实现 `bind_station_resource_plan`：通过 `StationResourceInventory` 将别名、设备/Material、Site owner、rail/tool 解析为 `canonical_key`；按并行兄弟、显式依赖、可判定条件和不同样品实例组合 possible-concurrency；未绑定动态参数采用候选资源并集并在无法证明时拒绝。调用 Tarjan/DFS 检测二节点及长环，诊断包含完整路径、区间重叠、声明来源、可安全打断边和吞吐代价。 | 部分：外部绑定事实接口与并发关系合并/DFS 已完成，Inventory 解析与 transfer 事实待后续 |
| TASK-012 | 更新 `unilabos/workflow/service.py` 的 `_build_execution_plan`、任务创建和 `unilabos/workflow/task_scheduler_bridge.py` 的提交 seam：在生成 `workflow_task.execution_plan` 前绑定资源计划并保存 `plan_id`、输入参数 hash、Inventory 事实版本/快照指纹和编译器版本；事实、Action 合同、并发模板或目标 Site 选择变化时强制重新绑定，禁止沿用旧 bound plan。 | | |
| TASK-013 | 将现有 `validate_static_device_tenancy_order` 接入新的关系检查作为专用规则，并把设备托管关系标注为 `reason="device_tenancy"`；移除“仅设备托管顺序检查即安全”的隐含结论。模板级计划可以保持 symbolic，只有 bound plan 的 `static_resource_dag_v1` 证明通过后才允许调度。 | | |

Phase 3 completion criteria: 计划 JSON 包含稳定 plan/resource/scope/interval/acquire-set/
relation 身份；`robot -> photo_scrape -> robot` 的可并发闭环被拒绝，显式依赖或
稳定 Site 消除重叠时不误报；拍照刮板示例生成 `photo_scrape -> {sampling, robot,
rail}` 等关系；目标 Site 未预留、transfer 无法配对、动态实例无法保守绑定时不产出
可运行计划。

### Implementation Phase 4 — Durable Scheduler 运行时所有权与准入

- **GOAL-004**: 让运行时按编译计划跨 Job 持有和释放资源，并保留现有 Claim/Fence 的持久恢复能力。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | 在 `unilabos/app/scheduler/models.py` 的 `WorkflowNode`、`WorkflowSpec`、`DispatchedJob` 增加只读 `resource_interval_ids`、`resource_plan_id`、`resource_ownership`/`acquire_set_id` 投影字段；在 `workflow/workflow_spec_compiler.py` 的 node/spec 编译中校验这些字段与 JSON 计划一致，repeat materialization 继承 interval/scope/branch 身份而不生成新的隐式资源语义。 | 部分：WorkflowNode/WorkflowSpec、compiler 和 repeat spec 传播已完成，DispatchedJob/ownership 运行时接入待后续 |
| TASK-015 | 在 `unilabos/app/scheduler/service.py` 的 ready candidate、dispatch、finish/cancel/recovery 路径中，以 `resource_plan_for_node` 返回的 AcquireSet 取代 `_resource_lock_keys` 作为安全权威；`_job_resource_locks` 仅保留兼容投影/指标。所有新增资源通过一个 `DispatchAdmissionRequest` 一次提交，冲突时不留下部分 Claim；同一设备即使祖先已持有也按 Action 串行。 | | |
| TASK-016 | 扩展 `unilabos/app/scheduler/inventory/dispatch_admission.py` 的 `DispatchResource`/`DispatchAdmissionRequest` 和 `unilabos/workflow/execution_lock_lease.py` 的 lease 元数据，携带 `plan_id`、`interval_id`、`acquire_set_id`、relation/diagnostic id、scope 和 safe-release evidence。未知 `lock_key`、不在计划的 scope 或不匹配参数 hash 的请求返回永久合同错误，而不是 wait。 | | |
| TASK-017 | 在 `task_scheduler_bridge.py` 的 `_on_job_pre_dispatch`、`_on_job_finished` 和 `execution_lock_lease` 投影中实现实际完成释放：Action 返回明确完成且物料已稳定交接才释放可提前释放成员；显式 root/with、夹持物料、开盖/对位/运动区状态继续保留；失败/取消/unknown 进入现有 uncertain/recovery 状态，不自动释放。重启时由活动 Claim/lease 与 bound plan 重建 task-level ownership。 | | |
| TASK-018 | 把 transfer Site reservation 与设备 Claim 绑定到同一 pick 前准入事务；`StationResourceInventory` 负责预留、占用结算和冲突事实，调度器不自行修改 Site。转运完成后才更新 Material location；不能把“夹持中”错误结算为目标 Site 已占用。 | | |
| TASK-019 | 保留 `unilabos/workflow/execution_wait_graph.py` 作为运行时观测/故障诊断，但 wait edge 增加 `plan_relation_id` 和 `plan_id`，并明确静态 DAG 是安全准入证明；运行时 Tarjan 发现环时只报告 plan drift、driver undeclared request、物理故障或数据损坏，不重新搜索或擅自取消一侧。 | | |

Phase 4 completion criteria: 同一资源可跨多个 Job 持有并在正确 scope/physical event
释放；原子准入和 restart recovery 通过 SQLite 测试；未声明 driver resource、错误
plan id、目标 Site 未预留都不能下发；历史无资源计划仍通过兼容路径；运行时 wait
graph 与静态 relation 可相互定位但不改变静态证明结果。

### Implementation Phase 5 — 回归、生产路径验证与文档

- **GOAL-005**: 用仓库内真实生产调用路径验证资源语义，并把规范写入 Uni-Lab-OS 共同语言。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-020 | 新增 authoring 测试：`tests/workflow/test_authoring_resource_scopes.py` 覆盖根/with/action 三种声明、嵌套复用、硬边界、parallel branch identity、join self-lock、动态表达式/重复资源/非法 acquire 拒绝和 Python round-trip。 | 部分：调度模块测试覆盖 root/with/action、嵌套固定点、分支、动态拒绝与 round-trip；专门 workflow 测试文件待后续 |
| TASK-021 | 新增计划编译测试：`tests/workflow/test_resource_lock_plan.py` 覆盖 interval merge、safe handoff、atomic acquire set、source/target Site 绑定、transfer pick/place 配对、两节点/长环、显式依赖消环、条件并集、composite boundary 和旧计划兼容。 | 部分：`tests/scheduler_core/test_resource_lock_plan.py` 已覆盖区间、嵌套/分支、原子集合、环检、序列化和执行计划 seam；Site/transfer golden case 待后续 |
| TASK-022 | 扩展 `tests/workflow/test_execution_plan*`、`tests/workflow/test_f05_workflow_spec_compiler.py`、`tests/registry/test_action_resource_contract.py`，验证版本/能力、contract v1/v2、plan serialization、schema diagnostics 和 plan hash/recompile。 | 部分：v1/v2 合同、能力/绑定校验、序列化与 schema 回归已覆盖，plan hash/recompile 待后续 |
| TASK-023 | 扩展 `tests/app/test_scheduler_resource_lock.py`、`tests/app/test_scheduler_site_lock.py`、`tests/scheduler_core/test_resource_admission.py`、`tests/workflow/test_local_execution_lock_lease.py` 与 `test_execution_wait_graph.py`，沿 `EdgeScheduler -> TaskSchedulerBridge -> StationResourceInventory -> Claim/Fence -> completion` 真实路径验证原子准入、跨 Job ownership、目标 Site 预留、实际完成释放、故障/取消/重启和 undeclared resource 拒绝。 | | |
| TASK-024 | 更新 `CONTEXT.md`，补充 Material UUID、Site Occupancy、Workflow/Operation、资源占用区间、可安全交接状态、静态整站 DAG、Claim/Fence/lease 的中文-first 定义；在 `docs/architecture/workflow-resource-locking-kernel-v3.md` 记录计划 JSON/诊断示例；将两份附件复制为经审阅的仓库规范前，使用附件绝对路径作为外部权威，不伪造已存在的文档链接。 | | |
| TASK-025 | 在提交前运行仓库本地静态检查、目标测试集、全量 `pytest`（若环境允许）和 `git diff --check`；运行 `python scripts/check_changed_boundaries.py --core-root ~/Uni-Lab-OS`（若脚本存在），搜索 sibling path/import、硬编码设备/Site、fake receipt/endpoint 和直接库存 SQL。记录每条命令及结果，未完成项不得标记本计划 Completed。 | 部分：compileall/diff check 与 128 项定向回归通过；全 scheduler_core 为 51 通过、3 项既有 manual-confirm 失败，Ruff/边界脚本缺失 |

Phase 5 completion criteria: 目标测试覆盖附件验收清单；至少一条使用真实
`StationResourceInventory`/Claim/Fence 的跨进程或持久化恢复证据；文档中的接口、JSON
字段和诊断与实现一致；边界检查无当前变更引入的违规；历史未跟踪 `plan/` 内容不被
覆盖或删除。

## 3. Alternatives

- **ALT-001**: 把整个 L3/实验步骤的资源足迹一次性加锁。拒绝：会把机械臂、仓库和工位在长工艺等待期间无意义地占住，损害多样品吞吐；本计划选择 Action/根/词法范围统一编译为区间。
- **ALT-002**: 只允许固定层级声明锁。拒绝：无法表达跨 Operation 的连续持有和 Action 内部的短资源；本计划保留三类作者声明。
- **ALT-003**: 暴露自由 `acquire()`/`release()`。拒绝：异常、取消、嵌套和并行无法可靠配对，编译器也无法重建边界；结构化根/with 足够表达明确范围。
- **ALT-004**: 每次运行时重新做资源图安全搜索或发现环后取消一侧。拒绝：增加运行时差异，无法安全处理持料/不可逆命令；运行时只执行 bound DAG，wait graph 只作观测和故障分类。
- **ALT-005**: 以预计 Action 时长证明无环。拒绝：时长会受硬件、通信、样品和故障影响；只能用于优先级/吞吐，不参与安全证明。
- **ALT-006**: 第一阶段新增独立 `resource_interval`/`resource_owner` SQL 表。暂不采用：已有 `execution_plan` JSON 和 Claim/lease 能保存不可变计划及活动所有权，先减少迁移面；后续若审计查询需要，再以单独迁移增加只读索引表。
- **ALT-007**: 把 rail/tool 资源硬编码在 Scheduler 或工作流作者代码。拒绝：违反 Uni-Lab-OS 共享仓库边界和事实完整性；由 `StationResourceInventory`/机器人公开合同返回具体实例。

## 4. Dependencies

- **DEP-001**: `unilabos/registry/action_resource_contract.py` 当前 v1 合同、Action Schema 和 registry scanner；必须先完成兼容规范化再扩展 v2。
- **DEP-002**: `unilabos/app/scheduler/inventory/station_resource.py` 的 Inventory Interface、`resolve_target_site`、`resolve_transfer_resources` 和 `acquire_dispatch_permit`；它是 Site/设备/rail/tool/Material 事实权威。
- **DEP-003**: `unilabos/app/scheduler/inventory/dispatch_admission.py` 的 Claim/Fence 原子事务，以及 `unilabos/workflow/execution_lock_lease.py` 的持久 lease/restart 语义。
- **DEP-004**: `unilabos/workflow/store.py` 的 `workflow_task.execution_plan`、`workflow_node_job` 投影和现有 migrations；第一版不改变旧列含义。
- **DEP-005**: `ExecutionPlanGraphNormalizer.flatten_composite_edges`、`WorkflowSpecCompiler`、`TaskSchedulerBridge` 和 `EdgeScheduler` 的生产调用路径；不能用独立 fake scheduler 代替。
- **DEP-006**: 附件 `~/.whalent_tmp/2026-09-04/8lqwsx51i39sx2rpgqdsyz27c-workflow-resource-locking-design.md` 与 `~/.whalent_tmp/2026-09-04/m55b1c3mxrraymdidqe16nr0a-workflow-resource-locking.md`；两者在实现阶段作为规范输入保存校验 hash，避免文档漂移。
- **DEP-007**: 机器人/实验室仓库只通过已发布资源事实和设备执行合同提供 Adapter；本仓库不直接依赖其源码，若缺少 rail/tool 公开事实则任务阻塞并报告缺失 Interface。

## 5. Files

- **FILE-001**: `unilabos/workflow/resource_lock_plan.py` — 新的资源区间/作用域/原子取得集合/关系图深模块、绑定器、DAG 校验、诊断和 JSON 序列化 Interface。
- **FILE-002**: `unilabos/registry/action_resource_contract.py` — Action resource contract v2、ResourceSlot role 校验、transfer motion/tool role 规范化；保留 v1 兼容。
- **FILE-003**: `unilabos/workflow/execution_resource_policy.py` — 合并/解析新资源字段并把设备托管规则作为专用 relation validator。
- **FILE-004**: `unilabos/workflow/authoring.py` — `resources` marker 和 workflow metadata 类型/运行时占位。
- **FILE-005**: `unilabos/workflow/authoring_ast.py` — root/with scope IR、Action 默认资源、transfer pairing 和静态语法拒绝。
- **FILE-006**: `unilabos/workflow/authoring_graph.py`、`unilabos/workflow/authoring_python.py` — 资源 scope/branch metadata 的候选图投影与 round-trip。
- **FILE-007**: `unilabos/workflow/execution_plan.py` — resource plan capability、模板编译接入、planned node interval 投影和旧版本兼容。
- **FILE-008**: `unilabos/workflow/workflow_spec_compiler.py`、`unilabos/workflow/models.py` — bound plan 能力校验、WorkflowSpec/WorkflowNode/DispatchedJob 只读投影。
- **FILE-009**: `unilabos/workflow/service.py`、`unilabos/workflow/task_scheduler_bridge.py` — 任务创建时绑定/重编译、生产 pre-dispatch/finish/recovery seam。
- **FILE-010**: `unilabos/app/scheduler/inventory/station_resource.py`、`unilabos/app/scheduler/transfer_resource_set.py` — Inventory 事实扩展和完整 transfer resource Adapter。
- **FILE-011**: `unilabos/app/scheduler/inventory/dispatch_admission.py`、`unilabos/workflow/execution_lock_lease.py`、必要时 `unilabos/workflow/store_migrations.py` — plan/interval ownership 的 Claim/lease metadata；无必要不新增表。
- **FILE-012**: `unilabos/app/scheduler/service.py`、`unilabos/workflow/execution_wait_graph.py` — 计划驱动派发、跨 Job 所有权兼容投影和 plan-linked wait diagnostics。
- **FILE-013**: `CONTEXT.md`、`docs/architecture/workflow-resource-locking-kernel-v3.md` — 中文-first 共同语言、接口、JSON 产物、故障/恢复和边界说明。
- **FILE-014**: `tests/workflow/test_authoring_resource_scopes.py`、`tests/workflow/test_resource_lock_plan.py`、`tests/workflow/test_execution_plan_resource_capability.py` — 新 AST/编译器/计划合同测试。
- **FILE-015**: `tests/app/test_scheduler_resource_lock.py`、`tests/app/test_scheduler_site_lock.py`、`tests/scheduler_core/test_resource_admission.py`、`tests/workflow/test_local_execution_lock_lease.py`、`tests/workflow/test_execution_wait_graph.py` — 真实调度准入、lease、恢复和诊断回归。
- **FILE-016**: 现有未跟踪 `plan/architecture-station-scheduler-unilabos-v1.md` 与 `plan/diagrams/` — 预先存在的用户内容，只读保留，不覆盖、不删除、不并入本计划实现。

## 6. Testing

- **TEST-001**: `pytest -q tests/registry/test_action_resource_contract.py tests/workflow/test_execution_resource_policy.py`；验证 v1 兼容、v2 role/schema、workflow policy 不可覆盖 Action 资源语义。
- **TEST-002**: `pytest -q tests/workflow/test_authoring_resource_scopes.py tests/workflow/test_qg01_group_parallel_authoring.py`；验证 root/with/action 语法、嵌套、branch identity 和旧 group/parallel 行为。
- **TEST-003**: `pytest -q tests/workflow/test_resource_lock_plan.py tests/workflow/test_execution_plan_resource_capability.py tests/workflow/test_f05_workflow_spec_compiler.py`；验证区间、原子集合、pick/place、Site 绑定、DAG、诊断、序列化和旧计划。
- **TEST-004**: `pytest -q tests/app/test_scheduler_resource_lock.py tests/app/test_scheduler_site_lock.py tests/app/test_scheduler_site_selector_lock_validation.py tests/scheduler_core/test_resource_admission.py`；验证计划驱动的冲突、目标 Site 预留、完整 transfer set 和 undeclared resource 拒绝。
- **TEST-005**: `pytest -q tests/workflow/test_local_execution_lock_lease.py tests/workflow/test_device_tenancy.py tests/workflow/test_execution_wait_graph.py`；验证 Claim/Fence、task-level ownership、实际完成释放、失败/取消/unknown、重启恢复和 plan-linked wait diagnostics。
- **TEST-006**: 使用仓库已有任务创建与 dispatch fixture，执行 `ExecutionPlanBuilder -> WorkflowSpecCompiler -> EdgeScheduler -> TaskSchedulerBridge -> StationResourceInventory` 的真实生产调用路径；不得用手工 receipt、临时 endpoint 或直接低层 driver 替代，模拟证据必须显式标记。
- **TEST-007**: 对附件示例建立 golden plan：拍照刮板根 `photo_scrape` 跨搬入/拍照/卸板，搬入/卸板分别原子取得来源/目标/robot/rail，`camera+scraper` 在词法范围取得；比较 relation、release boundary、diagnostic 和 plan hash。
- **TEST-008**: 运行 `pytest -q` 全量回归、`git diff --check`、仓库边界脚本（若存在）及 sibling import/硬编码/直接 SQL 扫描；将失败分类为当前改动、历史问题或环境阻塞。

## 7. Risks & Assumptions

- **RISK-001**: 现有 Inventory 可能无法提供 rail/tool/motion 的公开身份。若无法通过 `StationResourceInventory` 读取，不得猜 UUID；先阻塞转运计划并补充 Interface/Adapter。
- **RISK-002**: 动态 device selector、条件分支或多实例并发可能无法在任务创建时绑定。只能采用明确候选并集的保守 DAG 或拒绝任务，不能退回运行时猜测。
- **RISK-003**: 跨 Job ownership 改造会与当前 `_job_resource_locks` 立即释放逻辑冲突。迁移期必须保留兼容投影并增加 restart/failure 测试，禁止双重释放 Claim。
- **RISK-004**: `execution_plan` JSON 增大可能影响旧投影查询和 SQLite payload；先做 deterministic serialization/大小指标，必要时后续添加只读索引而不改变计划权威。
- **RISK-005**: `pick`/`place` 物理状态证据可能不完整。未知状态必须进入 uncertain/recovery，不能以 Job success 或预计时长推断 Site 已稳定。
- **RISK-006**: 并行兄弟对同一设备的顺序可能影响实验结果，而资源互斥只能保证安全不能保证实验语义；没有显式依赖时应诊断为作者合同错误。
- **RISK-007**: 运行时 wait graph 仍可能发现真实活性故障。其环不能被静态 DAG 证明掩盖；诊断必须区分 plan drift、未声明资源、物理故障和公平性问题。
- **ASSUMPTION-001**: 工作流节点的最终 ResourceSlot 参数和 Action contract 在创建 Task 前可获得稳定 hash；若不可获得，任务停留在未绑定而不可调度状态。
- **ASSUMPTION-002**: 每个单实例资源都有稳定 canonical key，且同一个 Site 的 owner/material 关系可以由 Inventory 在同一事务中证明。
- **ASSUMPTION-003**: 现有 Claim/Fence/lease 表可以在 metadata 中保存 plan/interval/acquire-set 关联，并能从活动 Claim 恢复 task-level ownership。
- **ASSUMPTION-004**: PLC 碰撞区域仍由 PLC 保证；工作流资源锁不扩展为 `access_region` 软件锁，也不取代硬件急停/安全恢复。
- **ASSUMPTION-005**: 共享锁、容量/池资源和新的 L2/L3 层级不在本版本范围；若产品需求改变，必须另建版本化架构决策和并发证明。

## 8. Related Specifications / Further Reading

- [附件：Uni-Lab 工作流资源锁架构设计](~/.whalent_tmp/2026-09-04/8lqwsx51i39sx2rpgqdsyz27c-workflow-resource-locking-design.md)
- [附件：子工作流与资源锁编写指南](~/.whalent_tmp/2026-09-04/m55b1c3mxrraymdidqe16nr0a-workflow-resource-locking.md)
- [`AGENTS.md`](../AGENTS.md) — Uni-Lab-OS Python、中文日志、Scheduler/Inventory 和测试边界。
- [`CONTEXT.md`](../CONTEXT.md) — Material、Site、Workflow、Control Plane 和共享领域语言。
- [`unilabos/workflow/execution_plan.py`](../unilabos/workflow/execution_plan.py) — 当前 flatten/plan/job 生产入口。
- [`unilabos/app/scheduler/inventory/dispatch_admission.py`](../unilabos/app/scheduler/inventory/dispatch_admission.py) — 当前 Claim/Fence 原子派发准入。
- [`unilabos/workflow/execution_lock_lease.py`](../unilabos/workflow/execution_lock_lease.py) — 当前持久执行锁 lease 与恢复接口。
- [`unilabos/app/scheduler/inventory/station_resource.py`](../unilabos/app/scheduler/inventory/station_resource.py) — 工站设备、Site 和转运事实的 Inventory seam。
