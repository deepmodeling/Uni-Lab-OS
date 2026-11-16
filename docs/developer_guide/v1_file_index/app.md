# U01 · app 区逐文件核验（V1 `690491a3` → 统一分支 #361 `6f94ec04`）

范围：92 个 V1 文件（`unilabos/app/**` 除调度器、`client/**`、`agent_tools/**`、`workspace_host/**`、`managed_runtime/**`）。
只读核验；路由级结论以 `docs/developer_guide/api_v1_route_map.md` 为准并抽查了实现。下文路径：V1 相对 `ulo-v1/`，统一分支相对 `ulo-a01/`。

## 统计

| 结论 | 数量 |
| --- | --- |
| PORTED | 50 |
| DROPPED | 19 |
| PARTIAL | 13 |
| MISSING | 1 |
| N/A | 9 |
| 合计 | 92 |

PARTIAL：`agent_tools/workflow.py`、`app/cli/material.py`、`app/cli/workflow.py`、`app/edge_control/execution_observation.py`、`app/main.py`、`app/runtime_capabilities.py`、`app/web/openapi_descriptions.py`、`app/web/openapi_docs.py`、`app/web/openapi_field_descriptions.py`、`app/workflow_api.py`、`app/workflow_authoring_transform.py`、`app/workflow_openapi.py`、`app/workflow_template_api.py`。
MISSING：`app/cli/lab.py`。

## 逐文件

