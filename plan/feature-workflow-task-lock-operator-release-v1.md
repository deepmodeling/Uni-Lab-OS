---
goal: "为工作流任务提供受保护、可审计的执行锁人工释放能力"
version: "1.0"
date_created: "2026-09-04"
last_updated: "2026-09-04"
owner: "Uni-Lab OS"
status: "Completed"
tags: ["feature", "workflow", "scheduler", "execution-lock", "operator"]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

本计划为工作流任务详情增加执行锁可见性和人工释放入口。释放动作必须经过任务/作业终态校验、物理安全确认、claim 与 fencing token 的并发校验，并写入持久化审计和运行时事件；它释放的是完整 `JobExecutionClaim` 及其全部锁租约，不直接删除任意数据库记录，也不改变工作流业务结果。

## 1. Requirements & Constraints

- **REQ-001**: 通过公开的工作流任务 API 查询任务当前活动执行锁，并返回锁租约、作业、claim、fencing token、状态和可释放原因。
- **REQ-002**: 通过公开 API 针对指定锁租约发起人工释放；服务端按租约所属作业原子释放该作业的全部活动锁和 claim。
- **REQ-003**: 仅允许任务与目标作业处于 `failed`、`canceled` 或 `timeout` 终态，且未结算设备托管；调用方必须提供非空原因、物理安全确认、expected claim UUID 和 expected fencing token。
- **REQ-004**: 人工释放必须是并发安全的 compare-and-swap 操作；陈旧页面、错误任务、错误 claim 或错误 fencing token 必须返回冲突/未找到，而不能释放其他作业的锁。
- **REQ-005**: 成功和幂等重复请求均需留下可查询的操作审计，并追加工作流运行时事件及前端失效通知。
- **REQ-006**: 任务列表/工作流详情 UI 显示活动锁，按作业分组；按钮在服务端报告不可释放时禁用，并要求用户输入原因和确认物理设备已安全。
- **SEC-001**: 不提供按任意 lock key 或 SQL 行删除的通用强制解锁；操作入口以 lease UUID 定位并验证任务归属。
- **SEC-002**: `uncertain` 状态、活动设备托管或非终态作业禁止人工释放，避免把物理执行风险伪装成数据库清理。
- **CON-001**: Uni-Lab-OS 是调度与执行锁权威源；uni-lab-fe 只能通过 `WorkflowRuntimePort` 调用公开 API，不得在组件中直接 fetch。
- **CON-002**: 保留现有任务控制、清理结算和工作流输出协议；人工释放不自动把任务标记为成功，也不跳过其他清理义务。
- **GUD-001**: 使用仓库既有 `WorkflowError`、`WorkflowConflict`、严格请求模型、事务边界和 Vitest/Pytest 测试惯例。
- **PAT-001**: 采用深模块边界：执行锁查询/释放和审计由 `execution_lock_lease.py` 与 `TaskRuntimeProjection` 持有，HTTP 与 React 层只编排其公开 seam。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 在 Uni-Lab-OS 建立安全的任务锁查询、CAS 人工释放、审计和调度唤醒路径。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 在 `unilabos/workflow/execution_lock_lease.py` 增加按 task/lease 查询、按作业整组释放、operator action 审计表及事务函数；保持既有锁状态和唯一约束不变。 | ✅ | 2026-09-04 |
| TASK-002 | 在 `unilabos/workflow/task_runtime_projection.py` 暴露任务锁列表和人工释放 seam，校验任务/作业终态、物理确认、claim/fencing CAS 与设备托管，并追加运行时事件。 | ✅ | 2026-09-04 |
| TASK-003 | 在 `unilabos/workflow/service.py` 增加锁查询/释放服务方法，复用既有业务错误映射，并在成功释放后调用现有调度桥接唤醒接口（若无则补受控的公开唤醒 seam）。 | ✅ | 2026-09-04 |
| TASK-004 | 在 `unilabos/app/workflow_api.py` 增加 `GET /workflow-tasks/{task_uuid}/execution-locks` 和 `POST /workflow-tasks/{task_uuid}/execution-locks/{lease_uuid}/force-release`，更新 OpenAPI 描述和严格请求/响应模型。 | ✅ | 2026-09-04 |

