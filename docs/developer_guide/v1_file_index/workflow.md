# U01 workflow 区：V1.0 `unilabos/workflow/` → 统一分支（#361 `6f94ec04`）逐文件核验

范围：129 个 V1 文件（V1 `690491a3`，共 85644 行）。只读核验；对照对象是统一分支 `server/services/runtime/workflow/`、`server/scheduler/`、`server/` 与 `client/host/` 下的执行协调模块、`server/services/runtime/`、`registry/workflow*.py`、`protocol/runtime|utils/`。“#n”指 [README](README.md)「已定的有意不移植」编号，W/A/I/R/D/F 指 `v1_dev_reconciliation.md` 的 ID。

## 统计

| 结论 | 文件数 | V1 行数 |
|---|---|---|
| PORTED | 87 | 64206 |
| PARTIAL | 12 | 11327 |
| MISSING | 5 | 1489 |
| DROPPED | 22 | 8501 |
| N/A | 3 | 121 |
| 合计 | 129 | 85644 |

## 缺口摘要（MISSING / PARTIAL，按建议优先级）

1. **组合调用的发布后自动刷新**（composite_contract_refresh MISSING；catalog_dependent_authoring_refresh MISSING，后者依赖源码内组合调用）。
2. **子实验操作的资源作用域在组合展开时丢失**（composite_graph_rewrite PARTIAL）。
3. **任务级 `inventory_bindings` 只冻结不生效**（quantity_inventory PARTIAL；run_preflight 只回显）。
4. **源码监视器 / 写回待恢复未接线**（source_monitor PARTIAL）。
5. **工作流内原生命令**：库存命令与人工交互命令（native_commands / inventory_commands MISSING；human_commands PARTIAL）。
6. **命名库位组**（execution_resource_policy PARTIAL；authoring_ast ①）。
7. 作者层表达力：图→源码只覆盖子集（authoring_python）、repeat_until carry / 源码组合（authoring_ast）、资源模板约束（workflow_io、material_graph_validation）、节点级人工确认超时（manual_confirmation）、UTF-16 列号（source_coordinates）。
8. 低优先：调度容量上限（scheduler_capacity MISSING）、Windows 原生写回（source_windows_publication）。

## 逐文件