| V1 文件 | 行数 | 结论 | 统一分支对应 / 依据 | 备注 |
| --- | --- | --- | --- | --- |
| unilabos/agent_tools/__init__.py | 5 | N/A | — | 只做导出 |
| unilabos/agent_tools/workflow.py | 526 | PARTIAL | `server/mcp/`（`catalog.py` 白名单、`server.py`；Streamable HTTP `/mcp`，仅本机非浏览器客户端） | 已有：工作区状态 / 启停 / 重启 / 日志 → `process-management.snapshot\|operations.submit\|operations.get\|logs.tail`；列表 / 查看 / 运行 / 命令 → `workflow.workflow.list\|get`、`workflow.task.create\|get\|jobs\|command`；布局 inspect/preview/apply → `process-management.operations.submit` 的 `material.layout.*`；重置 → `system.reset.*`。缺：`watch_task`（无任务事件 / 观察工具，`/workflow-tasks/{uuid}/events` 不在 MCP 目录）、`wait_authoring`（只有 `workflow.authoring.get`，需代理自己轮询）。有意去掉：`switch_workspace_authority`（`authority.switch` 不支持）、`debug_workflow`（V1 `/debug/workflow-tasks` 本身返回 410）、物料场景检查 / 截图 / 比对 / 基线（renderer 不托管，#1）、`validate_material_templates`（`material.template.validate` 不支持）。`unilab-mcp` stdio 入口改为管理端口上的 `/mcp` |
| unilabos/app/__init__.py | 0 | N/A | — | 空文件 |
| unilabos/app/backend.py | 54 | PORTED | `backend/__init__.py:start_backend`（hostlink / ros2，`app/runtime_startup.py:run_runtime`） | V1 只有 ros 分支可用（simple / automancer 是占位）；dev 默认 HostLink，ROS 2 可选（#4） |
| unilabos/app/cli/__init__.py | 34 | N/A | `app/cli/__init__.py`（惰性导出） | 导出表 |
| unilabos/app/cli/_client_factory.py | 40 | PORTED | `app/cli/management.py:management_connection`、`app/cli/workflow.py:_create_workflow_client` | 同样按 `resolve_effective_auth` 注入 ak/sk；无地址时回落本机管理端口而非云端 |
| unilabos/app/cli/auth.py | 95 | PORTED | `app/cli/auth.py` | 基本同文，`--addr` → `--address`（保留 `--addr` 兼容） |
| unilabos/app/cli/auth_resolver.py | 112 | PORTED | `app/cli/auth_resolver.py` | 去掉云端默认地址 `DEFAULT_BASE_URL`，三处都缺时返回空串（`base_url_source="none"`） |
| unilabos/app/cli/config.py | 45 | PORTED | `app/cli/config.py` | 基本同文 |
| unilabos/app/cli/lab.py | 41 | MISSING | 无 `unilab lab` 子命令（`app/cli/router.py` 未注册） | V1 `lab list` 调云端 Center `GET /lab/list`；统一分支 CLI 已改为只连本机微后端且去掉云端默认地址。属 Center 能力，建议按 F08 思路确认为有意不移植 |
| unilabos/app/cli/material.py | 397 | PARTIAL | `app/cli/material.py:cmd_material_list`（本机物料权威 `HTTPMaterialsClient.list_materials`，`--roots_only`） | V1 `material list` 查云端 `/lab/material?id=<lab_uuid>`，统一分支改查本机权威（等价替代）。缺 `material layout inspect\|preview\|apply` CLI（能力在 `/v1/operations` 的 `material.layout.*`、MCP 与 `HTTPLifecycleClient.execute_workspace_operation`）。`material scene *`、`material template validate` 随 renderer / 模板校验一起有意去掉（#1，`UNSUPPORTED_V1_COMMANDS`） |
| unilabos/app/cli/workflow.py | 357 | PARTIAL | `app/cli/workflow.py:cmd_workflow_command`、`app/cli/router.py:_register_workflow_authority_commands`，SDK `client/runtime/workflow.py:HTTPWorkflowClient` | 已有 `workflow list/inspect/run/watch/authoring/upload`。缺：①`workflow run --inputs`（统一分支 `run` 无入口参数选项，`HTTPWorkflowClient.create_task` 也不带 `input`，需要入口参数的工作流无法用 CLI 启动）；②`workflow command <task> pause\|resume\|continue\|step\|cancel`（SDK 有 `command_task`，CLI 未暴露）。`workflow debug` 对应 V1 已 410 的 `/debug/workflow-tasks`，不算缺口 |
| unilabos/app/communication.py | 263 | PORTED | `server/backend/legacy_adaptor/session.py`（`BaseBackendClient`、`BackendSessionFactory`、`get_backend_client`） | V1 的 `LocalCommunicationClient` 空实现对应 dev 单进程 / 本机权威会话 |
| unilabos/app/community_packages.py | 491 | PORTED | `app/community_packages.py` | 统一分支只按本地 manifest 解析；V1 的远端 resolve 分支在本地模式下 `http_client=None`，本就不走（`workspace_package_bootstrap.py:138`）。包安装改由 `unilab package install` / 驱动包服务（#3） |
| unilabos/app/control_plane.py | 106 | PORTED | `app/backend_main.py:run_backend_process`、`app/runtime_startup.py`、`server/composition.py`；角色路由见 `server/openapi_export.py` 的 host/backend 前缀表 | #4：dev 权威进程 + Host 子进程取代 V1 local 控制面 seam |
| unilabos/app/device_action_capabilities.py | 49 | PORTED | `server/backend/capabilities.py`（runtime.v1 `action_capabilities`，过滤 `_execute_driver_command*` 见 `backend/runtime/host_adapter.py`）、`server/services/runtime/devices.py` | 差异：runtime.v1 descriptor 白名单含 `display_name` 但不含动作 `description`（V1 上报给生产 Backend 的单点动作说明）；`/registry/entries/{name}` 仍可读完整声明 |
| unilabos/app/edge_control/__init__.py | 5 | N/A | — | 包说明 |
| unilabos/app/edge_control/addressing.py | 61 | DROPPED | #4；route map「Misc」行「Closed by design」 | 从 Backend 地址推导 Scheduler 地址；dev 只有一个权威地址（`--address`），无此推导 |
| unilabos/app/edge_control/client.py | 1724 | PORTED | `server/backend/edge_control.py:EdgeControlService`（权威侧）、`server/backend/legacy_adaptor/websocket.py:BackendWebSocketClient` + `session.py`（Host 侧）、`backend/hostlink/host_node.py` | #4：HTTP 传事实 + WS 传短通知 → runtime.v1（`/api/v1/ws/schedule` + HTTP 数据面）；会话注册、派发、取消、反馈、结果、错误决策都在 runtime.v1 上 |
| unilabos/app/edge_control/execution_observation.py | 70 | PARTIAL | `server/backend/execution_start.py:ExecutionStartGuard`、`POST /workflow-node-jobs/{uuid}/started`（`server/services/runtime/workflow/execution_claim.py:record_start_receipt`） | 统一分支的开始回执由通用执行包装在调用驱动前生成（许可 + `started_at_ms`），没有 V1「设备适配器在拿到设备启动回执后显式上报实际开始证据」的钩子（`report_execution_started(receipt_id, started_at_ms, source_ref)` + `execution_start_reporting` 上下文，平台存 `workflow_execution_started`）。V1 内置驱动没有调用者 |
| unilabos/app/edge_control/execution_start.py | 30 | PORTED | `server/backend/execution_start.py`、`protocol/runtime/execution_start.py` | R01：应答不确定时禁止再调用驱动（`ExecutionStartUnknown`） |
| unilabos/app/edge_control/http.py | 456 | PORTED | `server/backend/legacy_adaptor/http.py:BackendHTTPClient`、`client/runtime/data.py`、`client/materials/` | #4：runtime.v1 HTTP 数据面 |
| unilabos/app/edge_control/local_authority.py | 2737 | PORTED | `server/api/runtime/control.py`、`data.py`、`server/services/runtime/data.py`（command inbox / job / error gate）、`server/backend/coordinator.py` | #4；V1 `/api/v1/edge/*` 11 条路由在 route map 标「Closed by design」，持久派发意图与结果、begin / started、错误决策、resolve-unknown 由 runtime.v1 命令、执行认领、error gate、`/scheduler/physical-recovery` 承担 |
| unilabos/app/edge_control/local_edge_session.py | 534 | PORTED | `server/services/runtime/data.py`（`BackendSessionRecord`、`ExecutorEndpointRecord`）、`/api/v1/readiness` 的 `execution` 字段、`/api/v1/runtime/endpoints` | 会话 / 在线设备投影 / 就绪判定 |
| unilabos/app/edge_control/runtime.py | 39 | PORTED | `--address` 顶层 Host 连远端权威（`app/supervisor.py:maybe_supervise`、`legacy_adaptor/session.py`） | #4 |
| unilabos/app/edge_control/store.py | 1362 | PORTED | Host 自己的 `<root>/edge/runtime.db`：`CommandInboxRecord`、`BackendEventOutboxRecord`、claim / ack（`server/services/runtime/data.py`） | 命令确认前落盘、事件 ack 前留在 outbox 的语义一致 |
| unilabos/app/error_handling_api.py | 427 | PORTED | `server/api/runtime/stations.py`（21 条路由，与 V1 一一对应）、`server/services/runtime/station_handling.py` | route map「Error handling」p7-b；错误改为信封（4001/3003/1000/5001 而非 HTTP 404/409/422/503） |
| unilabos/app/instance_geometry.py | 162 | PORTED | `server/services/materials/graph.py`（`pose.position`、尺寸、旋转 → 物料位置） | 本机 Graph Authority 落库 |
| unilabos/app/instance_position_sync.py | 143 | PORTED | `GraphService.upsert_graph(on_existing="replace")`（`unilab graph upload`、`PUT /api/v1/graphs`）按 id 复用身份并更新位置 | V1 是向远端 Backend 发带修订号的位置更新；dev 不向远端 Backend 推数据（`release.publish` 不支持） |
| unilabos/app/instance_sync.py | 551 | PORTED | Graph Authority：`-g` 启动 `on_existing="adopt"`（`app/main.py:270`）、`unilab graph upload`；`server/backend/legacy_adaptor/sync/instances.py` 留有旧版 `InstanceSynchronizer` | V1 `unilab instance-sync [--check_only] [--devices_only]` 是给远端正式 Backend 的初始化 Job；统一分支没有该子命令，类无调用者。本机权威上「图节点→物料实例幂等初始化」已有 |
| unilabos/app/job_queue.py | 424 | PORTED | `server/backend/execution_queue.py`（`QueueItem`、`JobInfo`、`DeviceActionManager`、`format_job_log`） | dev 明确它只登记已准入动作，等待与排序只在 `server/backend/scheduler` |
| unilabos/app/main.py | 1643 | PARTIAL | `app/main.py`、`app/cli/parser.py`、`app/cli/router.py`、`app/backend_main.py`、`app/runtime_startup.py`、`config/working_dir.py` | 启动装配、工作目录（`.unilabos` / `unilabos_data`）、配置、登录类子命令、`workflow`、`graph`、`package`、`runtime` 都在。缺：①`unilab doctor net\|talker\|listener\|fake-device` 未注册（`backend/hostlink/doctor.py:run_doctor` 存在但无入口，parser / router 都没有）；②工作区包声明的物料 2.5D 外形 / 3D 模型目录未装配：`create_v1_resource_router(material_shapes=…, material_model_catalog=…)` 无调用者传值，`/material-shapes` 恒为空、`/material-models/*` 恒 404（V1 由 main 从 PackageCatalog 装配，跨 package 区）。有意变化：`template-sync` 改为启动时自动上报注册表（`report_registry_snapshot`）；`instance-sync` 由 Graph Authority 取代；`--control_plane/--process_role` → `--role`；`--action_mode simulate` → `--test_mode`；`--workspace` 与 Workspace Host 分发去掉（#1）；`--preserve_runtime_databases` 不需要（库持久，#8）；`lab` 见上 |
| unilabos/app/model.py | 151 | PORTED | `server/api/runtime/workflow_definition.py`（`JobAddRequest`，`/job/add`、`/job/{id}/status`） | 旧 `Resp` 信封换成 V1 Backend 信封（route map「Devices and registry」） |
| unilabos/app/operation_category_api.py | 196 | PORTED | `server/api/runtime/workflow_definition.py`、`server/services/runtime/workflow/operation_category.py`（runtime.db `workflow_operation_category`） | p7-a 五条路由；V1 的 `can_view_drafts` 检查见 runtime_capabilities 行 |
| unilabos/app/oss_upload.py | 246 | PORTED | `app/oss_upload.py` | 只把客户端换成 `HTTPConfig.remote_addr` + `BasicConfig.auth_secret()` |
| unilabos/app/process_shutdown.py | 113 | PORTED | `app/lifetime.py`（`begin_shutdown`、`exit_now`）、`app/backend_main.py:_stop_on_signal` | |
| unilabos/app/process_supervisor.py | 122 | PORTED | `app/supervisor.py`（`run_supervisor`、`HostChildSupervisor`） | 默认由权威进程看护 Host 子进程 |
| unilabos/app/register.py | 37 | PORTED | `app/register.py:collect_devices_and_resources` | V1 的「上传已移除」兼容分支不需要 |
| unilabos/app/runtime_capabilities.py | 109 | PARTIAL | `protocol/runtime/lifecycle.py:RuntimeCapabilities`（`/readiness.capabilities`，恒全开） | 投影在；V1 的能力闸门（`can_edit_definitions` 只读模式 → 1001、`can_run_unpublished`、`can_debug`、`can_execute_device_actions`、`can_view_drafts` 过滤草稿）没有移植，dev 也不发 1001。V1 产品代码从不设置非默认值（只有测试调用 `set_runtime_capabilities`），因此对外行为相同 |
| unilabos/app/runtime_storage.py | 363 | PORTED | `server/database/root_lock.py`、`app/main.py:_claim_process_database_root`（`<root>/unilab.lock`，冲突退出码 73）、`server/database/layout.py` | V1 默认每次启动用私有临时库（`--preserve_runtime_databases` 才保留）；dev 库持久，重置走 `/api/v1/reset` 归档（#8） |
| unilabos/app/runtime_thread_lifecycle.py | 119 | PORTED | `app/runtime_startup.py`（`_join_backend`、`_exit_on_backend_failure`、`_wait_backend_ready`）、`app/lifetime.py` | 关键线程退出即非零退出 |
| unilabos/app/runtime_topology.py | 113 | PORTED | `app/backend_main.py`、`app/supervisor.py`、`--role backend` / `--address` / `--no_safe_restart` | #4：combined ≈ `--no_safe_restart`，workspace_backend ≈ `--role backend`，edge_runtime ≈ `--address` Host |
| unilabos/app/template_sync.py | 732 | PORTED | `server/backend/legacy_adaptor/sync/templates.py`（`report_registry_snapshot[_local]`，`app/main.py` 的 Host 上报段）、`server/services/runtime/registry.py`；V1 回执形 `POST /api/v1/resource-templates` 由权威提供（route map Inventory 行） | V1 是独立 `unilab template-sync` Job（开发者 token、推正式 Backend、嵌入 2.5D `model.shape`）；统一分支在 Host 启动时把注册表按内容哈希上报到权威 Registry Authority，这是权威目录的唯一来源（2026-11-17 起权威不扫描）。上报带条目所属驱动包版本、本机 `machine_id` 与连着的 Slave，权威按规则 A / B 自动接受或挂起待确认（`v1_dev_reconciliation.md`「注册表只来自 Host 上报」）。2.5D 外形嵌入随外形目录一并缺失，见 main.py 行 |
| unilabos/app/utils.py | 425 | PORTED | `app/utils.py:patch_rclpy_dll_windows`；`cleanup_for_restart` → `server/backend/restart.py` + `app/lifetime.py` | |
| unilabos/app/web/__init__.py | 37 | N/A | — | 惰性导出 |
| unilabos/app/web/api.py | 1435 | PORTED | `server/api/runtime/registry.py`（`/devices`、`/authoring/device-catalog`、`/online-devices`、`/actions`、`/devices/{id}/actions[/…/schema]`）、`server/api/runtime/workflow_definition.py`（`/job/*`）、`server/api/materials/resources.py`（`/resources`）、`server/api/workspace.py`（`/file-browser`）、`server/api/runtime/diagnostics.py`（`/health`） | 三个 `/ws/device_status\|registry_editor\|status_page` 与文件导入分析服务于旧页面，route map 标 V1-DEPRECATED |
| unilabos/app/web/client.py | 816 | PORTED | `server/backend/legacy_adaptor/legacy/http.py:LegacyBackendHTTPClient`（文档串注明线协议一致） | V1 本地模式本就不挂这条云端桥 |
| unilabos/app/web/console.py | 173 | DROPPED | D01（仓库无内置控制台，正式 UI 是 uni-lab-fe）；route map `GET /`、`MOUNT /console` 为 n/a | 附带的 Host 白名单 / 同源检查（默认关闭）没有对应；dev 曾用控制面 token（`X-UniLab-Token`）保护写操作，11-17 取消，管理 API 不鉴权（只有工站调用要工站密钥） |
| unilabos/app/web/controller.py | 626 | PORTED | `server/services/runtime/devices.py`（设备 / 动作 / schema / 忙闲）、`/job/add` 作为 ad-hoc 设备动作任务 | |
| unilabos/app/web/device_catalog.py | 368 | PORTED | `server/services/runtime/devices.py`（`backend_devices`、`authoring_catalog`） | 字段与 V1 投影一致（含 `workflow_node_template_uuid`、`riskLevel`、输入输出 Schema、忙闲） |
| unilabos/app/web/openapi_descriptions.py | 380 | PARTIAL | `server/openapi_export.py`（导出全集 + `x-openlab-role`）；说明来自处理函数文档串与少量 `summary=` | V1 为每个操作给出通俗中文 summary / description（禁用 backend、dto、信封、幂等等术语，`tests/app/test_openapi_documentation.py` 强制覆盖）；统一分支无这层，361 个路由装饰器中约 57 个带 `summary=` |
| unilabos/app/web/openapi_docs.py | 602 | PARTIAL | 同上 | `enrich_openapi_schema` / `install_openapi_documentation`（参数与字段中文说明补齐）无对应 |
| unilabos/app/web/openapi_field_descriptions.py | 289 | PARTIAL | 同上 | 请求 / 返回字段的中文含义表无对应（`protocol`、`server/api` 中 `description=` 约 37 处） |
| unilabos/app/web/pages.py | 192 | DROPPED | route map「Misc」：`GET /`、`/status`、`/open-folder`、`/registry-editor` 均为 V1-DEPRECATED | 旧本地页面 |
| unilabos/app/web/server.py | 788 | PORTED | `server/api/app.py`（CORS 含 `Idempotency-Key`、`X-Workflow-Filename`；`log_requests` 中间件；envelope）、`server/api/runtime/diagnostics.py`（`/readiness` V1 字段） | 启动期 503 → dev 返回 200 + 5001 + `Retry-After`（#9） |
| unilabos/app/web/utils/__init__.py | 0 | N/A | — | 空文件 |
| unilabos/app/web/utils/action_utils.py | 365 | PORTED | `server/services/runtime/devices.py:action_schema / _action_entry`（注册表声明 + Host 能力描述） | 不再做 ROS 消息内省 |
| unilabos/app/web/utils/device_utils.py | 59 | DROPPED | 只服务 V1-DEPRECATED 的状态页 / `/ws/status_page`（route map） | |
| unilabos/app/web/utils/host_utils.py | 67 | DROPPED | 同上（`/ws/device_status`、状态页） | |
| unilabos/app/web/utils/ros_utils.py | 74 | DROPPED | 同上 | |
| unilabos/app/workflow_api.py | 2465 | PARTIAL | `server/api/runtime/workflow.py`、`workflow_definition.py`、`tasks_v1.py`、`interventions_v1.py`、`scheduling_v1.py`、`station_invocations_v1.py`、`inventory_seam_v1.py`、`events.py` | 全部路由已移植（route map Workflow authoring / Task lifecycle 两节，p7-a/b/d）。行为缺口（根因在 workflow 区）：①`inventory_bindings` 在建任务时只校验、存盘（`workflow_task.inventory_bindings`）、进幂等指纹，`run-preflight` 只回显——不参与任何预留或检查（V1 `workflow/quantity_inventory.py:prepare_task_allocations` 按绑定把工作流级 `inventory_requirements` 落到指定库存；统一分支 graph 的 `inventory_requirements` 恒为空）；②`/workflow-tasks/{uuid}/human-interactions` 与 `/workflow-human-interactions/{uuid}/reply` 无生产者：`HumanInteractions.open` 没有调用者（V1 由 `workflow/human_commands.py` 的 `HumanCommands.*` 原生命令开启），读到的永远是空列表 |
| unilabos/app/workflow_authoring_transform.py | 483 | PARTIAL | `server/services/runtime/workflow/authoring_compiler.py:WorkflowAuthoringCompiler`，路由在 `server/api/runtime/workflow_definition.py` | `/authoring/compile`、`/authoring/validate` 已有。`/authoring/generate-python` 只能渲染动作、循环、group/parallel；条件节点、py_script、人工确认、组合调用、具名输出、资源作用域、Handle 连线都返回 `graph_not_representable`（`render_block` / `fail`）。V1 `workflow/authoring_python.py:render_authoring_python` 能渲染条件、人工确认和展开的组合调用。影响：图编辑器里含 if/else 或组合调用的工作流无法生成源码 |
| unilabos/app/workflow_openapi.py | 153 | PARTIAL | `server/api/envelope.py`（`Envelope[T]`、`EnvelopeRoute` 自动包 `response_model`） | 信封形状进 OpenAPI；但工作流 / 实验操作路由没有声明 `response_model`（`WorkflowReadModel`、列表、发布、类别读模型等 V1 响应 Schema 不在 OpenAPI 中）。仅影响文档与生成类型 |
| unilabos/app/workflow_template_api.py | 390 | PARTIAL | `server/services/runtime/workflow/node_templates.py`（注册表 → runtime.db `workflow_node_template`），`GET /workflow-node-templates[/{uuid}]` | 设备动作模板已投影（V1 确定性 UUID）。结构模板 condition / repeat_until（及 group）未投影（route map p7-a 注明）；`material_io` 按 #6 不做。影响：FE 节点面板拿不到条件 / 循环模板 |
| unilabos/app/workspace_authoring_api.py | 43 | PORTED | `server/api/workspace.py`（`GET /workspace/package-mounts`，驱动包台账投影，`editable` 恒 false） | route map「Workspace and lifecycle」 |
| unilabos/app/workspace_package_bootstrap.py | 187 | PORTED | `app/main.py`（解析 `-g`、合并已启用驱动包扫描目录）、`app/community_packages.py`、`server/services/driver_packages.py` | #3/#4：V1 工作区包代（PackageCatalog）由 dev 驱动包系统承担 |
| unilabos/app/ws_client.py | 1836 | PORTED | `server/backend/legacy_adaptor/legacy/ws.py:LegacyBackendWebSocketClient`（旧云端 `/ws/schedule`） | V1 本地模式启动时拒绝该桥 |
| unilabos/client/__init__.py | 76 | N/A | `client/__init__.py` | 导出表 |
| unilabos/client/domain.py | 768 | PORTED | `client/runtime/workflow.py:HTTPWorkflowClient`（list / inspect / create_task / command_task / wait_authoring / watch_task） | 差异：不再经 Workspace Host 发现 Authority（#1）；无 debug 任务（V1 410）；`inspect_task` 不带事件；`watch_task` 改为 SSE 失效通知 + HTTP 快照，不再读 `/workflow-tasks/{uuid}/events` 持久时间线 |
| unilabos/client/envelope.py | 81 | PORTED | `client/utils/envelope.py` | 同时解析微后端 V1 信封与旧云端形状 |
| unilabos/client/http.py | 146 | PORTED | `client/http.py` | 增加 `Retry-After` 重试（曾带的 `X-UniLab-Token` 已随控制面 token 取消） |
| unilabos/client/material_layout.py | 80 | PORTED | `client/runtime/lifecycle.py:HTTPLifecycleClient.execute_workspace_operation`（`material.layout.*`） | 无专用薄封装 |
| unilabos/client/material_renderer.py | 390 | DROPPED | #1；`protocol/runtime/process_management.py:UNSUPPORTED_V1_COMMANDS` 的 `renderer.*`（「dev 不托管渲染器」） | Workbench renderer 自动化客户端 |
| unilabos/client/material_template.py | 47 | DROPPED | #1；`material.template.validate` → 「物料模板由注册表权威在上报时校验」 | |
| unilabos/client/material_visual_regression.py | 235 | DROPPED | #1（只比对 renderer 截图，随 renderer 去掉） | |
| unilabos/client/output.py | 177 | PORTED | `client/utils/output.py` | 同文 |
| unilabos/client/session.py | 185 | PORTED | `client/session.py` | 文件锁改用 `utils/file_lock.py`；无云端默认地址 |
| unilabos/managed_runtime/__init__.py | 3 | N/A | — | |
| unilabos/managed_runtime/supervisor.py | 781 | DROPPED | #1；`plc.*` 不支持（「dev 不运行 PLC 仿真器」）；Worker 由 `app/supervisor.py:HostChildSupervisor` 看护 | `unilab-supervisor` 入口已删（`setup.py` 只剩 `unilab`） |
| unilabos/workspace_host/__init__.py | 11 | N/A | — | |
| unilabos/workspace_host/authority_sync.py | 486 | DROPPED | #1；`authority.switch` 不支持 | |
| unilabos/workspace_host/cli.py | 178 | DROPPED | #1；status/start/stop/restart/logs → `unilab runtime status\|drain\|resume\|restart\|reset`（`app/cli/runtime.py`）与 `/v1/*`；`workspace init` → `.cursor/skills/create-device-package` | |
| unilabos/workspace_host/client.py | 274 | DROPPED | #1；替代为 `HTTPLifecycleClient.workspace_*`（`client/runtime/lifecycle.py`） | |
| unilabos/workspace_host/discovery.py | 95 | DROPPED | #1；单实例锁 → `server/database/root_lock.py`；本地令牌不移植（dev 的控制面 token 也已于 11-17 取消） | |
| unilabos/workspace_host/host.py | 2521 | DROPPED | #1；其协议 `/v1/snapshot`、`/v1/operations[/{id}]`、`/v1/logs/{component}` 已由 `server/api/process_management.py` + `server/services/process_management.py` 提供（route map p8，D02） | 抽查：FIFO 操作、重放 / 冲突（V1 的 409，11-17 起为信封 3003 + `error.code`）、`expectedRevision`、未完成操作重启后 `operation_interrupted` 均在 |
| unilabos/workspace_host/launch.py | 675 | DROPPED | #1 | backend / edge / PLC 启动计划；dev 由 `host_child_command` 派生 |
| unilabos/workspace_host/material_layout.py | 488 | PORTED | `server/services/materials/workspace_layout.py`（`material.layout.inspect\|preview\|apply`） | 布局源由图文件改为 materials.db 物料树，修订 = 根树 `state_hash` 汇总；preview / apply CAS 语义保留 |
| unilabos/workspace_host/material_template_worker.py | 90 | DROPPED | #1、#3；`material.template.validate` 不支持，模板由注册表权威上报时校验，静态检查用 `unilab package inspect` / `--check_mode` | |
| unilabos/workspace_host/model.py | 130 | DROPPED | #1；V1 线协议形状在 `protocol/runtime/process_management.py` | |
| unilabos/workspace_host/release_publish.py | 2912 | DROPPED | #1；`release.publish\|inspect` 不支持（「dev 不向远端 Backend 发布数据」） | |
| unilabos/workspace_host/reset_safety.py | 250 | PORTED | `app/backend_main.py:reset_preflight` + `server/backend/reset.py:ResetController`（`/api/v1/reset`，令牌 + 确认文本） | dev 是停机归档而非删除；预检看重启状态、活跃作业、在线 Slave、驱动包操作，不像 V1 逐库扫未终态任务 / 未确认命令 |
| unilabos/workspace_host/scheduler_lifecycle.py | 163 | PORTED | `/api/v1/scheduler/drain[/resume]`、`HTTPLifecycleClient.drain_and_wait`、process_management 的 `scheduler.drain\|resume` | |