### Implementation Phase 2

- GOAL-002: 在 uni-lab-fe 将锁能力接入任务控制器、运行时端口和工作流详情界面。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | 在 `packages/services/src/workflowTaskContracts.ts`、`packages/services/src/workflowPort.ts`、`packages/services/src/workflow.ts` 增加锁租约/释放结果类型、严格解码和公开 API 适配。 | ✅ | 2026-09-04 |
| TASK-006 | 在 `packages/workflow-editor/src/runtime/WorkflowTaskController.ts`、`useWorkflowTaskRuntime.ts` 维护锁快照、刷新和释放动作，处理冲突/错误状态。 | ✅ | 2026-09-04 |
| TASK-007 | 新增 `WorkflowTaskLocks.tsx` 深模块并在 `PersistentWorkflowAuthoringView.tsx` 的任务详情区域渲染；按作业分组展示锁，提供确认对话框、原因输入、禁用态和结果反馈。 | ✅ | 2026-09-04 |

### Implementation Phase 3

- GOAL-003: 用模块测试和公开接口契约测试覆盖正常、拒绝、竞争和幂等路径，并完成仓库验证。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | 在 `tests/workflow/` 与 `tests/app/` 增加 OS 锁查询、终态安全校验、CAS 冲突、整组原子释放、审计事件和幂等重复请求测试。 | ✅ | 2026-09-04 |
| TASK-009 | 在 `packages/services/src/workflow-task-runtime.test.ts`、`packages/workflow-editor/src/runtime/WorkflowTaskController.test.ts`、`WorkflowTaskLocks.test.tsx` 增加 FE 适配、状态机和 UI 测试。 | ✅ | 2026-09-04 |
| TASK-010 | 运行 OS 调度/工作流相关 pytest、FE 定向 Vitest、TypeScript 检查和 OpenAPI/路由契约测试；记录真实可执行验证与环境限制。 | ✅（OS、FE 定向测试/类型检查和路由契约均通过） | 2026-09-04 |

## 3. Alternatives

- **ALT-001**: 仅在前端删除或隐藏锁记录；不具备权威性、审计和并发安全，拒绝采用。
- **ALT-002**: 提供按 lock key 的全局强制删除；容易误释放其他任务或绕过 claim 不变量，拒绝采用。
- **ALT-003**: 只增加后端接口、不提供页面；无法满足任务列表操作需求，拒绝采用。
- **ALT-004**: 让人工释放自动标记 cleanup settled；会跳过物料/设备清理义务，改为仅释放目标作业 claim 并保留其他清理状态。

## 4. Dependencies

- **DEP-001**: Uni-Lab-OS `WorkflowStore` 已初始化 `execution_lock_lease`、`execution_claim` 相关 schema，并支持事务写入。
- **DEP-002**: `WorkflowService` 与 `TaskSchedulerBridge` 的公开调度唤醒 seam；若当前实现不存在，需在不暴露内部队列的前提下补充。
- **DEP-003**: uni-lab-fe 的 `WorkflowRuntimePort`、工作流任务控制器和既有任务详情容器。
- **DEP-004**: 现有前后端 UUID、错误信封、运行时事件和前端失效通知协议。

## 5. Files

- **FILE-001**: `unilabos/workflow/execution_lock_lease.py` — 锁查询、整组释放、审计持久化。
- **FILE-002**: `unilabos/workflow/task_runtime_projection.py` — 任务级安全策略与运行时事件。
- **FILE-003**: `unilabos/workflow/service.py` — 业务服务 seam 和调度唤醒。
- **FILE-004**: `unilabos/app/workflow_api.py`、`unilabos/app/web/openapi_descriptions.py` — HTTP 路由、模型、文档。
- **FILE-005**: `packages/services/src/workflowTaskContracts.ts`、`packages/services/src/workflowPort.ts`、`packages/services/src/workflow.ts` — FE 公开端口和适配器。
- **FILE-006**: `packages/workflow-editor/src/runtime/WorkflowTaskController.ts`、`useWorkflowTaskRuntime.ts` — 任务锁状态机。
- **FILE-007**: `packages/workflow-editor/src/components/WorkflowTaskLocks.tsx`、`PersistentWorkflowAuthoringView.tsx` — 锁列表和人工释放界面。
- **FILE-008**: `tests/workflow/`、`tests/app/`、`packages/services/src/*.test.ts`、`packages/workflow-editor/src/**/*.test.tsx` — 模块与契约测试。