| V1 文件 | 行数 | 结论 | 统一分支对应 / 依据 | 备注 |
|---|---|---|---|---|
| `unilabos/workflow/__init__.py` | 0 | N/A | 空包初始化文件 |  |
| `unilabos/workflow/_execution_plan_graph.py` | 1091 | PORTED | service.py `_build_execution_plan` 及同名 `_handle_data_key` / `_final_target_data_key` / `_dependency_only` / `_executor_kind`；组合虚拟节点平面化在 execution_plan_graph.py `CompositeExecutionPlanNormalizer` | 执行种类无 V1 的 `repeat_until` / `workflow_command` / `material_io\|material_source`（见 authoring_ast、native_commands、#6） |
| `unilabos/workflow/_workflow_spec_snapshot.py` | 174 | DROPPED | #4（V1 冻结任务→旧 EdgeScheduler WorkflowSpec 的身份校验；dev 调度器直接从执行计划建 DAG：scheduler/service.py `_build_dag`） |  |
| `unilabos/workflow/applied_authoring_projection.py` | 613 | PORTED | service.py `_validated_applied_backend_graph`、`_preserve_author_source_compilation`；authoring_compiler.py `_merge_applied` | 源码表达不了的字段（pose、模板、人工确认等）按身份沿用已应用图 |
| `unilabos/workflow/authoring.py` | 390 | PORTED | registry/workflows.py `@workflow` + `WorkflowBuildContext`（ctx.run / run_template / group / parallel / lock / resource_scope / loop_for / loop_while / step_output）；输出用函数末尾 `return {...}` | V1 标记模块按设计换成 dev ctx 语法（#7）；V1 独有标记中 material_source/material_io 属 #6，其余无对应的构造记在 authoring_ast.py |
| `unilabos/workflow/authoring_ast.py` | 2738 | PARTIAL | registry/workflow_ast.py `compile_workflow_source` + workflow_helpers.py（if/elif/else、with group/parallel/lock、循环、具名返回） | dev 语法没有的 V1 构造：① `site_group()` 命名库位组选择；② 源码内绝对导入并调用已发布实验操作（CompositeDeclaration，组合只能走 `/composite-invocations` API）；③ `service()` 本地原生命令（见 native_commands）；④ `repeat_until(carry=…)` + `loop.next()` 跨轮携带值（dev loop 无 carry，执行种类也不认 `repeat_until` 节点，V1 图里的该节点会被拒绝） |
| `unilabos/workflow/authoring_candidate_hash.py` | 49 | PORTED | server/services/runtime/workflow/service.py `_issue_candidate` / `apply_authoring`（dev 原有的 draft→candidate→apply 三元 CAS） | V1 八字段哈希规则不保留，dev 候选哈希自有口径；`/authoring/apply {candidate_hash}` 由 `apply_authoring_by_candidate` 反推 |
| `unilabos/workflow/authoring_engine.py` | 468 | PORTED | server/services/runtime/workflow/authoring_compiler.py `WorkflowAuthoringCompiler`（compile / generate_python / validate 三个纯转换），底层 registry/workflow_ast.py | 作者语法按设计换成 dev `@workflow(ctx)`（#7；authoring_compiler 文档串明言不移植 V1 `device()` 语法） |
| `unilabos/workflow/authoring_graph.py` | 2087 | PORTED | authoring_compiler.py `_compile_graph` + registry/workflows.py 模板实例化路径 |  |
| `unilabos/workflow/authoring_graph_semantics.py` | 260 | PORTED | authoring_compiler.py `candidate_changeset`；service.py `_validate_candidate_bundle_semantics` |  |
| `unilabos/workflow/authoring_identity.py` | 192 | PORTED | server/services/runtime/workflow/authoring_identity.py（expanded_node_uuid / authoring_edge_uuid） | `declared_workflow_uuid/type` 只服务领域包源码发现，dev 无此入口（见 source_discovery）；节点身份用 `# unilab:node_uuid` 锚点 |
| `unilabos/workflow/authoring_kernel.py` | 481 | PORTED | server/services/runtime/workflow/authoring_kernel.py（AuthoringCatalogSnapshot）；service.py `AuthoringCompiler` 协议 |  |
| `unilabos/workflow/authoring_material.py` | 752 | DROPPED | #6（运行时补料分支 material_source 不做）；dev 用 ResourceSlot 参数 + `inventory=[{kind:lot/content…}]` 需求 |  |
| `unilabos/workflow/authoring_material_io.py` | 851 | DROPPED | #6（material_io 人工补料/装卸节点不做） | `node_templates.py` 明确不投影 material_io 结构模板 |
| `unilabos/workflow/authoring_python.py` | 2981 | PARTIAL | authoring_compiler.py `_PythonRenderer`（`/authoring/generate-python`） | 只能渲染顺序动作、loop、group/parallel 和无约束的标量输入；条件节点、资源作用域、Handle 连线、具名输出、组合调用、带约束/ResourceSlot 输入、condition_guards 一律返回 `graph_not_representable`。V1 可对全部 DSL 结构做图→源码往返；带数据连线的常见图在统一分支无法生成源码 |
| `unilabos/workflow/candidate_validation.py` | 503 | PORTED | service.py `_validate_candidate_bundle_semantics`、`_require_candidate_graph_containers` |  |
| `unilabos/workflow/catalog.py` | 213 | PORTED | server/services/runtime/workflow/catalog.py（PublishedSourceCatalog） |  |
| `unilabos/workflow/catalog_dependent_authoring_refresh.py` | 185 | MISSING | 无对应；统一分支 workflow 服务内无任何 refresh 逻辑 | V1：实验操作应用新修订后，自动重编译引用它的父工作流源码，干净父图自动应用、脏草稿只重编译 |
| `unilabos/workflow/common.py` | 820 | PORTED | server/services/runtime/workflow/common.py |  |
| `unilabos/workflow/composite.py` | 510 | PORTED | server/services/runtime/workflow/composite.py |  |
| `unilabos/workflow/composite_compatibility.py` | 893 | PORTED | server/services/runtime/workflow/composite_compatibility.py | 陈旧 pin 不兼容时 composite_expansion 报 `composite_contract_stale` |
| `unilabos/workflow/composite_contract_refresh.py` | 608 | MISSING | 无对应；`publish_workflow` 不触及引用方 | V1（CONTEXT「组合工作流调用」）：子操作发布兼容新合同时，自动替换无待应用编辑的父工作流中的调用（保持调用 UUID/布局/参数/外部连线），不兼容时返回 pending 诊断 |
| `unilabos/workflow/composite_expansion.py` | 1993 | PORTED | server/services/runtime/workflow/composite_expansion.py `CompositeAuthoring` |  |
| `unilabos/workflow/composite_graph_rewrite.py` | 517 | PARTIAL | composite_expansion.py 已重映射条件引用（`remap_expression`、`bind_condition_inputs`、condition_guards）与静态实参 | 缺 `project_experiment_operation_resource_scopes`/`merge_expanded_resource_scopes`：子实验操作 meta `resource_scopes` 不投影进父图（全仓无生成 `composite_invocation_uuid` 作用域的代码，definition_edit 只有过滤分支），子操作里的 `ctx.lock/resource_scope` 被调用后失效 |
| `unilabos/workflow/composite_inventory_requirements.py` | 184 | PORTED | composite_expansion.py 对节点 `meta_data.inventory_requirements` 做 `map_input_bindings` / `remap_result_bindings` | dev 需求挂在节点元数据上，不是独立 requirement 表 |
| `unilabos/workflow/composite_invocation.py` | 606 | PORTED | service.py `insert_composite_invocation` + publication.py `merged_invocation_entities`；`POST /workflows/{uuid}/composite-invocations` V1 body（route map p7-a） |  |
| `unilabos/workflow/composition.py` | 1137 | PORTED | server/execution_composition.py `setup_local_scheduler`（configure_authoring、FrontendEventRetention、scheduler.start(recover=True)） | 不做领域包源码发现与固定点激活（#4）；启动时不调用 `recover_registered_sources`、不起源码监视器（见 source_monitor） |
| `unilabos/workflow/control_expression.py` | 179 | PORTED | unilabos/protocol/runtime/condition.py（validate_expression / evaluate_condition / strict_bool） |  |
| `unilabos/workflow/convert_from_json.py` | 319 | PORTED | server/services/runtime/workflow/convert_from_json.py |  |
| `unilabos/workflow/definition_edit.py` | 278 | PORTED | server/services/runtime/workflow/definition_edit.py + definition_service.py（单节点/单连线/批量删除/复制，整图 CAS 保存） |  |
| `unilabos/workflow/definition_hierarchy.py` | 110 | PORTED | definition_edit.py `private_node_owners` / `definition_graph` / `restore_private_subgraphs` |  |
| `unilabos/workflow/device_action_run.py` | 657 | PORTED | service.py `create_device_action_run`；`POST /api/v1/device-action-runs`（route map p7-a） | 映射到 dev ad-hoc 设备动作任务 |
| `unilabos/workflow/device_action_run_store.py` | 176 | PORTED | service.py `create_ad_hoc_device_action_task` / store.py |  |
| `unilabos/workflow/device_tenancy.py` | 351 | PORTED | server/services/runtime/workflow/device_tenancy.py `DeviceTenancies` + scheduler/tenancy_contract.py；`/workflow-device-tenancies/{uuid}/release` | A03 已启用 |
| `unilabos/workflow/dispatch_permit_saga.py` | 82 | PORTED | scheduler/attempt_inventory.py `freeze_inventory` + scheduler/inventory_recovery.py | R02 / workflow_inventory_recovery.md |
| `unilabos/workflow/domain_source_target.py` | 427 | DROPPED | #4（工作流定义权威是 runtime.db，不写回领域包） | 导入的 Python 源码落 `<db root>/workflow_sources/workflows/`（service.py `_register_managed_source`） |
| `unilabos/workflow/error_control.py` | 2017 | PORTED | server/services/runtime/station_handling.py（workflow_station_error / error_session / station_control / control_command）；`/api/v1/stations/{id}/*`（route map p7-b） |  |
| `unilabos/workflow/error_handling.py` | 119 | PORTED | station_handling.py `StationHandling` + services/runtime/error_decisions.py `ErrorDecisionDesk` |  |
| `unilabos/workflow/event_reader.py` | 175 | PORTED | store.py `list_events`（frontend_event 游标）+ `/api/v1/events` SSE（Last-Event-ID） |  |
| `unilabos/workflow/event_retention.py` | 95 | PORTED | server/services/runtime/workflow/event_retention.py `FrontendEventRetention`（execution_composition.py 启动） |  |
| `unilabos/workflow/event_writer.py` | 85 | PORTED | store.py `_append_event`（frontend_event） | V1 单独的运行日志由 frontend_event 投影承担（`/workflow-tasks/{uuid}/events`） |
| `unilabos/workflow/execution_claim.py` | 503 | PORTED | server/services/runtime/workflow/execution_claim.py `ExecutionClaims`（reserve / fencing / seal_dispatch / begin_execution / uncertain / timed_out） | ADR 0008 |
| `unilabos/workflow/execution_lock_lease.py` | 1500 | PORTED | execution_claim.py 租约 + resource_intervals.py + resource_waiters.py（老化优先级 FIFO）；`/workflow-tasks/{uuid}/execution-locks` + force-release |  |
| `unilabos/workflow/execution_plan.py` | 2206 | PORTED | service.py `_build_execution_plan`（冻结计划、topological_index、on_ready 需求） | 执行种类差异见 _execution_plan_graph |
| `unilabos/workflow/execution_resource_policy.py` | 347 | PARTIAL | scheduler/physical_contract.py、tenancy_contract.py、registry/action_resource_contract.py、resource_plan.py（静态取得顺序/死锁检查） | 缺「部署定义的命名库位组 / 等价库位组」：V1 `target_site_group` + `site_group()` 在建任务时由库存冻结为候选 Site UUID（V1 `inventory/site_selection.py`）；统一分支全仓无 site group 概念，动作目标库位只能固定 SiteSlot |
| `unilabos/workflow/execution_restart_recovery.py` | 433 | DROPPED | ADR 0007（取代 V1 ADR 0005）：Host 重启不整体失败任务，未决 attempt 转 execution_unknown 人工核对，锁/区间保留 | client/host/edge_executor.py `restore()`；共同原则（不伪造失败、超时不释放）保留 |
| `unilabos/workflow/execution_started.py` | 104 | PORTED | client/host/execution_start.py + execution_claim.py `record_start_receipt`（调用边界回执） | R01 |
| `unilabos/workflow/execution_wait_graph.py` | 131 | PORTED | task_lifecycle.py `TaskObservability.wait_graph`（Tarjan）；`/workflow-runtime/wait-graph` |  |
| `unilabos/workflow/from_python_script.py` | 241 | PORTED | scripts/workflow/from_python_script.py | 一次性导入转换器移到 scripts/（AGENTS.md） |
| `unilabos/workflow/from_xdl.py` | 131 | PORTED | scripts/workflow/from_xdl.py | 同上 |
| `unilabos/workflow/frozen_json_cache.py` | 99 | N/A | — | 纯性能缓存：服务于 V1 桥接层每个作业完成都重读并解码大计划的读法；dev 调度器在内存持有 DAG，只在恢复/个别库存绑定时读任务。超大图（V1 pTLC 5383 节点）性能未在统一分支测过 |
| `unilabos/workflow/graph_executability.py` | 73 | PORTED | protocol/utils/workflow_validation.py `validate_graph`（保存）；`_build_execution_plan`（预检/建任务） | 保存时不跑计划构建器，个别只有计划层才发现的结构错误会推迟到预检报出 |
| `unilabos/workflow/graph_validation.py` | 1298 | PORTED | unilabos/protocol/utils/workflow_validation.py |  |
| `unilabos/workflow/handle_projection.py` | 94 | PORTED | server/services/runtime/workflow/handle_projection.py |  |
| `unilabos/workflow/human_commands.py` | 231 | PARTIAL | server/services/runtime/workflow/human_interactions.py（workflow_human_interaction，`/human-interactions` 与 `/reply` 按类型校验） | 没有生产者：dev 没有能在工作流里发起 choose/text/numbers/sketch/review 的原生命令（模块文档串自述，只留 `HumanInteractions.open` 接口），读写接口实际永远为空 |
| `unilabos/workflow/intervention.py` | 484 | PORTED | server/services/runtime/workflow/interventions.py `WorkflowInterventions`；`/workflow-interventions*` |  |
| `unilabos/workflow/inventory_commands.py` | 247 | MISSING | 无对应（materials/inventory_commands.py 是 HTTP `/inventory/commands`，不是工作流节点） | V1 工作流节点里的库存命令（read_count→测量→reconcile_count 盘点、inspect_carrier_transfer、mark_used、locate、site_occupant）及其证据校验；对应 I03「按测量作业证据自动盘点未实现」 |
| `unilabos/workflow/inventory_resource_plan.py` | 468 | PORTED | scheduler/physical_contract.py `resolve_physical_contract` + resource_plan.py | transfer / operate_in_place / transfer_step / aliquot（A03） |
| `unilabos/workflow/job_evidence.py` | 455 | PORTED | runtime.db `workflow_node_job_feedback_history` + service.py `list_node_job_results` / `list_node_job_feedback_history`；`/workflow-node-jobs/{uuid}/feedback` |  |
| `unilabos/workflow/json_codec.py` | 340 | PORTED | unilabos/protocol/utils/json_codec.py |  |
| `unilabos/workflow/legacy/convert_from_json_legacy.py` | 356 | PORTED | scripts/workflow/legacy/convert_from_json_legacy.py |  |
| `unilabos/workflow/local_effects.py` | 27 | PORTED | server/inventory.py `ExecutionInventoryCoordinator`（驱动调用前校验预占）+ scheduler/inventory_recovery.py（固定幂等身份） |  |
| `unilabos/workflow/manual_action_review.py` | 38 | PORTED | station_handling.py `_review_version` / manual_review；`/stations/{id}/manual-action-jobs/{uuid}/review` |  |
| `unilabos/workflow/manual_confirmation.py` | 353 | PARTIAL | store.py 人工确认表 + service.py `open/decide/expire_workflow_manual_confirmation`；scheduler/service.py 对 `manual_confirm` 动作开确认单 | 节点级 `manual_confirmation.timeout_seconds` 只持久化、不生效（route map p7-a 自述）：dev 超时取 `manual_confirm` 动作参数；V1「人工确认包装真实设备动作」的节点配置被忽略 |
| `unilabos/workflow/material_aliquot_settlement.py` | 106 | PORTED | server/services/materials/execution.py 分装结算 + scheduler/physical_recovery.py | A03 / I03 |
| `unilabos/workflow/material_graph_validation.py` | 874 | PARTIAL | resource_plan.py `unordered_resource_actions` + interval_plan.py `order_sensitive`（同一资源上无先后的兄弟动作拒绝） | 缺 ResourceSlot 物料边的资源模板兼容校验（生产保证 `allowed_resource_template_uuids` 不能赋给消费者时拒绝）；`value_schema.intersect_resource_constraints` 无调用方 |
| `unilabos/workflow/material_selector.py` | 190 | DROPPED | #6（material_source 选择器） |  |
| `unilabos/workflow/material_source.py` | 53 | DROPPED | #6 |  |
| `unilabos/workflow/material_transfer_settlement.py` | 280 | PORTED | server/services/materials/execution.py 搬运结算；`/workflow-node-jobs/{uuid}/settle-material-transfer` |  |
| `unilabos/workflow/models.py` | 432 | PORTED | unilabos/protocol/runtime/workflow.py（WorkflowNodeWrite / CandidateCompilation / validate_uuid…）；优先级见 task_lifecycle.py `normalize_task_priority` |  |
| `unilabos/workflow/native_commands.py` | 193 | MISSING | 无对应；dev 执行种类无 `workflow_command`，host_services 只有 manual_confirm / test_* | V1 `service()` 选择器下的 OS 本地命令：InventoryCommands（计数盘点、载架检查、标记已用、定位、库位占用读取）与 HumanCommands（选择/文本/数值/手绘/复核）可作为工作流节点；统一分支工作流里无法写这些步骤 |
| `unilabos/workflow/operation_category.py` | 462 | PORTED | server/services/runtime/workflow/operation_category.py（runtime.db `workflow_operation_category`） | 存储从领域包 JSON 改为 runtime.db（文档串说明） |
| `unilabos/workflow/physical_settlement_policy.py` | 137 | PORTED | execution_claim.py（uncertain / timed_out 保锁）+ scheduler/physical_recovery.py | ADR 0008 |
| `unilabos/workflow/publication_catalog.py` | 1209 | PORTED | runtime.db 发布记录（service.py `publish_workflow_contract` / `list_workflow_publications`；`/workflow-publications`） | 不再写领域包 `workflow_publications/`（#4），跨重启由 runtime.db 保持 |
| `unilabos/workflow/published_contract.py` | 1226 | PORTED | service.py `publish_workflow` / `publication_contract` / `get_publication_by_uuid` + publication.py `freeze_publication_snapshot` | V1 合同 DTO（contract uuid、revision_fingerprint）见 route map p7-a；#9 指纹差异 |
| `unilabos/workflow/published_workflow_runtime.py` | 541 | PORTED | server/services/runtime/workflow/published_workflow_runtime.py |  |
| `unilabos/workflow/python_workflow_import.py` | 150 | PORTED | service.py `import_python_workflow`；`POST /local/workflows/import-python` | 只解析不导入 |
| `unilabos/workflow/quantity_inventory.py` | 1445 | PARTIAL | materials/quantity.py、contents.py、scheduler/attempt_inventory.py、inventory_recovery.py（I02：批次/内容物预留、多任务共享、结算与恢复） | 任务级 `inventory_bindings`（逻辑需求键→本次实际使用的试剂/当前内容物 + 预留量）在建任务时由 `task_lifecycle.py:bind_plan_inventory` 落到冻结计划里对应的 `kind="content"` 需求（换成绑定的容器与内容物，单位、预留量对不上或找不到需求即拒绝建任务，不静默丢弃），之后的预留、预检与恢复都按绑定的来源进行；运行预检按同一规则检查（`run_preflight.py`）。仍有差异：只有 `content` 需求能绑定，`lot` / `material` 需求的绑定被拒绝，来源或数量写成入口 / 上游引用的需求也不能再绑定 |
| `unilabos/workflow/resource_lock_key.py` | 231 | PORTED | server/scheduler/resource_keys.py（逐函数相同） |  |
| `unilabos/workflow/resource_lock_plan.py` | 2275 | PORTED | server/scheduler/resource_plan.py（逐函数相同）+ interval_plan.py |  |
| `unilabos/workflow/resource_reference.py` | 82 | PORTED | scheduler/materials.py `material_uuids_for_parameters` + run_preflight.py `validate_resource_values` | V1 `resource_ref()` 语法不保留；物料身份由 ResourceSlot 参数值经物料权威解析 |
| `unilabos/workflow/resource_usage.py` | 58 | PORTED | station_handling.py `/stations/{id}/resource-usage`；`/scheduler/resources` |  |
| `unilabos/workflow/run_preflight.py` | 391 | PORTED | server/services/runtime/workflow/run_preflight.py；GET/POST `/workflows/{uuid}/run-preflight` | `inventory_bindings` 只回显不参与检查（缺口记在 quantity_inventory） |
| `unilabos/workflow/runtime_material_source.py` | 134 | DROPPED | #6（运行时补料分支） |  |
| `unilabos/workflow/scheduler_capacity.py` | 256 | MISSING | 无对应；dev 调度器与 DAG 执行器无任何任务/作业容量上限 | V1 持久容量：全局运行任务（默认 500）、同一工作流运行任务（100）、全局在途作业（100），超出排队；统一分支不限并发 |
| `unilabos/workflow/scheduling_feedback.py` | 105 | DROPPED | #5 / #4（V1 调度引擎插件反馈协议） |  |
| `unilabos/workflow/scheduling_journal.py` | 695 | DROPPED | #5 / #4（引擎决策账） |  |
| `unilabos/workflow/scheduling_local_authority.py` | 90 | DROPPED | #4（dev 调度器在 WorkflowService 事务内推进） |  |
| `unilabos/workflow/scheduling_observation.py` | 153 | PORTED | runtime.db `job_runs` 视图 + timeline.py（实测平均时长）；attempt 上的预计时长（A01） | 不拟合学习模型（#5） |
| `unilabos/workflow/scheduling_session.py` | 323 | DROPPED | #5 / #4（引擎会话、预算与恢复屏障） |  |
| `unilabos/workflow/scheduling_step_permit.py` | 167 | PORTED | 任务级单步：`/workflow-tasks/{uuid}/commands` type=step / switch_to_step，`/step-state` | D04：保留 dev 任务级单步，不复制工站级单步额度 |
| `unilabos/workflow/scheduling_wait.py` | 180 | PORTED | server/services/runtime/workflow/resource_waiters.py（持久等待顺序） |  |
| `unilabos/workflow/schema.py` | 970 | PORTED | server/services/runtime/workflow/value_schema.py + entry_parameters.py（输入合同校验） |  |
| `unilabos/workflow/service.py` | 8601 | PORTED | server/services/runtime/workflow/service.py `WorkflowService` + task_lifecycle.py / definition_service.py / node_templates.py 等混入 | 整体落地；缺口按子模块列出（组合刷新、源码监视、库存绑定等） |
| `unilabos/workflow/site_selector_value_schema.py` | 221 | PORTED | unilabos/registry/site_selector_value_schema.py |  |
| `unilabos/workflow/source_bootstrap.py` | 432 | DROPPED | #4（可编辑领域包工作流源码不再在启动时安装为定义；设备包 `@workflow` 是模板，经 `/workflows/from-template` 实例化） |  |
| `unilabos/workflow/source_coordinates.py` | 137 | PARTIAL | authoring_compiler.py `_error` / source_map；registry/workflow_ast.py | 统一分支诊断与源码映射的列号是 UTF-8 字节偏移（ast col_offset），V1 合同是 UTF-16 码元列号（给前端编辑器用）；含中文的行在 FE 编辑器里高亮会错位 |
| `unilabos/workflow/source_descriptor_access.py` | 188 | PORTED | service.py `_source_parent_fd` / `_open_directory_chain` / `_read_regular_fd`（Linux dir_fd + lease 后端） |  |
| `unilabos/workflow/source_discovery.py` | 151 | DROPPED | #4 | 同 source_bootstrap |
| `unilabos/workflow/source_file_access.py` | 317 | PORTED | service.py `_assert_contained_regular_target` / `_path_contains_symlink` / `_portable_read_regular` |  |
| `unilabos/workflow/source_identity.py` | 72 | PORTED | publication.py `publication_symbol`（`unilabos_publications.wf_<hex>`） |  |
| `unilabos/workflow/source_layout.py` | 54 | PORTED | service.py `register_editable_source`（强制 `workflows/<name>.py`） |  |
| `unilabos/workflow/source_manifest.py` | 234 | DROPPED | #4（package.yaml 工作流源码声明） |  |
| `unilabos/workflow/source_monitor.py` | 327 | PARTIAL | server/services/runtime/workflow/source_monitor.py（代码已搬） | 没有实例化：全仓无 `WorkflowSourceMonitor(` 调用，启动也不调 `recover_registered_sources`；外部改动源码文件不会自动重编译出候选，Apply 后写回失败留下的 `writeback_status=pending`（接口警告「OS 已保留可恢复的源码记录」）永远不会被重试 |
| `unilabos/workflow/source_path_access.py` | 234 | PORTED | service.py `_portable_*` 后端（macOS/Windows） |  |
| `unilabos/workflow/source_publication.py` | 966 | PORTED | service.py `_compare_and_replace` / `_portable_compare_and_replace`（原子 CAS 写回 + 写后校验） |  |
| `unilabos/workflow/source_publication_errors.py` | 96 | PORTED | service.py 写回路径把 OS 错误收敛为 `draft_hash_conflict` / `internal_error` |  |
| `unilabos/workflow/source_windows_publication.py` | 349 | PARTIAL | service.py portable 后端（sidecar 锁 + 原子替换 + 写后校验） | 没有 Win32 适配（ReplaceFileW 带备份、持有目录句柄链、缺失目标恢复）；route map 自述 portable 后端在最后一次哈希校验与替换之间检测不到外部编辑器写入 |
| `unilabos/workflow/source_workspace.py` | 766 | PORTED | service.py `register_editable_source` / `_read_source` / `_atomic_write` / `_discard_source_file` |  |
| `unilabos/workflow/source_workspace_errors.py` | 22 | N/A | 错误类型别名 |  |
| `unilabos/workflow/station_event_http.py` | 173 | DROPPED | F08 / #4（向上游 Backend 投影工站事件；V1 仅 control_plane=backend 时启用） | 上游同步由 dev legacy_adaptor 承担 |
| `unilabos/workflow/station_event_outbox.py` | 267 | DROPPED | F08；V1 自身已停写（`append_station_event` 为空操作） |  |
| `unilabos/workflow/station_event_publisher.py` | 167 | DROPPED | F08（同 station_event_http） |  |
| `unilabos/workflow/station_status_projection.py` | 117 | DROPPED | F08；V1 自身已停写 |  |
| `unilabos/workflow/station_workflow_submission.py` | 175 | PORTED | server/services/runtime/workflow/station_invocations.py；`POST /station/workflow-invocations` | #9：已发布后被编辑按指纹调用 → 3003 |
| `unilabos/workflow/store.py` | 5809 | PORTED | server/services/runtime/workflow/store.py `WorkflowStore`（runtime.db） |  |
| `unilabos/workflow/store_migrations.py` | 1000 | DROPPED | #8（v1 基线，无迁移链） |  |
| `unilabos/workflow/task_input.py` | 928 | PORTED | entry_parameters.py + service.py `create_workflow_task` / `edit_workflow_task_input`（仅暂停时、已派发消费的入口参数拒绝修改）+ 单作业参数 PATCH | 库位组冻结缺口见 execution_resource_policy |
| `unilabos/workflow/task_material_admission.py` | 495 | DROPPED | #6（任务级缺料直接失败 `plan_not_executable`，不排队等待入库） | 物料占有由 dev 预留 + 默认物料锁承担 |
| `unilabos/workflow/task_runtime_projection.py` | 4539 | PORTED | #4：scheduler/service.py 经 WorkflowService（`record_workflow_node_job_terminal`、`mark_workflow_node_job_*` 等）原子落 Task/Job/Claim 事实 |  |
| `unilabos/workflow/task_scheduler_bridge.py` | 4787 | PORTED | #4：server/scheduler/service.py `BackendScheduler`（任务编译、恢复、生命周期） |  |
| `unilabos/workflow/template_projection_generation.py` | 199 | PORTED | node_templates.py（注册表指纹不变不重算）+ services/runtime/registry.py 版本 | 代际不单独持久化 |
| `unilabos/workflow/template_projection_store.py` | 909 | PORTED | node_templates.py `project_registry_templates`（workflow_node_template / workflow_handle_template，V1 固定 UUID 命名空间） |  |
| `unilabos/workflow/wf_utils.py` | 151 | PORTED | server/services/runtime/workflow/upload.py + app/cli/workflow.py（`unilab workflow upload`） |  |
| `unilabos/workflow/workflow_boundary.py` | 247 | PORTED | entry_parameters.py `resolve_declared_output`（声明输出缺失/类型不符即任务失败） |  |
| `unilabos/workflow/workflow_boundary_binding.py` | 280 | PORTED | entry_parameters.py `_OutputResolver` |  |
| `unilabos/workflow/workflow_io.py` | 1028 | PARTIAL | server/services/runtime/workflow/workflow_io.py + value_schema.py `schema_is_assignable` + entry_parameters.py | 工作流输入的模板筛选改写在 `@workflow(allowed_templates=...)`（发布时解析成 `allowed_resource_template_uuids`，预检 / 建任务核对；输入上的 `AllowedResourceTemplates` 报错并指向新写法）；ResourceSlot 透传/连线的模板兼容不校验（见 material_graph_validation） |
| `unilabos/workflow/workflow_spec_compiler.py` | 1238 | DROPPED | #4（执行计划→旧 EdgeScheduler 规格；dev `_build_dag` 直接消费计划） |  |
| `unilabos/workflow/workflow_type.py` | 35 | PORTED | operation_category.py `normalize_workflow_type` |  |