## 6. Testing

- **TEST-001**: 任务锁列表只返回指定任务的活动租约，字段包含 job UUID、lease UUID、claim UUID、fencing token、状态和可释放原因。
- **TEST-002**: 运行中、`uncertain`、非终态、物理确认缺失、活动设备托管、错误 claim/token 的释放请求均被拒绝且锁不变。
- **TEST-003**: 失败作业的成功释放同时释放该作业全部锁和 claim，写入 operator action 与 runtime event，并触发调度唤醒。
- **TEST-004**: 重复释放返回幂等结果，不重复破坏其他任务；未知 lease 或跨任务 lease 返回未找到/冲突。
- **TEST-005**: FE 适配器严格解码新接口；控制器刷新/释放后更新快照并展示错误；UI 覆盖确认、输入原因、禁用态和成功反馈。
- **TEST-006**: 运行后端调度/工作流定向 pytest、前端定向 Vitest、类型检查和路由/OpenAPI 契约检查。

## 7. Risks & Assumptions

- **RISK-001**: 操作员误确认物理设备已安全可能造成真实设备并发风险；通过终态、uncertain 拒绝、设备托管检查、显式确认和审计降低风险。
- **RISK-002**: 锁释放后等待作业未及时唤醒；必须复用或补充调度桥接公开唤醒 seam，并以测试验证。
- **RISK-003**: 前后端版本不一致导致旧客户端无法展示锁；新增接口为可选增强，既有任务控制接口保持兼容。
- **ASSUMPTION-001**: 任务/作业状态 `failed|canceled|timeout` 是当前终态命名，需以 OS 现有枚举和测试为准。
- **ASSUMPTION-002**: 一个 `JobExecutionClaim` 覆盖一个作业的全部资源锁，因此人工操作按作业整组释放而非单行释放。
- **ASSUMPTION-003**: 用户具备操作员权限；当前仓库暂无细粒度权限模型时，先保留服务端审计 seam，后续可接入认证授权。

### 验证证据（2026-09-04）

- Uni-Lab-OS：锁人工释放模块、调度桥接、异常处置、设备动作桥接、人工确认、工作流 Backend API/OpenAPI 定向测试共 `65 passed`；`compileall` 与 `git diff --check` 通过。
- Uni-Lab-OS 调度核心全包 `61 passed`；工作流全包运行至环境缺少 `python-multipart` 的库存导入测试前已通过 `572` 项，失败属于测试环境依赖缺失，未进入本次锁功能路径。
- uni-lab-fe：`@unilab/services` 全量 `30 files / 208 tests passed`，`@unilab/workflow-editor` 全量 `57 files / 375 tests passed`，两个包 TypeScript 类型检查通过。浏览器真实设备 E2E 未执行（本功能的安全写路径已由 OS API/模块测试覆盖，未伪造 E2E 证据）。
- uni-lab-fe 工作区级类型检查已跑到 `apps/desktop`，因既有 `ManagedRuntimeInstallationApi.chooseEnvironment` 类型不一致中止；本次涉及的 services、workflow-editor 及其依赖包均通过。
- uni-lab-fe 工作流运行时合同扫描 `test:workflow-runtime-contract` 通过（禁止运行时引用 `0`）。
- 前端仓库整体质量脚本仍报告既有超大文件/复杂度和既有 `!important` 规则问题，涉及文件不属于本次锁功能改动；未扩大修改范围处理。

## 8. Related Specifications / Further Reading

- `plan/architecture-workflow-resource-locking-kernel-v3.md`
- `CONTEXT.md`（Uni-Lab OS 领域词汇与边界）
- `../uni-lab-fe/AGENTS.md`（前端包边界与运行时端口约束）
